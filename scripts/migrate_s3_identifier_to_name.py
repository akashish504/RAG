"""Migrate S3 raw layout from record-id folders to Name-based identifier folders.

Use when ingestion accidentally used the Airtable record id (``rec…``) as the S3
path segment instead of the configured ``identifier_column`` (e.g. ``Name``).

Layout today (wrong)::

    raw/knowledge_library/recabc123/attachments/recabc123__normalized.txt

Layout after migration::

    raw/knowledge_library/my_document_title/attachments/my_document_title__normalized.txt

The script:
  1. Loads records from Airtable for the chosen ingestion target.
  2. Builds ``normalize_identifier(record_id) → normalize_identifier(Name)`` pairs.
  3. Lists every object under the old prefix and copies to the new prefix.
  4. Renames ``{old_id}__normalized.txt`` files and patches ``.airtable_meta.json``.
  5. Deletes the old keys after a successful copy (unless ``--keep-source``).

Usage
-----
    # Preview moves (default — no writes)
    python scripts/migrate_s3_identifier_to_name.py --target knowledge_library_sync

    # Apply migration
    python scripts/migrate_s3_identifier_to_name.py --target knowledge_library_sync --apply

    # Only migrate folders that still exist under the old rec* path
    python scripts/migrate_s3_identifier_to_name.py --target knowledge_library_sync --apply --only-existing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
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
from pipeline.airtable_ingestion.normalizers import (
    normalize_identifier,
    slugify_column_name,
    slugify_table_name,
)
from botocore.exceptions import ClientError

from pipeline.common.aws import s3_client

_REC_FOLDER_RE = re.compile(r"^rec[a-z0-9]{10,}$")
_META_SUFFIX = ".airtable_meta.json"
_NORMALIZED_SUFFIX = "__normalized.txt"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


@dataclass
class MigrationPair:
    record_id: str
    raw_name: str
    old_segment: str
    new_segment: str
    old_prefix: str
    new_prefix: str


@dataclass
class MigrationReport:
    pairs_considered: int = 0
    pairs_with_objects: int = 0
    objects_copied: int = 0
    objects_deleted: int = 0
    objects_skipped_exists: int = 0
    errors: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate S3 raw paths from Airtable record-id folders to "
            "identifier_column (Name) folders."
        )
    )
    parser.add_argument(
        "--target",
        default="knowledge_library_sync",
        help="Ingestion target from config/airtable_ingestion.yaml "
        "(default: knowledge_library_sync).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable_ingestion.yaml.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform copy/delete. Without this flag, only prints the migration plan.",
    )
    parser.add_argument(
        "--only-existing",
        action="store_true",
        help="Only migrate pairs where the old S3 prefix currently has objects.",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Copy to new keys but do not delete the old keys (safe rerun).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional AWS credential profile (local dev only).",
    )
    return parser.parse_args()


def _iter_s3_keys(s3: Any, *, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if key and not key.endswith("/"):
                keys.append(key)
    return keys


def _prefix_has_objects(s3: Any, *, bucket: str, prefix: str) -> bool:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(resp.get("KeyCount"))


def _build_migration_pairs(
    *,
    airtable: AirtableClient,
    base_id: str,
    table_name: str,
    identifier_column: str,
    s3_prefix: str,
    table_slug: str,
) -> list[MigrationPair]:
    base = s3_prefix.rstrip("/")
    pairs: list[MigrationPair] = []
    seen_new: dict[str, str] = {}

    for record in airtable.iter_records(
        base_id=base_id,
        table_name=table_name,
        fields=[identifier_column],
        page_size=100,
    ):
        rec_id = record.get("id")
        fields = record.get("fields") or {}
        raw_name = fields.get(identifier_column)
        if not isinstance(rec_id, str) or not rec_id.startswith("rec"):
            continue
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue

        raw_name = raw_name.strip()
        old_segment = normalize_identifier(rec_id)
        new_segment = normalize_identifier(raw_name)
        if old_segment == new_segment:
            continue

        if new_segment in seen_new and seen_new[new_segment] != rec_id:
            _p(
                f"WARNING: duplicate normalized name {new_segment!r} for records "
                f"{seen_new[new_segment]} and {rec_id} — skipping {rec_id}"
            )
            continue
        seen_new[new_segment] = rec_id

        pairs.append(
            MigrationPair(
                record_id=rec_id,
                raw_name=raw_name,
                old_segment=old_segment,
                new_segment=new_segment,
                old_prefix=f"{base}/{table_slug}/{old_segment}/",
                new_prefix=f"{base}/{table_slug}/{new_segment}/",
            )
        )
    return pairs


def _remap_object_key(key: str, pair: MigrationPair) -> str | None:
    if not key.startswith(pair.old_prefix):
        return None
    suffix = key[len(pair.old_prefix) :]
    old_norm_name = f"{pair.old_segment}{_NORMALIZED_SUFFIX}"
    new_norm_name = f"{pair.new_segment}{_NORMALIZED_SUFFIX}"
    if suffix == old_norm_name or suffix.endswith(f"/{old_norm_name}"):
        suffix = suffix.replace(old_norm_name, new_norm_name)
    return pair.new_prefix + suffix


def _patch_meta_payload(payload: dict[str, Any], pair: MigrationPair, new_key: str) -> dict[str, Any]:
    out = dict(payload)
    out["identifier"] = pair.raw_name
    if isinstance(out.get("original_s3_key"), str):
        remapped = _remap_object_key(out["original_s3_key"], pair)
        if remapped:
            out["original_s3_key"] = remapped
    out["migrated_from_s3_segment"] = pair.old_segment
    out["migrated_to_s3_segment"] = pair.new_segment
    return out


def _copy_object(
    s3: Any,
    *,
    bucket: str,
    source_key: str,
    dest_key: str,
    pair: MigrationPair,
    apply: bool,
) -> None:
    if source_key.endswith(_META_SUFFIX):
        if not apply:
            return
        body = s3.get_object(Bucket=bucket, Key=source_key)["Body"].read()
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        patched = _patch_meta_payload(payload, pair, dest_key)
        s3.put_object(
            Bucket=bucket,
            Key=dest_key,
            Body=json.dumps(patched, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return

    if apply:
        s3.copy_object(
            Bucket=bucket,
            Key=dest_key,
            CopySource={"Bucket": bucket, "Key": source_key},
        )


def _migrate_pair(
    s3: Any,
    *,
    bucket: str,
    pair: MigrationPair,
    apply: bool,
    keep_source: bool,
    report: MigrationReport,
) -> None:
    keys = _iter_s3_keys(s3, bucket=bucket, prefix=pair.old_prefix)
    if not keys:
        return

    report.pairs_with_objects += 1
    _p(f"  {pair.old_segment} → {pair.new_segment!r}  ({len(keys)} object(s))")
    _p(f"    record {pair.record_id}  |  Name: {pair.raw_name!r}")

    for old_key in sorted(keys):
        new_key = _remap_object_key(old_key, pair)
        if not new_key:
            report.errors.append(f"could not remap key {old_key!r}")
            continue

        if old_key == new_key:
            continue

        try:
            s3.head_object(Bucket=bucket, Key=new_key)
            dest_exists = True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                dest_exists = False
            else:
                raise

        if dest_exists:
            report.objects_skipped_exists += 1
            _p(f"    SKIP (dest exists): {new_key}")
            continue

        _p(f"    {'COPY' if apply else 'PLAN'}: {old_key}")
        _p(f"         → {new_key}")

        try:
            _copy_object(
                s3,
                bucket=bucket,
                source_key=old_key,
                dest_key=new_key,
                pair=pair,
                apply=apply,
            )
            report.objects_copied += 1

            if apply and not keep_source:
                s3.delete_object(Bucket=bucket, Key=old_key)
                report.objects_deleted += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{old_key} → {new_key}: {exc}")
            _p(f"    ERROR: {exc}")


def _scan_orphan_rec_folders(
    s3: Any,
    *,
    bucket: str,
    table_prefix: str,
    known_old_segments: set[str],
) -> list[str]:
    """List rec* folder segments under the table prefix not covered by Airtable map."""

    orphans: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=table_prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            folder = cp.get("Prefix", "")
            segment = folder.rstrip("/").split("/")[-1]
            if _REC_FOLDER_RE.match(segment) and segment not in known_old_segments:
                orphans.append(segment)
    return sorted(orphans)


def main() -> None:
    args = parse_args()
    ingestion = load_airtable_ingestion_settings(config_path=Path(args.config))
    target = ingestion.target(args.target)
    table_slug = slugify_table_name(target.table_name)
    table_prefix = f"{target.s3_prefix.rstrip('/')}/{table_slug}/"

    _p("=" * 60)
    _p("  S3 identifier migration (record id → Name)")
    _p(f"  Target         : {target.name} ({target.table_name})")
    _p(f"  Identifier col : {target.identifier_column}")
    _p(f"  Table prefix   : s3://{ingestion.s3_bucket}/{table_prefix}")
    _p(f"  Mode           : {'APPLY' if args.apply else 'DRY RUN'}")
    if args.keep_source:
        _p("  Delete source  : no (--keep-source)")
    _p("=" * 60)

    airtable = AirtableClient(
        pat_token=ingestion.airtable_pat_token,
        timeout_seconds=ingestion.defaults.request_timeout_seconds,
    )
    pairs = _build_migration_pairs(
        airtable=airtable,
        base_id=target.database_id,
        table_name=target.table_name,
        identifier_column=target.identifier_column,
        s3_prefix=target.s3_prefix,
        table_slug=table_slug,
    )
    _p(f"Airtable: {len(pairs)} record(s) need id→name path change.")

    if not pairs:
        _p("Nothing to migrate.")
        return

    s3 = s3_client(profile_name=args.profile)
    bucket = ingestion.s3_bucket
    report = MigrationReport(pairs_considered=len(pairs))

    _p("")
    for pair in pairs:
        if args.only_existing and not _prefix_has_objects(s3, bucket=bucket, prefix=pair.old_prefix):
            continue
        _migrate_pair(
            s3,
            bucket=bucket,
            pair=pair,
            apply=args.apply,
            keep_source=args.keep_source,
            report=report,
        )

    orphans = _scan_orphan_rec_folders(
        s3,
        bucket=bucket,
        table_prefix=table_prefix,
        known_old_segments={p.old_segment for p in pairs},
    )
    if orphans:
        _p("")
        _p("Orphan rec* folders in S3 with no Airtable mapping (manual review):")
        for seg in orphans[:20]:
            _p(f"  - {table_prefix}{seg}/")
        if len(orphans) > 20:
            _p(f"  ... and {len(orphans) - 20} more")

    _p("")
    _p("Summary:")
    _p(f"  pairs considered     : {report.pairs_considered}")
    _p(f"  pairs with S3 objects: {report.pairs_with_objects}")
    _p(f"  objects {'copied' if args.apply else 'planned'}: {report.objects_copied}")
    if args.apply and not args.keep_source:
        _p(f"  objects deleted      : {report.objects_deleted}")
    _p(f"  skipped (dest exists): {report.objects_skipped_exists}")
    if report.errors:
        _p(f"  errors               : {len(report.errors)}")
        for err in report.errors[:10]:
            _p(f"    - {err}")

    if not args.apply:
        _p("")
        _p("Dry run complete. Re-run with --apply to execute.")

    _p("")
    _p("Done.")


if __name__ == "__main__":
    main()
