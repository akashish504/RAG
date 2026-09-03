"""Record-level summary: per-file summary extraction, the single/multi-file
decision in ``_write_record_summary``, and multi-file no-collision in run_target.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pipeline.airtable_ingestion.models import IngestionRunReport, IngestionTargetConfig
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline
from pipeline.airtable_ingestion.record_summary import extract_file_summary


# --------------------------------------------------------------------------
# extract_file_summary
# --------------------------------------------------------------------------

def test_extract_uses_document_summary_block() -> None:
    text = (
        "---\nName: X\n---\n\n"
        "DOCUMENT_SUMMARY: A deck about health access in Kenya.\n\n"
        "## Slide 1: Intro\n[Page 1]"
    )
    assert extract_file_summary(text) == "A deck about health access in Kenya."


def test_extract_falls_back_to_body_head_without_summary() -> None:
    text = "---\nName: X\n---\n\nSome plain extracted text with no summary line."
    out = extract_file_summary(text)
    assert "Some plain extracted text" in out
    assert "Name: X" not in out  # front-matter stripped


def test_extract_empty() -> None:
    assert extract_file_summary("") == ""


def test_fallback_head_is_richer_than_600() -> None:
    # Non-deck files (no DOCUMENT_SUMMARY) now contribute a larger, cleaned head
    # so the record summary represents them properly.
    body = "Sentence number %d. " % 0 + " ".join(f"word{i}" for i in range(1000))
    text = "---\nName: X\n---\n\n" + body
    out = extract_file_summary(text)
    assert len(out) > 600  # used to truncate at 600
    assert "Name: X" not in out  # front-matter stripped
    assert "  " not in out  # whitespace collapsed


# --------------------------------------------------------------------------
# _write_record_summary decision logic
# --------------------------------------------------------------------------

class _CapturingUploader:
    def __init__(self) -> None:
        self.bytes_uploads: dict[str, bytes] = {}
        self.json_uploads: dict[str, object] = {}

    def upload_bytes(self, *, data: bytes, key: str, content_type: str | None = None) -> None:
        self.bytes_uploads[key] = data

    def upload_json(self, *, payload, key: str) -> None:
        self.json_uploads[key] = payload


class _FakeSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, sections):
        self.calls += 1
        return "COMBINED SUMMARY"


def _pipeline(uploader, summarizer=None) -> AirtableAttachmentIngestionPipeline:
    return AirtableAttachmentIngestionPipeline(
        airtable=object(),
        uploader=uploader,
        metadata_local_dir="/tmp",
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        record_summarizer=summarizer,
    )


_TARGET = IngestionTargetConfig(
    name="d_quals_sync",
    enabled=True,
    database_id="appX",
    database_name="db",
    table_id="tblX",
    table_name="(D.Quals)",
    identifier_column="Project Number",
    attachment_columns=("Deliverable Attachments",),
)


def _write(uploader, summarizer, file_summaries):
    pipe = _pipeline(uploader, summarizer)
    report = IngestionRunReport(target_name="t", started_at=datetime.now(timezone.utc))
    pipe._write_record_summary(
        record_dir="raw/d_quals/p1",
        target=_TARGET,
        record={"id": "rec1", "fields": {}},
        raw_identifier="P1",
        fields={"Name": "Proj"},
        file_summaries=file_summaries,
        report=report,
    )
    return report


def test_no_files_writes_nothing() -> None:
    up = _CapturingUploader()
    sm = _FakeSummarizer()
    report = _write(up, sm, [])
    assert up.bytes_uploads == {}
    assert sm.calls == 0
    assert report.record_summaries_done == 0


def test_single_file_reuses_summary_no_call() -> None:
    up = _CapturingUploader()
    sm = _FakeSummarizer()
    report = _write(up, sm, [("col/a.pptx", "Lone deck summary.")])
    body = up.bytes_uploads["raw/d_quals/p1/__record_summary.txt"].decode()
    assert "DOCUMENT_SUMMARY: Lone deck summary." in body
    assert sm.calls == 0, "single-file must not spend a Claude call"
    assert report.record_summaries_done == 1
    # sidecar tags the record-level parent role
    sidecar = up.json_uploads["raw/d_quals/p1/.airtable_meta.json"]
    assert sidecar["doc_role"] == "record_summary"
    assert "original_s3_key" not in sidecar


def test_multi_file_calls_summarizer() -> None:
    up = _CapturingUploader()
    sm = _FakeSummarizer()
    report = _write(
        up, sm,
        [("col/a.pptx", "Deck A summary."), ("col/b.docx", "Doc B summary.")],
    )
    body = up.bytes_uploads["raw/d_quals/p1/__record_summary.txt"].decode()
    assert "DOCUMENT_SUMMARY: COMBINED SUMMARY" in body
    assert "- col/a.pptx" in body and "- col/b.docx" in body  # manifest
    assert sm.calls == 1
    assert report.record_summaries_done == 1


def test_multi_file_without_summarizer_concatenates() -> None:
    up = _CapturingUploader()
    report = _write(up, None, [("a", "Sum A."), ("b", "Sum B.")])
    body = up.bytes_uploads["raw/d_quals/p1/__record_summary.txt"].decode()
    assert "Sum A." in body and "Sum B." in body
    assert report.record_summaries_done == 1
