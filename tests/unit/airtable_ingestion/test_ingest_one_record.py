"""Tests for AirtableAttachmentIngestionPipeline.ingest_one_record — the
event-driven worker's single-record entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.airtable_ingestion.models import AttachmentSpec, IngestionTargetConfig
from pipeline.airtable_ingestion.normalizers import (
    normalize_identifier,
    slugify_column_name,
    slugify_table_name,
)
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline


class _FakeUploader:
    def __init__(self, present: set[str] | None = None) -> None:
        self.present = present or set()
        self.uploaded_bytes: list[str] = []
        self.uploaded_files: list[str] = []

    def key_exists(self, *, key: str) -> bool:
        return key in self.present

    def get_text(self, *, key: str) -> str | None:  # noqa: ARG002
        return None

    def get_bytes(self, *, key: str) -> bytes | None:  # noqa: ARG002
        return None

    def get_etag(self, *, key: str) -> str | None:  # noqa: ARG002
        return None

    def upload_file(self, *, local_path, key: str, content_type=None) -> None:  # noqa: ARG002
        self.uploaded_files.append(key)

    def upload_bytes(self, *, data: bytes, key: str, content_type=None) -> None:  # noqa: ARG002
        self.uploaded_bytes.append(key)

    def upload_json(self, *, payload, key: str) -> None:  # noqa: ARG002
        pass


class _FakeAirtable:
    @staticmethod
    def extract_attachments(record: dict, field_name: str) -> list[AttachmentSpec]:
        return [
            AttachmentSpec(id=a["id"], filename=a["filename"], url="http://x")
            for a in (record.get("fields", {}).get(field_name) or [])
        ]

    def download_attachment(self, attachment: AttachmentSpec, *, path: str) -> None:  # noqa: ARG002
        Path(path).write_bytes(b"binary")


class _TextNormalizer:
    """Stand-in for an llm_* normalizer producing normalized text."""

    def normalize(self, binary: bytes, filename: str) -> str | None:  # noqa: ARG002
        return "DOCUMENT_SUMMARY: a fine deck\n\nbody text"


class _FailingNormalizer:
    def normalize(self, binary: bytes, filename: str) -> str | None:  # noqa: ARG002
        raise RuntimeError("claude exploded")


_TARGET = IngestionTargetConfig(
    name="d_quals_sync",
    enabled=True,
    database_id="app1",
    database_name="db",
    table_id="tbl1",
    table_name="(D.Quals)",
    identifier_column="Project Number",
    attachment_columns=("Deliverable Attachments",),
)

_RECORD = {
    "id": "recXYZ",
    "fields": {
        "Project Number": "P-42",
        "Deliverable Attachments": [{"id": "attA", "filename": "deck.pdf"}],
    },
}


def _pipeline(normalizer, uploader=None) -> AirtableAttachmentIngestionPipeline:
    return AirtableAttachmentIngestionPipeline(
        airtable=_FakeAirtable(),
        uploader=uploader or _FakeUploader(),
        metadata_local_dir="/tmp",
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        normalizer=normalizer,
    )


def test_returns_written_keys_for_new_document() -> None:
    keys = _pipeline(_TextNormalizer()).ingest_one_record(target=_TARGET, record=_RECORD)
    # normalized text + record summary (single usable file summary reuses it)
    assert any(k.endswith("attA__normalized.txt") for k in keys)
    assert any(k.endswith("__record_summary.txt") for k in keys)


def test_returns_empty_list_when_already_processed() -> None:
    slug_dir = (
        f"raw/{slugify_table_name(_TARGET.table_name)}"
        f"/{normalize_identifier('P-42')}"
        f"/{slugify_column_name('Deliverable Attachments')}/attA"
    )
    uploader = _FakeUploader(
        present={f"{slug_dir}/deck.pdf", f"{slug_dir}/attA__normalized.txt"}
    )
    keys = _pipeline(_TextNormalizer(), uploader).ingest_one_record(
        target=_TARGET, record=_RECORD
    )
    assert keys == []


def test_raises_runtime_error_when_record_reports_errors() -> None:
    with pytest.raises(RuntimeError, match="ingest_one_record failed for record=recXYZ"):
        _pipeline(_FailingNormalizer()).ingest_one_record(target=_TARGET, record=_RECORD)
