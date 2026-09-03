"""SlidesDeckNormalizer — document-batched extraction for presentation decks.

PPTX/PPT:
    python-pptx parser gives lossless native text/tables for every slide (free),
    then ONE Claude call (default Haiku) sends every visual slide image at once,
    plus one final call writes a deck summary. The deck renders to the
    ``## Slide N: Title`` markdown the ``pptx_slide`` chunker understands, with
    a ``DOCUMENT_SUMMARY:`` header and ``[Page N]`` markers for page-accurate citations.

Environment:
    SLIDES_ENRICH_MODEL              — default claude-haiku-4-5-20251001
    SLIDES_ENRICH_SINGLE_CALL        — default true (all images in one call)
    SLIDES_BATCH_SIZE                — only used when single-call is false

PDFs fall through to :class:`LLMContentNormalizer` (PDF → Claude). Anything else
returns ``None`` so the original binary is kept.

The normalizer is fail-safe per the :class:`AttachmentNormalizer` contract: any
extraction error degrades to ``None`` (upload original) rather than raising.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pipeline.preprocessing.normalizers.base import AttachmentNormalizer

log = logging.getLogger(__name__)

# Only the MODERN .pptx (zip/xml) can be parsed by python-pptx for the per-slide
# renderer. Old binary .ppt (pre-2007, an OLE compound file) is NOT a zip, so
# python-pptx can't open it — route it to llm_content instead, which converts it
# via LibreOffice → PDF → Claude (native, full visual coverage). This avoids a
# guaranteed-to-fail parse + alarming traceback on every .ppt.
_PPTX_SUFFIXES = frozenset({".pptx"})

_DEFAULT_ENRICH_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_BATCH_SIZE = 20


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class SlidesDeckNormalizer:
    """Document-batched slide extraction for PPTX; PDF delegated to llm_content."""

    def __init__(
        self,
        *,
        api_key: str,
        enrich_model: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._enrich_model = (
            enrich_model or os.environ.get("SLIDES_ENRICH_MODEL", _DEFAULT_ENRICH_MODEL)
        )
        self._batch_size = batch_size or int(
            os.environ.get("SLIDES_BATCH_SIZE", _DEFAULT_BATCH_SIZE)
        )
        self._single_call = _env_bool("SLIDES_ENRICH_SINGLE_CALL", default=True)
        self._extractor = None  # built lazily (pulls in pptx/anthropic/LibreOffice)
        self._pdf_fallback: AttachmentNormalizer | None = None

    # -- lazily-built collaborators ----------------------------------------

    def _tiered(self):
        if self._extractor is None:
            from pipeline.preprocessing.slides.backends import ClaudeDeckEnricher  # noqa: PLC0415
            from pipeline.preprocessing.slides.enricher import BatchedDeckEnricher  # noqa: PLC0415
            from pipeline.preprocessing.slides.render import PptxSlideRenderer  # noqa: PLC0415

            log.info(
                "slides_deck: enrich_model=%s single_call=%s batch_size=%d",
                self._enrich_model,
                self._single_call,
                self._batch_size,
            )
            self._extractor = BatchedDeckEnricher(
                renderer=PptxSlideRenderer(),
                enricher=ClaudeDeckEnricher(api_key=self._api_key, model=self._enrich_model),
                batch_size=self._batch_size,
                single_call_enrichment=self._single_call,
            )
        return self._extractor

    def _pdf(self) -> AttachmentNormalizer:
        if self._pdf_fallback is None:
            from pipeline.preprocessing.normalizers.llm_content import (  # noqa: PLC0415
                LLMContentNormalizer,
            )

            self._pdf_fallback = LLMContentNormalizer(api_key=self._api_key)
        return self._pdf_fallback

    # -- contract ----------------------------------------------------------

    def normalize(self, binary: bytes, filename: str) -> str | None:
        suffix = Path(filename).suffix.lower()

        # Only modern .pptx uses the slide renderer below. Every other format the
        # target ingests — old binary .ppt, PDF, DOCX/DOC, images — is routed to
        # LLMContentNormalizer, which dispatches by extension (LibreOffice→PDF→Claude
        # for .ppt/.docx). (Returning None here for docx would silently upload the
        # raw binary and never embed it.)
        if suffix not in _PPTX_SUFFIXES:
            try:
                return self._pdf().normalize(binary, filename)
            except Exception:  # noqa: BLE001
                log.warning(
                    "slides_deck: normalization failed for %s", filename, exc_info=True
                )
                return None

        document_id = Path(filename).stem or "deck"
        try:
            deck = self._tiered().extract_deck(
                binary, document_id=document_id, source_s3_key=""
            )
            markdown = deck.to_markdown().strip()
            if markdown:
                log.info(
                    "slides_deck: %s → %d slides (%d escalated), summary=%s",
                    filename, deck.slide_count, deck.escalated_count,
                    "yes" if (deck.summary or "").strip() else "no",
                )
                return markdown
        except Exception:  # noqa: BLE001
            log.warning("slides_deck: tiered extraction failed for %s", filename, exc_info=True)

        # Last resort: whole-deck Claude (also handles PPTX via LibreOffice→PDF).
        try:
            return self._pdf().normalize(binary, filename)
        except Exception:  # noqa: BLE001
            log.warning("slides_deck: llm_content fallback failed for %s", filename, exc_info=True)
            return None
