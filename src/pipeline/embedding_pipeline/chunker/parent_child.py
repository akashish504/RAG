"""Parent-child chunker.

Strategy:

1. Iterate sections in document order. Each ``ParsedSection`` provides natural
   structural boundaries (markdown-style headings and ALL-CAPS lines) produced
   by ``TextParser`` from ingested ``.txt`` files.
2. For each section:
   - If the section text fits inside ``parent_max_tokens``, it becomes one
     parent chunk.
   - If it exceeds the limit, fall back to overlapping token windows, with
     each window becoming its own parent chunk. The section heading is
     preserved on every parent.
3. Children are produced from the parent text using ``child_max_tokens`` and
   ``child_overlap_tokens``. Children carry ``parent_chunk_id`` so KNN search
   over child vectors can resolve the full parent context at query time.

Provenance: every chunk carries ``table_name``, ``document_id`` (the source
primary key), ``s3_path``, ``chunk_id``, ``chunk_index``, and (when
applicable) ``section_title`` and ``section_path``.
"""

from __future__ import annotations

from pipeline.common.ids import chunk_id as build_chunk_id
from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.resume import canonical_section
from pipeline.embedding_pipeline.chunker.tokens import count_tokens, split_to_token_window
from pipeline.embedding_pipeline.models import Chunk, ChunkType, Document
from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection


class ParentChildChunker:
    """Default chunker. Produces parent + child chunks per section."""

    def chunk(
        self,
        *,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        parent_position = 0

        for section in parsed.sections:
            if section.is_empty:
                continue

            section_text = self._compose_section_text(section)
            section_tokens = count_tokens(section_text, config.tokenizer)
            if section_tokens == 0:
                continue

            parent_texts = self._split_section_into_parents(
                text=section_text,
                section_tokens=section_tokens,
                config=config,
            )

            for parent_text in parent_texts:
                parent_tokens = count_tokens(parent_text, config.tokenizer)
                parent = self._build_parent_chunk(
                    document=document,
                    section=section,
                    text=parent_text,
                    token_count=parent_tokens,
                    parent_position=parent_position,
                )
                chunks.append(parent)
                chunks.extend(
                    self._build_children(
                        document=document,
                        section=section,
                        parent=parent,
                        parent_position=parent_position,
                        config=config,
                    )
                )
                parent_position += 1

        return chunks

    @staticmethod
    def _compose_section_text(section: ParsedSection) -> str:
        """Prefix the section heading so each parent chunk is self-contained."""

        if section.heading and section.text:
            return f"{section.heading}\n\n{section.text}"
        if section.heading:
            return section.heading
        return section.text

    @staticmethod
    def _split_section_into_parents(
        *,
        text: str,
        section_tokens: int,
        config: ChunkingConfig,
    ) -> list[str]:
        if section_tokens <= config.parent_max_tokens:
            return [text]
        return split_to_token_window(
            text,
            max_tokens=config.parent_max_tokens,
            overlap=config.parent_overlap_tokens,
            encoding_name=config.tokenizer,
        )

    @staticmethod
    def _build_parent_chunk(
        *,
        document: Document,
        section: ParsedSection,
        text: str,
        token_count: int,
        parent_position: int,
    ) -> Chunk:
        cid = build_chunk_id(
            document_hash_value=document.document_hash,
            chunk_type=ChunkType.PARENT,
            parent_position=parent_position,
        )
        metadata: dict[str, object] = {
            "section_title": section.heading,
            "section_canonical": canonical_section(section.heading),
            "section_level": section.level,
            "section_path": list(section.section_path),
            "chunk_index": parent_position,
            "table_name": document.source.table_name,
            "document_id": document.source.primary_key,
            "s3_path": document.source.s3_key,
            "strategy": "parent_child",
        }
        for key, value in section.metadata.items():
            metadata.setdefault(key, value)
        return Chunk(
            chunk_id=cid,
            chunk_type=ChunkType.PARENT,
            text=text,
            token_count=token_count,
            position=parent_position,
            document_hash=document.document_hash,
            source=document.source,
            parent_chunk_id=None,
            metadata=metadata,
        )

    @staticmethod
    def _build_children(
        *,
        document: Document,
        section: ParsedSection,
        parent: Chunk,
        parent_position: int,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        child_texts = split_to_token_window(
            parent.text,
            max_tokens=config.child_max_tokens,
            overlap=config.child_overlap_tokens,
            encoding_name=config.tokenizer,
        )
        if not child_texts:
            return []

        children: list[Chunk] = []
        for child_position, child_text in enumerate(child_texts):
            tokens = count_tokens(child_text, config.tokenizer)
            # Drop a trailing sliver only when at least one child already exists.
            if tokens < config.child_min_tokens and children:
                continue
            cid = build_chunk_id(
                document_hash_value=document.document_hash,
                chunk_type=ChunkType.CHILD,
                parent_position=parent_position,
                child_position=child_position,
            )
            metadata: dict[str, object] = {
                "section_title": section.heading,
                "section_canonical": canonical_section(section.heading),
                "section_level": section.level,
                "section_path": list(section.section_path),
                "chunk_index": child_position,
                "child_index_in_parent": child_position,
                "table_name": document.source.table_name,
                "document_id": document.source.primary_key,
                "s3_path": document.source.s3_key,
                "strategy": "parent_child",
            }
            for key, value in section.metadata.items():
                metadata.setdefault(key, value)
            children.append(
                Chunk(
                    chunk_id=cid,
                    chunk_type=ChunkType.CHILD,
                    text=child_text,
                    token_count=tokens,
                    position=child_position,
                    document_hash=document.document_hash,
                    source=document.source,
                    parent_chunk_id=parent.chunk_id,
                    metadata=metadata,
                )
            )
        return children
