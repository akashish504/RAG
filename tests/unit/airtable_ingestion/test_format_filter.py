"""Tests for the positive format allowlist in the ingestion pipeline.

Disallowed extensions must be rejected BEFORE the Airtable download and any
S3 upload (sidecar included), counted in attachments_skipped_disallowed, and
the allowlist must be overridable per target via allowed_extensions.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.airtable_ingestion.config import load_airtable_ingestion_settings
from pipeline.airtable_ingestion.models import AttachmentSpec, IngestionTargetConfig
from pipeline.airtable_ingestion.pipeline import (
    DEFAULT_ALLOWED_EXTENSIONS,
    AirtableAttachmentIngestionPipeline,
)


class _FakeUploader:
    """Records every write; nothing pre-exists in the bucket."""

    def __init__(self) -> None:
        self.uploaded_files: list[str] = []
        self.uploaded_bytes: list[str] = []
        self.uploaded_json: list[str] = []

    def key_exists(self, *, key: str) -> bool:  # noqa: ARG002
        return False

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
        self.uploaded_json.append(key)


class _FakeAirtable:
    """Serves attachments from the record; download must never run for
    disallowed formats."""

    def __init__(self) -> None:
        self.downloads: list[str] = []

    @staticmethod
    def extract_attachments(record: dict, field_name: str) -> list[AttachmentSpec]:
        return [
            AttachmentSpec(id=a["id"], filename=a["filename"], url="http://x")
            for a in (record.get("fields", {}).get(field_name) or [])
        ]

    def download_attachment(self, attachment: AttachmentSpec, *, path: str) -> None:  # noqa: ARG002
        self.downloads.append(attachment.filename)
        Path(path).write_bytes(b"binary")


def _target(**overrides) -> IngestionTargetConfig:
    kwargs = dict(
        name="t",
        enabled=True,
        database_id="app1",
        database_name="db",
        table_id="tbl1",
        table_name="T",
        identifier_column="Name",
        attachment_columns=("Files",),
        process_images=True,  # isolate the allowlist gate from the image gate
    )
    kwargs.update(overrides)
    return IngestionTargetConfig(**kwargs)


def _record(*filenames: str) -> dict:
    return {
        "id": "rec1",
        "fields": {
            "Name": "P-001",
            "Files": [
                {"id": f"att{i}", "filename": name} for i, name in enumerate(filenames)
            ],
        },
    }


def _run(target: IngestionTargetConfig, record: dict):
    uploader = _FakeUploader()
    airtable = _FakeAirtable()
    pipe = AirtableAttachmentIngestionPipeline(
        airtable=airtable,
        uploader=uploader,
        metadata_local_dir="/tmp",
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
    )
    report, keys = pipe._process_record(
        record=record, target=target, table_slug="t", attachment_columns=["Files"], index=1
    )
    return report, keys, uploader, airtable


def test_disallowed_extensions_skipped_before_download() -> None:
    report, _, uploader, airtable = _run(
        _target(), _record("virus.exe", "notes.zip", "README")
    )
    assert report.attachments_skipped_disallowed == 3
    assert airtable.downloads == [], "disallowed file must never be downloaded"
    assert uploader.uploaded_files == [], "disallowed file must never reach S3"
    assert uploader.uploaded_json == [], "not even a metadata sidecar may be written"


def test_allowed_extension_processed_normally() -> None:
    report, keys, uploader, airtable = _run(_target(), _record("deck.pptx", "junk.tmp"))
    assert report.attachments_skipped_disallowed == 1  # junk.tmp
    assert airtable.downloads == ["deck.pptx"]
    assert any("deck.pptx" in k for k in uploader.uploaded_files)
    # Passthrough default normalizer → no normalized text → original key returned
    assert any(k.endswith("deck.pptx") for k in keys)


def test_per_target_override_restricts_default_set() -> None:
    report, _, _, airtable = _run(
        _target(allowed_extensions=(".pdf",)), _record("report.pdf", "deck.pptx")
    )
    assert report.attachments_skipped_disallowed == 1  # .pptx not in the override
    assert airtable.downloads == ["report.pdf"]


def test_case_insensitive_via_lowercased_suffix() -> None:
    report, _, _, airtable = _run(_target(), _record("REPORT.PDF"))
    assert report.attachments_skipped_disallowed == 0
    assert airtable.downloads == ["REPORT.PDF"]


def test_default_set_matches_extractable_formats() -> None:
    assert DEFAULT_ALLOWED_EXTENSIONS == frozenset(
        {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xlsx", ".xlsm",
         ".png", ".jpg", ".jpeg", ".webp", ".gif"}
    )


def test_config_normalizes_allowed_extensions(tmp_path, monkeypatch) -> None:
    """Loader lowercases and dot-prefixes allowed_extensions entries."""
    config = tmp_path / "ingestion.yaml"
    config.write_text(
        """
defaults: {}
targets:
  t1:
    enabled: true
    database_id: app1
    table_id: tbl1
    table_name: T1
    identifier_column: Name
    attachment_columns: [Files]
    allowed_extensions: ["PDF", ".PpTx", "docx"]
"""
    )
    monkeypatch.setenv("AIRTABLE_PAT_TOKEN", "test_pat")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    settings = load_airtable_ingestion_settings(config_path=config)
    assert settings.target("t1").allowed_extensions == (".pdf", ".pptx", ".docx")


def test_config_defaults_to_empty_allowlist(monkeypatch) -> None:
    """Real repo config: no target sets an allowlist (→ default applies); every
    real table is poll-enabled, the proposal-library stub is not."""
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("AIRTABLE_PAT_TOKEN", "test_pat")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    settings = load_airtable_ingestion_settings(
        config_path=root / "config" / "airtable_ingestion.yaml"
    )
    d_quals = settings.target("d_quals_sync")
    assert d_quals.allowed_extensions == ()
    assert d_quals.poll_enabled is True
    assert settings.target("profiles_sync").poll_enabled is True
    assert settings.target("knowledge_library_sync").poll_enabled is True
    # Disabled stub (no table_id / attachment columns): polling it would
    # error every cycle — must stay off until it is actually configured.
    assert settings.target("proposal_library_sync").poll_enabled is False
