"""Typed models for Airtable attachment ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AttachmentSpec:
    """Airtable attachment descriptor."""

    id: str
    filename: str
    url: str
    content_type: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionTargetConfig:
    """One configurable Airtable sync target."""

    name: str
    enabled: bool
    database_id: str
    database_name: str
    table_id: str
    table_name: str
    identifier_column: str
    attachment_columns: tuple[str, ...]
    # Opt-in to the event-driven poller/worker (scripts/run_poller.py,
    # scripts/run_worker.py). Separate from `enabled`, which only gates
    # manual/batch runs (run_airtable_ingestion.py --target ...). Defaults to
    # False so adding a target here never silently starts polling it.
    poll_enabled: bool = False
    # Positive allowlist of attachment extensions (lowercase, dot-prefixed).
    # Empty tuple = use DEFAULT_ALLOWED_EXTENSIONS in pipeline.py (everything
    # the pipeline can extract today). Anything not in the effective set is
    # skipped BEFORE download / S3 upload and counted in the run report.
    allowed_extensions: tuple[str, ...] = ()
    text_columns: tuple[str, ...] = ()
    # Record fields fetched purely to enrich each attachment's normalized.txt
    # front-matter (co-embedded + indexed as metadata). Unlike text_columns,
    # these are NOT uploaded as standalone documents.
    metadata_columns: tuple[str, ...] = ()
    # Airtable columns stored as STRUCTURED, filterable facets (keyword/date fields
    # in OpenSearch) on every chunk — e.g. "Client Organisation", "Practice Area",
    # "Project Region". Enables filtered-KNN (narrow the search by facet) and
    # faceting; co-embedded via the header too. Column name -> slug is automatic.
    facet_columns: tuple[str, ...] = ()
    s3_prefix: str = "raw"
    process_images: bool = False  # when True, image attachments are passed to the normalizer instead of skipped
    normalizer: str | None = None  # default registered normalizer for all attachment columns
    # Per-column normalizer overrides: tuple of (column_name, normalizer_name_or_None).
    # Takes precedence over `normalizer` for the named column.
    # e.g.: (("CV Attachment", "llm_cv"), ("Bio Attachment", "llm_bio"))
    attachment_column_normalizers: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionDefaults:
    """Top-level defaults shared across targets."""

    metadata_local_dir: str = "data/metadata/airtable"
    metadata_s3_prefix: str = "metadata/airtable_schema"
    upload_schema_to_s3: bool = True
    request_timeout_seconds: float = 120.0
    page_size: int = 100


@dataclass(frozen=True, slots=True)
class AirtableIngestionSettings:
    """Resolved ingestion settings and targets."""

    airtable_pat_token: str
    aws_region: str
    s3_bucket: str
    defaults: IngestionDefaults
    targets: tuple[IngestionTargetConfig, ...]

    def target(self, name: str) -> IngestionTargetConfig:
        for item in self.targets:
            if item.name == name:
                return item
        raise KeyError(f"Unknown ingestion target {name!r}")


@dataclass(slots=True)
class IngestionRunReport:
    """Aggregated report for one run."""

    target_name: str
    started_at: datetime
    finished_at: datetime | None = None
    records_seen: int = 0
    records_skipped_no_identifier: int = 0
    attachment_fields_seen: int = 0
    attachments_downloaded: int = 0
    attachments_uploaded: int = 0
    attachments_skipped: int = 0
    # Attachments rejected by the per-target format allowlist (allowed_extensions /
    # DEFAULT_ALLOWED_EXTENSIONS) BEFORE download or S3 upload.
    attachments_skipped_disallowed: int = 0
    # Attachments provably password-protected (encrypted PDF / Office file),
    # skipped after download but BEFORE the original S3 upload and extraction.
    attachments_skipped_password_protected: int = 0
    normalizations_done: int = 0  # attachments converted to .normalized.txt by a normalizer
    record_summaries_done: int = 0  # record-level __record_summary.txt artifacts written
    # Files uploaded as originals but NOT turned into normalized text (normalizer
    # returned None / unsupported type / LibreOffice failed). Surfaced so "all files
    # have normalized text" is verifiable. Each entry: "label — reason".
    attachments_unextracted: list[str] = field(default_factory=list)
    text_fields_uploaded: int = 0
    text_fields_skipped: int = 0
    errors: list[str] = field(default_factory=list)
