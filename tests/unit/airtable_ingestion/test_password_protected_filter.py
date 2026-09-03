"""Tests for the password-protected attachment gate.

Provably encrypted files must be skipped AFTER download but BEFORE the
original S3 upload and extraction, counted in
attachments_skipped_password_protected, and leave no trace in the bucket
(the pre-written metadata sidecar is deleted). Detection is positive-only:
corrupt or ambiguous bytes must fall through to normal processing.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from pipeline.airtable_ingestion.encryption_check import is_password_protected
from pipeline.airtable_ingestion.models import AttachmentSpec, IngestionTargetConfig
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# CFB directory entry name: UTF-16LE + null terminator/padding.
_ENC_INFO_STREAM = "EncryptionInfo".encode("utf-16-le") + b"\x00" * 8
_ENCRYPTED_OFFICE = _CFB_MAGIC + b"\x00" * 120 + _ENC_INFO_STREAM + b"\x00" * 64


# ---------------------------------------------------------------------------
# Detection unit tests (dependency-free paths)
# ---------------------------------------------------------------------------


def test_encrypted_office_cfb_detected() -> None:
    for name in ("cv.docx", "deck.pptx", "data.xlsx", "macro.xlsm"):
        assert is_password_protected(_ENCRYPTED_OFFICE, name) is True


def test_plain_ooxml_zip_not_detected() -> None:
    assert is_password_protected(b"PK\x03\x04" + b"\x00" * 64, "cv.docx") is False


def test_legacy_doc_without_encryption_streams_not_detected() -> None:
    # Legacy .doc/.ppt are natively CFB; a bare container is NOT protected.
    assert is_password_protected(_CFB_MAGIC + b"\x00" * 512, "old.doc") is False


def test_prose_mention_of_stream_name_not_detected() -> None:
    # A legacy .doc whose *text* contains "EncryptionInfo " (UTF-16 content,
    # followed by a space, not null padding) must not false-positive.
    prose = "The EncryptionInfo stream is documented here.".encode("utf-16-le")
    assert is_password_protected(_CFB_MAGIC + b"\x00" * 64 + prose, "notes.doc") is False


def test_garbage_bytes_never_detected() -> None:
    for name in ("cv.pdf", "cv.docx", "img.png", "x.xlsx"):
        assert is_password_protected(b"fake", name) is False


def test_images_never_checked() -> None:
    assert is_password_protected(_ENCRYPTED_OFFICE, "photo.png") is False


# ---------------------------------------------------------------------------
# Real encrypted PDFs (needs pypdf — present in the Docker image's parsers
# extras; skipped in environments without it)
# ---------------------------------------------------------------------------


def _pdf_bytes(*, user_password: str | None, owner_password: str | None) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if user_password is not None or owner_password is not None:
        writer.encrypt(user_password=user_password or "", owner_password=owner_password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_user_password_pdf_detected() -> None:
    binary = _pdf_bytes(user_password="secret123", owner_password="secret123")
    assert is_password_protected(binary, "cv.pdf") is True


def test_owner_only_pdf_allowed_through() -> None:
    # Opens without a prompt (empty user password) → machine-readable.
    binary = _pdf_bytes(user_password="", owner_password="ownerpw")
    assert is_password_protected(binary, "report.pdf") is False


def test_unencrypted_pdf_not_detected() -> None:
    binary = _pdf_bytes(user_password=None, owner_password=None)
    assert is_password_protected(binary, "plain.pdf") is False


# ---------------------------------------------------------------------------
# Pipeline integration: gate fires inside _process_record
# ---------------------------------------------------------------------------


class _FakeUploader:
    """Records every write and delete; nothing pre-exists in the bucket."""

    def __init__(self) -> None:
        self.uploaded_files: list[str] = []
        self.uploaded_bytes: list[str] = []
        self.uploaded_json: list[str] = []
        self.deleted: list[str] = []

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

    def delete_object(self, *, key: str) -> None:
        self.deleted.append(key)


class _FakeAirtable:
    """Serves an encrypted binary for files named locked.*, plain otherwise."""

    def __init__(self) -> None:
        self.downloads: list[str] = []

    @staticmethod
    def extract_attachments(record: dict, field_name: str) -> list[AttachmentSpec]:
        return [
            AttachmentSpec(id=a["id"], filename=a["filename"], url="http://x")
            for a in (record.get("fields", {}).get(field_name) or [])
        ]

    def download_attachment(self, attachment: AttachmentSpec, *, path: str) -> None:
        self.downloads.append(attachment.filename)
        data = _ENCRYPTED_OFFICE if attachment.filename.startswith("locked") else b"binary"
        Path(path).write_bytes(data)


def _run(record: dict):
    target = IngestionTargetConfig(
        name="t",
        enabled=True,
        database_id="app1",
        database_name="db",
        table_id="tbl1",
        table_name="T",
        identifier_column="Name",
        attachment_columns=("Files",),
    )
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


def test_locked_file_skipped_before_s3_upload() -> None:
    report, keys, uploader, airtable = _run(_record("locked.docx", "cv.docx"))
    assert report.attachments_skipped_password_protected == 1
    # Download necessarily happened (detection needs the bytes)...
    assert airtable.downloads == ["locked.docx", "cv.docx"]
    # ...but the locked original never reached S3 and produced no keys.
    assert not any("locked.docx" in k for k in uploader.uploaded_files)
    assert not any("locked" in k for k in keys)
    # Its pre-written metadata sidecar was cleaned up again.
    assert any("att0" in k and ".airtable_meta.json" in k for k in uploader.deleted)
    # The plain sibling was processed normally.
    assert any(k.endswith("cv.docx") for k in uploader.uploaded_files)
    assert report.errors == []


def test_clean_files_unaffected_by_gate() -> None:
    report, keys, uploader, _ = _run(_record("cv.docx"))
    assert report.attachments_skipped_password_protected == 0
    assert uploader.deleted == []
    assert any(k.endswith("cv.docx") for k in keys)
