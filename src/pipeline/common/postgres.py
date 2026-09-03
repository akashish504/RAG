"""PostgreSQL client for pipeline observability and MCP server state.

Requires the ``postgres`` extra:
    pip install 'dalberg-mcp[postgres]'

Connection is configured via environment variables (set in .env):
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD

Design
------
- No ORM — plain psycopg2 with parameterised queries.
- ``ensure_schema()`` is idempotent: safe to call on every startup.
- All write helpers catch exceptions and log them without re-raising so that
  a PostgreSQL outage never takes down the embedding pipeline.
- PostgreSQL is optional: if the package is not installed or the connection
  fails, callers can handle ``PostgresUnavailable`` and skip logging.

Tables
------
pipeline_runs
    One row per Pipeline.run() / Pipeline.run_one() call.  Written at the
    end of each run so the finished_at and all counters are final.

document_index_log
    One row per successfully processed document.  Provides a queryable log
    of what was indexed, when, and with which embedding model.  The
    deleted_at column supports soft-deletion tracking (file replacement /
    Airtable record removal).

user_sessions  (future MCP server)
    One row per MCP conversation session.

chat_context   (future MCP server)
    Per-turn message store for conversation history.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generator

import structlog

if TYPE_CHECKING:
    from pipeline.embedding_pipeline.models import RunReport

log = structlog.get_logger(__name__)


class PostgresUnavailable(RuntimeError):
    """Raised when psycopg2 is not installed or a connection cannot be made."""


# ---------------------------------------------------------------------------
# DSN builder
# ---------------------------------------------------------------------------


def _build_dsn() -> str:
    host = os.environ.get("POSTGRES_HOST", "")
    port = os.environ.get("POSTGRES_PORT", "5432")
    dbname = os.environ.get("POSTGRES_DB", "")
    user = os.environ.get("POSTGRES_USER", "")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not host or not dbname or not user:
        raise PostgresUnavailable(
            "POSTGRES_HOST, POSTGRES_DB, and POSTGRES_USER must be set."
        )
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    """Yield a psycopg2 connection; caller is responsible for commit/rollback."""
    try:
        import psycopg2  # noqa: PLC0415
        import psycopg2.extras  # noqa: PLC0415
    except ImportError as exc:
        raise PostgresUnavailable(
            "psycopg2 is not installed. "
            "Run: pip install 'dalberg-mcp[postgres]'"
        ) from exc

    conn = psycopg2.connect(_build_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              SERIAL PRIMARY KEY,
    run_id          UUID        NOT NULL UNIQUE,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    prefix          TEXT        NOT NULL,
    documents_read  INTEGER     NOT NULL DEFAULT 0,
    documents_skipped INTEGER   NOT NULL DEFAULT 0,
    documents_failed  INTEGER   NOT NULL DEFAULT 0,
    chunks_produced INTEGER     NOT NULL DEFAULT 0,
    chunks_embedded INTEGER     NOT NULL DEFAULT 0,
    chunks_indexed  INTEGER     NOT NULL DEFAULT 0,
    index_failed    INTEGER     NOT NULL DEFAULT 0,
    embed_tokens    INTEGER     NOT NULL DEFAULT 0,
    embedding_model TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS document_index_log (
    id              SERIAL PRIMARY KEY,
    run_id          UUID        NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    s3_key          TEXT        NOT NULL,
    s3_bucket       TEXT        NOT NULL,
    source_url      TEXT,
    document_hash   TEXT        NOT NULL,
    table_name      TEXT        NOT NULL,
    primary_key     TEXT        NOT NULL,
    column_name     TEXT        NOT NULL,
    chunks_produced INTEGER     NOT NULL DEFAULT 0,
    embedding_model TEXT,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_doc_log_s3_key
    ON document_index_log (s3_key);

CREATE INDEX IF NOT EXISTS idx_doc_log_table_pk
    ON document_index_log (table_name, primary_key);

CREATE INDEX IF NOT EXISTS idx_doc_log_document_hash
    ON document_index_log (document_hash);

CREATE TABLE IF NOT EXISTS user_sessions (
    id             SERIAL PRIMARY KEY,
    session_id     UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    user_email     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata       JSONB       NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chat_context (
    id          SERIAL PRIMARY KEY,
    session_id  UUID   NOT NULL REFERENCES user_sessions(session_id) ON DELETE CASCADE,
    role        TEXT   NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata    JSONB  NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chat_context_session
    ON chat_context (session_id, created_at);
"""


def ensure_schema() -> None:
    """Create all tables if they do not already exist (idempotent)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
    log.info("postgres_schema_ready")


# ---------------------------------------------------------------------------
# Pipeline observability helpers
# ---------------------------------------------------------------------------


def log_pipeline_run(report: RunReport, *, embedding_model: str | None = None) -> None:
    """Insert or update the pipeline_runs row for this run.

    Safe to call even if the run produced no documents.  Failures are logged
    and swallowed so a PG outage never aborts an embedding run.
    """
    try:
        d = report.to_dict()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_runs (
                        run_id, started_at, finished_at, prefix,
                        documents_read, documents_skipped, documents_failed,
                        chunks_produced, chunks_embedded, chunks_indexed,
                        index_failed, embed_tokens, embedding_model
                    ) VALUES (
                        %(run_id)s, %(started_at)s, %(finished_at)s, %(prefix)s,
                        %(documents_read)s, %(documents_skipped)s, %(documents_failed)s,
                        %(chunks_produced)s, %(chunks_embedded)s, %(chunks_indexed)s,
                        %(index_failed)s, %(embed_tokens)s, %(embedding_model)s
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        finished_at       = EXCLUDED.finished_at,
                        documents_read    = EXCLUDED.documents_read,
                        documents_skipped = EXCLUDED.documents_skipped,
                        documents_failed  = EXCLUDED.documents_failed,
                        chunks_produced   = EXCLUDED.chunks_produced,
                        chunks_embedded   = EXCLUDED.chunks_embedded,
                        chunks_indexed    = EXCLUDED.chunks_indexed,
                        index_failed      = EXCLUDED.index_failed,
                        embed_tokens      = EXCLUDED.embed_tokens,
                        embedding_model   = EXCLUDED.embedding_model
                    """,
                    {
                        "run_id": d["run_id"],
                        "started_at": d["started_at"],
                        "finished_at": d["finished_at"],
                        "prefix": d["prefix"],
                        "documents_read": d["documents_read"],
                        "documents_skipped": d["documents_skipped_unchanged"],
                        "documents_failed": d["documents_failed"],
                        "chunks_produced": d["chunks_produced"],
                        "chunks_embedded": d["embed"]["chunks_embedded"],
                        "chunks_indexed": d["index"]["indexed"],
                        "index_failed": d["index"]["failed"],
                        "embed_tokens": d["embed"]["total_tokens"],
                        "embedding_model": embedding_model,
                    },
                )
        log.info("pg_run_logged", run_id=d["run_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("pg_log_pipeline_run_failed", error=str(exc))


def log_document_indexed(
    *,
    run_id: str,
    s3_key: str,
    s3_bucket: str,
    source_url: str | None,
    document_hash: str,
    table_name: str,
    primary_key: str,
    column_name: str,
    chunks_produced: int,
    embedding_model: str | None,
) -> None:
    """Insert a document_index_log row after successful indexing."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO document_index_log (
                        run_id, s3_key, s3_bucket, source_url,
                        document_hash, table_name, primary_key, column_name,
                        chunks_produced, embedding_model, indexed_at
                    ) VALUES (
                        %(run_id)s, %(s3_key)s, %(s3_bucket)s, %(source_url)s,
                        %(document_hash)s, %(table_name)s, %(primary_key)s, %(column_name)s,
                        %(chunks_produced)s, %(embedding_model)s, NOW()
                    )
                    """,
                    {
                        "run_id": run_id,
                        "s3_key": s3_key,
                        "s3_bucket": s3_bucket,
                        "source_url": source_url,
                        "document_hash": document_hash,
                        "table_name": table_name,
                        "primary_key": primary_key,
                        "column_name": column_name,
                        "chunks_produced": chunks_produced,
                        "embedding_model": embedding_model,
                    },
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("pg_log_document_indexed_failed", s3_key=s3_key, error=str(exc))


def mark_document_deleted(*, s3_key: str, deleted_at: datetime | None = None) -> int:
    """Soft-delete all document_index_log rows for the given S3 key.

    Call this when a file is removed from Airtable / S3 so the log accurately
    reflects the current state.  Returns the number of rows updated.
    """
    ts = deleted_at or datetime.now(timezone.utc)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE document_index_log
                       SET deleted_at = %(deleted_at)s
                     WHERE s3_key = %(s3_key)s
                       AND deleted_at IS NULL
                    """,
                    {"s3_key": s3_key, "deleted_at": ts},
                )
                return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.warning("pg_mark_deleted_failed", s3_key=s3_key, error=str(exc))
        return 0
