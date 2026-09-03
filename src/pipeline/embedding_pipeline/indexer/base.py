"""Indexer protocol.

All indexers share this contract so the pipeline orchestrator can swap
backends (OpenSearch, Elasticsearch, local flat-file for tests, …) without
touching call sites.

Design notes
------------
- ``index()`` receives all chunks for a document (parent + child).  Only
  child chunks carry ``embedding``; parent chunks are written without it.
  The OpenSearch KNN pre-filter on ``chunk_type = "child"`` ensures vector
  search never touches parent-only documents.
- ``ensure_index()`` is idempotent: safe to call on every startup.
- ``document_hash_exists()`` supports content-addressed skip logic in the
  pipeline: if a document's sha256 hash is already present in the index,
  no re-embedding or re-indexing is needed unless the file has changed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.embedding_pipeline.models import Chunk, IndexReport


@runtime_checkable
class Indexer(Protocol):
    """Any object that bulk-writes chunks to a search index."""

    def index(self, chunks: list[Chunk]) -> IndexReport:
        """Bulk-upsert chunks using ``chunk_id`` as the document ID.

        Parent and child chunks are both indexed.  Only child chunks will
        carry a populated ``embedding`` field.

        Errors are collected per batch and surfaced via ``IndexReport.errors``
        rather than raised, so a single bad chunk does not abort a full run.
        """
        ...

    def ensure_index(self) -> None:
        """Create the index with the correct mapping if it does not exist.

        Safe to call on every pipeline startup (idempotent).
        """
        ...

    def document_hash_exists(self, document_hash: str) -> bool:
        """Return True if at least one chunk with this hash is already indexed.

        Used by the pipeline to skip unchanged documents and avoid wasting
        Voyage API credits on content that has not changed since the last run.
        """
        ...

    def delete_by_s3_key(self, s3_key: str) -> int:
        """Delete all chunks whose ``s3_key`` field matches the given key.

        Called by the pipeline before re-indexing a changed document so that
        stale chunks (produced from the old file content) do not accumulate
        alongside the new chunks.  Returns the number of documents deleted.
        """
        ...
