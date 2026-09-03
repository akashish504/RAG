"""Metadata-only backfill of ``airtable_record_id`` on existing OpenSearch chunks.

Why this exists
---------------
``CitationResolver`` builds Airtable citation URLs from the indexed
``airtable_record_id`` field. Chunks indexed **before** the Airtable sidecar
(``.airtable_meta.json``) feature shipped have ``primary_key`` (the lowercased
email) but no record id, so every citation lookup falls back to a runtime
Airtable API call.

Re-running the full ingestion + embed pipeline just to populate three
metadata fields is wasteful (re-downloads attachments, re-parses, re-embeds).
This script does the minimum: read Airtable once, build a
``lowercase_email → record_id`` map, then patch existing OpenSearch documents
in place using scroll + bulk update. It always prints a verification summary
(chunks with vs. without the field) before and after, so you can tell at a
glance whether anything was done.

Optional ``--write-sidecars`` also uploads ``.airtable_meta.json`` next to each
attachment folder in S3, so any future re-embed naturally picks up the same
record id without needing to re-run this script.

Usage
-----
    # Check current state without modifying anything
    python scripts/backfill_airtable_record_ids.py --dry-run

    # Apply updates to OpenSearch
    python scripts/backfill_airtable_record_ids.py

    # Apply + also write .airtable_meta.json sidecars in S3
    python scripts/backfill_airtable_record_ids.py --write-sidecars

    # Use a different ingestion target / table
    python scripts/backfill_airtable_record_ids.py --target profiles_sync --table dalberg_profiles

    # Force overwrite chunks that already have airtable_record_id (e.g. after
    # an email was reassigned in Airtable and the record_id changed)
    python scripts/backfill_airtable_record_ids.py --force
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

from opensearchpy.helpers import bulk as opensearch_bulk

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.config import (
    DEFAULT_INGESTION_CONFIG_PATH,
    load_airtable_ingestion_settings,
)
from pipeline.airtable_ingestion.normalizers import (
    normalize_identifier,
    slugify_column_name,
    slugify_table_name,
)
from pipeline.common.aws import s3_client
from pipeline.common.opensearch import build_opensearch_client
from pipeline.config import load_settings

DISPLAY_NAME_FIELD = "Display Name"
_SCROLL_BATCH = 200
_SCROLL_TTL = "2m"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill airtable_record_id (and base/table ids) onto existing "
            "OpenSearch chunks without re-embedding."
        )
    )
    parser.add_argument(
        "--target",
        default="profiles_sync",
        help="Airtable ingestion target name (default: profiles_sync).",
    )
    parser.add_argument(
        "--table",
        default="dalberg_profiles",
        help="tables.yaml entry whose OpenSearch index will be patched (default: dalberg_profiles).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable_ingestion.yaml.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also overwrite chunks that already have airtable_record_id.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve records and report what would change, but do not write.",
    )
    parser.add_argument(
        "--write-sidecars",
        action="store_true",
        help="Also upload .airtable_meta.json next to each attachment folder in S3.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional AWS credential profile (local development only).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------


def _build_record_map(
    *,
    airtable: AirtableClient,
    base_id: str,
    table_name: str,
    identifier_column: str,
) -> dict[str, dict[str, Any]]:
    """``lowercase_email → {record_id, raw_identifier, display_name}``."""

    record_map: dict[str, dict[str, Any]] = {}
    fields = [identifier_column, DISPLAY_NAME_FIELD]
    seen = 0
    for record in airtable.iter_records(
        base_id=base_id,
        table_name=table_name,
        fields=fields,
        page_size=100,
    ):
        seen += 1
        rec_id = record.get("id")
        row_fields = record.get("fields") or {}
        raw_identifier = row_fields.get(identifier_column)
        if not isinstance(raw_identifier, str) or not raw_identifier.strip():
            continue
        if not isinstance(rec_id, str) or not rec_id.startswith("rec"):
            continue
        key = raw_identifier.strip().lower()
        display = row_fields.get(DISPLAY_NAME_FIELD)
        record_map[key] = {
            "record_id": rec_id,
            "raw_identifier": raw_identifier.strip(),
            "display_name": display if isinstance(display, str) else None,
        }
    _p(f"Airtable: {seen} records scanned, {len(record_map)} usable identifiers.")
    return record_map


# ---------------------------------------------------------------------------
# OpenSearch helpers
# ---------------------------------------------------------------------------


def _existing_primary_keys(client: Any, index: str) -> set[str]:
    """Distinct ``primary_key`` values currently in the index."""

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


def _verify_backfill_status(
    client: Any,
    index: str,
    pk_list: list[str],
) -> dict[str, int]:
    """Count chunks that have vs. are missing ``airtable_record_id`` for matched PKs.

    Returns ``{"with_id": N, "without_id": N, "total": N}``.
    """
    if not pk_list:
        return {"with_id": 0, "without_id": 0, "total": 0}

    terms_clause = {"terms": {"primary_key": pk_list}}

    with_resp = client.count(
        index=index,
        body={
            "query": {
                "bool": {
                    "must": [terms_clause, {"exists": {"field": "airtable_record_id"}}],
                }
            }
        },
    )
    without_resp = client.count(
        index=index,
        body={
            "query": {
                "bool": {
                    "must": [terms_clause],
                    "must_not": [{"exists": {"field": "airtable_record_id"}}],
                }
            }
        },
    )
    with_count = int(with_resp.get("count", 0))
    without_count = int(without_resp.get("count", 0))
    return {"with_id": with_count, "without_id": without_count, "total": with_count + without_count}


def _print_verify(label: str, status: dict[str, int]) -> None:
    _p(f"Verification ({label}):")
    _p(f"  chunks with    airtable_record_id : {status['with_id']}")
    _p(f"  chunks without airtable_record_id : {status['without_id']}")
    _p(f"  total chunks for matched profiles : {status['total']}")


def _scroll_bulk_update(
    *,
    client: Any,
    index: str,
    base_id: str,
    table_id: str,
    matched_pks: dict[str, str],
    force: bool,
    dry_run: bool,
) -> dict[str, int]:
    """Scroll over docs that need the field and bulk-update them in batches.

    Uses explicit doc-level updates (``_update`` with ``doc``), no Painless
    script required — avoids params-size limits and request timeouts.
    """
    pk_list = list(matched_pks.keys())
    if not pk_list:
        return {"scrolled": 0, "updated": 0, "failed": 0, "batches": 0}

    if force:
        query: dict[str, Any] = {"terms": {"primary_key": pk_list}}
    else:
        query = {
            "bool": {
                "must": [{"terms": {"primary_key": pk_list}}],
                "must_not": [{"exists": {"field": "airtable_record_id"}}],
            }
        }

    if dry_run:
        count = int(client.count(index=index, body={"query": query}).get("count", 0))
        return {"scrolled": count, "updated": 0, "failed": 0, "batches": 0, "dry_run": True}

    # Open scroll
    resp = client.search(
        index=index,
        scroll=_SCROLL_TTL,
        body={"size": _SCROLL_BATCH, "query": query, "_source": ["primary_key"]},
    )
    scroll_id = resp.get("_scroll_id")
    hits = resp.get("hits", {}).get("hits", [])

    total_scrolled = 0
    total_updated = 0
    total_failed = 0
    batch_num = 0

    try:
        while hits:
            batch_num += 1
            actions = []
            for doc in hits:
                pk = (doc.get("_source") or {}).get("primary_key")
                record_id = matched_pks.get(pk) if pk else None
                if not record_id:
                    continue
                actions.append({
                    "_op_type": "update",
                    "_index": doc["_index"],
                    "_id": doc["_id"],
                    "doc": {
                        "airtable_record_id": record_id,
                        "airtable_base_id": base_id,
                        "airtable_table_id": table_id,
                    },
                })

            total_scrolled += len(hits)
            if actions:
                success, errors = opensearch_bulk(
                    client,
                    actions,
                    raise_on_error=False,
                    raise_on_exception=False,
                )
                total_updated += success
                total_failed += len(errors) if isinstance(errors, list) else 0
                _p(f"  batch {batch_num:>3}: {len(hits):>4} docs scrolled, {success:>4} updated"
                   + (f", {len(errors)} failed" if errors else ""))

            # Next scroll page
            resp = client.scroll(scroll_id=scroll_id, scroll=_SCROLL_TTL)
            scroll_id = resp.get("_scroll_id")
            hits = resp.get("hits", {}).get("hits", [])

    finally:
        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:  # noqa: BLE001
                pass

    # Refresh so the updated values are immediately visible to search/count
    client.indices.refresh(index=index)

    return {
        "scrolled": total_scrolled,
        "updated": total_updated,
        "failed": total_failed,
        "batches": batch_num,
    }


# ---------------------------------------------------------------------------
# S3 sidecar helpers
# ---------------------------------------------------------------------------


def _write_sidecars(
    *,
    s3: Any,
    bucket: str,
    s3_prefix: str,
    table_slug: str,
    attachment_columns: tuple[str, ...],
    base_id: str,
    table_id: str,
    record_map: dict[str, dict[str, Any]],
    dry_run: bool,
) -> dict[str, int]:
    """Upload ``.airtable_meta.json`` next to each attachment folder in S3.

    Only writes for identifier/column folders that actually exist in S3, so we
    do not litter the bucket with sidecars for profiles that were never ingested.
    """
    written = 0
    skipped_no_folder = 0
    base_prefix = s3_prefix.rstrip("/")

    for raw_email, info in record_map.items():
        identifier = normalize_identifier(info["raw_identifier"])
        for column in attachment_columns:
            col_slug = slugify_column_name(column)
            key_dir = f"{base_prefix}/{table_slug}/{identifier}/{col_slug}"
            listing = s3.list_objects_v2(Bucket=bucket, Prefix=f"{key_dir}/", MaxKeys=1)
            if not listing.get("KeyCount"):
                skipped_no_folder += 1
                continue
            meta_key = f"{key_dir}/.airtable_meta.json"
            payload = {
                "airtable_record_id": info["record_id"],
                "airtable_base_id": base_id,
                "airtable_table_id": table_id,
                "identifier": info["raw_identifier"],
                "column_name": column,
            }
            if not dry_run:
                s3.put_object(
                    Bucket=bucket,
                    Key=meta_key,
                    Body=json.dumps(payload, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
            written += 1
    return {"sidecars_written": written, "sidecars_skipped_no_folder": skipped_no_folder}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    settings = load_settings()
    ingestion_settings = load_airtable_ingestion_settings(config_path=Path(args.config))
    target = ingestion_settings.target(args.target)

    table_cfg = settings.tables.get(args.table)
    index_name = table_cfg.index_name or settings.opensearch.index

    _p("=" * 60)
    _p("  Airtable record_id backfill (metadata only)")
    _p(f"  Target          : {target.name}  ({target.database_name} / {target.table_name})")
    _p(f"  Base / table id : {target.database_id} / {target.table_id}")
    _p(f"  Identifier col  : {target.identifier_column}")
    _p(f"  OpenSearch idx  : {index_name}")
    _p(f"  Mode            : {'DRY RUN' if args.dry_run else 'APPLY'}"
       f"{' + sidecars' if args.write_sidecars else ''}"
       f"{' (force overwrite)' if args.force else ''}")
    _p("=" * 60)

    airtable = AirtableClient(
        pat_token=ingestion_settings.airtable_pat_token,
        timeout_seconds=ingestion_settings.defaults.request_timeout_seconds,
    )
    record_map = _build_record_map(
        airtable=airtable,
        base_id=target.database_id,
        table_name=target.table_name,
        identifier_column=target.identifier_column,
    )
    if not record_map:
        _p("No usable Airtable records found — nothing to do.")
        sys.exit(1)

    os_client = build_opensearch_client(
        settings.opensearch.endpoint,
        username=settings.opensearch.username,
        password=settings.opensearch.password,
        aws_region=settings.aws_region,
    )

    existing_pks = _existing_primary_keys(os_client, index_name)
    _p(f"OpenSearch: {len(existing_pks)} distinct primary_key values in '{index_name}'.")

    matched_pks = {pk: record_map[pk]["record_id"] for pk in existing_pks if pk in record_map}
    missing_in_airtable = sorted(existing_pks - record_map.keys())
    missing_in_opensearch = sorted(record_map.keys() - existing_pks)
    _p(f"  matched          : {len(matched_pks)}")
    _p(f"  index pk → no airtable row : {len(missing_in_airtable)}")
    _p(f"  airtable row → no index    : {len(missing_in_opensearch)}")
    if missing_in_airtable[:5]:
        _p(f"    sample missing in airtable: {missing_in_airtable[:5]}")

    pk_list = list(matched_pks.keys())

    # ---- Verification: current state before any writes --------------------
    _p("")
    before = _verify_backfill_status(os_client, index_name, pk_list)
    _print_verify("before", before)

    if before["without_id"] == 0 and not args.force:
        _p("  → All matched profiles already have airtable_record_id. Nothing to update.")
        _p("    Re-run with --force to overwrite existing values.")
        _p("")
        _p("Done.")
        return

    # ---- Scroll + bulk update ---------------------------------------------
    _p("")
    if args.dry_run:
        _p("Dry-run: counting docs that would be updated (no changes written).")
    else:
        _p("Updating chunks (scroll + bulk)...")

    update_report = _scroll_bulk_update(
        client=os_client,
        index=index_name,
        base_id=target.database_id,
        table_id=target.table_id,
        matched_pks=matched_pks,
        force=args.force,
        dry_run=args.dry_run,
    )

    _p("")
    _p("Update result:")
    _p(f"  docs scrolled  : {update_report.get('scrolled', 0)}")
    if args.dry_run:
        _p("  (dry-run — no documents modified)")
    else:
        _p(f"  docs updated   : {update_report.get('updated', 0)}")
        _p(f"  docs failed    : {update_report.get('failed', 0)}")
        _p(f"  batches        : {update_report.get('batches', 0)}")

    # ---- Verification: state after writes ---------------------------------
    if not args.dry_run:
        _p("")
        after = _verify_backfill_status(os_client, index_name, pk_list)
        _print_verify("after", after)
        if after["without_id"] == 0:
            _p("  → Backfill complete: all matched profiles now have airtable_record_id.")
        else:
            _p(f"  → WARNING: {after['without_id']} chunks still missing airtable_record_id.")
            _p("    Check for errors above or re-run with --force.")

    # ---- Optional S3 sidecars --------------------------------------------
    if args.write_sidecars:
        s3 = s3_client(region_name=settings.aws_region, profile_name=args.profile)
        table_slug = slugify_table_name(target.table_name)
        sidecar_report = _write_sidecars(
            s3=s3,
            bucket=ingestion_settings.s3_bucket,
            s3_prefix=target.s3_prefix,
            table_slug=table_slug,
            attachment_columns=target.attachment_columns,
            base_id=target.database_id,
            table_id=target.table_id,
            record_map={k: v for k, v in record_map.items() if k in matched_pks},
            dry_run=args.dry_run,
        )
        _p("")
        _p("S3 sidecar result:")
        _p(f"  sidecars written : {sidecar_report['sidecars_written']}"
           f"{' (dry-run)' if args.dry_run else ''}")
        _p(f"  folders missing  : {sidecar_report['sidecars_skipped_no_folder']}")

    _p("")
    _p("Done.")


if __name__ == "__main__":
    main()
