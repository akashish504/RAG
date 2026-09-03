"""CLI entrypoint for a full embedding pipeline run.

Usage
-----
    # Process all tables under the default S3 prefix:
    python scripts/run_pipeline.py --prefix raw/

    # Process a single table:
    python scripts/run_pipeline.py --prefix raw/profile/

    # Process exactly one S3 object (SQS-style single-document mode):
    python scripts/run_pipeline.py --key raw/profile/recABC/cv.txt

    # Dry-run: read + chunk only, no embedding or indexing:
    python scripts/run_pipeline.py --prefix raw/ --dry-run

    # Use the Voyage embedder instead of the stub:
    python scripts/run_pipeline.py --prefix raw/ --embedder voyage

    # Force re-embedding even if document hash already exists in OpenSearch:
    python scripts/run_pipeline.py --prefix raw/ --no-skip-unchanged

Composition root
----------------
This script is the single place that wires together:
  S3Reader → DocumentLoader → ChunkerRegistry → Embedder → OpenSearchIndexer → Pipeline

Future SQS worker
-----------------
An SQS handler can import ``build_pipeline`` from this module and call
``pipeline.run_one(s3_key)`` for each message without duplicating the
composition logic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Load .env for local development before any dalberg_mcp imports.
try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.common.aws import s3_client
from pipeline.config import DEFAULT_TABLES_PATH, load_settings
from pipeline.logging_config import setup_logging
from pipeline.embedding_pipeline.chunker.registry import default_chunker_registry
from pipeline.embedding_pipeline.embedder.base import Embedder
from pipeline.embedding_pipeline.embedder.stub import StubEmbedder
from pipeline.embedding_pipeline.indexer.base import Indexer
from pipeline.embedding_pipeline.indexer.opensearch import (
    OpenSearchIndexer,
    build_opensearch_client,
)
from pipeline.embedding_pipeline.parser.registry import ParserRegistry
from pipeline.embedding_pipeline.pipeline import Pipeline
from pipeline.embedding_pipeline.reader.document_loader import DocumentLoader
from pipeline.embedding_pipeline.reader.s3_reader import S3Reader
from pipeline.embedding_pipeline.reader.tables import TableRegistry


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Dalberg MCP embedding pipeline (S3 → chunk → embed → OpenSearch)"
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--prefix",
        default=None,
        help=(
            "S3 prefix to process (e.g. 'raw/' or 'raw/profile/'). "
            "Omit to iterate all registered tables."
        ),
    )
    source.add_argument(
        "--key",
        default=None,
        help="Process a single S3 object by exact key (run_one mode).",
    )

    parser.add_argument(
        "--embedder",
        choices=["stub", "voyage"],
        default="stub",
        help="Embedding provider (default: stub — no API calls).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and chunk only.  No embedding or indexing.",
    )
    parser.add_argument(
        "--no-skip-unchanged",
        action="store_true",
        help="Re-embed even if the document hash already exists in OpenSearch.",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Backfill facets onto already-indexed chunks IN PLACE (no re-embed, "
             "no LLM, no delete). Run after re-ingesting to add facets.",
    )
    parser.add_argument(
        "--tables-config",
        default=str(DEFAULT_TABLES_PATH),
        help="Path to tables.yaml (default: config/tables.yaml).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region override (default: AWS_REGION env var or eu-west-1).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS credential profile (local development only).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the RunReport as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Write run and document logs to PostgreSQL (requires [postgres] extra and POSTGRES_* env vars).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def build_pipeline(
    *,
    settings,
    tables_config: str,
    embedder_name: str,
    dry_run: bool,
    skip_unchanged: bool,
    aws_region: str,
    aws_profile: str | None,
    enable_postgres: bool = False,
) -> Pipeline:
    """Wire all pipeline components together.

    Importable by a future SQS worker so the composition logic is not
    duplicated:

        from scripts.run_pipeline import build_pipeline
        pipeline = build_pipeline(...)
        pipeline.run_one(s3_key)
    """
    s3 = s3_client(region_name=aws_region, profile_name=aws_profile)
    reader = S3Reader(bucket=settings.s3.bucket, s3_client=s3)
    parser_registry = ParserRegistry()
    table_registry = TableRegistry.from_yaml(tables_config)
    chunker_registry = default_chunker_registry()

    loader = DocumentLoader(
        reader=reader,
        parser_registry=parser_registry,
        table_registry=table_registry,
    )

    embedder: Embedder
    if dry_run:
        embedder = _NullEmbedder()
    elif embedder_name == "voyage":
        from pipeline.embedding_pipeline.embedder.voyage import VoyageEmbedder  # noqa: PLC0415
        voyage_key = os.environ.get("VOYAGE_API_KEY", "").strip()
        if not voyage_key:
            raise ValueError(
                "VOYAGE_API_KEY is not set. "
                "Export it or set it in .env before using --embedder voyage."
            )
        embedder = VoyageEmbedder(
            api_key=voyage_key,
            model=os.environ.get("VOYAGE_MODEL", "voyage-4"),
            dims=int(os.environ.get("VOYAGE_EMBED_DIMS", "1024")),
            batch_size=int(os.environ.get("EMBED_BATCH_SIZE", "32")),
        )
    else:
        embedder = StubEmbedder()

    indexer: Indexer
    if dry_run:
        indexer = _NullIndexer()
    else:
        os_client = build_opensearch_client(
            settings.opensearch.endpoint,
            username=settings.opensearch.username,
            password=settings.opensearch.password,
            aws_region=aws_region,
        )
        # Route each table's chunks to their own isolated index (e.g.
        # mcp-dalberg-profiles, mcp-d-quals). Prefix routing is authoritative — it
        # resolves by the chunk's actual S3 location, so a dotted slug (raw/d.quals/)
        # can't silently fall back to mcp-docs. table_name map is a secondary path.
        table_index_map = {t.name: t.index_name for t in table_registry.list()}
        prefix_index_map = {t.s3_prefix: t.index_name for t in table_registry.list()}
        indexer = OpenSearchIndexer(
            client=os_client,
            index=settings.opensearch.index,  # fallback for unmapped tables
            batch_size=int(os.environ.get("CHUNK_BATCH_SIZE", "128")),
            embedding_model=embedder.model if not dry_run else None,
            table_index_map=table_index_map,
            prefix_index_map=prefix_index_map,
        )
        indexer.ensure_index()

    pg_doc_logger = None
    if enable_postgres and not dry_run:
        try:
            from pipeline.common.postgres import ensure_schema, log_document_indexed  # noqa: PLC0415
            ensure_schema()
            pg_doc_logger = log_document_indexed
        except Exception as exc:  # noqa: BLE001
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning("postgres_unavailable: %s", exc)

    return Pipeline(
        loader=loader,
        chunker_registry=chunker_registry,
        embedder=embedder,
        indexer=indexer,
        skip_unchanged=skip_unchanged,
        pg_doc_logger=pg_doc_logger,
    )


# ---------------------------------------------------------------------------
# Dry-run no-op implementations
# ---------------------------------------------------------------------------


class _NullEmbedder:
    """No-op embedder for --dry-run mode."""

    model: str = "null"
    dims: int = 1024

    def embed(self, _):  # type: ignore[override]
        from pipeline.embedding_pipeline.models import EmbedReport  # noqa: PLC0415
        return EmbedReport()


class _NullIndexer:
    """No-op indexer for --dry-run mode. Counts chunks without writing."""

    def ensure_index(self) -> None:
        pass

    def document_hash_exists(self, _: str) -> bool:
        return False

    def delete_by_s3_key(self, _: str) -> int:
        return 0

    def index(self, chunks):  # type: ignore[override]
        from pipeline.embedding_pipeline.models import IndexReport  # noqa: PLC0415
        return IndexReport(indexed=len(chunks))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _p(msg: str = "") -> None:
    """Print with immediate flush — works in non-TTY sessions (SSM, CI)."""
    print(msg, flush=True)


def main() -> None:
    import datetime as _dt  # noqa: PLC0415

    args = parse_args()

    setup_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_logs=os.environ.get("MCP_ENV", "dev") != "dev",
    )

    settings = load_settings()
    region = args.region or settings.aws_region
    prefix_or_key = args.key or args.prefix or settings.s3.prefix
    mode = "single-file" if args.key else "batch"
    dry = " (DRY RUN — no embed/index)" if args.dry_run else ""

    # Resolve & show the target index for this prefix up front — so a misroute is
    # caught BEFORE spending on embeddings (the dotted-slug bug sent d.quals to mcp-docs).
    _reg = TableRegistry.from_yaml(args.tables_config)
    _match = next(
        (t for t in _reg.list()
         if prefix_or_key.startswith(t.s3_prefix) or t.s3_prefix.startswith(prefix_or_key)),
        None,
    )
    target_index = _match.index_name if _match else settings.opensearch.index

    _p("=" * 60)
    _p("  Embedding Pipeline")
    _p(f"  Mode      : {mode}{dry}")
    _p(f"  Source    : {prefix_or_key}")
    _p(f"  Target idx: {target_index}" + ("  ⚠ FALLBACK (no table matched!)" if _match is None else ""))
    _p(f"  Embedder  : {args.embedder}")
    _p(f"  Started   : {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _p("=" * 60)

    pipeline = build_pipeline(
        settings=settings,
        tables_config=args.tables_config,
        embedder_name=args.embedder,
        dry_run=args.dry_run,
        skip_unchanged=not args.no_skip_unchanged,
        aws_region=region,
        aws_profile=args.profile,
        enable_postgres=args.postgres,
    )

    if args.refresh_metadata:
        # In-place facet backfill — no embed, no LLM, no delete.
        stats = pipeline.refresh_metadata(prefix=args.prefix or settings.s3.prefix)
        _p(f"  metadata refresh: updated {stats['chunks']} chunk(s) "
           f"across {stats['records']} record(s)")
        return

    if args.key:
        report = pipeline.run_one(args.key)
    else:
        report = pipeline.run(prefix=args.prefix or settings.s3.prefix)

    if args.postgres and not args.dry_run:
        try:
            from pipeline.common.postgres import log_pipeline_run  # noqa: PLC0415
            log_pipeline_run(
                report,
                embedding_model=os.environ.get("VOYAGE_MODEL") if args.embedder == "voyage" else None,
            )
        except Exception as exc:  # noqa: BLE001
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning("postgres_run_log_failed: %s", exc)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    d = report.to_dict()
    has_failures = d["documents_failed"] > 0 or d["index"]["failed"] > 0 or d["embed"]["errors"]
    status = "COMPLETED WITH ERRORS" if has_failures else "COMPLETED SUCCESSFULLY"

    _p("")
    _p("=" * 60)
    _p(f"  {status}")
    _p("=" * 60)
    _p(f"  run_id              : {d['run_id']}")
    _p(f"  duration            : {d['duration_seconds']:.1f}s")
    _p(f"  documents processed : {d['documents_read']}")
    _p(f"  documents skipped   : {d['documents_skipped_unchanged']}  (unchanged)")
    _p(f"  documents failed    : {d['documents_failed']}")
    _p(f"  chunks produced     : {d['chunks_produced']}")
    _p(f"  chunks embedded     : {d['embed']['chunks_embedded']}")
    _p(f"  tokens used         : {d['embed']['total_tokens']}")
    _p(f"  chunks indexed      : {d['index']['indexed']}")
    _p(f"  index failures      : {d['index']['failed']}")
    if d["embed"]["errors"]:
        _p(f"\n  Embed errors ({d['embed']['errors']}):")
        for err in report.embed_report.errors[:10]:
            _p(f"    ! {err}")
    if d["index"]["errors"]:
        _p(f"\n  Index errors ({d['index']['failed']}):")
        for err in report.index_report.errors[:10]:
            _p(f"    ! {err}")
    _p("")
    # Exact-match token for CloudWatch metric filters (batch heartbeat /
    # error-spike). --refresh-metadata and --json runs intentionally don't
    # emit it — they are not batch ingestion runs.
    _p(
        f"PIPELINE_RUN_COMPLETED component=batch "
        f"status={'errors' if has_failures else 'success'} "
        f"run_id={d['run_id']} documents_failed={d['documents_failed']} "
        f"index_failed={d['index']['failed']}"
    )


if __name__ == "__main__":
    main()
