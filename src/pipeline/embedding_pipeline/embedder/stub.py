"""Stub embedder — deterministic zero-vectors for testing.

Fills every child chunk's ``embedding`` field with ``[0.0] * dims`` so the
full read → chunk → embed → index path can be exercised against a real
OpenSearch cluster (with the correct 1024-dim mapping) before Voyage
credentials are available.

Swap to ``VoyageEmbedder`` by changing ``embedding.provider: voyage`` in
``config/default.yaml`` — no other code changes required.
"""

from __future__ import annotations

import structlog

from pipeline.embedding_pipeline.models import Chunk, ChunkType, EmbedReport

log = structlog.get_logger(__name__)


class StubEmbedder:
    """Emits zero-vectors of the configured dimensionality.

    Implements the ``Embedder`` protocol without network calls.
    """

    model: str = "stub"
    dims: int = 1024

    def __init__(self, *, dims: int = 1024) -> None:
        self.dims = dims
        self._zero_vector: list[float] = [0.0] * self.dims

    def embed(self, chunks: list[Chunk]) -> EmbedReport:
        report = EmbedReport()
        child_chunks = [c for c in chunks if c.chunk_type is ChunkType.CHILD]

        for chunk in child_chunks:
            chunk.embedding = list(self._zero_vector)
            chunk.metadata["embedding_model"] = self.model
            report.chunks_embedded += 1

        if child_chunks:
            log.debug(
                "stub_embed_complete",
                chunks_embedded=report.chunks_embedded,
                total_input=len(chunks),
            )

        return report
