"""Reusable Airtable connector (ported from existing data_extract module).

This keeps Airtable connection and metadata logic centralized so the ingestion
pipeline can reuse it without duplicating API client code.
"""

from __future__ import annotations

from typing import Any

import requests
from pyairtable import Api


class AirtableConnector:
    """Handles connections and data extraction from Airtable bases."""

    def __init__(
        self,
        pat_token: str,
        base_id: str | None = None,
        *,
        auto_discover_if_single: bool = False,
    ) -> None:
        self.pat_token = pat_token
        self.api = Api(pat_token)
        self.base_id: str | None = base_id
        self.base: Any = None
        if base_id:
            self.attach_base(base_id)
        elif auto_discover_if_single:
            discovered = self._discover_base_id()
            self.attach_base(discovered)

    def _discover_base_id(self) -> str:
        bases = self.list_bases()
        if not bases:
            raise ValueError("No bases accessible with this PAT token.")
        if len(bases) == 1:
            return bases[0]["id"]
        raise ValueError("BASE_ID not set and multiple bases exist.")

    def attach_base(self, base_id: str) -> None:
        self.base_id = base_id
        self.base = self.api.base(base_id)

    def _require_base(self) -> None:
        if not self.base_id or self.base is None:
            raise ValueError("No base selected. Call attach_base(base_id) first.")

    def list_bases(self) -> list[dict[str, Any]]:
        """List all Airtable bases accessible by this PAT."""

        url = "https://api.airtable.com/v0/meta/bases"
        headers = {"Authorization": f"Bearer {self.pat_token}"}
        output: list[dict[str, Any]] = []
        offset: str | None = None

        while True:
            params = {"offset": offset} if offset else {}
            response = requests.get(url, headers=headers, params=params, timeout=120)
            response.raise_for_status()
            payload = response.json()
            output.extend(payload.get("bases", []))
            offset = payload.get("offset")
            if not offset:
                break
        return output

    def _fetch_tables_meta(self, base_id: str) -> list[dict[str, Any]]:
        url = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables"
        headers = {"Authorization": f"Bearer {self.pat_token}"}
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        return response.json().get("tables", [])

    def fetch_tables_metadata(self, base_id: str | None = None) -> list[dict[str, Any]]:
        """Return raw table definitions from the Meta API (``GET /meta/bases/{{baseId}}/tables``).

        Each table includes ``id``, ``name``, ``primaryFieldId``, ``fields``, ``views``, etc.
        Each field object includes ``id``, ``name``, ``type``, ``description`` (if set),
        and type-specific ``options`` (select choices, linked table ids, etc.).
        """

        selected = base_id or self.base_id
        if not selected:
            raise ValueError("Provide base_id or attach a base first.")
        return self._fetch_tables_meta(selected)

    def list_bases_with_table_names(self) -> list[dict[str, Any]]:
        """Return base list with only table names (metadata summary)."""

        output: list[dict[str, Any]] = []
        for base in self.list_bases():
            base_id = base["id"]
            tables_payload = self._fetch_tables_meta(base_id)
            output.append(
                {
                    "database_id": base_id,
                    "database_name": base["name"],
                    "permission_level": base.get("permissionLevel"),
                    "tables": [table["name"] for table in tables_payload],
                }
            )
        return output

    def get_tables_with_columns(
        self,
        base_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return all tables with field metadata for the target base."""

        selected_base_id = base_id or self.base_id
        if not selected_base_id:
            raise ValueError("Provide base_id or attach a base first.")

        layout: dict[str, dict[str, Any]] = {}
        for table in self._fetch_tables_meta(selected_base_id):
            by_field_id = {field["id"]: field for field in table.get("fields", [])}
            primary_field_id = table.get("primaryFieldId")
            primary_field = by_field_id.get(primary_field_id) if primary_field_id else None
            columns = [
                {
                    "name": field["name"],
                    "type": field.get("type", "unknown"),
                    "id": field.get("id"),
                }
                for field in table.get("fields", [])
            ]
            layout[table["name"]] = {
                "table_id": table.get("id"),
                "primary_field": (
                    {
                        "field_id": primary_field_id,
                        "name": primary_field.get("name"),
                        "type": primary_field.get("type"),
                    }
                    if primary_field
                    else None
                ),
                "columns": columns,
            }
        return layout

    def get_table(self, table_name: str):
        self._require_base()
        return self.base.table(table_name)

    def fetch_all_records(
        self,
        table_name: str,
        fields: list[str] | None = None,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        table = self.get_table(table_name)
        return table.all(fields=fields, max_records=max_records)
