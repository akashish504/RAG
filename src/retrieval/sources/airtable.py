"""Airtable retrieval adapter — generic over (base_id, table_name).

Loads field metadata from a frozen JSON snapshot at ``schema_snapshot_path``
when that file exists (fast, no Meta API per deploy). If the file is missing
(e.g. fresh Docker image without checked-in ``config/*.json``), the adapter
calls the Airtable **Meta API** (``GET /v0/meta/bases/{baseId}/tables``) once
at start-up, builds an equivalent snapshot in memory, and optionally writes
that JSON to the configured path so the next process start is disk-backed.

The PAT must include **schema.bases:read** (in addition to **data.records:read**)
for the live fetch path.

Long-text detection happens here (not in the planner) so the same hint is
emitted whether the call comes via ``airtable_lookup`` or ``search``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from pipeline.airtable_ingestion.data_extract import AirtableConnector
from retrieval.models import (
    FieldDescriptor,
    Hint,
    SchemaDescriptor,
    SearchResult,
)

if TYPE_CHECKING:
    from retrieval.config import AirtableSourceConfig

log = structlog.get_logger(__name__)

# Field types Airtable returns that we treat as "long text" for hint emission.
_LONG_TEXT_TYPES = {"multilineText", "richText", "longText"}
_ATTACHMENT_TYPES = {"multipleAttachments", "attachment"}
_LINKED_TYPES = {"multipleRecordLinks", "singleRecordLink"}


def build_schema_snapshot_from_meta_api(
    connector: AirtableConnector,
    *,
    base_id: str,
    table_name: str,
) -> dict[str, Any]:
    """Build a snapshot-shaped dict from the Airtable Meta API (no local file).

    ``table_name`` must match the Airtable UI table name exactly.
    """
    tables = connector.fetch_tables_metadata(base_id)
    table_def: dict[str, Any] | None = None
    for t in tables:
        if isinstance(t, dict) and t.get("name") == table_name:
            table_def = t
            break
    if table_def is None:
        known = sorted(
            str(t.get("name", "")) for t in tables if isinstance(t, dict) and t.get("name")
        )
        msg = f"No table named {table_name!r} in base {base_id!r}. Known: {known}"
        raise ValueError(msg)

    fields = table_def.get("fields") or []
    field_count = len(fields) if isinstance(fields, list) else 0

    return {
        "source": "live_meta_api",
        "snapshot_version": 1,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "base_id": base_id,
        "table_id": table_def.get("id"),
        "table_name": table_def.get("name"),
        "primary_field_id": table_def.get("primaryFieldId"),
        "field_count": field_count,
        "table": table_def,
        "views": table_def.get("views") or [],
    }


def _maybe_persist_schema_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """After a live Meta API fetch, persist JSON so the next cold start is fast.

    Disabled when ``AIRTABLE_SCHEMA_AUTO_SAVE`` is ``0`` / ``false`` / ``no``.
    Default is to **try** saving (common for Docker bind-mount of ``config/``).
    """

    val = os.environ.get("AIRTABLE_SCHEMA_AUTO_SAVE", "1").strip().lower()
    if val in ("0", "false", "no", "off"):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        log.info("airtable_schema_snapshot_saved", path=str(path))
    except OSError as exc:
        log.warning("airtable_schema_snapshot_save_failed", path=str(path), error=str(exc))


class AirtableSource:
    """Structured-rows adapter for one logical source."""

    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        cfg: "AirtableSourceConfig",
        identifier_field: str | None,
        description: str | None,
        pat_token: str,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self._cfg = cfg
        self._identifier_field = identifier_field
        self._description = description
        self._connector = AirtableConnector(pat_token=pat_token, base_id=cfg.base_id)
        self._snapshot = self._load_snapshot()
        # The table_id (e.g. "tblXXXXXX") is stored in the snapshot and is
        # required to construct direct Airtable record URLs for citations.
        self._table_id: str | None = self._snapshot.get("table_id")
        self._field_index = {
            f["name"]: f
            for f in (self._snapshot.get("table", {}).get("fields", []) or [])
            if isinstance(f, dict) and f.get("name")
        }
        self._auto_long_text = [
            name
            for name, field in self._field_index.items()
            if field.get("type") in _LONG_TEXT_TYPES
        ]
        # Configured override wins; falls back to auto-detected.
        self._long_text_fields = list(cfg.long_text_fields) or list(self._auto_long_text)
        # Attachment-type columns hold Airtable-hosted file objects
        # (dl.airtable.com URLs). They are stripped from every returned row —
        # documents are served ONLY via S3 citations, never via Airtable links.
        self._attachment_fields = {
            name
            for name, field in self._field_index.items()
            if field.get("type") in _ATTACHMENT_TYPES
        }

    # ------------------------------------------------------------------
    # Snapshot loading
    # ------------------------------------------------------------------

    def _load_snapshot(self) -> dict[str, Any]:
        path = Path(self._cfg.schema_snapshot_path)
        if not path.is_absolute():
            from retrieval.paths import REPO_ROOT  # noqa: PLC0415

            path = REPO_ROOT / path

        if path.is_file():
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self._validate_snapshot_matches_cfg(snapshot)
            return snapshot

        log.info(
            "airtable_schema_snapshot_missing_using_meta_api",
            source=self.name,
            path=str(path),
            table=self._cfg.table_name,
        )
        snapshot = build_schema_snapshot_from_meta_api(
            self._connector,
            base_id=self._cfg.base_id,
            table_name=self._cfg.table_name,
        )
        self._validate_snapshot_matches_cfg(snapshot)
        _maybe_persist_schema_snapshot(path, snapshot)
        return snapshot

    def _validate_snapshot_matches_cfg(self, snapshot: dict[str, Any]) -> None:
        """Ensure snapshot base/table align with ``retrieval_sources.yaml``."""

        snap_base = snapshot.get("base_id")
        if snap_base and snap_base != self._cfg.base_id:
            msg = (
                f"source {self.name!r}: BASE_ID / yaml base_id ({self._cfg.base_id!r}) does not match "
                f"snapshot base_id ({snap_base!r}). Update env or regenerate snapshot."
            )
            raise ValueError(msg)
        snap_table = snapshot.get("table_name")
        if snap_table and snap_table != self._cfg.table_name:
            msg = (
                f"source {self.name!r}: table_name in YAML ({self._cfg.table_name!r}) does not "
                f"match snapshot table_name ({snap_table!r}). Update YAML or regenerate snapshot."
            )
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Capability advertisement
    # ------------------------------------------------------------------

    def supported_filters(self) -> set[str]:
        return set(self._field_index.keys())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_schema(self) -> SchemaDescriptor:
        fields: list[FieldDescriptor] = []
        for name, raw in self._field_index.items():
            ftype = str(raw.get("type", "unknown"))
            options = raw.get("options") or {}
            choices: list[str] | None = None
            if isinstance(options, dict) and isinstance(options.get("choices"), list):
                choices = [
                    str(c.get("name"))
                    for c in options["choices"]
                    if isinstance(c, dict) and c.get("name")
                ] or None
            fields.append(
                FieldDescriptor(
                    name=name,
                    type=ftype,
                    description=(raw.get("description") or None),
                    select_choices=choices,
                    is_long_text=ftype in _LONG_TEXT_TYPES,
                    is_attachment=ftype in _ATTACHMENT_TYPES,
                    linked_table_id=(
                        options.get("linkedTableId") if isinstance(options, dict) else None
                    ),
                )
            )
        from retrieval.sources.opensearch import _SEMANTIC_METADATA_FIELDS  # noqa: PLC0415

        return SchemaDescriptor(
            source=self.name,
            display_name=self.display_name,
            description=self._description,
            capabilities=["semantic", "structured"],
            identifier_field=self._identifier_field,
            fields=fields,
            long_text_fields=list(self._long_text_fields),
            semantic_metadata_fields=list(_SEMANTIC_METADATA_FIELDS),
        )

    async def filter_structured(
        self,
        *,
        formula: str | None,
        fields: list[str] | None,
        max_records: int | None,
    ) -> tuple[list[SearchResult], list[Hint]]:
        """Run an Airtable ``listRecords`` call (sync pyairtable on a thread)."""

        from retrieval.confidentiality import ensure_required_fetch_fields  # noqa: PLC0415

        fields = ensure_required_fetch_fields(self.name, fields)

        kwargs: dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        if formula:
            kwargs["formula"] = formula
        if max_records is not None and max_records > 0:
            kwargs["max_records"] = max_records

        try:
            rows = await asyncio.to_thread(self._fetch_rows, kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "airtable_lookup_failed",
                source=self.name,
                table=self._cfg.table_name,
                error=str(exc),
            )
            raise

        return self._format_rows(rows)

    def _fetch_rows(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        table = self._connector.get_table(self._cfg.table_name)
        return table.all(**kwargs)

    def _format_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[SearchResult], list[Hint]]:
        results: list[SearchResult] = []
        hints: list[Hint] = []
        truncated_fields: dict[str, int] = {}
        truncate_at = self._cfg.long_text_truncate

        for row in rows:
            row_fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
            text_parts: list[str] = []
            metadata: dict[str, Any] = {}
            display_fields: dict[str, Any] = {}
            for fname, value in row_fields.items():
                # Attachment columns carry Airtable-hosted file objects
                # (dl.airtable.com URLs) — never returned. Documents reach the
                # client exclusively as S3 citations on semantic hits.
                if fname in self._attachment_fields:
                    continue
                if fname in self._long_text_fields and isinstance(value, str):
                    if len(value) > truncate_at:
                        truncated_fields[fname] = truncated_fields.get(fname, 0) + 1
                        display_fields[fname] = value[:truncate_at] + "…[truncated]"
                    else:
                        display_fields[fname] = value
                    text_parts.append(f"{fname}: {display_fields[fname]}")
                else:
                    display_fields[fname] = value
                    if isinstance(value, str) and value:
                        text_parts.append(f"{fname}: {value}")
                metadata[fname] = value
            record_id = row.get("id")
            # PRODUCT DECISION: no Airtable citations, ever — structured rows
            # return their DATA but no airtable.com link. (The CitationResolver
            # machinery is retained in citations.py, unused, should this need
            # to be revisited.)
            results.append(
                SearchResult(
                    source=self.name,
                    source_type="structured",
                    score=1.0,
                    text="\n".join(text_parts),
                    record_id=record_id,
                    metadata=metadata,
                    payload={
                        "id": record_id,
                        "createdTime": row.get("createdTime"),
                        "fields": display_fields,
                    },
                    citation_url=None,
                    citations=[],
                )
            )

        for fname, count in truncated_fields.items():
            hints.append(
                Hint(
                    tool="semantic_search",
                    source=self.name,
                    reason="long_text_field_truncated",
                    detail={
                        "field": fname,
                        "rows_truncated": count,
                        "truncate_at": truncate_at,
                    },
                )
            )
        return results, hints
