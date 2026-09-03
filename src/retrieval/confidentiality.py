"""D.Quals confidentiality tagging — query/response-time only.

Full record content is shown in every case (no dropping, no redaction). When
either flag below is set, a bold bracketed tag is prepended to the hit's text
(and to its `record_summary`/`deck_summary`, when present) so a reader can
see at a glance that a result is confidential:

- ``confidential_project == "CONFIDENTIAL"`` -> ``"**[CONFIDENTIAL PROJECT]**"``
- ``confidential_client == "CONFIDENTIAL"``   -> ``"**[CONFIDENTIAL CLIENT]**"``
- both flags set                              -> ``"**[CONFIDENTIAL PROJECT & CLIENT]**"``

Blank/missing facet value on either field means "not confidential" — only an
explicit ``"CONFIDENTIAL"`` string triggers a tag. Scoped to the ``d_quals``
logical source only.

Key-spelling note: semantic (OpenSearch-native) hits carry snake_case
metadata keys (see ``retrieval.sources.opensearch._FACET_FIELDS``);
structured (Airtable-native) hits carry the raw Title Case Airtable field
names verbatim (see ``retrieval.sources.airtable._format_rows``). Every
lookup here checks both spellings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from retrieval.models import SearchResult

# Only this logical source has these fields today; gating on the source name
# (not merely "does this facet key exist") is deliberate — it guards against
# a hypothetical future source reusing "confidential_client"/
# "confidential_project" as key names with a different meaning.
CONFIDENTIAL_SOURCES: frozenset[str] = frozenset({"d_quals"})

_CONFIDENTIAL_PROJECT_KEYS: tuple[str, ...] = ("confidential_project", "Confidential Project")
_CONFIDENTIAL_CLIENT_KEYS: tuple[str, ...] = ("confidential_client", "Confidential Client")

_PROJECT_TAG = "**[CONFIDENTIAL PROJECT]**"
_CLIENT_TAG = "**[CONFIDENTIAL CLIENT]**"
_BOTH_TAG = "**[CONFIDENTIAL PROJECT & CLIENT]**"

# Airtable field names (Title Case, as they appear in the base) that MUST
# always be fetched for a CONFIDENTIAL_SOURCES table, regardless of any
# caller-supplied field subset (e.g. the `fields` argument on the
# `airtable_lookup` MCP tool, or the enrichment field list built in
# `retrieval.mcp.tools._enrichment_fields_for_schema`). Airtable's API only
# returns the fields explicitly requested, so a narrower selection would make
# a genuinely confidential record indistinguishable from a blank/non-
# confidential one — this constant closes that gap at the fetch layer, before
# is_confidential_project/is_confidential_client ever run.
REQUIRED_FETCH_FIELDS: dict[str, tuple[str, ...]] = {
    "d_quals": ("Confidential Project", "Confidential Client", "Client Organisation"),
}


def ensure_required_fetch_fields(source_name: str, fields: list[str] | None) -> list[str] | None:
    """Union in this source's required enforcement fields, if any.

    ``None``/empty ``fields`` means "fetch everything" (Airtable's default) —
    left untouched, since the required fields are already included. Only a
    caller-narrowed, non-empty list needs the required fields appended.
    """
    required = REQUIRED_FETCH_FIELDS.get(source_name)
    if not required or not fields:
        return fields
    merged = list(fields)
    for field in required:
        if field not in merged:
            merged.append(field)
    return merged


def _as_list(value: Any) -> list[str]:
    """Normalise a facet value (list[str] | str | None) to list[str]."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


def _facet_values(metadata: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    for key in keys:
        if key in metadata:
            return _as_list(metadata[key])
    return []


def _is_exactly_confidential(values: list[str]) -> bool:
    return any(v.strip().upper() == "CONFIDENTIAL" for v in values)


def is_confidential_project(hit: "SearchResult") -> bool:
    if hit.source not in CONFIDENTIAL_SOURCES:
        return False
    return _is_exactly_confidential(_facet_values(hit.metadata, _CONFIDENTIAL_PROJECT_KEYS))


def is_confidential_client(hit: "SearchResult") -> bool:
    if hit.source not in CONFIDENTIAL_SOURCES:
        return False
    return _is_exactly_confidential(_facet_values(hit.metadata, _CONFIDENTIAL_CLIENT_KEYS))


def _prepend_tag(value: str, tag: str) -> str:
    return f"{tag} {value}" if value else tag


def tag_confidential_hits(hits: list["SearchResult"]) -> list["SearchResult"]:
    """Prepend a confidentiality tag to hits that need one, in place.

    Every hit is returned (no dropping) with all other fields — text content,
    metadata, citations — otherwise unchanged. Non-d_quals hits, and d_quals
    hits with blank/missing confidentiality facets, are untouched.
    """
    for h in hits:
        is_project = is_confidential_project(h)
        is_client = is_confidential_client(h)
        if not is_project and not is_client:
            continue
        tag = _BOTH_TAG if (is_project and is_client) else (_PROJECT_TAG if is_project else _CLIENT_TAG)

        h.text = _prepend_tag(h.text or "", tag)
        for key in ("record_summary", "deck_summary"):
            value = h.metadata.get(key)
            if isinstance(value, str) and value:
                h.metadata[key] = _prepend_tag(value, tag)

    return hits
