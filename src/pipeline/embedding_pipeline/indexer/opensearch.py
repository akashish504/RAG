"""OpenSearch indexer — bulk-upsert chunks with content-addressed skip logic.

Connection modes
----------------
Basic auth (dev / local)
    Set ``OPENSEARCH_USERNAME`` and ``OPENSEARCH_PASSWORD`` in ``.env``.
    TLS is disabled automatically when the host is ``localhost`` or ``127.*``.

AWS SigV4 (prod / EC2)
    Leave username/password empty.  The indexer uses the ambient IAM role's
    live, auto-refreshing credentials via ``boto3.Session().get_credentials()``,
    signed with ``opensearchpy.AWSV4SignerAuth`` (already a core dependency).

SQS compatibility
-----------------
``OpenSearchIndexer`` is stateless after construction.  A future SQS worker
handler can share one indexer instance across messages (the OpenSearch client
manages connection pooling internally).

Usage
-----
    from dalberg_mcp.pipeline.indexer.opensearch import (
        OpenSearchIndexer,
        build_opensearch_client,
    )
    from dalberg_mcp.config import load_settings

    settings = load_settings()
    client = build_opensearch_client(settings.opensearch, aws_region=settings.aws_region)
    indexer = OpenSearchIndexer(client=client, index=settings.opensearch.index)
    indexer.ensure_index()
    report = indexer.index(chunks)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk as opensearch_bulk

from pipeline.common.opensearch import build_opensearch_client
from pipeline.embedding_pipeline.indexer.mappings import index_create_body
from pipeline.embedding_pipeline.models import Chunk, IndexReport

log = structlog.get_logger(__name__)


# ``build_opensearch_client`` was previously defined here; it now lives in
# ``pipeline.common.opensearch`` so the retrieval module can share it
# without importing the writer. Re-exported here for back-compat with any
# existing callers that did ``from ...indexer.opensearch import build_opensearch_client``.
__all__ = ["OpenSearchIndexer", "build_opensearch_client"]


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class OpenSearchIndexer:
    """Bulk-upsert chunks to OpenSearch using ``chunk_id`` as document ID.

    Re-running the pipeline on an unchanged document is a no-op: the same
    ``chunk_id`` is used as the ``_id``, so each upsert overwrites the
    previous version with identical content.

    Parameters
    ----------
    client:
        A pre-built ``OpenSearch`` client (from ``build_opensearch_client``).
    index:
        Target index name (e.g. ``"mcp-docs"``).
    batch_size:
        Number of bulk actions per ``/_bulk`` call.  256 is the recommended
        default; reduce if individual chunks are very large.
    embedding_model:
        Stored on every indexed document so retrieval queries can filter by
        model version.  Falls back to reading ``chunk.metadata["embedding_model"]``
        if not set here.
    """

    def __init__(
        self,
        *,
        client: OpenSearch,
        index: str,
        batch_size: int = 256,
        embedding_model: str | None = None,
        table_index_map: dict[str, str] | None = None,
        prefix_index_map: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._index = index  # fallback index when nothing else matches
        self._batch_size = batch_size
        self._embedding_model = embedding_model
        # Maps table_name → index_name (e.g. "dalberg_profiles" → "mcp-dalberg-profiles").
        self._table_index_map: dict[str, str] = table_index_map or {}
        # AUTHORITATIVE routing: maps an S3 prefix → index_name (e.g.
        # "raw/d.quals/" → "mcp-d-quals"). Resolving by the chunk's actual S3
        # location is immune to table_name/slug quirks (the dotted-slug bug that
        # silently sent chunks to the fallback). Longest prefix wins.
        self._prefix_index_map: list[tuple[str, str]] = sorted(
            (prefix_index_map or {}).items(), key=lambda kv: len(kv[0]), reverse=True
        )

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------

    def _get_index(self, source: Any) -> str:
        """Resolve the target index for a chunk's source — by S3 prefix first
        (authoritative), then table_name, then the fallback. Warns loudly if a
        chunk from a configured prefix would land in the fallback (a routing bug)."""
        s3_key = getattr(source, "s3_key", "") or ""
        for prefix, idx in self._prefix_index_map:
            if s3_key.startswith(prefix):
                return idx
        table_name = getattr(source, "table_name", "") or ""
        if table_name in self._table_index_map:
            return self._table_index_map[table_name]
        if self._table_index_map or self._prefix_index_map:
            log.warning(
                "index_routing_fell_back_to_default",
                s3_key=s3_key, table_name=table_name, fallback_index=self._index,
                hint="no prefix/table match — chunk would land in the fallback index",
            )
        return self._index

    def _managed_indexes(self) -> list[str]:
        """All unique index names managed by this indexer."""
        indexes: set[str] = set(self._table_index_map.values())
        indexes.update(idx for _, idx in self._prefix_index_map)
        indexes.add(self._index)
        return sorted(indexes)

    # ------------------------------------------------------------------
    # Indexer protocol
    # ------------------------------------------------------------------

    def ensure_index(self) -> None:
        """Create all configured table indexes with the KNN mapping (idempotent)."""
        for idx in self._managed_indexes():
            if self._client.indices.exists(index=idx):
                log.info("index_exists", index=idx)
                continue
            self._client.indices.create(
                index=idx,
                body=index_create_body(idx),
            )
            log.info("index_created", index=idx)

    def document_hash_exists(self, document_hash: str) -> bool:
        """Return True if any chunk for this content hash is already indexed.

        Searches across all managed indexes so a table-specific index is
        checked correctly regardless of which index the hash lives in.
        """
        all_indexes = ",".join(self._managed_indexes())
        try:
            resp = self._client.count(
                index=all_indexes,
                body={"query": {"term": {"document_hash": document_hash}}},
            )
            return resp.get("count", 0) > 0
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "document_hash_check_failed",
                document_hash=document_hash[:12],
                error=str(exc),
            )
            return False

    def delete_by_s3_key(self, s3_key: str) -> int:
        """Delete all chunks whose ``s3_key`` matches the given value.

        Searches across all managed indexes.  Returns total deleted count.
        """
        all_indexes = ",".join(self._managed_indexes())
        try:
            resp = self._client.delete_by_query(
                index=all_indexes,
                body={"query": {"term": {"s3_key": s3_key}}},
                params={"refresh": "true"},
            )
            deleted: int = resp.get("deleted", 0)
            if deleted:
                log.info("stale_chunks_deleted", s3_key=s3_key, deleted=deleted)
            return deleted
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_by_s3_key_failed", s3_key=s3_key, error=str(exc))
            return 0

    def update_fields_by_query(
        self, *, term_field: str, term_value: str, fields: dict[str, Any]
    ) -> int:
        """Set ``fields`` on every existing chunk matching ``term_field=term_value``.

        Metadata-only, in-place update via ``update_by_query`` — NO re-embedding,
        NO delete, NO new chunks. Used to backfill facets onto already-indexed
        records without spending Voyage/LLM. Returns the number of chunks updated.
        """
        if not fields:
            return 0
        all_indexes = ",".join(self._managed_indexes())
        body = {
            "query": {"term": {term_field: term_value}},
            "script": {
                "lang": "painless",
                "source": (
                    "for (entry in params.fields.entrySet()) "
                    "{ ctx._source[entry.getKey()] = entry.getValue(); }"
                ),
                "params": {"fields": fields},
            },
        }
        try:
            resp = self._client.update_by_query(
                index=all_indexes, body=body, params={"refresh": "true", "conflicts": "proceed"}
            )
            updated: int = resp.get("updated", 0)
            log.info("metadata_updated", term=f"{term_field}={term_value}", updated=updated)
            return updated
        except Exception as exc:  # noqa: BLE001
            log.warning("update_by_query_failed", term=f"{term_field}={term_value}", error=str(exc))
            return 0

    def index(self, chunks: list[Chunk]) -> IndexReport:
        """Bulk-upsert all chunks.  Collects per-batch errors without raising."""
        report = IndexReport()
        if not chunks:
            return report

        actions = [self._to_action(c) for c in chunks]
        total_batches = (len(actions) + self._batch_size - 1) // self._batch_size

        for batch_idx in range(total_batches):
            start = batch_idx * self._batch_size
            batch = actions[start : start + self._batch_size]
            try:
                success, errors = opensearch_bulk(
                    self._client,
                    batch,
                    raise_on_error=False,
                    raise_on_exception=False,
                )
                report.indexed += success
                if errors:
                    report.failed += len(errors)
                    for err in errors[:20]:
                        report.errors.append(str(err))
                    log.warning(
                        "bulk_partial_failure",
                        batch=batch_idx + 1,
                        failed=len(errors),
                    )
            except Exception as exc:  # noqa: BLE001
                report.failed += len(batch)
                report.errors.append(
                    f"batch {batch_idx + 1}/{total_batches} exception: {exc}"
                )
                log.error(
                    "bulk_batch_exception",
                    batch=batch_idx + 1,
                    error=str(exc),
                )

        # Report the index/indexes actually written to (not the fallback), so a
        # misroute is visible in the logs.
        target_indexes = sorted({a["_index"] for a in actions})
        log.info(
            "index_complete",
            index=",".join(target_indexes),
            indexed=report.indexed,
            failed=report.failed,
        )
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _to_action(self, chunk: Chunk) -> dict[str, Any]:
        idx = self._get_index(chunk.source)
        meta = chunk.metadata or {}
        doc: dict[str, Any] = {
            "chunk_id": chunk.chunk_id,
            "chunk_type": chunk.chunk_type.value,
            "text": chunk.text,
            "token_count": chunk.token_count,
            "position": chunk.position,
            "document_hash": chunk.document_hash,
            # Provenance (flattened from SourceMetadata)
            "table_name": chunk.source.table_name,
            "primary_key": chunk.source.primary_key,
            "column_name": chunk.source.column_name,
            "s3_key": chunk.source.s3_key,
            "s3_bucket": chunk.source.s3_bucket,
            "source_url": chunk.source.source_url,
            "filename": chunk.source.filename,
            "source_s3_key": chunk.source.source_s3_key,
            "airtable_record_id": chunk.source.airtable_record_id,
            "airtable_base_id": chunk.source.airtable_base_id,
            "airtable_table_id": chunk.source.airtable_table_id,
            # Role of the document this chunk represents. The chunker may set a
            # per-chunk role (e.g. "deck_summary" on a deck's summary chunk);
            # otherwise fall back to the document-level role from the sidecar
            # ("record_summary"). Drives retrieval's summary hydration.
            "doc_role": meta.get("doc_role") or chunk.source.doc_role,
            # Hoisted from metadata so retrieval can filter/boost without relying
            # on dynamic sub-object mapping.
            "section_canonical": meta.get("section_canonical"),
            "slide_number": meta.get("slide_number"),
            # Relationships — every child carries parent_chunk_id so
            # the retriever can fetch the full parent after a KNN hit.
            "parent_chunk_id": chunk.parent_chunk_id,
            # Per-table metadata (section_title, entry_index, section_canonical, …)
            "metadata": meta,
            # Timestamp for audit and freshness checks.
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

        # Hoist structured facets to top-level keyword/date fields so KNN/BM25 can
        # be FILTERED by facet (e.g. practice_area, project_region). Record-level,
        # so they ride on every chunk via SourceMetadata.
        for facet_key, facet_value in (chunk.source.facets or {}).items():
            doc[facet_key] = facet_value

        if chunk.embedding is not None:
            doc["embedding"] = chunk.embedding
            doc["embedding_model"] = (
                self._embedding_model
                or chunk.metadata.get("embedding_model")
                or "unknown"
            )

        return {
            "_index": idx,
            "_id": chunk.chunk_id,
            "_source": doc,
        }
