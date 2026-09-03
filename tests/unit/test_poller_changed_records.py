"""Tests for AirtableClient.iter_changed_records — the poller's change query.

The formula must scope LAST_MODIFIED_TIME() to exactly the watched attachment
columns and parse the cursor with the explicit moment.js format that
PipelineStateStore.set_cursor writes. No cursor → full scan (bootstrap).
"""

from __future__ import annotations

from pipeline.airtable_ingestion.airtable_client import AirtableClient


class _FakeTable:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def all(self, **kwargs):
        self.calls.append(kwargs)
        return [{"id": "rec1"}]


class _FakeConnector:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table
        self.attached: list[str] = []

    def attach_base(self, base_id: str) -> None:
        self.attached.append(base_id)

    def get_table(self, table_name: str) -> _FakeTable:  # noqa: ARG002
        return self._table


def _client(table: _FakeTable) -> AirtableClient:
    client = AirtableClient.__new__(AirtableClient)
    client.connector = _FakeConnector(table)
    client.timeout_seconds = 1.0
    return client


def test_formula_scopes_watched_columns_and_parses_cursor() -> None:
    table = _FakeTable()
    records = list(
        _client(table).iter_changed_records(
            base_id="app1",
            table_name="(D.Quals)",
            watch_fields=["Deliverable Attachments", "Insight", "Proposal Attachment"],
            since_iso="2026-07-22T10:00:00Z",
        )
    )
    assert records == [{"id": "rec1"}]
    formula = table.calls[0]["formula"]
    assert formula == (
        "IS_AFTER(LAST_MODIFIED_TIME({Deliverable Attachments}, {Insight}, "
        "{Proposal Attachment}), "
        "DATETIME_PARSE('2026-07-22T10:00:00Z', 'YYYY-MM-DDTHH:mm:ssZ'))"
    )


def test_no_cursor_means_full_scan_bootstrap() -> None:
    table = _FakeTable()
    records = list(
        _client(table).iter_changed_records(
            base_id="app1",
            table_name="(D.Quals)",
            watch_fields=["Deliverable Attachments"],
            since_iso=None,
        )
    )
    assert records == [{"id": "rec1"}]
    assert "formula" not in table.calls[0], "bootstrap run must not filter"
