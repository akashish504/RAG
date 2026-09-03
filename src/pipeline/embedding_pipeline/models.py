"""Core dataclasses passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ChunkType(StrEnum):
    """OpenSearch chunk type.

    Retrieval will run KNN against child chunks and then fetch the parent chunk
    for the fuller context returned to Claude.
    """

    PARENT = "parent"
    CHILD = "child"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Provenance parsed from the S3 key.

    Preferred S3 layout:
        raw/{table_name}/{primary_key}/{column_name}.{ext}

    For ad-hoc files like sample.txt, these fields fall back to neutral values
    so we can still test the reader before Airtable-shaped paths exist.
    """

    s3_bucket: str
    s3_key: str
    table_name: str
    primary_key: str
    column_name: str
    filename: str
    source_url: str | None = None
    # Key of the ORIGINAL source document (e.g. the PDF/DOCX), as opposed to
    # ``s3_key`` which points at the normalized .txt. Used for citations.
    source_s3_key: str | None = None
    airtable_record_id: str | None = None
    airtable_base_id: str | None = None
    airtable_table_id: str | None = None
    # Role of the document this chunk came from. "record_summary" marks the
    # record-level parent summary (one per record, spanning all its files) so
    # retrieval can hydrate it as parent context. None/"" for ordinary files.
    doc_role: str | None = None
    # Structured, filterable Airtable facets ({field_slug: value}, e.g.
    # {"practice_area": [...], "client_organisation": "..."}). Hoisted to
    # keyword/date fields so KNN can be filtered by facet (filtered-KNN).
    facets: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Document:
    """One source object pulled from S3.

    Either `text` or `body` must be populated:
      - `text`: UTF-8 decoded content for text formats (.txt, .md, ...).
      - `body`: raw bytes, kept so binary parsers (PDF/DOCX/PPTX) can read them.
    """

    document_hash: str
    source: SourceMetadata
    text: str = ""
    body: bytes | None = None
    content_type: str | None = None
    content_length: int | None = None
    last_modified: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Chunk:
    """One indexable chunk.

    Embeddings are optional here because chunking and S3 reading can be tested
    before Voyage credentials are available. Provenance fields make every chunk
    independently traceable back to S3 and (later) Airtable.
    """

    chunk_id: str
    chunk_type: ChunkType
    text: str
    token_count: int
    position: int
    document_hash: str
    source: SourceMetadata
    parent_chunk_id: str | None = None
    embedding: list[float] | None = None
    # What to feed the embedder, when it must differ from the stored/cited ``text``
    # (e.g. a child block prefixed with its slide title for context). When None,
    # the embedder uses ``text``. Never indexed — embedding input only.
    embed_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk_index(self) -> int:
        """Alias for `position`, matching the task requirements vocabulary."""

        return self.position

    @property
    def table_name(self) -> str:
        return self.source.table_name

    @property
    def document_id(self) -> str:
        """The Airtable record / source primary key."""

        return self.source.primary_key

    @property
    def s3_path(self) -> str:
        return self.source.s3_key


@dataclass(frozen=True, slots=True)
class ReadReport:
    """Small summary for reader-only smoke tests."""

    documents_read: int
    skipped_keys: int


# ---------------------------------------------------------------------------
# Stage-level reports
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EmbedReport:
    """Aggregated result from one embedder.embed() call or a full run."""

    chunks_embedded: int = 0
    batches_sent: int = 0
    total_tokens: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IndexReport:
    """Aggregated result from one indexer.index() call or a full run."""

    indexed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunReport:
    """End-to-end summary for one Pipeline.run() or Pipeline.run_one() call."""

    run_id: str
    prefix: str
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    finished_at: datetime | None = None

    # Document-level counters
    documents_read: int = 0
    documents_skipped_unchanged: int = 0
    documents_failed: int = 0

    # Chunk-level counters
    chunks_produced: int = 0

    # Stage sub-reports (aggregated across all documents in the run)
    embed_report: EmbedReport = field(default_factory=EmbedReport)
    index_report: IndexReport = field(default_factory=IndexReport)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "prefix": self.prefix,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "documents_read": self.documents_read,
            "documents_skipped_unchanged": self.documents_skipped_unchanged,
            "documents_failed": self.documents_failed,
            "chunks_produced": self.chunks_produced,
            "embed": {
                "chunks_embedded": self.embed_report.chunks_embedded,
                "batches_sent": self.embed_report.batches_sent,
                "total_tokens": self.embed_report.total_tokens,
                "errors": len(self.embed_report.errors),
            },
            "index": {
                "indexed": self.index_report.indexed,
                "failed": self.index_report.failed,
                "errors": len(self.index_report.errors),
            },
        }
