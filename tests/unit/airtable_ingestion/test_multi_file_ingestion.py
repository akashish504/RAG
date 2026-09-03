"""run_target end-to-end (with fakes): two attachments in ONE column must not
collide, each keeps its own per-file sidecar, and one record summary is written.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.airtable_ingestion.models import AttachmentSpec, IngestionTargetConfig
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline


class _FakeAirtable:
    def __init__(self, records, attachments_by_column) -> None:
        self._records = records
        self._by_col = attachments_by_column

    def get_tables_with_columns(self, *, base_id):  # noqa: ARG002
        return {"(D.Quals)": {"fields": []}}

    def iter_records(self, *, base_id, table_name, fields, page_size):  # noqa: ARG002
        yield from self._records

    def extract_attachments(self, record, column):  # noqa: ARG002
        return list(self._by_col.get(column, []))

    def download_attachment(self, attachment, *, path):
        Path(path).write_bytes(f"binary-of-{attachment.id}".encode())


class _FakeUploader:
    def __init__(self) -> None:
        self.present: set[str] = set()
        self.bytes_uploads: dict[str, bytes] = {}
        self.json_uploads: dict[str, dict] = {}

    def key_exists(self, *, key: str) -> bool:
        return key in self.present

    def get_etag(self, *, key: str):  # noqa: ARG002
        return None

    def get_text(self, *, key: str):
        data = self.bytes_uploads.get(key)
        return data.decode() if data is not None else None

    def get_bytes(self, *, key: str):
        return self.bytes_uploads.get(key)

    def upload_file(self, *, local_path, key, content_type=None) -> None:  # noqa: ARG002
        self.bytes_uploads[key] = Path(local_path).read_bytes()
        self.present.add(key)

    def upload_bytes(self, *, data, key, content_type=None) -> None:  # noqa: ARG002
        self.bytes_uploads[key] = data
        self.present.add(key)

    def upload_json(self, *, payload, key) -> None:
        self.json_uploads[key] = payload
        self.present.add(key)


class _DeckNormalizer:
    """Returns a deck-shaped normalized text whose summary embeds the filename."""

    def normalize(self, binary: bytes, filename: str) -> str | None:  # noqa: ARG002
        return f"DOCUMENT_SUMMARY: Summary of {filename}.\n\n## Slide 1: X\n[Page 1]"


class _FakeSummarizer:
    def __init__(self) -> None:
        self.calls = 0
        self.last_sections = None

    def summarize(self, sections):
        self.calls += 1
        self.last_sections = sections
        return "RECORD-LEVEL SUMMARY"


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


def test_two_files_one_column_no_collision_and_record_summary(tmp_path) -> None:
    records = [{"id": "rec1", "fields": {"Project Number": "P-1", "Name": "Proj"}}]
    atts = [
        AttachmentSpec(id="att1", filename="deck1.pptx", url="u1", size=1024),
        AttachmentSpec(id="att2", filename="deck2.pptx", url="u2", size=2048),
    ]
    uploader = _FakeUploader()
    summarizer = _FakeSummarizer()
    pipe = AirtableAttachmentIngestionPipeline(
        airtable=_FakeAirtable(records, {"Deliverable Attachments": atts}),
        uploader=uploader,
        metadata_local_dir=str(tmp_path),
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        column_normalizers={"Deliverable Attachments": _DeckNormalizer()},
        record_summarizer=summarizer,
    )

    report = pipe.run_target(target=_TARGET)

    # Two DISTINCT normalized artifacts (no overwrite).
    norm_keys = [k for k in uploader.bytes_uploads if k.endswith("__normalized.txt")]
    assert sorted(norm_keys) == [
        "raw/d.quals/p-1/deliverable_attachments/att1/att1__normalized.txt",
        "raw/d.quals/p-1/deliverable_attachments/att2/att2__normalized.txt",
    ]

    # Per-file sidecars carry DISTINCT original_s3_key (citation preserved).
    s1 = uploader.json_uploads["raw/d.quals/p-1/deliverable_attachments/att1/.airtable_meta.json"]
    s2 = uploader.json_uploads["raw/d.quals/p-1/deliverable_attachments/att2/.airtable_meta.json"]
    assert s1["original_s3_key"].endswith("att1/deck1.pptx")
    assert s2["original_s3_key"].endswith("att2/deck2.pptx")

    # Exactly one record-level summary, built from BOTH files, tagged as parent.
    assert report.record_summaries_done == 1
    assert summarizer.calls == 1
    assert len(summarizer.last_sections) == 2
    rec_body = uploader.bytes_uploads["raw/d.quals/p-1/__record_summary.txt"].decode()
    assert "DOCUMENT_SUMMARY: RECORD-LEVEL SUMMARY" in rec_body
    rec_meta = uploader.json_uploads["raw/d.quals/p-1/.airtable_meta.json"]
    assert rec_meta["doc_role"] == "record_summary"
    assert report.normalizations_done == 2


def test_parallel_workers_match_serial(tmp_path) -> None:
    # Running with multiple workers must produce the same aggregated report as
    # serial — records are independent and each thread owns its own report.
    records = [
        {"id": f"rec{i}", "fields": {"Project Number": f"P-{i}", "Name": f"Proj {i}"}}
        for i in range(6)
    ]
    atts = [AttachmentSpec(id=f"att{i}", filename=f"deck{i}.pptx", url="u", size=1024)
            for i in range(6)]

    def _make(workers):
        up = _FakeUploader()
        pipe = AirtableAttachmentIngestionPipeline(
            airtable=_FakeAirtable(
                records, {"Deliverable Attachments": []}
            ),
            uploader=up,
            metadata_local_dir=str(tmp_path),
            metadata_s3_prefix="meta",
            upload_schema_to_s3_enabled=False,
            page_size=100,
            workers=workers,
        )
        return pipe, up

    # Give each record exactly its own attachment by faking extract_attachments per record.
    def _attachments(record, column):  # noqa: ARG001
        i = int(record["id"].removeprefix("rec"))
        return [atts[i]]

    serial_pipe, _ = _make(1)
    serial_pipe.airtable.extract_attachments = _attachments
    serial = serial_pipe.run_target(target=_TARGET)

    par_pipe, _ = _make(4)
    par_pipe.airtable.extract_attachments = _attachments
    par = par_pipe.run_target(target=_TARGET)

    assert par.records_seen == serial.records_seen == 6
    assert par.attachments_uploaded == serial.attachments_uploaded == 6
    assert par.normalizations_done == serial.normalizations_done
    assert par.errors == serial.errors == []


def test_existing_s3_original_skips_airtable_download(tmp_path) -> None:
    # Originals already in S3 (e.g. from a prior --batch run) must be reused, not
    # re-downloaded from Airtable.
    records = [{"id": "rec1", "fields": {"Project Number": "P-1", "Name": "Proj"}}]
    atts = [AttachmentSpec(id="att1", filename="deck1.pptx", url="u1", size=1024)]
    uploader = _FakeUploader()
    # Pre-seed the original in S3.
    original_key = "raw/d.quals/p-1/deliverable_attachments/att1/deck1.pptx"
    uploader.bytes_uploads[original_key] = b"existing-bytes"
    uploader.present.add(original_key)

    airtable = _FakeAirtable(records, {"Deliverable Attachments": atts})
    downloads: list = []
    orig_download = airtable.download_attachment
    def _track(attachment, *, path):  # noqa: ANN001
        downloads.append(attachment.id)
        orig_download(attachment, path=path)
    airtable.download_attachment = _track

    pipe = AirtableAttachmentIngestionPipeline(
        airtable=airtable,
        uploader=uploader,
        metadata_local_dir=str(tmp_path),
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        column_normalizers={"Deliverable Attachments": _DeckNormalizer()},
    )
    report = pipe.run_target(target=_TARGET)

    assert downloads == []  # never re-downloaded from Airtable
    assert report.attachments_downloaded == 0
    assert report.normalizations_done == 1  # still normalized from the S3 bytes
    assert original_key in uploader.bytes_uploads  # original untouched


class _NoneNormalizer:
    """Normalizer that never produces normalized text (e.g. unsupported type)."""

    def normalize(self, binary, filename):  # noqa: ARG002
        return None


def test_unextracted_files_are_reported(tmp_path) -> None:
    # An ALLOWED format (passes the allowlist gate) whose normalizer still
    # produces no text — e.g. a corrupt .docx. Disallowed formats like .zip
    # never get this far: they're skipped pre-download (see
    # test_format_filter.py) and are counted separately.
    records = [{"id": "rec1", "fields": {"Project Number": "P-1", "Name": "Proj"}}]
    atts = [AttachmentSpec(id="att1", filename="weird.docx", url="u1", size=1024)]
    uploader = _FakeUploader()
    pipe = AirtableAttachmentIngestionPipeline(
        airtable=_FakeAirtable(records, {"Deliverable Attachments": atts}),
        uploader=uploader,
        metadata_local_dir=str(tmp_path),
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        column_normalizers={"Deliverable Attachments": _NoneNormalizer()},
    )
    report = pipe.run_target(target=_TARGET)

    assert report.normalizations_done == 0
    assert report.attachments_skipped_disallowed == 0
    assert len(report.attachments_unextracted) == 1
    assert "weird.docx" in report.attachments_unextracted[0]
    assert ".docx" in report.attachments_unextracted[0]


class _ExplodingNormalizer:
    """Must never be called in batch mode."""

    def normalize(self, binary, filename):  # noqa: ARG002
        raise AssertionError("normalizer called in batch mode")


def test_batch_mode_uploads_originals_only(tmp_path) -> None:
    records = [{"id": "rec1", "fields": {"Project Number": "P-1", "Name": "Proj"}}]
    atts = [AttachmentSpec(id="att1", filename="deck1.pptx", url="u1", size=1024)]
    uploader = _FakeUploader()
    summarizer = _FakeSummarizer()
    pipe = AirtableAttachmentIngestionPipeline(
        airtable=_FakeAirtable(records, {"Deliverable Attachments": atts}),
        uploader=uploader,
        metadata_local_dir=str(tmp_path),
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
        column_normalizers={"Deliverable Attachments": _ExplodingNormalizer()},
        record_summarizer=summarizer,
        batch_mode=True,
    )

    report = pipe.run_target(target=_TARGET)

    # Original uploaded, but NO normalized.txt and NO record summary written.
    assert "raw/d.quals/p-1/deliverable_attachments/att1/deck1.pptx" in uploader.bytes_uploads
    assert not any(k.endswith("__normalized.txt") for k in uploader.bytes_uploads)
    assert not any(k.endswith("__record_summary.txt") for k in uploader.bytes_uploads)
    assert report.normalizations_done == 0
    assert report.record_summaries_done == 0
    assert summarizer.calls == 0
    # Sidecar carries the metadata header for the offline extractor.
    sidecar = uploader.json_uploads["raw/d.quals/p-1/deliverable_attachments/att1/.airtable_meta.json"]
    assert "Name: Proj" in sidecar["metadata_header"]
