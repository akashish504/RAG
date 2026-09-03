"""PPTX parser using python-pptx.

Each slide becomes one section: title is the heading, body text follows.
Speaker notes (if present) are appended with a ``[Notes:]`` prefix.

Enhancements over baseline:
- Table shapes are serialised as markdown rows (``| col | col |``) so table
  content is preserved for retrieval.
- Title-only slides are detected as *section markers*; their title is propagated
  as ``presentation_section`` metadata onto all subsequent slides, enabling
  section-level retrieval queries (e.g. "slides about Methodology").
- Image-only slides (no extractable text) receive a placeholder string so they
  are not silently dropped by the ``is_empty`` filter — they remain findable by
  slide number or title.

Install with: ``pip install 'dalberg-mcp[parsers]'``.
"""

from __future__ import annotations

import io

from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection


class PPTXParser:
    name = "pptx"
    extensions = (".pptx",)

    def parse(self, body: bytes, *, key: str = "") -> ParsedDocument:
        try:
            from pptx import Presentation  # noqa: PLC0415
        except ImportError as exc:
            msg = (
                "PPTX parsing requires python-pptx. Install with: "
                "pip install 'dalberg-mcp[parsers]'"
            )
            raise NotImplementedError(msg) from exc

        presentation = Presentation(io.BytesIO(body))
        raw_sections: list[ParsedSection] = []

        for slide_idx, slide in enumerate(presentation.slides, start=1):
            title = self._extract_title(slide)
            table_md = self._extract_tables(slide)
            body_text = self._extract_body(slide, title, table_md)
            notes = self._extract_notes(slide)
            if notes:
                body_text = f"{body_text}\n\n[Notes:] {notes}".strip()

            # Image-only placeholder: slide has shapes but no extractable text.
            if not body_text and self._has_visual_shapes(slide):
                body_text = "[Visual content — no extractable text]"

            heading = title or f"Slide {slide_idx}"
            raw_sections.append(
                ParsedSection(
                    text=body_text,
                    heading=heading,
                    level=1,
                    section_path=[heading],
                    metadata={"slide_number": slide_idx},
                )
            )

        # Second pass: propagate presentation_section from title-only section markers.
        sections = self._propagate_section_context(raw_sections)

        return ParsedDocument(
            sections=[s for s in sections if not s.is_empty],
            parser_name=self.name,
            metadata={"slide_count": len(raw_sections)},
        )

    # ------------------------------------------------------------------
    # Section context propagation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_section_marker(section: ParsedSection) -> bool:
        """A title-only slide with no body is treated as a deck section divider."""
        return not section.text.strip() or section.text.strip() == "[Visual content — no extractable text]"

    @staticmethod
    def _propagate_section_context(sections: list[ParsedSection]) -> list[ParsedSection]:
        """Walk sections in order; set presentation_section metadata on each."""
        current_section = ""
        result: list[ParsedSection] = []
        for sec in sections:
            if PPTXParser._is_section_marker(sec) and sec.heading:
                current_section = sec.heading
            sec.metadata["presentation_section"] = current_section
            result.append(sec)
        return result

    # ------------------------------------------------------------------
    # Content extractors
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_title(slide) -> str | None:
        try:
            title_shape = slide.shapes.title
        except Exception:  # noqa: BLE001
            return None
        if title_shape is None:
            return None
        text = (title_shape.text or "").strip()
        return text or None

    @staticmethod
    def _extract_tables(slide) -> str:
        """Serialise table shapes as markdown rows."""
        table_blocks: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_table", False):
                continue
            rows: list[str] = []
            for row in shape.table.rows:
                cells = [cell.text.strip().replace("|", "\\|") for cell in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                table_blocks.append("\n".join(rows))
        return "\n\n".join(table_blocks)

    @staticmethod
    def _extract_body(slide, title: str | None, table_md: str) -> str:
        title_text = title or ""
        parts: list[str] = []
        for shape in slide.shapes:
            # Skip table shapes — already handled by _extract_tables.
            if getattr(shape, "has_table", False):
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            block = (shape.text_frame.text or "").strip()
            if not block:
                continue
            if title_text and block == title_text:
                continue
            parts.append(block)
        body = "\n".join(parts).strip()
        if table_md:
            body = f"{body}\n\n{table_md}".strip() if body else table_md
        return body

    @staticmethod
    def _extract_notes(slide) -> str:
        try:
            if not slide.has_notes_slide:
                return ""
            return (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _has_visual_shapes(slide) -> bool:
        """Return True if the slide has any non-text shapes (images, charts, etc.)."""
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False) and not getattr(shape, "has_table", False):
                return True
        return False
