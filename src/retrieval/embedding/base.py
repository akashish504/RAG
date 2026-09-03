"""Query embedder protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class QueryEmbedder(Protocol):
    """Embed a single query string into a fixed-dimension vector."""

    model: str
    dims: int

    async def embed_query(self, text: str) -> list[float]:
        ...
