"""Pipeline orchestrator — read → chunk → embed → index.

Design
------
The ``Pipeline`` class is the single composition root that wires the four
stages together.  It exposes two entry points:

``run(prefix)``
    Batch mode.  Iterates every supported S3 object under ``prefix`` via
    ``DocumentLoader.iter_prefix``, processes each document, and returns an
    aggregated ``RunReport``.  Used by ``scripts/run_pipeline.py``.

``run_one(s3_key)``
    Single-document mode.  Loads and processes exactly one object.  This is
    the natural atomic unit for a future SQS worker: the handler will
    instantiate the pipeline once and call ``run_one(message.s3_key)`` for
    each SQS message, inheriting all the batching, retry, and skip logic.

Content-addressed skip
----------------------
When ``skip_unchanged=True`` (the default), the pipeline calls
``indexer.document_hash_exists(document_hash)`` before spending any Voyage
credits.  If the sha256 of the S3 bytes is already in OpenSearch, the
document is skipped and ``RunReport.documents_skipped_unchanged`` is
incremented.  A changed file produces a new hash and is always re-processed.

Per-document error isolation
-----------------------------
A single document that raises an unhandled exception is logged and counted
in ``RunReport.documents_failed``; the run continues.  Stage-level errors
(embedding batch failures, bulk partial failures) are collected inside
``EmbedReport.errors`` and ``IndexReport.errors`` respectively.
"""

from __future__ import annotations

import uuid
import gc
import os
from collections.abc import Callable
from datetime import datetime, timezone

import structlog

from pipeline.embedding_pipeline.chunker.registry import ChunkerRegistry
from pipeline.embedding_pipeline.embedder.base import Embedder
from pipeline.embedding_pipeline.indexer.base import Indexer
from pipeline.embedding_pipeline.models import Chunk, ChunkType, RunReport
from pipeline.embedding_pipeline.reader.document_loader import DocumentLoader, LoadedDocument

log = structlog.get_logger(__name__)

# Embed + index in SMALL flushed groups of ~this many child chunks, so RAM stays
# tiny (~this×vector-size, not the whole corpus), each unit is quick to process, and
# indexed progress persists across a crash. Deliberately conservative for small EC2
# instances; raise via PIPELINE_FLUSH_CHILDREN if the box has plenty of headroom.
_FLUSH_CHILDREN = int(os.environ.get("PIPELINE_FLUSH_CHILDREN", "500"))

# A single document producing more than this many child chunks is almost certainly
# corrupt extraction (e.g. a normalized.txt with hundreds/thousands of "## Slide"
# sections). children ≈ slides × ~5-6, so ~4000 ≈ >600 slides — no real deck/report
# is that large, while genuinely long reports still pass. Skip + flag it rather than
# crash the run (each vector is a ~32 KB Python list, so a huge doc OOMs the embed
# step). Tunable via PIPELINE_MAX_CHILDREN_PER_DOC.
_MAX_CHILDREN_PER_DOC = int(os.environ.get("PIPELINE_MAX_CHILDREN_PER_DOC", "4000"))


def _dedupe_chunks_preserve_order(chunks: list[Chunk]) -> tuple[list[Chunk], int]:
    """Drop later rows that reuse the same ``chunk_id`` (deterministic id).

    OpenSearch upserts on ``chunk_id``, so duplicates waste work and can confuse
    operators counting distinct child vectors. This guards against accidental
    double-extension of the chunk list in a future refactor.

    Returns ``(deduped_chunks, duplicate_count_removed)``.
    """

    seen: set[str] = set()
    out: list[Chunk] = []
    dup = 0
    for c in chunks:
        if c.chunk_id in seen:
            dup += 1
            continue
        seen.add(c.chunk_id)
        out.append(c)
    return out, dup


def _p(msg: str) -> None:
    """Print with immediate flush — works in non-TTY sessions (SSM, CI)."""
    print(msg, flush=True)

# Type alias for the optional PostgreSQL document-log callback.
# Matches the signature of ``pipeline.common.postgres.log_document_indexed``.
PgDocLogger = Callable[..., None]


class Pipeline:
    """Orchestrate the read → chunk → embed → index pipeline.

    Parameters
    ----------
    loader:
        ``DocumentLoader`` wiring the S3 reader, parser registry, and table
        registry together.
    chunker_registry:
        Registry of named chunking strategies (``"parent_child"``,
        ``"resume"``, …).  The strategy is selected per table from
        ``TableConfig.chunker_strategy``.
    embedder:
        ``StubEmbedder`` or ``VoyageEmbedder`` (or any future provider).
        Must implement the ``Embedder`` protocol.
    indexer:
        ``OpenSearchIndexer`` or any future backend.
        Must implement the ``Indexer`` protocol.
    skip_unchanged:
        When True (default), documents whose sha256 hash already exists in
        the index are skipped without embedding or re-indexing.
    """

    def __init__(
        self,
        *,
        loader: DocumentLoader,
        chunker_registry: ChunkerRegistry,
        embedder: Embedder,
        indexer: Indexer,
        skip_unchanged: bool = True,
        pg_doc_logger: PgDocLogger | None = None,
    ) -> None:
        self._loader = loader
        self._chunker_registry = chunker_registry
        self._embedder = embedder
        self._indexer = indexer
        self._skip_unchanged = skip_unchanged
        self._pg_doc_logger = pg_doc_logger

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def _embed_and_index(
        self,
        pending: list[tuple[LoadedDocument, list[Chunk]]],
        report: RunReport,
        run_id: str,
    ) -> None:
        """Embed all child chunks in ``pending`` (one batched Voyage pass) then index
        each document. Called on each incremental flush and once at the end, so RAM is
        bounded and indexed progress survives a later crash."""
        if not pending:
            return

        all_children: list[Chunk] = [
            c for _, chunks in pending for c in chunks if c.chunk_type is ChunkType.CHILD
        ]
        if all_children:
            _p(f"\n  Embedding {len(all_children)} child chunks across {len(pending)} documents ...")
            embed_sub = self._embedder.embed(all_children)
            report.embed_report.chunks_embedded += embed_sub.chunks_embedded
            report.embed_report.batches_sent += embed_sub.batches_sent
            report.embed_report.total_tokens += embed_sub.total_tokens
            report.embed_report.errors.extend(embed_sub.errors)

        for loaded, chunks in pending:
            key = loaded.document.source.s3_key
            try:
                index_sub = self._indexer.index(chunks)
                report.index_report.indexed += index_sub.indexed
                report.index_report.failed += index_sub.failed
                report.index_report.errors.extend(index_sub.errors)
                _p(f"         → indexed  {key}  ({index_sub.indexed} OK"
                   + (f", {index_sub.failed} failed" if index_sub.failed else "")
                   + ")")

                if self._pg_doc_logger is not None and run_id:
                    doc = loaded.document
                    embedded = [c for c in chunks if c.chunk_type is ChunkType.CHILD and c.embedding]
                    embedding_model = embedded[0].metadata.get("embedding_model") if embedded else None
                    self._pg_doc_logger(
                        run_id=run_id,
                        s3_key=doc.source.s3_key,
                        s3_bucket=doc.source.s3_bucket,
                        source_url=doc.source.source_url,
                        document_hash=doc.document_hash,
                        table_name=doc.source.table_name,
                        primary_key=doc.source.primary_key,
                        column_name=doc.source.column_name,
                        chunks_produced=len(chunks),
                        embedding_model=embedding_model,
                    )
            except Exception as exc:  # noqa: BLE001
                report.documents_failed += 1
                _p(f"  [FAIL] index {key} — {exc}")
                log.error("document_index_failed", key=key, error=str(exc), exc_info=True)

    def refresh_metadata(self, prefix: str | None = None) -> dict[str, int]:
        """Backfill structured facets onto already-indexed chunks IN PLACE.

        Reads each record's facets from its S3 sidecars and applies them to every
        chunk of that record (matched by ``primary_key``, so attachment chunks AND
        the record/deck summary are all covered in one update). NO re-embedding, NO
        LLM, NO delete — pure metadata update. Cheap, idempotent, safe to re-run.
        Use after re-running ingestion to add facets that didn't exist at index time.
        """
        source = (
            self._loader.iter_prefix(prefix)
            if prefix is not None
            else self._loader.iter_all()
        )
        facets_by_pk: dict[str, dict] = {}
        for loaded in source:
            src = loaded.document.source
            if src.facets and src.primary_key and src.primary_key not in facets_by_pk:
                facets_by_pk[src.primary_key] = src.facets

        records, chunks = 0, 0
        for pk, facets in facets_by_pk.items():
            updated = self._indexer.update_fields_by_query(
                term_field="primary_key", term_value=pk, fields=facets
            )
            if updated:
                records += 1
                chunks += updated
        log.info("metadata_refresh_complete", records=records, chunks=chunks)
        return {"records": records, "chunks": chunks}

    def run(self, prefix: str | None = None) -> RunReport:
        """Process all supported documents under ``prefix``.

        Embedding is batched across ALL documents in the prefix before any
        indexing happens.  This turns N sequential Voyage API calls (one per
        document) into ceil(total_children / batch_size) calls, which is
        dramatically faster when each API call has significant fixed latency.

        Parameters
        ----------
        prefix:
            S3 prefix to iterate (e.g. ``"raw/"`` or ``"raw/profile/"``).
            When ``None``, falls back to iterating all registered tables
            via ``DocumentLoader.iter_all()``.
        """
        run_id = str(uuid.uuid4())
        report = RunReport(
            run_id=run_id,
            prefix=prefix or "iter_all",
            started_at=datetime.now(timezone.utc),
        )

        source = (
            self._loader.iter_prefix(prefix)
            if prefix is not None
            else self._loader.iter_all()
        )

        with structlog.contextvars.bound_contextvars(run_id=run_id):
            log.info("pipeline_run_start", prefix=report.prefix)

            # Phase 1 — Load + chunk every document (no API calls yet).
            # Documents that are unchanged are skipped here; changed ones have
            # their stale index entries deleted before we proceed.
            pending: list[tuple[LoadedDocument, list[Chunk]]] = []
            buffered_children = 0
            doc_num = 0
            for loaded in source:
                doc_num += 1
                key = loaded.document.source.s3_key
                doc = loaded.document
                _p(f"  [{doc_num:>4}] {key}")
                try:
                    if self._skip_unchanged and self._indexer.document_hash_exists(
                        doc.document_hash
                    ):
                        report.documents_skipped_unchanged += 1
                        _p("         → unchanged, skipped")
                        log.info(
                            "document_skipped_unchanged",
                            key=key,
                            document_hash=doc.document_hash[:12],
                        )
                        continue

                    self._indexer.delete_by_s3_key(key)

                    strategy = loaded.table.chunker_strategy
                    chunker = self._chunker_registry.get(strategy)
                    chunks = chunker.chunk(
                        document=doc,
                        parsed=loaded.parsed,
                        config=loaded.table.chunking,
                    )
                    chunks, dups = _dedupe_chunks_preserve_order(chunks)
                    if dups:
                        log.warning("duplicate_chunk_ids_after_chunking", key=key, removed=dups)

                    parents = sum(1 for c in chunks if c.chunk_type is ChunkType.PARENT)
                    children = sum(1 for c in chunks if c.chunk_type is ChunkType.CHILD)
                    _p(f"         → chunked  ({parents} parents, {children} children, strategy={strategy})")
                    log.info("document_chunked", key=key, strategy=strategy, parents=parents, children=children)

                    # Guard against a corrupt/degenerate document exploding into a
                    # huge chunk count (would OOM the embed step). Skip + flag it for
                    # re-extraction instead of crashing the whole run.
                    if children > _MAX_CHILDREN_PER_DOC:
                        report.documents_failed += 1
                        _p(f"  [SKIP] {key} — {children} children (> {_MAX_CHILDREN_PER_DOC}); "
                           f"likely CORRUPT extraction, not embedded. Re-extract this file.")
                        log.error(
                            "document_skipped_too_many_chunks",
                            key=key, parents=parents, children=children,
                            limit=_MAX_CHILDREN_PER_DOC,
                        )
                        continue

                    report.documents_read += 1
                    report.chunks_produced += len(chunks)
                    pending.append((loaded, chunks))
                    buffered_children += children

                except Exception as exc:  # noqa: BLE001
                    report.documents_failed += 1
                    _p(f"  [FAIL] {key} — {exc}")
                    log.error("document_failed", key=key, error=str(exc), exc_info=True)

                # Incremental flush: embed + index this buffer once it's big enough,
                # so memory stays bounded and progress PERSISTS (a later crash can't
                # lose already-indexed docs; re-running skips them via the hash check).
                if buffered_children >= _FLUSH_CHILDREN:
                    self._embed_and_index(pending, report, run_id)
                    pending = []
                    buffered_children = 0
                    gc.collect()  # release the flushed group's memory back to the OS

            # Phase 2/3 — Embed + index the final partial buffer (most groups were
            # already flushed incrementally inside the loop above).
            self._embed_and_index(pending, report, run_id)

        report.finished_at = datetime.now(timezone.utc)
        log.info("pipeline_run_complete", **report.to_dict())
        return report

    def run_one(self, s3_key: str) -> RunReport:
        """Process a single S3 object.

        Designed as the atomic unit for a future SQS consumer:

            handler = lambda msg: pipeline.run_one(msg.body["s3_key"])

        All skip, chunk, embed, and index logic is identical to ``run``.
        """
        run_id = str(uuid.uuid4())
        report = RunReport(
            run_id=run_id,
            prefix=s3_key,
            started_at=datetime.now(timezone.utc),
        )

        with structlog.contextvars.bound_contextvars(run_id=run_id):
            log.info("pipeline_run_one_start", key=s3_key)
            try:
                loaded = self._loader.load_one(s3_key)
                self._process_one(loaded, report, run_id=run_id)
            except Exception as exc:  # noqa: BLE001
                report.documents_failed += 1
                log.error(
                    "document_failed",
                    key=s3_key,
                    error=str(exc),
                    exc_info=True,
                )

        report.finished_at = datetime.now(timezone.utc)
        log.info("pipeline_run_one_complete", **report.to_dict())
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_one(self, loaded: LoadedDocument, report: RunReport, run_id: str = "") -> None:
        doc = loaded.document
        key = doc.source.s3_key

        doc_num = report.documents_read + report.documents_skipped_unchanged + report.documents_failed + 1
        _p(f"  [{doc_num:>4}] {key}")

        # --- Content-addressed skip -----------------------------------
        if self._skip_unchanged and self._indexer.document_hash_exists(
            doc.document_hash
        ):
            log.info(
                "document_skipped_unchanged",
                key=key,
                document_hash=doc.document_hash[:12],
            )
            report.documents_skipped_unchanged += 1
            _p("         → unchanged, skipped")
            return

        # --- Delete stale chunks for this S3 key ---------------------
        # A changed file has a new hash (so the skip above did not fire) but
        # its old chunks are still in the index under the old hash.  Deleting
        # by s3_key before indexing keeps the index clean.  For a brand-new
        # document this is a no-op (nothing to delete).
        self._indexer.delete_by_s3_key(key)

        # --- Chunk ----------------------------------------------------
        strategy = loaded.table.chunker_strategy
        chunker = self._chunker_registry.get(strategy)
        chunks = chunker.chunk(
            document=doc,
            parsed=loaded.parsed,
            config=loaded.table.chunking,
        )

        chunks, chunk_id_dups = _dedupe_chunks_preserve_order(chunks)
        if chunk_id_dups:
            log.warning(
                "duplicate_chunk_ids_after_chunking",
                key=key,
                removed=chunk_id_dups,
                remaining=len(chunks),
            )
            _p(f"         → deduped  ({chunk_id_dups} duplicate chunk_id rows removed)")

        report.documents_read += 1
        report.chunks_produced += len(chunks)

        parents = sum(1 for c in chunks if c.chunk_type is ChunkType.PARENT)
        children = sum(1 for c in chunks if c.chunk_type is ChunkType.CHILD)
        _p(f"         → chunked  ({parents} parents, {children} children, strategy={strategy})")
        log.info(
            "document_chunked",
            key=key,
            strategy=strategy,
            parents=parents,
            children=children,
        )

        # --- Embed (child chunks only) --------------------------------
        child_chunks = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
        if child_chunks:
            embed_sub = self._embedder.embed(child_chunks)
            # Embedder mutates chunks in place; accumulate report counters.
            report.embed_report.chunks_embedded += embed_sub.chunks_embedded
            report.embed_report.batches_sent += embed_sub.batches_sent
            report.embed_report.total_tokens += embed_sub.total_tokens
            report.embed_report.errors.extend(embed_sub.errors)

        # --- Index (all chunks: parent + child, children with vectors) -
        index_sub = self._indexer.index(chunks)
        report.index_report.indexed += index_sub.indexed
        report.index_report.failed += index_sub.failed
        report.index_report.errors.extend(index_sub.errors)
        _p(f"         → indexed  ({index_sub.indexed} OK"
           + (f", {index_sub.failed} failed" if index_sub.failed else "")
           + ")")

        # --- Optional PostgreSQL document log -------------------------
        if self._pg_doc_logger is not None and run_id:
            embedding_model: str | None = doc.metadata.get("embedding_model")
            if not embedding_model:
                embedded = [c for c in chunks if c.chunk_type is ChunkType.CHILD and c.embedding]
                if embedded:
                    embedding_model = embedded[0].metadata.get("embedding_model")
            self._pg_doc_logger(
                run_id=run_id,
                s3_key=doc.source.s3_key,
                s3_bucket=doc.source.s3_bucket,
                source_url=doc.source.source_url,
                document_hash=doc.document_hash,
                table_name=doc.source.table_name,
                primary_key=doc.source.primary_key,
                column_name=doc.source.column_name,
                chunks_produced=len(chunks),
                embedding_model=embedding_model,
            )
