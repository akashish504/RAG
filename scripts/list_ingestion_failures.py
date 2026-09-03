"""List stuck/dead ingestion jobs from the S3 failure ledger.

This is the interim "check for pipeline failures" mechanism (requirement
#3): every object under ``_pipeline_state/jobs/<target>/`` in S3 is a record
that has NOT yet fully succeeded — this is a thin CLI over
``PipelineStateStore.list_jobs()``.

Usage
-----
    # Everything not yet succeeded, any target:
    python scripts/list_ingestion_failures.py

    # Only jobs that hit WORKER_MAX_ATTEMPTS and stopped retrying:
    python scripts/list_ingestion_failures.py --status dead

    # Scope to one target:
    python scripts/list_ingestion_failures.py --target d_quals_sync --status failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.airtable_ingestion.config import load_airtable_ingestion_settings
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.common.aws import s3_client
from pipeline.common.state_store import PipelineStateStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List ingestion jobs stuck in the S3 failure ledger"
    )
    parser.add_argument(
        "--status",
        choices=["queued", "processing", "failed", "dead"],
        default=None,
        help="Only show jobs in this status (default: all).",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Only show jobs for this target (default: all targets).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw job records as JSON instead of a table.",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS profile (local only)")
    return parser.parse_args()


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> None:
    args = parse_args()

    settings = load_airtable_ingestion_settings()
    s3 = s3_client(region_name=settings.aws_region, profile_name=args.profile)
    uploader = S3Uploader(client=s3, bucket=settings.s3_bucket)
    state_store = PipelineStateStore(uploader=uploader)

    jobs = state_store.list_jobs(target=args.target, status=args.status)

    if args.json:
        print(json.dumps(jobs, indent=2))
        return

    if not jobs:
        _p("No matching jobs — nothing stuck.")
        return

    _p(f"{'TARGET':<20} {'RECORD ID':<20} {'STATUS':<12} {'ATTEMPTS':<9} UPDATED_AT / LAST_ERROR")
    for job in jobs:
        _p(
            f"{job.get('target', '?'):<20} "
            f"{job.get('record_id', '?'):<20} "
            f"{job.get('status', '?'):<12} "
            f"{job.get('attempts', '?'):<9} "
            f"{job.get('updated_at', '?')}"
        )
        if job.get("last_error"):
            _p(f"    ! {job['last_error']}")

    dead = sum(1 for j in jobs if j.get("status") == "dead")
    failed = sum(1 for j in jobs if j.get("status") == "failed")
    _p("")
    _p(f"{len(jobs)} job(s) total — {dead} dead, {failed} retrying, "
       f"{len(jobs) - dead - failed} other.")

    sys.exit(1 if dead else 0)


if __name__ == "__main__":
    main()
