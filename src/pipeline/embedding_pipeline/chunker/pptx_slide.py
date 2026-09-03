"""PPTXSlideChunker — slide-aware chunker for presentation content.

Core rule: the SLIDE is the atomic unit. One parent chunk per slide; children
are intra-slide content blocks (bullets, table rows, notes). Slides are never
split across parent chunk boundaries regardless of token count.

Three dispatch paths based on the input ParsedDocument:

  Path A — normalized .txt (primary path):
    ``parsed.parser_name == "text"`` (TextParser processed the LLM-generated
    ``__normalized.txt``). Sections have headings like ``"Slide 1: Introduction"``
    matching ``^[Ss]lide\\s+\\d+``.

  Path B — raw PPTX (fallback path):
    ``parsed.parser_name == "pptx"`` (PPTXParser processed the raw binary).
    Sections carry ``section.metadata["slide_number"]`` directly.

  Path C — non-slide content:
    Sections do not match the slide heading pattern (e.g. PDF text, Description
    .txt files). Delegated to ParentChildChunker without modification.

In all cases the chunker is safe to set as the ``chunker_strategy`` for a table
that holds a mix of PPTX, PDF, and plain-text files.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.common.ids import chunk_id as build_chunk_id
from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.resume import canonical_section
from pipeline.embedding_pipeline.chunker.tokens import count_tokens, split_to_token_window
from pipeline.embedding_pipeline.models import Chunk, ChunkType, Document
from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection

_SLIDE_HEADING_RE = re.compile(r"(?i)^slide\s+(\d+)")


def _parse_slide_number(heading: str | None) -> int | None:
    """Return slide number from a 'Slide N: ...' heading, or None."""
    if not heading:
        return None
    m = _SLIDE_HEADING_RE.match(heading)
    return int(m.group(1)) if m else None


def _is_table_line(line: str) -> bool:
    return line.startswith("|")


def _is_notes_line(line: str) -> bool:
    return line.startswith("[Notes:]") or line.startswith("Notes:")


def _is_visual_line(line: str) -> bool:
    return line.upper().startswith(("VISUAL:", "INSIGHTS:"))


def _atomic_or_windowed(block: str, config: ChunkingConfig) -> list[str]:
    """Keep a block whole unless it exceeds child_max_tokens (then token-window it)."""
    if count_tokens(block, config.tokenizer) > config.child_max_tokens:
        return split_to_token_window(
            block, max_tokens=config.child_max_tokens, overlap=0,
            encoding_name=config.tokenizer,
        )
    return [block]


def _split_slide_into_blocks(section: ParsedSection, config: ChunkingConfig) -> list[str]:
    """Split slide body text into intra-slide content blocks for children.

    Structure-aware: a markdown TABLE (consecutive ``|...|`` lines), a VISUAL/INSIGHTS
    description, and a Notes block each stay ATOMIC (one child) so a table row or a
    chart insight is never split across unrelated children. Plain text/bullets are
    grouped to ~child_max_tokens. Oversized atomic blocks are token-windowed.
    """
    body = section.text.strip()
    if not body:
        return [section.heading or ""]
    lines = [line.strip() for line in body.split("\n") if line.strip()]
    if not lines:
        return [section.heading or ""]

    blocks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def flush_text() -> None:
        nonlocal current, current_tokens
        if current:
            blocks.append("\n".join(current))
            current = []
            current_tokens = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_notes_line(line):
            flush_text()
            blocks.append(line)
            i += 1
        elif _is_table_line(line):
            flush_text()
            tbl: list[str] = []
            while i < len(lines) and _is_table_line(lines[i]):
                tbl.append(lines[i])
                i += 1
            blocks.extend(_atomic_or_windowed("\n".join(tbl), config))
        elif _is_visual_line(line):
            flush_text()
            vis = [line]
            i += 1
            while i < len(lines) and not (
                _is_table_line(lines[i]) or _is_notes_line(lines[i]) or _is_visual_line(lines[i])
            ):
                vis.append(lines[i])
                i += 1
            blocks.extend(_atomic_or_windowed("\n".join(vis), config))
        else:
            line_tokens = count_tokens(line, config.tokenizer)
            if line_tokens > config.child_max_tokens:
                flush_text()
                blocks.extend(_atomic_or_windowed(line, config))
            elif current_tokens + line_tokens > config.child_max_tokens and current:
                flush_text()
                current = [line]
                current_tokens = line_tokens
            else:
                current.append(line)
                current_tokens += line_tokens
            i += 1

    flush_text()
    return [b for b in blocks if b.strip()] or [section.heading or ""]


def _summary_chunk(
    document: Document, text: str, *, doc_role: str, position: int, config: ChunkingConfig
) -> Chunk:
    """One embedded vector for a whole summary (record- or deck-level).

    A summary is a coherent gist, so it is embedded as a SINGLE child chunk (not
    split) — best project/deck-level recall, and it doubles as the parent context
    hydrated on a hit. ``doc_role`` tags it (record_summary | deck_summary).
    """
    cid = build_chunk_id(
        document_hash_value=document.document_hash,
        chunk_type=ChunkType.CHILD,
        parent_position=position,
        child_position=0,
    )
    body = text.strip()
    return Chunk(
        chunk_id=cid,
        chunk_type=ChunkType.CHILD,
        text=body,
        token_count=count_tokens(body, config.tokenizer),
        position=position,
        document_hash=document.document_hash,
        source=document.source,
        parent_chunk_id=None,
        metadata={
            "doc_role": doc_role,
            "table_name": document.source.table_name,
            "document_id": document.source.primary_key,
            "s3_path": document.source.s3_key,
            "strategy": "summary",
        },
    )


class PPTXSlideChunker:
    """Slide-aware chunker — one parent per slide, children are intra-slide blocks."""

    def chunk(
        self,
        *,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        # A record-summary document is embedded as ONE vector (the whole summary),
        # tagged record_summary — not split into blocks.
        if (document.source.doc_role or "") == "record_summary":
            text = document.text or "\n\n".join(
                s.text for s in parsed.sections if s.text.strip()
            )
            return [_summary_chunk(document, text, doc_role="record_summary",
                                   position=0, config=config)]

        if parsed.parser_name == "pptx":
            return self._chunk_pptx_sections(document, parsed, config)

        # Check whether any section carries a 'Slide N:' heading pattern.
        has_slide_headings = any(
            _parse_slide_number(s.heading) is not None
            for s in parsed.sections
            if not s.is_empty
        )
        if has_slide_headings:
            return self._chunk_slide_headings(document, parsed, config)

        # No slide structure detected — delegate to generic chunker.
        from pipeline.embedding_pipeline.chunker.parent_child import ParentChildChunker  # noqa: PLC0415

        return ParentChildChunker().chunk(document=document, parsed=parsed, config=config)

    # ------------------------------------------------------------------
    # Path A: normalized .txt with '## Slide N: Title' headings
    # ------------------------------------------------------------------

    def _chunk_slide_headings(
        self,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        parent_position = 0
        slide_count = len([s for s in parsed.sections if not s.is_empty])

        for section in parsed.sections:
            if section.is_empty:
                continue

            slide_number = _parse_slide_number(section.heading)
            if slide_number is None:
                # The deck's DOCUMENT_SUMMARY preamble → ONE deck-summary vector.
                if "DOCUMENT_SUMMARY" in (section.text or ""):
                    chunks.append(
                        _summary_chunk(document, section.text, doc_role="deck_summary",
                                       position=parent_position, config=config)
                    )
                    parent_position += 1
                    continue
                # Other non-slide preamble — treat as parent_child.
                from pipeline.embedding_pipeline.chunker.parent_child import ParentChildChunker  # noqa: PLC0415

                extra = ParentChildChunker().chunk(
                    document=document,
                    parsed=ParsedDocument(sections=[section], parser_name=parsed.parser_name),
                    config=config,
                )
                for chunk in extra:
                    chunk.position = parent_position
                    parent_position += 1
                chunks.extend(extra)
                continue

            extra_meta: dict[str, Any] = {
                "slide_number": slide_number,
                "slide_title": section.heading,
                "slide_count": slide_count,
            }
            # presentation_section may have been embedded in the text by the LLM
            # or set by PPTXParser; surface it if present.
            if section.metadata.get("presentation_section"):
                extra_meta["presentation_section"] = section.metadata["presentation_section"]

            chunks.extend(
                self._build_slide_chunks(
                    document=document,
                    section=section,
                    parent_position=parent_position,
                    config=config,
                    extra_meta=extra_meta,
                )
            )
            parent_position += 1

        return chunks

    # ------------------------------------------------------------------
    # Path B: raw PPTX with slide_number in section.metadata
    # ------------------------------------------------------------------

    def _chunk_pptx_sections(
        self,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        parent_position = 0
        slide_count = parsed.metadata.get("slide_count", len(parsed.sections))

        for section in parsed.sections:
            if section.is_empty:
                continue

            slide_number = section.metadata.get("slide_number")
            extra_meta: dict[str, Any] = {
                "slide_number": slide_number,
                "slide_title": section.heading,
                "slide_count": slide_count,
                "presentation_section": section.metadata.get("presentation_section", ""),
            }
            chunks.extend(
                self._build_slide_chunks(
                    document=document,
                    section=section,
                    parent_position=parent_position,
                    config=config,
                    extra_meta=extra_meta,
                )
            )
            parent_position += 1

        return chunks

    # ------------------------------------------------------------------
    # Shared chunk builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_slide_chunks(
        *,
        document: Document,
        section: ParsedSection,
        parent_position: int,
        config: ChunkingConfig,
        extra_meta: dict[str, Any],
    ) -> list[Chunk]:
        full_text = (
            f"{section.heading}\n\n{section.text}"
            if section.text
            else (section.heading or "")
        )
        parent_tokens = count_tokens(full_text, config.tokenizer)

        parent_meta: dict[str, Any] = {
            "section_title": section.heading,
            "section_canonical": canonical_section(section.heading),
            "section_level": section.level,
            "section_path": list(section.section_path),
            "chunk_index": parent_position,
            "table_name": document.source.table_name,
            "document_id": document.source.primary_key,
            "s3_path": document.source.s3_key,
            "strategy": "pptx_slide",
            **extra_meta,
        }

        parent_cid = build_chunk_id(
            document_hash_value=document.document_hash,
            chunk_type=ChunkType.PARENT,
            parent_position=parent_position,
        )
        parent = Chunk(
            chunk_id=parent_cid,
            chunk_type=ChunkType.PARENT,
            text=full_text,
            token_count=parent_tokens,
            position=parent_position,
            document_hash=document.document_hash,
            source=document.source,
            parent_chunk_id=None,
            metadata=parent_meta,
        )

        children = PPTXSlideChunker._build_children(
            document=document,
            section=section,
            parent=parent,
            parent_position=parent_position,
            config=config,
            extra_meta=extra_meta,
        )
        return [parent, *children]

    @staticmethod
    def _build_children(
        *,
        document: Document,
        section: ParsedSection,
        parent: Chunk,
        parent_position: int,
        config: ChunkingConfig,
        extra_meta: dict[str, Any],
    ) -> list[Chunk]:
        blocks = _split_slide_into_blocks(section, config)
        children: list[Chunk] = []

        # Slide-context prefix for the EMBEDDING input only (not stored/cited): a bare
        # bullet like "23% CAGR" embeds far better when it carries its slide context.
        slide_no = extra_meta.get("slide_number")
        slide_title = (extra_meta.get("slide_title") or section.heading or "").strip()
        if slide_no is None:
            context_prefix = ""
        elif slide_title.lower().startswith("slide "):
            context_prefix = f"{slide_title}\n\n"  # already "Slide N: Title" (Path A)
        elif slide_title:
            context_prefix = f"Slide {slide_no}: {slide_title}\n\n"  # Path B
        else:
            context_prefix = f"Slide {slide_no}\n\n"

        for child_position, block_text in enumerate(blocks):
            tokens = count_tokens(block_text, config.tokenizer)
            is_notes = block_text.startswith("[Notes:]") or block_text.startswith("Notes:")
            if tokens < config.child_min_tokens and children and not is_notes:
                continue
            child_cid = build_chunk_id(
                document_hash_value=document.document_hash,
                chunk_type=ChunkType.CHILD,
                parent_position=parent_position,
                child_position=child_position,
            )
            child_meta: dict[str, Any] = {
                "section_title": section.heading,
                "section_canonical": canonical_section(section.heading),
                "section_level": section.level,
                "section_path": list(section.section_path),
                "chunk_index": child_position,
                "child_index_in_parent": child_position,
                "table_name": document.source.table_name,
                "document_id": document.source.primary_key,
                "s3_path": document.source.s3_key,
                "strategy": "pptx_slide",
                **extra_meta,
            }
            children.append(
                Chunk(
                    chunk_id=child_cid,
                    chunk_type=ChunkType.CHILD,
                    text=block_text,
                    token_count=tokens,
                    position=child_position,
                    document_hash=document.document_hash,
                    source=document.source,
                    parent_chunk_id=parent.chunk_id,
                    embed_text=f"{context_prefix}{block_text}" if context_prefix else None,
                    metadata=child_meta,
                )
            )
        return children
