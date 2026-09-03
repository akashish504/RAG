"""Chunker protocol and chunking config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pipeline.embedding_pipeline.models import Chunk, Document
from pipeline.embedding_pipeline.parser.base import ParsedDocument


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Per-table chunking parameters.

    Defaults match the design doc: ~500-1000 token parent windows with smaller
    children for precise KNN retrieval and a small overlap to preserve context
    across chunk boundaries.
    """

    parent_max_tokens: int = 800
    parent_overlap_tokens: int = 100
    child_max_tokens: int = 200
    child_min_tokens: int = 30
    child_overlap_tokens: int = 50
    tokenizer: str = "cl100k_base"

    def __post_init__(self) -> None:
        if self.parent_max_tokens <= 0 or self.child_max_tokens <= 0:
            msg = "max token settings must be positive"
            raise ValueError(msg)
        if self.parent_overlap_tokens >= self.parent_max_tokens:
            msg = "parent_overlap_tokens must be < parent_max_tokens"
            raise ValueError(msg)
        if self.child_overlap_tokens >= self.child_max_tokens:
            msg = "child_overlap_tokens must be < child_max_tokens"
            raise ValueError(msg)


class Chunker(Protocol):
    """Any chunking strategy that turns a parsed document into Chunks."""

    def chunk(
        self,
        *,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        """Produce parent and child chunks with full provenance metadata."""
