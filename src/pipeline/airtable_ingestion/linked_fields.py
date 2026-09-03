"""Resolve Airtable linked-record fields to human-readable display values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.airtable_ingestion.airtable_client import AirtableClient

_REC_ID_PREFIX = "rec"


def looks_like_record_ids(value: Any) -> bool:
    """True when ``value`` is a non-empty list of Airtable record ids."""

    if not isinstance(value, list):
        return False
    return bool(value) and all(
        isinstance(item, str) and item.startswith(_REC_ID_PREFIX) for item in value
    )


class LinkedFieldResolver:
    """Resolve ``multipleRecordLinks`` values (``rec…`` ids) to primary-field text."""

    def __init__(
        self,
        *,
        airtable: AirtableClient,
        base_id: str,
        table_name: str,
    ) -> None:
        self._airtable = airtable
        self._base_id = base_id
        self._table_name = table_name
        self._field_by_name: dict[str, dict[str, Any]] | None = None
        self._table_by_id: dict[str, dict[str, Any]] | None = None
        self._record_cache: dict[str, dict[str, str]] = {}
        self._prefetched_tables: set[str] = set()

    def prefetch_for_columns(self, column_names: list[str] | tuple[str, ...]) -> None:
        """Load linked tables referenced by ``column_names`` into the id→name cache."""

        self._ensure_schema()
        for name in column_names:
            field = (self._field_by_name or {}).get(name) or {}
            if field.get("type") != "multipleRecordLinks":
                continue
            linked_table_id = (field.get("options") or {}).get("linkedTableId")
            if isinstance(linked_table_id, str) and linked_table_id:
                self._prefetch_table(linked_table_id)

    def resolve_value(self, column_name: str, value: Any) -> Any:
        """Return display names for linked-record columns; pass through other values."""

        if value is None or value == "" or value == []:
            return value

        self._ensure_schema()
        field = (self._field_by_name or {}).get(column_name) or {}
        linked_table_id = (field.get("options") or {}).get("linkedTableId")
        if field.get("type") == "multipleRecordLinks" and isinstance(linked_table_id, str):
            if isinstance(value, list):
                return self._resolve_ids(linked_table_id, value)
            return value

        if looks_like_record_ids(value) and isinstance(value, list):
            # Fallback when schema is unavailable but values look like record ids.
            for table_id in (self._table_by_id or {}):
                if table_id in self._prefetched_tables:
                    resolved = self._resolve_ids(table_id, value)
                    if resolved != value:
                        return resolved
        return value

    def _ensure_schema(self) -> None:
        if self._field_by_name is not None:
            return
        tables = self._airtable.fetch_tables_metadata(base_id=self._base_id)
        self._table_by_id = {table["id"]: table for table in tables}
        self._field_by_name = {}
        for table in tables:
            if table.get("name") != self._table_name:
                continue
            for field in table.get("fields", []):
                name = field.get("name")
                if isinstance(name, str):
                    self._field_by_name[name] = field

    def _resolve_ids(self, linked_table_id: str, record_ids: list[str]) -> list[str]:
        if linked_table_id not in self._prefetched_tables:
            self._prefetch_table(linked_table_id)
        cache = self._record_cache.get(linked_table_id, {})
        names = [cache[rec_id] for rec_id in record_ids if rec_id in cache and cache[rec_id]]
        return names if names else [str(item) for item in record_ids]

    def _prefetch_table(self, linked_table_id: str) -> None:
        if linked_table_id in self._prefetched_tables:
            return
        self._prefetched_tables.add(linked_table_id)
        table_meta = (self._table_by_id or {}).get(linked_table_id)
        if not table_meta:
            return
        table_name = table_meta.get("name")
        primary_field_id = table_meta.get("primaryFieldId")
        if not isinstance(table_name, str) or not primary_field_id:
            return
        by_id = {field["id"]: field for field in table_meta.get("fields", [])}
        primary_field = by_id.get(primary_field_id) or {}
        primary_name = primary_field.get("name")
        if not isinstance(primary_name, str):
            return
        cache: dict[str, str] = {}
        for record in self._airtable.iter_records(
            base_id=self._base_id,
            table_name=table_name,
            fields=[primary_name],
            page_size=100,
        ):
            rec_id = record.get("id")
            if not isinstance(rec_id, str):
                continue
            fields = record.get("fields") or {}
            display = fields.get(primary_name)
            if display is not None and str(display).strip():
                cache[rec_id] = str(display).strip()
        self._record_cache[linked_table_id] = cache
