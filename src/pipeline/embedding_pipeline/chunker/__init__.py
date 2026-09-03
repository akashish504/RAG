"""Chunker stage — turn a parsed Document into parent + child Chunks.

This package is the place to evolve chunking strategies. New strategies should
register themselves with ``ChunkerRegistry`` (see ``registry.py``). Tables
choose a strategy by name via ``TableConfig.chunker_strategy``, set in
``config/tables.yaml``.
"""

from pipeline.embedding_pipeline.chunker.base import Chunker, ChunkingConfig
from pipeline.embedding_pipeline.chunker.parent_child import ParentChildChunker
from pipeline.embedding_pipeline.chunker.registry import (
    ChunkerRegistry,
    default_chunker_registry,
)
from pipeline.embedding_pipeline.chunker.resume import (
    ResumeChunker,
    canonical_section,
    split_section_into_entries,
)
from pipeline.embedding_pipeline.chunker.tokens import (
    count_tokens,
    get_encoding,
    split_to_token_window,
)

__all__ = [
    "Chunker",
    "ChunkerRegistry",
    "ChunkingConfig",
    "ParentChildChunker",
    "ResumeChunker",
    "canonical_section",
    "count_tokens",
    "default_chunker_registry",
    "get_encoding",
    "split_section_into_entries",
    "split_to_token_window",
]
