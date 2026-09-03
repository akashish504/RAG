"""Document-batched deck extraction.

Per deck:
    render every slide  -> image + python-pptx native text/tables (lossless) + has_visual
    parser base         -> a SlideExtraction for every slide, free, no LLM
    enrichment          -> ONE Claude call with all visual slide images (default), or
                           batches of N slides when ``single_call_enrichment=False``
    summary             -> one final Claude call -> DeckExtraction.summary

The enricher and renderer are injected, so batching / merging / summary assembly are
unit-testable without Anthropic, LibreOffice or python-pptx.
"""

from __future__ import annotations

import logging
from typing import Protocol

from pipeline.preprocessing.slides.extractor import (
    RenderedSlide,
    SlideContent,
    SlideRenderer,
)
from pipeline.preprocessing.slides.models import DeckExtraction, SlideExtraction

log = logging.getLogger(__name__)

# Slides per enrichment call — sized so the corrected output for a batch fits the
# ~16K output budget (≈350 tokens/slide) and stays under the image-per-request limit.
# A typical ~30-slide deck → 1 call; a 60-slide deck → 2; only huge decks exceed 2.
_MAX_SLIDES_PER_CALL = 35
_DEFAULT_BATCH_SIZE = _MAX_SLIDES_PER_CALL
_DIGEST_SLIDE_CHARS = 450       # per-slide text fed into the summary digest (richer summary)
_DIGEST_MAX_CHARS = 14000       # overall summary-digest cap (richer summary)
_CONTEXT_MAX_CHARS = 600        # running cross-batch context cap
_SUMMARY_MAX_IMAGES = 12        # representative slide images sent with the summary


class DeckEnricher(Protocol):
    """Enriches visual slides from their images and writes a deck summary."""

    def enrich_batch(
        self, slides: list[RenderedSlide], *, running_context: str = ""
    ) -> dict[int, SlideContent]: ...

    def summarize(self, digest: str, images: list[bytes] | None = None) -> str: ...


def _base_slide(r: RenderedSlide) -> SlideExtraction:
    """Lossless parser-only slide (the base every slide starts from)."""
    return SlideExtraction(
        slide_number=r.slide_number,
        title=r.parsed_title,
        text=r.parsed_text,
        tables=list(r.parsed_tables),
        visuals="",
        notes=r.notes,
        classification=r.classification,
        extractor="pptx",
        escalated=False,
    )


def _merge_enrichment(
    slide: SlideExtraction,
    enriched: SlideContent,
    *,
    extractor_name: str = "claude",
) -> None:
    """Replace a parser slide with the model's corrected, image-verified content.

    The model corrects the BODY (text/tables/visuals), cross-checked against the
    image, so those fields supersede the parser. SLIDE NUMBER and TITLE stay
    parser-owned (the file's structural truth): the model's title is used only as a
    fallback when the parser found none. Each field is replaced only when the model
    produced something — omitted fields keep the parser value (fail-safe).
    """
    # Title: parser is authoritative; only fill from the model if the parser had none.
    if not (slide.title or "").strip() and enriched.title and enriched.title.strip():
        slide.title = enriched.title.strip()
    if enriched.text.strip():
        slide.text = enriched.text.strip()
    # Tables: python-pptx reads native table cells EXACTLY from the file — far more
    # reliable than a vision transcription. Keep the parser's table; use the model's
    # table ONLY when the parser found none (i.e. a table that exists only as an
    # image/picture, which python-pptx can't read).
    cleaned_tables = [t.strip() for t in enriched.tables if t.strip()]
    if cleaned_tables and not slide.tables:
        slide.tables = cleaned_tables
    if enriched.visuals.strip():
        slide.visuals = enriched.visuals.strip()
    slide.extractor = extractor_name
    slide.escalated = True


class BatchedDeckEnricher:
    """Parser base for all slides; Claude enriches visual slides from their images."""

    def __init__(
        self,
        *,
        renderer: SlideRenderer,
        enricher: DeckEnricher | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        single_call_enrichment: bool = True,
    ) -> None:
        self._renderer = renderer
        self._enricher = enricher
        self._batch_size = max(1, batch_size)
        self._single_call = single_call_enrichment

    def extract_deck(
        self,
        pptx_bytes: bytes,
        *,
        document_id: str,
        source_s3_key: str,
        title: str | None = None,
    ) -> DeckExtraction:
        rendered = self._renderer.render(pptx_bytes)
        slides = [_base_slide(r) for r in rendered]
        by_number = {s.slide_number: s for s in slides}
        n_enriched = 0

        if self._enricher is not None:
            extractor_name = getattr(self._enricher, "name", "claude")
            # EVERY slide is corrected against the parser text (+ its image when the
            # deck has visuals), in batches of ``batch_size`` so neither the image
            # nor the output budget per call is exceeded.
            running_context = ""
            for start in range(0, len(rendered), self._batch_size):
                batch = rendered[start : start + self._batch_size]
                n_enriched += self._enrich_into(
                    batch, running_context, by_number, extractor_name
                )
                running_context = self._running_context(batch)

        summary = self._summarize(slides, rendered) if self._enricher is not None else None

        log.info(
            "deck %s: %d slides, %d enriched, summary=%s",
            document_id, len(slides), n_enriched, "yes" if summary else "no",
        )
        return DeckExtraction(
            document_id=document_id,
            source_s3_key=source_s3_key,
            title=title,
            summary=summary,
            slides=slides,
        )

    # -- helpers -----------------------------------------------------------

    def _enrich_into(
        self,
        slides: list[RenderedSlide],
        running_context: str,
        by_number: dict[int, SlideExtraction],
        extractor_name: str,
    ) -> int:
        """Correct one group of slides and merge results in place; return the count.

        Always calls the model (text-only groups get restructured too). Failures are
        swallowed so the parser output for the group is kept.
        """
        if not slides:
            return 0
        n = 0
        try:
            enriched = self._enricher.enrich_batch(  # type: ignore[union-attr]
                slides, running_context=running_context
            )
        except Exception:  # noqa: BLE001 — keep parser output on enrich failure
            log.warning("deck enrichment failed for a group, keeping parser output", exc_info=True)
            return 0
        for number, content in enriched.items():
            target = by_number.get(number)
            if target is not None:
                _merge_enrichment(target, content, extractor_name=extractor_name)
                n += 1
        return n

    @staticmethod
    def _running_context(batch: list[RenderedSlide]) -> str:
        titles = [r.parsed_title.strip() for r in batch if (r.parsed_title or "").strip()]
        return ("; ".join(titles))[:_CONTEXT_MAX_CHARS]

    def _summarize(
        self, slides: list[SlideExtraction], rendered: list[RenderedSlide]
    ) -> str | None:
        """Build a text digest from the (enriched) slides and summarise it together
        with a representative sample of the deck's slide images."""
        parts: list[str] = []
        for s in slides:
            head = f"Slide {s.slide_number}: {s.title or ''}".strip()
            body = "\n".join(
                p for p in (s.text, *s.tables, s.visuals) if p.strip()
            )[:_DIGEST_SLIDE_CHARS]
            parts.append(f"{head}\n{body}".strip())
        digest = "\n\n".join(parts)[:_DIGEST_MAX_CHARS]
        if not digest.strip():
            return None
        try:
            images = self._summary_images(rendered)
            return self._enricher.summarize(digest, images=images) or None  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            log.warning("deck summary generation failed", exc_info=True)
            return None

    @staticmethod
    def _summary_images(rendered: list[RenderedSlide]) -> list[bytes]:
        """Pick up to _SUMMARY_MAX_IMAGES slide images, evenly spread, always incl. slide 1."""
        usable = [r for r in rendered if r.image_png]
        if len(usable) <= _SUMMARY_MAX_IMAGES:
            return [r.image_png for r in usable]
        last = len(usable) - 1
        idxs = sorted({
            round(i * last / (_SUMMARY_MAX_IMAGES - 1)) for i in range(_SUMMARY_MAX_IMAGES)
        })
        return [usable[i].image_png for i in idxs]
