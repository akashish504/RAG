"""Thin Airtable client wrapper around `data_extract.AirtableConnector`."""

from __future__ import annotations

from typing import Any

import requests

from pipeline.airtable_ingestion.data_extract import AirtableConnector
from pipeline.airtable_ingestion.models import AttachmentSpec


class AirtableClient:
    """Reusable Airtable operations used by the ingestion pipeline."""

    def __init__(self, *, pat_token: str, timeout_seconds: float = 120.0) -> None:
        self.connector = AirtableConnector(pat_token=pat_token)
        self.timeout_seconds = timeout_seconds

    def list_bases_with_tables(self) -> list[dict[str, Any]]:
        return self.connector.list_bases_with_table_names()

    def get_tables_with_columns(self, *, base_id: str) -> dict[str, dict[str, Any]]:
        return self.connector.get_tables_with_columns(base_id=base_id)

    def fetch_tables_metadata(self, *, base_id: str) -> list[dict[str, Any]]:
        """Full table + field definitions from the Airtable Meta API (for Text-to-SQL / agents)."""

        return self.connector.fetch_tables_metadata(base_id)

    def iter_records(
        self,
        *,
        base_id: str,
        table_name: str,
        fields: list[str] | None = None,
        page_size: int = 100,
    ):
        # Fetch all pages up front so the offset token never expires mid-iteration.
        # Lazy iteration (table.iterate) fails with LIST_RECORDS_ITERATOR_NOT_AVAILABLE
        # when per-page processing (e.g. S3 uploads) takes longer than Airtable's offset TTL.
        self.connector.attach_base(base_id)
        table = self.connector.get_table(table_name)
        records = table.all(fields=fields, page_size=page_size)
        yield from records

    def iter_changed_records(
        self,
        *,
        base_id: str,
        table_name: str,
        watch_fields: list[str],
        since_iso: str | None,
        fields: list[str] | None = None,
        page_size: int = 100,
    ):
        """Records whose ``watch_fields`` changed after ``since_iso``.

        Used by the polling trigger (scripts/run_poller.py) instead of a full
        table scan. Scoping ``LAST_MODIFIED_TIME()`` to just the watched
        columns (not the whole record) means an edit to an unrelated field
        never enqueues a re-ingestion.

        ``since_iso=None`` (no prior poll for this target) fetches every
        record — same as a normal full sync — so the first poll after
        deploying a new target acts as a bootstrap rather than silently
        skipping every existing row.
        """
        self.connector.attach_base(base_id)
        table = self.connector.get_table(table_name)
        if since_iso is None:
            records = table.all(fields=fields, page_size=page_size)
            yield from records
            return

        # Airtable's DATETIME_PARSE needs an explicit moment.js-style format to
        # reliably parse an ISO string (it can silently mis-parse offsets /
        # fractional seconds otherwise). PipelineStateStore.set_cursor() always
        # writes UTC timestamps in exactly this "...Z" shape, so the two stay
        # in lockstep — see state_store.py's _CURSOR_FORMAT.
        field_list = ", ".join(f"{{{f}}}" for f in watch_fields)
        formula = (
            f"IS_AFTER(LAST_MODIFIED_TIME({field_list}), "
            f"DATETIME_PARSE('{since_iso}', 'YYYY-MM-DDTHH:mm:ssZ'))"
        )
        records = table.all(formula=formula, fields=fields, page_size=page_size)
        yield from records

    def get_record(self, *, base_id: str, table_name: str, record_id: str) -> dict[str, Any]:
        """Fetch exactly one record by id (used by the worker for one message)."""
        self.connector.attach_base(base_id)
        table = self.connector.get_table(table_name)
        return table.get(record_id)

    @staticmethod
    def extract_attachments(record: dict[str, Any], field_name: str) -> list[AttachmentSpec]:
        fields = record.get("fields", {}) or {}
        value = fields.get(field_name) or []
        if not isinstance(value, list):
            return []

        attachments: list[AttachmentSpec] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            attachment_id = str(item.get("id", ""))
            filename = str(item.get("filename", "attachment"))
            content_type = item.get("type")
            size = item.get("size")
            attachments.append(
                AttachmentSpec(
                    id=attachment_id or "unknown_attachment",
                    filename=filename,
                    url=url,
                    content_type=str(content_type) if content_type else None,
                    size=int(size) if isinstance(size, int) else None,
                )
            )
        return attachments

    def download_attachment(self, attachment: AttachmentSpec, *, path: str) -> None:
        """Download Airtable attachment URL to local temp file."""

        with requests.get(attachment.url, stream=True, timeout=self.timeout_seconds) as response:
            response.raise_for_status()
            with open(path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
