"""Metadata-only backfill of D.Quals contact person + project description.

Patches ``dalberg_contact_person`` and ``project_description`` onto every
existing chunk in ``mcp-d-quals`` via ``update_by_query`` — NO re-embedding.

Usage
-----
    python scripts/backfill_d_quals_record_meta.py --dry-run
    python scripts/backfill_d_quals_record_meta.py
    python scripts/backfill_d_quals_record_meta.py --write-sidecars
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore[import]

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.config import (
    DEFAULT_INGESTION_CONFIG_PATH,
    load_airtable_ingestion_settings,
)
from pipeline.airtable_ingestion.linked_fields import LinkedFieldResolver, looks_like_record_ids
from pipeline.airtable_ingestion.normalizers import (
    normalize_identifier,
    slugify_column_name,
    slugify_table_name,
)
from pipeline.common.aws import s3_client
from pipeline.common.opensearch import build_opensearch_client
from pipeline.config import load_settings


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Resolver-aware facet/record-meta builders (local copies).
#
# This branch's ingestion pipeline builds facets WITHOUT linked-record
# resolution, but "Dalberg Contact Person" is a multipleRecordLinks column —
# without resolution the backfill would write opaque rec-IDs instead of names.
# These mirror the resolver-aware helpers from the dqual-vllm branch's
# airtable_ingestion.pipeline; kept local so the deployed ingestion pipeline
# on this branch is untouched.
# ---------------------------------------------------------------------------

_RECORD_META_KEY_ALIASES: dict[str, str] = {
    "Project Description (1-paragraph)": "project_description",
}


def _record_meta_key(column: str) -> str:
    return _RECORD_META_KEY_ALIASES.get(column, slugify_column_name(column))


def _field_display_text(
    value: Any, *, column: str | None = None, resolver: LinkedFieldResolver | None = None
) -> str:
    if value is None or value == "" or value == []:
        return ""
    if resolver is not None and column is not None:
        value = resolver.resolve_value(column, value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return str(value).strip()


def _build_facets(
    fields: dict[str, Any],
    facet_columns: tuple[str, ...],
    *,
    resolver: LinkedFieldResolver | None = None,
    unresolved: dict[str, int] | None = None,
) -> dict[str, Any]:
    """``{field_slug: value}`` — rec-IDs resolved to display names via resolver.

    A value that STILL looks like Airtable record ids after resolution (person
    record deleted, meta-API scope missing, …) is DROPPED, not indexed: an
    absent facet is better than an opaque "recXXXX…" posing as a person's name.
    Drops are tallied per column into ``unresolved`` so the run can report them.
    """
    out: dict[str, Any] = {}
    for col in facet_columns:
        value = fields.get(col)
        if value is None or value == "" or value == []:
            continue
        if resolver is not None:
            value = resolver.resolve_value(col, value)
        if looks_like_record_ids(value):
            if unresolved is not None:
                unresolved[col] = unresolved.get(col, 0) + 1
            continue
        key = _FACET_KEY_ALIASES.get(col, slugify_column_name(col))
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            if cleaned:
                out[key] = cleaned
        else:
            out[key] = str(value).strip()
    return out


def _build_record_meta(
    fields: dict[str, Any],
    metadata_columns: tuple[str, ...],
    *,
    resolver: LinkedFieldResolver | None = None,
) -> dict[str, str]:
    """Long-text record metadata (e.g. project_description) keyed by slug."""
    out: dict[str, str] = {}
    for col in metadata_columns:
        text = _field_display_text(fields.get(col), column=col, resolver=resolver)
        if text:
            out[_record_meta_key(col)] = text
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill dalberg_contact_person and project_description onto "
            "existing D.Quals OpenSearch chunks without re-embedding."
        )
    )
    parser.add_argument(
        "--target",
        default="d_quals_sync",
        help="Airtable ingestion target (default: d_quals_sync).",
    )
    parser.add_argument(
        "--table",
        default="d_quals",
        help="tables.yaml entry for OpenSearch index routing (default: d_quals).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable_ingestion.yaml.",
    )
    parser.add_argument(
        "--extra-facet-columns",
        default=(
            "Dalberg Contact Person,Dalberg Team Members,Total Fees Charged,"
            "Dalberg Entity,Insight Type,"
            "Which lenses were you intentional about applying to your project?"
        ),
        help=(
            "Comma-separated Airtable columns to backfill IN ADDITION to the "
            "target's configured facet_columns (glossary field-coverage set). "
            "Linked-record columns are resolved to display names; a value that "
            "still looks like record-ids after resolution is DROPPED (never "
            "written). A column name that does not exist in Airtable is silently "
            "skipped — check the per-field populated counts in the run summary. "
            "Pass '' to backfill only the target's configured columns."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve Airtable records and report counts without writing.",
    )
    parser.add_argument(
        "--write-sidecars",
        action="store_true",
        help="Also merge facets/record_meta into .airtable_meta.json in S3.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional AWS credential profile (local development only).",
    )
    return parser.parse_args()


def _build_meta_map(
    *,
    airtable: AirtableClient,
    target: Any,
) -> dict[str, dict[str, Any]]:
    """``normalized_primary_key → {dalberg_contact_person, project_description, …}``."""

    resolver = LinkedFieldResolver(
        airtable=airtable,
        base_id=target.database_id,
        table_name=target.table_name,
    )
    # Fail FAST if the linked-record cache cannot be built (401/403 = PAT lacks
    # schema.bases:read, wrong base, network). Continuing without it would write
    # raw "recXXXX…" ids into the index as contact-person "names".
    try:
        resolver.prefetch_for_columns([*target.facet_columns, *target.metadata_columns])
    except Exception as exc:  # noqa: BLE001
        _p(f"ERROR: could not load Airtable schema / linked tables: {exc}")
        _p("       The PAT needs meta-API access (schema.bases:read) to resolve")
        _p("       'Dalberg Contact Person' rec-IDs to names. Aborting — nothing written.")
        raise SystemExit(1) from exc

    # Fetch ALL fields (fields=None) rather than an explicit allow-list: Airtable
    # 422s the WHOLE request if any single requested column name is unknown, so a
    # stray/renamed column would abort the run. Fetching all fields is resilient —
    # a column that does not exist simply never appears (and shows 0 in the
    # per-field populated tally below), instead of crashing.
    meta_map: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, int] = {}
    populated: dict[str, int] = {}
    seen = 0
    for record in airtable.iter_records(
        base_id=target.database_id,
        table_name=target.table_name,
        fields=None,
        page_size=100,
    ):
        seen += 1
        row_fields = record.get("fields") or {}
        raw_identifier = row_fields.get(target.identifier_column)
        if not isinstance(raw_identifier, str) or not raw_identifier.strip():
            continue
        pk = normalize_identifier(raw_identifier.strip())
        facets = _build_facets(
            row_fields, target.facet_columns, resolver=resolver, unresolved=unresolved
        )
        record_meta = _build_record_meta(
            row_fields, target.metadata_columns, resolver=resolver
        )
        fields = {**facets, **record_meta}
        for slug in fields:
            populated[slug] = populated.get(slug, 0) + 1
        if fields:
            meta_map[pk] = fields

    _p(f"Airtable: {seen} records scanned, {len(meta_map)} with metadata to patch.")
    # Per-field populated counts: a slug at 0 (or absent) means the Airtable column
    # name was wrong or empty — surfaced so a typo never silently no-ops.
    if populated:
        _p("  Populated per field (of records with any metadata):")
        for slug, n in sorted(populated.items()):
            _p(f"    {slug}: {n}")
    if unresolved:
        for col, n in sorted(unresolved.items()):
            _p(f"  ⚠ {col!r}: {n} record(s) still held raw record-ids after "
               f"resolution — DROPPED (not written). Check the PAT's meta-API scope "
               f"and that the linked records exist.")
    return meta_map


# Field types the backfill writes. The dquals-era index was created WITHOUT
# these fields, so before patching values we put_mapping them onto the live
# index — adding NEW fields to an existing index is always allowed (only type
# CHANGES are not). Without this, dynamic mapping would type the contact person
# as `text` and bare `term` facet filters would silently miss.
# People-name fields: keyword (exact filter + display) + an analyzed `.text`
# sub-field so they are name-searchable via BM25 (a bare keyword `match` won't
# tokenise "Jane Smith"). Populating `.text` needs NO re-embed — update_by_query
# reindexes each doc from _source, deriving the sub-field.
_PERSON_NAME_MAPPING: dict[str, Any] = {
    "type": "keyword",
    "fields": {"text": {"type": "text", "analyzer": "dalberg_english"}},
}
_BACKFILL_FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # People-name fields (name-searchable via .text). D.Quals has no PM/client-
    # contact column, so those glossary entries are intentionally absent.
    "dalberg_contact_person": _PERSON_NAME_MAPPING,  # multipleRecordLinks → names
    "dalberg_team_members": _PERSON_NAME_MAPPING,    # singleLineText, comma-joined
    # Surfaced-for-display / term-filter facets (no name search needed).
    "dalberg_entity": {"type": "keyword"},
    "insight_type": {"type": "keyword"},
    "total_fees_charged": {"type": "keyword"},
    "project_lenses": {"type": "keyword"},
    "project_description": {"type": "text", "index": False},
}

# Airtable column display name → clean OpenSearch facet slug. Only needed where
# slugify_column_name would produce an unwieldy key (e.g. the lenses question).
# Must mirror the read-path field names in retrieval `_FACET_FIELDS`.
_FACET_KEY_ALIASES: dict[str, str] = {
    "Which lenses were you intentional about applying to your project?": "project_lenses",
}


def _ensure_field_mappings(client: Any, index: str, *, dry_run: bool) -> None:
    """Declare the backfill fields on the live index (idempotent)."""
    resp = client.indices.get_mapping(index=index)
    props = next(iter(resp.values())).get("mappings", {}).get("properties", {})

    to_add: dict[str, dict[str, Any]] = {}
    for field, desired in _BACKFILL_FIELD_MAPPINGS.items():
        existing = props.get(field)
        if existing is None:
            to_add[field] = desired
        elif existing.get("type") != desired["type"]:
            _p(f"  ⚠ '{field}' already mapped as '{existing.get('type')}' "
               f"(wanted '{desired['type']}') — cannot change a live type; "
               f"term filters may need '{field}.keyword'.")
        else:
            # Same type, but the field may be missing a NEW multi-field sub-field
            # (e.g. adding `.text` to an existing keyword). Adding a sub-field to a
            # live mapping is allowed — put_mapping merges it. Existing docs get the
            # sub-field populated when update_by_query reindexes them below.
            missing_sub = set((desired.get("fields") or {})) - set((existing.get("fields") or {}))
            if missing_sub:
                to_add[field] = desired
                _p(f"  + '{field}': adding sub-field(s) {sorted(missing_sub)}.")
    if not to_add:
        _p("  mapping: backfill fields already declared.")
        return
    if dry_run:
        _p(f"  mapping: WOULD declare {sorted(to_add)} on '{index}'.")
        return
    client.indices.put_mapping(index=index, body={"properties": to_add})
    _p(f"  mapping: declared {sorted(to_add)} on '{index}'.")


def _existing_primary_keys(client: Any, index: str) -> set[str]:
    body = {
        "size": 0,
        "aggs": {
            "pks": {
                "terms": {"field": "primary_key", "size": 10000, "missing": "__none__"},
            }
        },
    }
    resp = client.search(index=index, body=body)
    buckets = resp.get("aggregations", {}).get("pks", {}).get("buckets", [])
    return {b["key"] for b in buckets if b["key"] and b["key"] != "__none__"}


def _update_by_query(
    *,
    client: Any,
    index: str,
    primary_key: str,
    fields: dict[str, Any],
    dry_run: bool,
) -> int:
    if not fields:
        return 0
    if dry_run:
        count = int(
            client.count(
                index=index,
                body={"query": {"term": {"primary_key": primary_key}}},
            ).get("count", 0)
        )
        return count

    body = {
        "query": {"term": {"primary_key": primary_key}},
        "script": {
            "lang": "painless",
            "source": (
                "for (entry in params.fields.entrySet()) "
                "{ ctx._source[entry.getKey()] = entry.getValue(); }"
            ),
            "params": {"fields": fields},
        },
    }
    resp = client.update_by_query(
        index=index, body=body, params={"refresh": "false", "conflicts": "proceed"}
    )
    return int(resp.get("updated", 0))


def _write_sidecars(
    *,
    s3: Any,
    bucket: str,
    s3_prefix: str,
    table_slug: str,
    meta_map: dict[str, dict[str, Any]],
    dry_run: bool,
) -> dict[str, int]:
    written = 0
    skipped = 0
    base_prefix = s3_prefix.rstrip("/")

    for pk, fields in meta_map.items():
        record_prefix = f"{base_prefix}/{table_slug}/{pk}/"
        paginator = s3.get_paginator("list_objects_v2")
        meta_keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=record_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if isinstance(key, str) and key.endswith("/.airtable_meta.json"):
                    meta_keys.append(key)

        if not meta_keys:
            skipped += 1
            continue

        for meta_key in meta_keys:
            existing: dict[str, Any] = {}
            try:
                resp = s3.get_object(Bucket=bucket, Key=meta_key)
                existing = json.loads(resp["Body"].read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                pass

            facets = {k: v for k, v in fields.items() if k != "project_description"}
            record_meta = {}
            if "project_description" in fields:
                record_meta["project_description"] = fields["project_description"]
            if facets:
                existing["facets"] = {**(existing.get("facets") or {}), **facets}
            if record_meta:
                existing["record_meta"] = {**(existing.get("record_meta") or {}), **record_meta}

            if not dry_run:
                s3.put_object(
                    Bucket=bucket,
                    Key=meta_key,
                    Body=json.dumps(existing, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
            written += 1

    return {"sidecars_written": written, "records_without_sidecars": skipped}


def main() -> None:
    args = parse_args()
    settings = load_settings()
    ingestion_settings = load_airtable_ingestion_settings(config_path=Path(args.config))
    target = ingestion_settings.target(args.target)

    # Merge extra backfill columns (e.g. "Dalberg Contact Person") into the
    # target's facet list — script-local; the deployed ingestion config is
    # untouched, so this run alone decides what gets backfilled.
    extra_cols = tuple(
        c.strip() for c in (args.extra_facet_columns or "").split(",") if c.strip()
    )
    new_cols = tuple(c for c in extra_cols if c not in target.facet_columns)
    if new_cols:
        import dataclasses  # noqa: PLC0415

        target = dataclasses.replace(
            target, facet_columns=(*target.facet_columns, *new_cols)
        )

    try:
        table_cfg = settings.tables.get(args.table)  # TableRegistry.get raises KeyError
    except KeyError as exc:
        _p(f"Unknown table '{args.table}' in tables.yaml: {exc}")
        sys.exit(1)
    index_name = table_cfg.index_name or settings.opensearch.index
    s3_prefix = table_cfg.s3_prefix or target.s3_prefix
    table_slug = slugify_table_name(target.table_name)

    _p("=" * 60)
    _p("  D.Quals record metadata backfill (no re-embed)")
    _p(f"  Target          : {target.name}  ({target.table_name})")
    _p(f"  OpenSearch idx  : {index_name}")
    _p(f"  S3 prefix       : {s3_prefix}")
    _p(f"  Mode            : {'DRY RUN' if args.dry_run else 'APPLY'}"
       f"{' + sidecars' if args.write_sidecars else ''}")
    _p("=" * 60)

    airtable = AirtableClient(
        pat_token=ingestion_settings.airtable_pat_token,
        timeout_seconds=ingestion_settings.defaults.request_timeout_seconds,
    )
    meta_map = _build_meta_map(airtable=airtable, target=target)
    if not meta_map:
        _p("No metadata resolved from Airtable — nothing to do.")
        sys.exit(1)

    os_client = build_opensearch_client(
        settings.opensearch.endpoint,
        username=settings.opensearch.username,
        password=settings.opensearch.password,
        aws_region=settings.aws_region,
    )

    # Declare the backfill fields on the live index BEFORE writing values, so
    # dalberg_contact_person is a proper keyword facet (term-filterable).
    _ensure_field_mappings(os_client, index_name, dry_run=args.dry_run)

    existing_pks = _existing_primary_keys(os_client, index_name)
    matched = {pk: meta_map[pk] for pk in existing_pks if pk in meta_map}
    _p(f"OpenSearch: {len(existing_pks)} primary_key values; {len(matched)} matched Airtable.")

    total_chunks = 0
    records_updated = 0
    for pk, fields in sorted(matched.items()):
        updated = _update_by_query(
            client=os_client,
            index=index_name,
            primary_key=pk,
            fields=fields,
            dry_run=args.dry_run,
        )
        if updated:
            records_updated += 1
            total_chunks += updated

    if not args.dry_run:
        os_client.indices.refresh(index=index_name)

    label = "would update" if args.dry_run else "updated"
    _p(f"OpenSearch: {records_updated} records, {total_chunks} chunks {label}.")

    if args.write_sidecars:
        s3 = s3_client(profile_name=args.profile)
        sidecar_stats = _write_sidecars(
            s3=s3,
            bucket=settings.s3.bucket,
            s3_prefix=s3_prefix,
            table_slug=table_slug,
            meta_map=meta_map,
            dry_run=args.dry_run,
        )
        _p(
            f"S3 sidecars: {sidecar_stats['sidecars_written']} written, "
            f"{sidecar_stats['records_without_sidecars']} records with no sidecars."
        )

    _p("Done.")


if __name__ == "__main__":
    main()
