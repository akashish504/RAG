"""Chunker strategy registry.

Maps a strategy name (as configured per table in ``config/tables.yaml``) to a
concrete ``Chunker`` implementation. Adding a new strategy is two steps:

1. Implement a ``Chunker`` (see ``base.py``).
2. Register it here, or build a custom ``ChunkerRegistry`` and pass it to the
   composition root (e.g. ``scripts/chunk_s3_object.py``).

Selection happens via ``TableConfig.chunker_strategy`` so different tables
(profile, proposals, finance, ...) can use different strategies without any
code changes at call sites.
"""

from __future__ import annotations

from pipeline.embedding_pipeline.chunker.base import Chunker
from pipeline.embedding_pipeline.chunker.parent_child import ParentChildChunker
from pipeline.embedding_pipeline.chunker.pptx_slide import PPTXSlideChunker
from pipeline.embedding_pipeline.chunker.resume import ResumeChunker


class ChunkerRegistry:
    """In-memory registry of named chunking strategies."""

    def __init__(self) -> None:
        self._by_name: dict[str, Chunker] = {}

    def register(self, name: str, chunker: Chunker) -> None:
        if not name:
            msg = "Chunker strategy name must be non-empty"
            raise ValueError(msg)
        self._by_name[name] = chunker

    def get(self, name: str) -> Chunker:
        if name not in self._by_name:
            msg = (
                f"Unknown chunker strategy: {name!r}. "
                f"Registered: {sorted(self._by_name)}"
            )
            raise KeyError(msg)
        return self._by_name[name]

    def names(self) -> list[str]:
        return sorted(self._by_name)


def default_chunker_registry() -> ChunkerRegistry:
    """Build the default registry shipped with the project."""

    registry = ChunkerRegistry()
    registry.register("parent_child", ParentChildChunker())
    registry.register("pptx_slide", PPTXSlideChunker())
    registry.register("resume", ResumeChunker())
    return registry
