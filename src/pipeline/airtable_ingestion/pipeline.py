"""Airtable attachment ingestion pipeline."""

from __future__ import annotations

import hashlib
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.encryption_check import is_password_protected
from pipeline.airtable_ingestion.models import IngestionRunReport, IngestionTargetConfig
from pipeline.airtable_ingestion.normalizers import (
    mask_identifier,
    normalize_facet_value,
    normalize_identifier,
    sanitize_attachment_filename,
    slugify_column_name,
    slugify_table_name,
)
from pipeline.airtable_ingestion.record_summary import (
    RecordSummarizer,
    extract_file_summary,
)
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.airtable_ingestion.schema import save_schema_local, upload_schema_to_s3
from pipeline.preprocessing.normalizers.base import AttachmentNormalizer
from pipeline.preprocessing.normalizers.passthrough import PassthroughNormalizer

# File extensions that cannot produce embeddable text — skip during ingestion
# so we don't waste bandwidth downloading images that will never be indexed.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".heic", ".svg"}
)

# Positive allowlist of attachment extensions the pipeline can actually turn
# into text today (llm_content/slides_deck dispatch sets + openpyxl for
# spreadsheets + Claude vision for images). Anything else is skipped BEFORE
# download / S3 upload so disallowed formats never reach the bucket. Override
# per target via `allowed_extensions` in config/airtable_ingestion.yaml.
DEFAULT_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".ppt",
        ".pptx",
        ".doc",
        ".docx",
        ".xlsx",
        ".xlsm",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
    }
)

# Metadata fields injected as a header block into every normalized.txt.
# Co-embedding metadata with content lets semantic search match on categories,
# geography, authors, etc. without separate thin embeddings.
_METADATA_HEADER_FIELDS = [
    "Name",
    "Practice Area",
    "KD Type",
    "Country/Region",
    "Author",
    "Date of Publication",
    "Item Type",
    "Insight Type",
    "Language",
    "Client",
    "Description",
]


def _build_metadata_header(
    fields: dict[str, Any], *, extra_fields: tuple[str, ...] = ()
) -> str:
    """Build a YAML-style metadata block from Airtable record fields.

    ``extra_fields`` are target-specific columns (e.g. "Project Description
    (1-paragraph)") appended after the standard set, de-duplicated and order-
    preserving. Multi-select values (lists) are joined with ", ".
    Returns an empty string if no relevant fields are populated.
    """
    header_fields = list(_METADATA_HEADER_FIELDS)
    for col in extra_fields:
        if col not in header_fields:
            header_fields.append(col)

    lines: list[str] = []
    for field in header_fields:
        value = fields.get(field)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            text = ", ".join(str(v) for v in value if v)
        else:
            text = str(value).strip()
        if text:
            lines.append(f"{field}: {text}")
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n\n"


# Integer counters on IngestionRunReport that sum when merging per-record reports
# (record-level parallelism builds one report per record and merges them).
_REPORT_INT_FIELDS = (
    "records_seen",
    "records_skipped_no_identifier",
    "attachment_fields_seen",
    "attachments_downloaded",
    "attachments_uploaded",
    "attachments_skipped",
    "attachments_skipped_disallowed",
    "attachments_skipped_password_protected",
    "normalizations_done",
    "record_summaries_done",
    "text_fields_uploaded",
    "text_fields_skipped",
)


def _build_facets(
    fields: dict[str, Any], facet_columns: tuple[str, ...]
) -> dict[str, Any]:
    """Structured, filterable facets for the index: ``{field_slug: value}``.

    Multi-selects stay lists (keyword arrays); scalars become strings; dates pass
    through. Empty values are dropped. Keyed by ``slugify_column_name`` so the
    OpenSearch field is e.g. ``client_organisation`` / ``practice_area``.
    """
    out: dict[str, Any] = {}
    for col in facet_columns:
        value = fields.get(col)
        if value is None or value == "" or value == []:
            continue
        key = slugify_column_name(col)
        if isinstance(value, list):
            # Multi-selects arrive as native lists — keep them as keyword arrays.
            # Do NOT split on comma: some single values contain commas (e.g.
            # "Spain,Latin America") and would over-split.
            cleaned = [
                normalize_facet_value(str(v)) for v in value if str(v).strip()
            ]
            if cleaned:
                out[key] = cleaned
        else:
            out[key] = normalize_facet_value(str(value))
    return out


def _merge_report(into: IngestionRunReport, other: IngestionRunReport) -> None:
    """Fold one record's report into the run total (sum counters, gather errors)."""
    for field_name in _REPORT_INT_FIELDS:
        setattr(into, field_name, getattr(into, field_name) + getattr(other, field_name))
    into.errors.extend(other.errors)
    into.attachments_unextracted.extend(other.attachments_unextracted)


class AirtableAttachmentIngestionPipeline:
    """Sync configured Airtable fields to S3 raw storage."""

    def __init__(
        self,
        *,
        airtable: AirtableClient,
        uploader: S3Uploader,
        metadata_local_dir: str,
        metadata_s3_prefix: str,
        upload_schema_to_s3_enabled: bool,
        page_size: int,
        normalizer: AttachmentNormalizer | None = None,
        column_normalizers: dict[str, AttachmentNormalizer] | None = None,
        record_summarizer: RecordSummarizer | None = None,
        batch_mode: bool = False,
        workers: int = 1,
    ) -> None:
        self.airtable = airtable
        self.uploader = uploader
        self.metadata_local_dir = metadata_local_dir
        self.metadata_s3_prefix = metadata_s3_prefix
        self.upload_schema_to_s3_enabled = upload_schema_to_s3_enabled
        self.page_size = page_size
        # Default normalizer (fallback for columns not in column_normalizers)
        self._normalizer: AttachmentNormalizer = normalizer or PassthroughNormalizer()
        # Per-column normalizer overrides; keyed by Airtable column name
        self._column_normalizers: dict[str, AttachmentNormalizer] = column_normalizers or {}
        # Optional record-level summarizer (synthesises one parent summary per
        # record from its per-file summaries). When None, no record summary is
        # written for multi-file records (single-file still reuses its summary).
        self._record_summarizer: RecordSummarizer | None = record_summarizer
        # Batch mode: upload originals + sidecars (incl. the metadata header) ONLY;
        # the offline batch-extraction job (slides/batch.py) owns normalization and
        # the record summary. Skips every inline Claude call during ingestion.
        self._batch_mode = batch_mode
        # Number of records processed concurrently. Records are independent, and the
        # slow steps (LibreOffice render = subprocess, Claude calls = network I/O)
        # both release the GIL, so threads give real speedup. 1 = serial.
        self._workers = max(1, workers)

    def _already_processed(
        self,
        *,
        col_normalizer: AttachmentNormalizer,
        original_key: str,
        normalized_key: str,
    ) -> tuple[bool, str]:
        """Decide whether an attachment is fully processed and can be skipped.

        The original binary is uploaded BEFORE normalization, so its presence
        alone does NOT mean the (expensive) normalized text was produced — a
        prior run could have uploaded the original and then failed during
        normalization. Using the original as the skip marker would permanently
        skip those partial failures.

        Therefore:
          • normalizing columns (llm_*): "done" only when the __normalized.txt
            artifact exists → partial failures get re-processed on the next run.
          • passthrough columns: no normalized text is ever produced, so the
            original's presence is the completion marker.

        Returns ``(already_done, marker_label)``.
        """
        produces_normalized = not isinstance(col_normalizer, PassthroughNormalizer)
        if produces_normalized:
            return self.uploader.key_exists(key=normalized_key), "normalized text"
        return self.uploader.key_exists(key=original_key), "original in S3"

    def discover_schema(self, *, target: IngestionTargetConfig) -> dict[str, Any]:
        """Fetch and persist metadata for one table target."""

        all_tables = self.airtable.get_tables_with_columns(base_id=target.database_id)
        if target.table_name not in all_tables:
            raise KeyError(
                f"Table {target.table_name!r} not found in base {target.database_id}. "
                f"Found: {sorted(all_tables)}"
            )

        schema_payload = {
            "database_id": target.database_id,
            "database_name": target.database_name,
            "table_id": target.table_id,
            "table_name": target.table_name,
            "schema": all_tables[target.table_name],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_schema_local(
            output_dir=self.metadata_local_dir,
            database_id=target.database_id,
            table_name=target.table_name,
            schema_payload=schema_payload,
        )
        if self.upload_schema_to_s3_enabled:
            upload_schema_to_s3(
                uploader=self.uploader,
                metadata_prefix=self.metadata_s3_prefix,
                database_id=target.database_id,
                table_name=target.table_name,
                schema_payload=schema_payload,
            )
        return schema_payload

    def _upload_airtable_meta_sidecar(
        self,
        *,
        key_dir: str,
        target: IngestionTargetConfig,
        record: dict[str, Any],
        column: str,
        identifier: str,
        original_s3_key: str | None = None,
        doc_role: str | None = None,
        metadata_header: str | None = None,
        facets: dict[str, Any] | None = None,
    ) -> None:
        """Write ``.airtable_meta.json`` beside attachment objects for citation indexing.

        Carries the ORIGINAL document key (``original_s3_key``) so the embedding
        pipeline can index it and citations point at the source file rather than
        the normalized .txt. ``doc_role`` tags special artifacts (e.g.
        ``"record_summary"`` for the record-level parent) for retrieval.
        """

        record_id = record.get("id")
        if not record_id or not target.table_id:
            return
        payload = {
            "airtable_record_id": record_id,
            "airtable_base_id": target.database_id,
            "airtable_table_id": target.table_id,
            "identifier": identifier,
            "column_name": column,
        }
        if original_s3_key:
            payload["original_s3_key"] = original_s3_key
        if doc_role:
            payload["doc_role"] = doc_role
        # Batch mode persists the metadata header so the offline extractor can
        # prepend it to normalized.txt without re-reading Airtable.
        if metadata_header:
            payload["metadata_header"] = metadata_header
        # Structured facets for filtered-KNN (indexed as keyword/date fields).
        if facets:
            payload["facets"] = facets
        meta_key = f"{key_dir.rstrip('/')}/.airtable_meta.json"
        self.uploader.upload_json(payload=payload, key=meta_key)

    def _write_record_summary(
        self,
        *,
        record_dir: str,
        target: IngestionTargetConfig,
        record: dict[str, Any],
        raw_identifier: str,
        fields: dict[str, Any],
        file_summaries: list[tuple[str, str]],
        report: IngestionRunReport,
    ) -> str | None:
        """Write the record-level ``__record_summary.txt`` parent artifact.

        • 0 usable file summaries → nothing written, returns None.
        • exactly 1 → reuse that file's summary verbatim (no Claude call).
        • 2+ → one Haiku call over the per-file summaries (falls back to
          concatenation when no summarizer is wired or the call fails).

        Returns the S3 key written, or None when nothing was written — lets
        ``ingest_one_record`` know whether to feed this key to the embedding
        pipeline.
        """
        usable = [(label, text.strip()) for label, text in file_summaries if text.strip()]
        if not usable:
            return None

        if len(usable) == 1:
            summary = usable[0][1]
        elif self._record_summarizer is not None:
            summary = self._record_summarizer.summarize(usable) or " ".join(
                text for _, text in usable
            )
        else:
            summary = " ".join(text for _, text in usable)

        header = _build_metadata_header(fields, extra_fields=target.metadata_columns)
        manifest = "\n".join(f"- {label}" for label, _ in usable)
        body = (
            f"DOCUMENT_SUMMARY: {summary}\n\n"
            f"Files in this record:\n{manifest}\n"
        )
        record_summary_key = f"{record_dir.rstrip('/')}/__record_summary.txt"
        self.uploader.upload_bytes(
            data=(header + body).encode("utf-8"),
            key=record_summary_key,
            content_type="text/plain; charset=utf-8",
        )
        # Sidecar marks this as the record-level parent (no single original file).
        self._upload_airtable_meta_sidecar(
            key_dir=record_dir,
            target=target,
            record=record,
            column="",
            identifier=raw_identifier,
            doc_role="record_summary",
            facets=_build_facets(fields, target.facet_columns),
        )
        report.record_summaries_done += 1
        _p(
            f"         → __record_summary.txt written"
            f" ({len(usable)} file{'s' if len(usable) != 1 else ''})"
        )
        return record_summary_key

    def _process_record(
        self,
        *,
        record: dict[str, Any],
        target: IngestionTargetConfig,
        table_slug: str,
        attachment_columns: list[str],
        index: int,
    ) -> tuple[IngestionRunReport, list[str]]:
        """Process ONE record into its own report — the unit of parallelism.

        Each record is independent, so a thread owns its report and the run merges
        them (see ``_merge_report``). Shared collaborators (S3 uploader, Airtable
        client, the normalizers' Anthropic client) are safe for concurrent use.

        Returns ``(report, keys_written)`` — ``keys_written`` lists every S3 key
        newly written or changed THIS call (normalized text, or the original
        when no normalizer produced text, plus the record summary). Attachments
        skipped as already-processed contribute nothing, since nothing about
        them changed. Consumed by ``ingest_one_record`` to know what to feed
        the embedding pipeline; ``run_target`` ignores it.
        """
        report = IngestionRunReport(
            target_name=target.name, started_at=datetime.now(timezone.utc)
        )
        keys_written: list[str] = []
        report.records_seen = 1
        fields = record.get("fields", {}) or {}
        raw_identifier = str(fields.get(target.identifier_column, "")).strip()
        if not raw_identifier:
            report.records_skipped_no_identifier += 1
            _p(f"  [{index:>4}] SKIP  (no identifier in record {record.get('id', '?')})")
            return report, keys_written
        identifier = normalize_identifier(raw_identifier)
        # Log the record id + a masked identifier, not the raw value: the
        # identifier column is PII (Email for profiles_sync) and these lines
        # ship to CloudWatch.
        _p(f"  [{index:>4}] {record.get('id', '?')} ({mask_identifier(identifier)})")

        record_dir = f"{target.s3_prefix.rstrip('/')}/{table_slug}/{identifier}"
        # (label, per-file summary) for every file in this record, across all
        # columns — fed to the record-level summary after the column loop.
        file_summaries: list[tuple[str, str]] = []
        # Effective format allowlist for this target (constant per target, so
        # hoisted above the loops). Empty config tuple → project-wide default.
        allowed_extensions = (
            frozenset(target.allowed_extensions) or DEFAULT_ALLOWED_EXTENSIONS
        )

        for column in attachment_columns:
            report.attachment_fields_seen += 1
            col_slug = slugify_column_name(column)
            # Use per-column normalizer if configured; fall back to the default.
            col_normalizer = self._column_normalizers.get(column, self._normalizer)
            attachments = self.airtable.extract_attachments(record, column)
            if not attachments:
                _p(f"         {column}: (no attachments)")
                continue
            for attachment in attachments:
                file_name = sanitize_attachment_filename(attachment.filename)
                size_kb = f"{attachment.size // 1024} KB" if attachment.size else "? KB"

                # Positive format allowlist — enforced BEFORE the sidecar
                # upload, the Airtable download, and the S3 upload so a
                # disallowed file never leaves a trace in the bucket. Applies
                # to sync, batch, and worker paths alike (they all run here).
                ext = Path(file_name).suffix.lower()
                if ext not in allowed_extensions:
                    report.attachments_skipped_disallowed += 1
                    _p(
                        f"         {column}/{file_name}: extension {ext or '(none)'} "
                        f"not in allowed formats — SKIPPED before download"
                    )
                    continue

                # Skip image file types unless the target explicitly opts in
                # to image processing (e.g. knowledge_library with llm_content).
                if Path(file_name).suffix.lower() in _IMAGE_EXTENSIONS and not target.process_images:
                    report.attachments_skipped += 1
                    _p(f"         {column}/{file_name}: image file, skipped (not embeddable)")
                    continue

                # Each attachment gets its OWN directory so multiple files in
                # one column never overwrite each other and each keeps its own
                # citation sidecar (original_s3_key).
                label = f"{column}/{file_name}"
                attach_dir = (
                    f"{target.s3_prefix.rstrip('/')}/{table_slug}/{identifier}"
                    f"/{col_slug}/{attachment.id}"
                )
                original_key = f"{attach_dir}/{file_name}"
                normalized_key = f"{attach_dir}/{attachment.id}__normalized.txt"
                # Record the ORIGINAL document key in the sidecar so the
                # embedding pipeline indexes it and citations point at the
                # source file (e.g. the PDF/DOCX), not the normalized .txt.
                self._upload_airtable_meta_sidecar(
                    key_dir=attach_dir,
                    target=target,
                    record=record,
                    column=column,
                    identifier=raw_identifier,
                    original_s3_key=original_key,
                    metadata_header=(
                        _build_metadata_header(fields, extra_fields=target.metadata_columns)
                        if self._batch_mode
                        else None
                    ),
                    facets=_build_facets(fields, target.facet_columns),
                )

                # Skip only when this attachment is FULLY processed. In batch
                # mode ingestion writes no normalized.txt, so the original's
                # presence is the completion marker.
                if self._batch_mode:
                    already_done = self.uploader.key_exists(key=original_key)
                    done_marker = "original in S3 (batch mode)"
                else:
                    already_done, done_marker = self._already_processed(
                        col_normalizer=col_normalizer,
                        original_key=original_key,
                        normalized_key=normalized_key,
                    )
                if already_done:
                    report.attachments_skipped += 1
                    _p(f"         {label}: already processed ({done_marker}), skipped")
                    # Read back the existing normalized text so the record
                    # summary still aggregates files skipped on this run.
                    prior = self.uploader.get_text(key=normalized_key)
                    if prior:
                        file_summaries.append((label, extract_file_summary(prior)))
                    continue
                tmp_path = None
                try:
                    existing = self.uploader.get_bytes(key=original_key)
                    if existing is not None:
                        # Original already in S3 (e.g. from a prior --batch run):
                        # reuse it — no Airtable re-download, no re-upload.
                        binary = existing
                        _p(f"         {label}: reusing original in S3")
                    else:
                        report.attachments_downloaded += 1
                        _p(f"         {column}/{file_name} ({size_kb}): downloading...")
                        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                            tmp_path = Path(tmp_file.name)
                        self.airtable.download_attachment(attachment, path=str(tmp_path))
                        binary = tmp_path.read_bytes()

                    # Password protection is only detectable from the bytes,
                    # so this gate sits right after download — but still
                    # BEFORE the original S3 upload and any extraction, so a
                    # locked file never enters the pipeline (and never churns
                    # through worker retries into the dead ledger). Positive
                    # detection only: ambiguous bytes fall through.
                    if is_password_protected(binary, file_name):
                        report.attachments_skipped_password_protected += 1
                        _p(
                            f"         {label}: password-protected — "
                            f"SKIPPED before S3 upload/extraction"
                        )
                        # The metadata sidecar was written before download;
                        # remove it so the skipped file leaves no trace. Also
                        # drop a stale original uploaded by runs that predate
                        # this gate, so batch embeds stop tripping over it.
                        self.uploader.delete_object(
                            key=f"{attach_dir}/.airtable_meta.json"
                        )
                        if existing is not None:
                            self.uploader.delete_object(key=original_key)
                        continue

                    if existing is None:
                        # Upload the original binary — kept as permanent backup.
                        self.uploader.upload_file(
                            local_path=tmp_path,
                            key=original_key,
                            content_type=attachment.content_type,
                        )
                        report.attachments_uploaded += 1

                    # Batch mode: original + sidecar only — the offline batch
                    # job renders, normalises, and summarises. No Claude here.
                    if self._batch_mode:
                        _p(f"         {label}: uploaded OK (batch mode — extraction deferred)")
                        continue

                    # Also upload a normalised .txt if the normalizer fires.
                    # The embedding pipeline will prefer __normalized.txt over the
                    # original binary when both exist (see s3_reader.py).
                    normalized_text = col_normalizer.normalize(binary, file_name)
                    if normalized_text is not None:
                        header = _build_metadata_header(
                            fields, extra_fields=target.metadata_columns
                        )
                        final_text = header + normalized_text
                        self.uploader.upload_bytes(
                            data=final_text.encode("utf-8"),
                            key=normalized_key,
                            content_type="text/plain; charset=utf-8",
                        )
                        report.normalizations_done += 1
                        file_summaries.append(
                            (label, extract_file_summary(final_text))
                        )
                        keys_written.append(normalized_key)
                        _p(
                            f"         {label}: uploaded + normalised"
                            f" → {attachment.id}__normalized.txt"
                        )
                    else:
                        # Uploaded the original but produced no normalized text —
                        # surface it so "all files extracted" is verifiable.
                        report.attachments_unextracted.append(
                            f"{identifier}/{label} — no normalized text "
                            f"({Path(file_name).suffix.lower() or 'no-ext'})"
                        )
                        # Still feed the original to the embedding pipeline — the
                        # S3 reader parses originals directly when no sibling
                        # __normalized.txt exists.
                        keys_written.append(original_key)
                        _p(f"         {label}: uploaded OK (no normalized text)")
                except Exception as exc:  # noqa: BLE001
                    err = (
                        f"record={record.get('id')} column={column!r} "
                        f"attachment={attachment.id}: {exc}"
                    )
                    report.errors.append(err)
                    _p(f"         {column}/{file_name}: ERROR — {exc}")
                finally:
                    if tmp_path is not None and tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)

        # All attachment columns processed — synthesise the record-level
        # parent summary from every file's summary (across all columns).
        # Batch mode defers this to the offline batch job.
        if not self._batch_mode:
            try:
                summary_key = self._write_record_summary(
                    record_dir=record_dir,
                    target=target,
                    record=record,
                    raw_identifier=raw_identifier,
                    fields=fields,
                    file_summaries=file_summaries,
                    report=report,
                )
                if summary_key is not None:
                    keys_written.append(summary_key)
            except Exception as exc:  # noqa: BLE001 — never fail a record over its summary
                report.errors.append(
                    f"record={record.get('id')} record_summary: {exc}"
                )
                _p(f"         record summary: ERROR — {exc}")

        for column in target.text_columns:
            value = fields.get(column)
            if value is None:
                continue
            col_slug = slugify_column_name(column)
            base_key = f"{target.s3_prefix.rstrip('/')}/{table_slug}/{identifier}/{col_slug}/"
            try:
                if isinstance(value, str):
                    payload = value.encode("utf-8")
                    key = f"{base_key}value.txt"
                    content_type = "text/plain; charset=utf-8"
                else:
                    payload = json.dumps({"value": value}, indent=2, ensure_ascii=False).encode("utf-8")
                    key = f"{base_key}value.json"
                    content_type = "application/json"

                new_md5 = hashlib.md5(payload).hexdigest()
                existing_etag = self.uploader.get_etag(key=key)
                if existing_etag == new_md5:
                    report.text_fields_skipped += 1
                    _p(f"         {column}: unchanged, skipped")
                    continue

                self.uploader.upload_bytes(data=payload, key=key, content_type=content_type)
                report.text_fields_uploaded += 1
                _p(f"         {column}: uploaded OK")
            except Exception as exc:  # noqa: BLE001
                err = f"record={record.get('id')} column={column!r} text-upload-failed: {exc}"
                report.errors.append(err)
                _p(f"         {column}: ERROR — {exc}")

        return report, keys_written

    def run_target(self, *, target: IngestionTargetConfig) -> IngestionRunReport:
        """Run ingestion for one configured target."""

        started = datetime.now(timezone.utc)
        report = IngestionRunReport(target_name=target.name, started_at=started)

        _p(f"Discovering schema for '{target.table_name}' ...")
        self.discover_schema(target=target)
        _p(f"Schema OK. Starting record sync → s3://<bucket>/{target.s3_prefix}/")
        _p("")

        table_slug = slugify_table_name(target.table_name)
        attachment_columns = list(target.attachment_columns)
        selected_fields = [
            target.identifier_column,
            *attachment_columns,
            *target.text_columns,
            *target.metadata_columns,
            *target.facet_columns,
        ]

        records = self.airtable.iter_records(
            base_id=target.database_id,
            table_name=target.table_name,
            fields=selected_fields,
            page_size=self.page_size,
        )

        def _run_one(item: tuple[int, dict[str, Any]]) -> IngestionRunReport:
            index, rec = item
            # keys_written is only consumed by ingest_one_record's caller
            # (the event-driven worker); a full-table run() doesn't need it.
            rec_report, _keys_written = self._process_record(
                record=rec,
                target=target,
                table_slug=table_slug,
                attachment_columns=attachment_columns,
                index=index,
            )
            return rec_report

        if self._workers > 1:
            _p(f"Processing records with {self._workers} parallel workers...")
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = [pool.submit(_run_one, item) for item in enumerate(records, start=1)]
                for fut in as_completed(futures):
                    _merge_report(report, fut.result())
        else:
            for item in enumerate(records, start=1):
                _merge_report(report, _run_one(item))

        duration = (datetime.now(timezone.utc) - started).total_seconds()
        report.finished_at = datetime.now(timezone.utc)
        _p("")
        _p(f"Sync complete in {duration:.1f}s")
        _p(f"  Records processed  : {report.records_seen}")
        _p(f"  Records skipped    : {report.records_skipped_no_identifier}  (no identifier)")
        _p(f"  Attachments new    : {report.attachments_uploaded}")
        _p(f"  Attachments skipped: {report.attachments_skipped}  (already in S3)")
        _p(
            f"  Attachments skipped: {report.attachments_skipped_disallowed}"
            f"  (disallowed format)"
        )
        _p(
            f"  Attachments skipped: {report.attachments_skipped_password_protected}"
            f"  (password-protected)"
        )
        _p(f"  Normalised to .txt : {report.normalizations_done}")
        _p(f"  Record summaries   : {report.record_summaries_done}")
        _p(f"  Text fields new    : {report.text_fields_uploaded}")
        _p(f"  Text fields skipped: {report.text_fields_skipped}")
        if report.attachments_unextracted:
            _p(f"  Files WITHOUT normalized text ({len(report.attachments_unextracted)}):")
            for item in report.attachments_unextracted:
                _p(f"    • {item}")
        if report.errors:
            _p(f"  Errors ({len(report.errors)}):")
            for e in report.errors:
                _p(f"    ! {e}")
        return report

    def ingest_one_record(
        self,
        *,
        target: IngestionTargetConfig,
        record: dict[str, Any],
    ) -> list[str]:
        """Ingest exactly ONE already-fetched Airtable record (no table scan).

        The event-driven counterpart to ``run_target()``: used by the polling
        worker (scripts/run_worker.py) after the poller has identified one
        changed record. Delegates entirely to ``_process_record`` — all
        idempotency (``_already_processed`` / S3 ``key_exists`` skips) and
        Claude normalization logic is inherited unchanged.

        Returns
        -------
        The S3 keys newly written or changed by this call — feed each one to
        ``Pipeline.run_one(key)`` (the embedding pipeline) to chunk/embed/index
        it. Empty list means nothing about this record's attachments actually
        changed (already processed).

        Raises
        ------
        RuntimeError
            If ``_process_record`` recorded any error for this record, so the
            caller's retry/failure-ledger logic (SQS redelivery + the S3 job
            ledger) kicks in instead of silently swallowing a partial failure.
        """
        table_slug = slugify_table_name(target.table_name)
        report, keys_written = self._process_record(
            record=record,
            target=target,
            table_slug=table_slug,
            attachment_columns=list(target.attachment_columns),
            index=1,
        )
        if report.errors:
            raise RuntimeError(
                f"ingest_one_record failed for record={record.get('id')}: {report.errors}"
            )
        return keys_written


def _p(msg: str) -> None:
    """Print with immediate flush — essential for SSM / non-TTY sessions."""
    print(msg, flush=True)
