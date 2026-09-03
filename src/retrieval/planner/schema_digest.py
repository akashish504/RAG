"""Cached, compact schema digests for the Retrieval Planner.

The planner needs to know each source's fields, identifier (join key), and
linked-table relationships so it can write correct formulas and plan
cross-table joins WITHOUT calling get_schema for every query.

Schemas change infrequently, so we build a compact digest from the on-disk
Airtable snapshot (or live adapter) once and cache it in-process with a TTL.
The planner uses the cached digest as authoritative; ``get_schema`` remains
the fallback when a digest is missing, stale, or an airtable_lookup fails on
an unknown field.

Refresh: TTL (``SCHEMA_CACHE_TTL_SECONDS``, default 3600s) or ``reload=True``.
"""

from __future__ import annotations

import os
import time
from typing import Callable

from retrieval.models import SchemaDescriptor

# source name -> (monotonic_timestamp, digest)
_CACHE: dict[str, tuple[float, dict]] = {}

_DEFAULT_TTL_SECONDS = 3600
_MAX_FIELDS = 60
_MAX_CHOICES = 30


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("SCHEMA_CACHE_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def build_source_digest(schema: SchemaDescriptor) -> dict:
    """Compact, planner-facing digest of one source's schema.

    Includes the identifier field (natural join key), a trimmed field catalog
    with types / long-text flags / select choices, and any linked-table
    relationships (secondary join hints).
    """
    caps = list(schema.capabilities)
    is_structured = "structured" in caps

    structured_fields: list[dict] = []
    linked_tables: list[dict] = []
    if is_structured:
        for f in schema.fields[:_MAX_FIELDS]:
            entry: dict = {"name": f.name, "type": f.type}
            if f.is_long_text:
                entry["is_long_text"] = True
            if f.select_choices:
                entry["choices"] = list(f.select_choices)[:_MAX_CHOICES]
            if f.linked_table_id:
                entry["linked_table_id"] = f.linked_table_id
                linked_tables.append({"field": f.name, "linked_table_id": f.linked_table_id})
            structured_fields.append(entry)

    # Join keys the planner can reason with: the human identifier first
    # (email / Project ID / Doc ID — present in both metadata and rows),
    # then any linked-table fields as secondary hints.
    join_keys: list[str] = []
    if schema.identifier_field:
        join_keys.append(schema.identifier_field)
    join_keys.extend(lt["field"] for lt in linked_tables)

    digest: dict = {
        "name": schema.source,
        "display_name": schema.display_name,
        "description": schema.description or "",
        "capabilities": caps,
        "identifier_field": schema.identifier_field,
        "join_keys": join_keys,
    }
    if is_structured:
        digest["structured_fields"] = structured_fields
        if linked_tables:
            digest["linked_tables"] = linked_tables
    if schema.long_text_fields:
        # Long-text fields are best searched semantically, not filtered.
        digest["semantic_text_fields"] = list(schema.long_text_fields)
    return digest


def get_cached_digest(
    source: str,
    loader: Callable[[], SchemaDescriptor],
    *,
    reload: bool = False,
) -> dict:
    """Return the cached digest for ``source``, building it via ``loader`` on miss/stale.

    ``loader`` is a zero-arg callable returning a :class:`SchemaDescriptor`
    (typically wrapping ``logical.get_schema()`` with a snapshot fallback).
    On any loader error, returns a minimal digest so planning still proceeds.
    """
    now = time.monotonic()
    if not reload:
        cached = _CACHE.get(source)
        if cached is not None and (now - cached[0]) < _ttl_seconds():
            return cached[1]

    try:
        schema = loader()
        digest = build_source_digest(schema)
    except Exception:  # noqa: BLE001
        # Minimal fallback — planner can still route by name and will emit a
        # get_schema step when it needs field-level detail.
        digest = {"name": source, "capabilities": ["semantic"], "schema_unavailable": True}

    _CACHE[source] = (now, digest)
    return digest


def clear_cache() -> None:
    """Drop all cached digests (e.g. after a snapshot refresh)."""
    _CACHE.clear()
