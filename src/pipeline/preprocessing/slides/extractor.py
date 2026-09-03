"""Tiered slide extraction: local VLM first, escalate hard slides to Haiku.

Flow per deck:
    render slides -> images + python-pptx shape stats / notes
    for each slide:
        local VLM (free, on GPU) extracts text/tables/visuals
        ESCALATION GATE: if the slide is visual (chart/diagram/image) AND the
            local output looks thin/weak -> re-extract that slide with Haiku
    merge -> DeckExtraction (slides.json)

Everything the orchestrator depends on (renderer, local backend, haiku backend)
is injected, so the gate and orchestration are unit-testable without a GPU,
Ollama, or LibreOffice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from pipeline.preprocessing.slides.models import (
    DeckExtraction,
    SlideClass,
    SlideExtraction,
)

log = logging.getLogger(__name__)

# A slide that is visual in nature — local VLMs are weakest here, so these are
# the only candidates for Haiku escalation.
_VISUAL_CLASSES: frozenset[SlideClass] = frozenset({"FRAMEWORK", "DIAGRAM", "MIXED", "IMAGE"})


@dataclass(slots=True)
class RenderedSlide:
    """One rendered slide handed to an extractor backend."""

    slide_number: int
    image_png: bytes
    classification: SlideClass
    notes: str | None = None
    # Raw shape inventory from python-pptx (has_chart, has_picture, n_text_chars, …)
    shape_stats: dict = field(default_factory=dict)
    # True when the slide carries chart/picture/diagram content the text parser
    # cannot read — the batched enricher attaches an image only for these.
    has_visual: bool = False
    # True when the parser output looks incomplete for this slide (much less text
    # than the shapes report, SmartArt/embedded objects, etc.) — so we attach the
    # image and let Claude recover what the parser missed rather than trusting it.
    needs_image: bool = False
    # Native content read straight from the PPTX XML (no LLM). The free
    # PptxParserExtractor returns these verbatim; the renderer fills them in the
    # same python-pptx pass that produces shape_stats.
    parsed_title: str | None = None
    parsed_text: str = ""
    parsed_tables: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SlideContent:
    """What a backend returns for a single slide."""

    title: str | None
    text: str
    tables: list[str] = field(default_factory=list)
    visuals: str = ""


def attaches_image(slide: RenderedSlide) -> bool:
    """Whether the enricher should send this slide's image to Claude.

    True for genuinely visual slides (charts/pictures/SmartArt) AND for slides
    where the text parser looks like it missed content — so we never blindly ship
    thin parser output without letting the vision model check the rendered slide.
    """
    return bool(slide.has_visual or slide.needs_image)


class SlideExtractorBackend(Protocol):
    """A vision backend that reads one rendered slide image."""

    name: str

    def extract(self, slide: RenderedSlide) -> SlideContent: ...


class SlideRenderer(Protocol):
    """Renders a PPTX into per-slide images + shape stats."""

    def render(self, pptx_bytes: bytes) -> list[RenderedSlide]: ...


# ---------------------------------------------------------------------------
# Escalation gate (pure, unit-tested)
# ---------------------------------------------------------------------------

_MIN_VISUAL_CHARS = 40   # local output shorter than this on a visual slide = thin


def needs_escalation(slide: RenderedSlide, local: SlideContent) -> bool:
    """Decide whether a slide's local extraction should be redone by Haiku.

    Only visual slides are eligible, and only when the local output looks
    insufficient — so text/table slides (which the local VLM handles well) never
    incur Haiku cost.
    """
    if slide.classification not in _VISUAL_CLASSES:
        return False
    produced = len((local.text + " " + local.visuals).strip())
    # Thin output on a visual slide, or no visual block at all on a chart/diagram.
    if produced < _MIN_VISUAL_CHARS:
        return True
    if slide.classification in ("FRAMEWORK", "DIAGRAM") and not local.visuals.strip():
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TieredSlideExtractor:
    """Run local VLM on every slide; escalate weak visual slides to Haiku."""

    def __init__(
        self,
        *,
        renderer: SlideRenderer,
        local_backend: SlideExtractorBackend,
        haiku_backend: SlideExtractorBackend | None = None,
    ) -> None:
        self._renderer = renderer
        self._local = local_backend
        self._haiku = haiku_backend

    def extract_deck(
        self,
        pptx_bytes: bytes,
        *,
        document_id: str,
        source_s3_key: str,
        title: str | None = None,
    ) -> DeckExtraction:
        rendered = self._renderer.render(pptx_bytes)
        slides: list[SlideExtraction] = []
        n_escalated = 0

        for r in rendered:
            content = self._local.extract(r)
            extractor = self._local.name
            escalated = False

            if self._haiku is not None and needs_escalation(r, content):
                try:
                    content = self._haiku.extract(r)
                    extractor = self._haiku.name
                    escalated = True
                    n_escalated += 1
                except Exception:  # noqa: BLE001 — keep local result on Haiku failure
                    log.warning(
                        "slide %d: Haiku escalation failed, keeping local output",
                        r.slide_number,
                        exc_info=True,
                    )

            slides.append(
                SlideExtraction(
                    slide_number=r.slide_number,
                    title=content.title,
                    text=content.text,
                    tables=list(content.tables),
                    visuals=content.visuals,
                    notes=r.notes,
                    classification=r.classification,
                    extractor=extractor,  # type: ignore[arg-type]
                    escalated=escalated,
                )
            )

        log.info(
            "deck %s: %d slides, %d escalated to Haiku",
            document_id, len(slides), n_escalated,
        )
        return DeckExtraction(
            document_id=document_id,
            source_s3_key=source_s3_key,
            title=title,
            slides=slides,
        )
