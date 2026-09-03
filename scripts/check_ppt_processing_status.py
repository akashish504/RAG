"""Check which d.quals records have fully processed PPT/deck files in S3.

A record is considered FULLY PROCESSED when:
  - Every deck/doc under it has a ``{att_id}__normalized.txt`` sibling, AND
  - The record directory contains a ``__record_summary.txt``

A record is PARTIALLY PROCESSED when some (but not all) attachments are done.
A record is UNPROCESSED when no normalized.txt files exist yet.

Usage
-----
    python scripts/check_ppt_processing_status.py

    # Filter to a specific identifier (project number / slug):
    python scripts/check_ppt_processing_status.py --filter-id some-identifier

    # Show only records with a specific status:
    python scripts/check_ppt_processing_status.py --status complete
    python scripts/check_ppt_processing_status.py --status partial
    python scripts/check_ppt_processing_status.py --status unprocessed

    # Machine-readable JSON output:
    python scripts/check_ppt_processing_status.py --json

    # Override the S3 prefix (default: raw/d.quals/):
    python scripts/check_ppt_processing_status.py --prefix raw/d.quals/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.common.aws import s3_client

_DECK_SUFFIXES = (".pptx", ".ppt")
_DOC_SUFFIXES = (".pdf", ".docx", ".doc", ".xlsx", ".xlsm")
_ALL_SUFFIXES = _DECK_SUFFIXES + _DOC_SUFFIXES

BUCKET = "claude-mcp-object-store"
DEFAULT_PREFIX = "raw/d.quals/"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check PPT processing status in S3")
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"S3 prefix to scan (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--bucket",
        default=BUCKET,
        help=f"S3 bucket name (default: {BUCKET})",
    )
    parser.add_argument(
        "--filter-id",
        default=None,
        help="Show only records matching this identifier substring",
    )
    parser.add_argument(
        "--status",
        choices=["complete", "partial", "unprocessed"],
        default=None,
        help="Filter output to records with this status only",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print results as JSON instead of human-readable text",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "eu-west-1"),
        help="AWS region (default: eu-west-1 or AWS_REGION env var)",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS credential profile")
    return parser.parse_args()


def scan_bucket(s3, bucket: str, prefix: str) -> dict:
    """
    Returns a dict keyed by record_dir (e.g. raw/d.quals/proj-123) with:
      {
        "attachments": {att_id: {"original": key, "normalized": key or None}},
        "has_record_summary": bool,
      }
    """
    paginator = s3.get_paginator("list_objects_v2")

    # Collect all keys in one pass — avoids per-file HEAD calls.
    all_keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            all_keys.append(obj["Key"])

    _p(f"  Listed {len(all_keys)} object(s) under s3://{bucket}/{prefix}")

    # An attachment is "done" when its OWN directory contains a *__normalized.txt.
    # True for BOTH layouts — {att_id}/{file} + {att_id}/{att_id}__normalized.txt
    # AND the flat {att_id}__{file} + {att_id}__normalized.txt — so we don't depend
    # on path depth (the old parent.parent assumption silently dropped records).
    dirs_with_normalized = {
        PurePosixPath(k).parent.as_posix()
        for k in all_keys
        if k.endswith("__normalized.txt")
    }

    pfx = prefix if prefix.endswith("/") else prefix + "/"

    # record_dir -> {attachments: {key: {...}}, has_record_summary}
    records: dict[str, dict] = defaultdict(lambda: {
        "attachments": {},
        "has_record_summary": False,
    })

    for key in all_keys:
        # Record identifier = FIRST path segment after the prefix. Robust to the
        # attachment sub-layout (the bug was deriving it via a fixed depth).
        rel = key[len(pfx):] if key.startswith(pfx) else key
        identifier = rel.split("/", 1)[0]
        if not identifier:
            continue
        record_dir = f"{pfx}{identifier}"

        if PurePosixPath(key).name == "__record_summary.txt":
            records[record_dir]["has_record_summary"] = True
            continue

        if not key.lower().endswith(_ALL_SUFFIXES):
            continue

        att_dir = PurePosixPath(key).parent.as_posix()
        records[record_dir]["attachments"][key] = {
            "original": key,
            "normalized": att_dir in dirs_with_normalized,
        }

    return dict(records)


def classify_record(info: dict) -> str:
    """Return 'complete', 'partial', or 'unprocessed'."""
    attachments = info["attachments"]
    if not attachments:
        return "unprocessed"

    done = sum(1 for a in attachments.values() if a["normalized"])
    total = len(attachments)

    if done == total and info["has_record_summary"]:
        return "complete"
    if done > 0:
        return "partial"
    return "unprocessed"


def main() -> None:
    args = parse_args()

    s3 = s3_client(region_name=args.region, profile_name=args.profile)

    _p("=" * 60)
    _p("  PPT Processing Status Check")
    _p(f"  Bucket  : {args.bucket}")
    _p(f"  Prefix  : {args.prefix}")
    _p("=" * 60)

    records = scan_bucket(s3, args.bucket, args.prefix)

    results = []
    for record_dir, info in sorted(records.items()):
        identifier = PurePosixPath(record_dir).name
        if args.filter_id and args.filter_id.lower() not in identifier.lower():
            continue

        status = classify_record(info)
        attachments = info["attachments"]
        done = sum(1 for a in attachments.values() if a["normalized"])
        total = len(attachments)

        results.append({
            "identifier": identifier,
            "record_dir": record_dir,
            "status": status,
            "attachments_total": total,
            "attachments_done": done,
            "has_record_summary": info["has_record_summary"],
            "pending": [
                att_id for att_id, a in attachments.items() if not a["normalized"]
            ],
        })

    # Apply status filter
    if args.status:
        results = [r for r in results if r["status"] == args.status]

    if args.json:
        print(json.dumps(results, indent=2))
        return

    # Human-readable output
    counts = {"complete": 0, "partial": 0, "unprocessed": 0}
    for r in results:
        counts[r["status"]] += 1

    status_symbol = {"complete": "✓", "partial": "~", "unprocessed": "✗"}

    for r in results:
        sym = status_symbol[r["status"]]
        line = (
            f"  [{sym}] {r['identifier']:<40}  "
            f"{r['attachments_done']}/{r['attachments_total']} files  "
            f"summary={'yes' if r['has_record_summary'] else 'no'}"
        )
        if r["status"] == "partial" and r["pending"]:
            pending_sample = r["pending"][:3]
            suffix = f" +{len(r['pending']) - 3} more" if len(r["pending"]) > 3 else ""
            line += f"  pending: {pending_sample}{suffix}"
        _p(line)

    _p("")
    _p("=" * 60)
    _p(f"  SUMMARY  (showing {len(results)} record(s))")
    _p(f"  Complete    (all files + summary) : {counts['complete']}")
    _p(f"  Partial     (some files done)     : {counts['partial']}")
    _p(f"  Unprocessed (no files done)       : {counts['unprocessed']}")
    _p("=" * 60)


if __name__ == "__main__":
    main()
