"""Embedder protocol.

All embedders share this contract so the pipeline orchestrator and future
SQS worker can swap providers (Voyage, OpenAI, Cohere, …) with zero changes
to call sites.

Embedding semantics
-------------------
- Only **child** chunks are embedded. Parent chunks carry full section text
  for context retrieval and are never sent to the embedding API.
- Embedding is done **in-place**: implementations mutate ``chunk.embedding``
  on each eligible chunk, then return an ``EmbedReport`` summarising what
  happened.
- The passage prefix (e.g. ``"passage: "``) is applied *inside* the
  embedder, not by the chunker, so prefix policy stays local to the
  provider adapter.
- ``embedding_model`` is written into ``chunk.metadata`` by the embedder
  so every indexed document records which model version produced its vector.

Adding a new provider
---------------------
1. Create a new module under ``embedder/``.
2. Implement the ``Embedder`` Protocol (``embed`` + ``model`` + ``dims``).
3. Add a factory branch in ``embedder/__init__.py``.
4. Point ``config/default.yaml`` at the new provider name.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.embedding_pipeline.models import Chunk, EmbedReport


@runtime_checkable
class Embedder(Protocol):
    """Any object that produces dense vectors for a list of Chunks."""

    model: str
    """Short model identifier stored in chunk metadata (e.g. ``"voyage-4"``)."""

    dims: int
    """Embedding dimensionality — must match the OpenSearch index mapping."""

    def embed(self, chunks: list[Chunk]) -> EmbedReport:
        """Embed eligible chunks in place and return a run report.

        Parameters
        ----------
        chunks:
            Mixed list of parent and child chunks from the chunker.  Only
            child chunks (``chunk.chunk_type is ChunkType.CHILD``) are
            embedded; parent chunks are silently skipped.

        Returns
        -------
        EmbedReport
            Counts of embedded chunks, API batches sent, tokens consumed,
            and any per-batch error messages.  Errors are collected rather
            than raised so a single bad batch does not abort a full run.
        """
        ...
