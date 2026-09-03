"""Backfill ``source_s3_key`` (the ORIGINAL document key) onto OpenSearch chunks.

Why this exists
---------------
The embedding pipeline indexes the NORMALIZED text key
(``…/{identifier}__normalized.txt``) as ``s3_key``. The original attachment
(e.g. ``…/{attachment_id}__{filename}.pdf``) lives in the same S3 folder but its
key is not stored in the index. S3 citations therefore have to LIST the folder
at query time to find the original document.

This script resolves each chunk's original document key ONCE (by listing the
folder, cached per folder) and writes it back as ``source_s3_key`` so:
  • the citation resolver uses it directly — no per-query S3 listing, and
  • the original document path is available in the response metadata.

No re-embedding: it patches existing documents in place via scroll + bulk update.

Usage
-----
    # Check what would change, no writes
    python scripts/backfill_source_s3_key.py --table dalberg_profiles --dry-run

    # Apply
    python scripts/backfill_source_s3_key.py --table dalberg_profiles

    # Another table
    python scripts/backfill_source_s3_key.py --table knowledge_library

    # Re-resolve and overwrite chunks that already have source_s3_key
    python scripts/backfill_source_s3_key.py --table dalberg_profiles --force
"""

from __future__ import annotations

import argparse
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

from pipeline.common.aws import list_s3_keys
from pipeline.common.opensearch import build_opensearch_client
from pipeline.config import load_settings

_SCROLL_BATCH = 200
_SCROLL_TTL = "2m"
_NORMALIZED_SUFFIX = "__normalized.txt"
_META_SUFFIX = ".airtable_meta.json"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill source_s3_key (original document key) on OpenSearch chunks."
    )
    parser.add_argument(
        "--table",
        default="dalberg_profiles",
        help="tables.yaml entry whose OpenSearch index will be patched.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-resolve and overwrite chunks that already have source_s3_key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional AWS credential profile (local development only).",
    )
    return parser.parse_args()


def _resolve_original_key(
    s3_key: str,
    *,
    bucket: str,
    region_name: str,
    profile_name: str | None,
    cache: dict[str, str],
) -> str:
    """Return the original document key for a normalized-text ``s3_key``.

    Lists the chunk's folder and picks the file that is neither the normalized
    text nor the Airtable sidecar. Falls back to ``s3_key`` (which exists) so a
    citation can never point at a missing object. Cached per folder.
    """
    if not s3_key.endswith(_NORMALIZED_SUFFIX):
        return s3_key  # already an original (e.g. a PDF indexed directly)
    key_dir = s3_key.rsplit("/", 1)[0] + "/"
    if key_dir in cache:
        return cache[key_dir]
    original = s3_key
    for key in list_s3_keys(
        key_dir, bucket=bucket, region_name=region_name, profile_name=profile_name
    ):
        leaf = key.rsplit("/", 1)[-1]
        if leaf.endswith(_NORMALIZED_SUFFIX) or leaf.endswith(_META_SUFFIX):
            continue
        original = key
        break
    cache[key_dir] = original
    return original


def _count_missing(client: Any, index: str) -> dict[str, int]:
    total = int(client.count(index=index, body={"query": {"match_all": {}}}).get("count", 0))
    without = int(
        client.count(
            index=index,
            body={"query": {"bool": {"must_not": [{"exists": {"field": "source_s3_key"}}]}}},
        ).get("count", 0)
    )
    return {"total": total, "without": without, "with": total - without}


def _scroll_bulk_update(
    *,
    client: Any,
    index: str,
    default_bucket: str,
    region_name: str,
    profile_name: str | None,
    force: bool,
    dry_run: bool,
) -> dict[str, int]:
    if force:
        query: dict[str, Any] = {"match_all": {}}
    else:
        query = {"bool": {"must_not": [{"exists": {"field": "source_s3_key"}}]}}

    if dry_run:
        count = int(client.count(index=index, body={"query": query}).get("count", 0))
        return {"scrolled": count, "updated": 0, "failed": 0, "batches": 0, "dry_run": True}

    resp = client.search(
        index=index,
        scroll=_SCROLL_TTL,
        body={"size": _SCROLL_BATCH, "query": query, "_source": ["s3_key", "s3_bucket"]},
    )
    scroll_id = resp.get("_scroll_id")
    hits = resp.get("hits", {}).get("hits", [])

    dir_cache: dict[str, str] = {}
    total_scrolled = total_updated = total_failed = batch_num = 0

    try:
        while hits:
            batch_num += 1
            actions = []
            for doc in hits:
                src = doc.get("_source") or {}
                s3_key = src.get("s3_key")
                bucket = src.get("s3_bucket") or default_bucket
                if not s3_key or not bucket:
                    continue
                original = _resolve_original_key(
                    s3_key,
                    bucket=bucket,
                    region_name=region_name,
                    profile_name=profile_name,
                    cache=dir_cache,
                )
                actions.append({
                    "_op_type": "update",
                    "_index": doc["_index"],
                    "_id": doc["_id"],
                    "doc": {"source_s3_key": original},
                })

            total_scrolled += len(hits)
            if actions:
                success, errors = opensearch_bulk(
                    client, actions, raise_on_error=False, raise_on_exception=False
                )
                total_updated += success
                total_failed += len(errors) if isinstance(errors, list) else 0
                _p(f"  batch {batch_num:>3}: {len(hits):>4} scrolled, {success:>4} updated"
                   + (f", {len(errors)} failed" if errors else ""))

            resp = client.scroll(scroll_id=scroll_id, scroll=_SCROLL_TTL)
            scroll_id = resp.get("_scroll_id")
            hits = resp.get("hits", {}).get("hits", [])
    finally:
        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:  # noqa: BLE001
                pass

    client.indices.refresh(index=index)
    return {
        "scrolled": total_scrolled,
        "updated": total_updated,
        "failed": total_failed,
        "batches": batch_num,
        "folders_listed": len(dir_cache),
    }


def main() -> None:
    args = parse_args()
    settings = load_settings()
    table_cfg = settings.tables.get(args.table)
    if table_cfg is None:
        _p(f"Unknown table {args.table!r}. Known: {sorted(settings.tables)}")
        sys.exit(1)
    index_name = table_cfg.index_name or settings.opensearch.index
    default_bucket = settings.s3.bucket

    _p("=" * 60)
    _p("  source_s3_key backfill (metadata only — no re-embed)")
    _p(f"  Table / index : {args.table} → {index_name}")
    _p(f"  Bucket        : {default_bucket}")
    _p(f"  Mode          : {'DRY RUN' if args.dry_run else 'APPLY'}"
       f"{' (force overwrite)' if args.force else ''}")
    _p("=" * 60)

    client = build_opensearch_client(
        settings.opensearch.endpoint,
        username=settings.opensearch.username,
        password=settings.opensearch.password,
        aws_region=settings.aws_region,
    )

    before = _count_missing(client, index_name)
    _p(f"before: {before['total']} chunks, {before['with']} with source_s3_key, "
       f"{before['without']} without")

    if before["without"] == 0 and not args.force:
        _p("  → All chunks already have source_s3_key. Nothing to do (use --force to re-resolve).")
        return

    report = _scroll_bulk_update(
        client=client,
        index=index_name,
        default_bucket=default_bucket,
        region_name=settings.aws_region,
        profile_name=args.profile,
        force=args.force,
        dry_run=args.dry_run,
    )

    _p("")
    if report.get("dry_run"):
        _p(f"DRY RUN: {report['scrolled']} chunks would be updated.")
    else:
        after = _count_missing(client, index_name)
        _p(f"updated {report['updated']} chunks across {report['batches']} batches "
           f"({report.get('folders_listed', 0)} S3 folders listed; {report['failed']} failed).")
        _p(f"after: {after['with']} with source_s3_key, {after['without']} without")
    _p("Done.")


if __name__ == "__main__":
    main()
