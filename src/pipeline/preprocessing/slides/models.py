"""Canonical slide / deck data shapes — the source of truth stored as slides.json.

These are extraction-format-agnostic: the same shapes are produced whether a
slide was read by the local VLM, escalated to Haiku, or parsed by python-pptx.
``DeckExtraction.to_markdown()`` renders the existing ``## Slide N`` markdown so
current chunkers keep working during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Cheap, python-pptx-derived slide class used to drive the escalation gate.
SlideClass = Literal["TEXT", "TABLE", "FRAMEWORK", "DIAGRAM", "MIXED", "IMAGE", "EMPTY"]

# Which backend produced the slide's main content (provenance / debugging).
ExtractorName = Literal["local_vlm", "haiku", "sonnet", "pptx"]


@dataclass(slots=True)
class SlideExtraction:
    """One slide, fully extracted."""

    slide_number: int
    title: str | None = None
    text: str = ""                      # body text (markdown bullets/paragraphs)
    tables: list[str] = field(default_factory=list)   # markdown tables
    visuals: str = ""                   # CHART/DIAGRAM/MAP/MATRIX blocks (visual content)
    notes: str | None = None            # speaker notes (python-pptx only — not in the image)
    classification: SlideClass = "TEXT"
    extractor: ExtractorName = "local_vlm"
    escalated: bool = False             # True if local output was escalated to Haiku

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide_number": self.slide_number,
            "title": self.title,
            "text": self.text,
            "tables": list(self.tables),
            "visuals": self.visuals,
            "notes": self.notes,
            "classification": self.classification,
            "extractor": self.extractor,
            "escalated": self.escalated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SlideExtraction:
        return cls(
            slide_number=int(d["slide_number"]),
            title=d.get("title"),
            text=d.get("text") or "",
            tables=list(d.get("tables") or []),
            visuals=d.get("visuals") or "",
            notes=d.get("notes"),
            classification=d.get("classification") or "TEXT",
            extractor=d.get("extractor") or "local_vlm",
            escalated=bool(d.get("escalated", False)),
        )

    def to_markdown(self) -> str:
        """Render one slide as ``## Slide N: Title`` + blocks (chunker-compatible)."""
        lines = [f"## Slide {self.slide_number}: {self.title or '(untitled)'}"]
        if self.text.strip():
            lines.append(self.text.strip())
        for tbl in self.tables:
            if tbl.strip():
                lines.append(tbl.strip())
        if self.visuals.strip():
            lines.append(self.visuals.strip())
        if self.notes and self.notes.strip():
            lines.append(f"Notes: {self.notes.strip()}")
        lines.append(f"[Page {self.slide_number}]")
        return "\n\n".join(lines)


@dataclass(slots=True)
class DeckExtraction:
    """A whole deck — the canonical slides.json payload."""

    document_id: str
    source_s3_key: str
    title: str | None = None
    summary: str | None = None          # deck-level synthesis (semantic/discovery aid)
    slides: list[SlideExtraction] = field(default_factory=list)
    schema_version: int = 1

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def escalated_count(self) -> int:
        return sum(1 for s in self.slides if s.escalated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "source_s3_key": self.source_s3_key,
            "title": self.title,
            "summary": self.summary,
            "slide_count": self.slide_count,
            "escalated_count": self.escalated_count,
            "slides": [s.to_dict() for s in self.slides],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeckExtraction:
        return cls(
            document_id=str(d["document_id"]),
            source_s3_key=str(d.get("source_s3_key") or ""),
            title=d.get("title"),
            summary=d.get("summary"),
            slides=[SlideExtraction.from_dict(s) for s in (d.get("slides") or [])],
            schema_version=int(d.get("schema_version", 1)),
        )

    def to_markdown(self) -> str:
        """Whole-deck markdown (backward-compatible with current pptx_slide chunker)."""
        head = f"DOCUMENT_TITLE: {self.title}\n\n" if self.title else ""
        if self.summary and self.summary.strip():
            head += f"DOCUMENT_SUMMARY: {self.summary.strip()}\n\n"
        return head + "\n\n".join(s.to_markdown() for s in self.slides)
