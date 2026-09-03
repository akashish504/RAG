"""Poll Airtable for changed attachment columns and enqueue them to SQS.

Usage
-----
    # Poll every target with poll_enabled: true in config/airtable_ingestion.yaml
    # (today this is only d_quals_sync):
    python scripts/run_poller.py

    # Explicit single-target override (ignores poll_enabled, polls this one anyway):
    python scripts/run_poller.py --target d_quals_sync

    # Dry run: print matched record IDs + the formula used, enqueue nothing,
    # advance no cursor:
    python scripts/run_poller.py --dry-run

Meant to be invoked weekly by cron (Saturday 09:00 server time; registered by
scripts/reset_deployment.sh as a host crontab entry running
``docker compose run --rm pipeline python scripts/run_poller.py``)
— this is a one-shot script, not a long-running process. See
scripts/run_worker.py for the SQS consumer half.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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

import os

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.config import (
    DEFAULT_INGESTION_CONFIG_PATH,
    load_airtable_ingestion_settings,
)
from pipeline.airtable_ingestion.models import IngestionTargetConfig
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.common.aws import s3_client, sqs_client
from pipeline.common.state_store import PipelineStateStore
from pipeline.queue.producer import SqsProducer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Airtable for changed attachment columns and enqueue to SQS"
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Poll exactly this target (overrides poll_enabled). "
        "Omit to poll every target with poll_enabled: true.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable ingestion config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matched record IDs + the formula used. Enqueues nothing "
        "and does not advance the cursor.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary instead of human-readable text.",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS profile (local only)")
    return parser.parse_args()


def _p(msg: str = "") -> None:
    """Print with immediate flush — essential for cron / non-TTY sessions."""
    print(msg, flush=True)


def _poll_one_target(
    *,
    target: IngestionTargetConfig,
    airtable: AirtableClient,
    state_store: PipelineStateStore,
    producer: SqsProducer | None,
    dry_run: bool,
) -> dict:
    cursor = state_store.get_cursor(target.name)
    watch_fields = list(target.attachment_columns)

    changed_ids: list[str] = []
    for record in airtable.iter_changed_records(
        base_id=target.database_id,
        table_name=target.table_name,
        watch_fields=watch_fields,
        since_iso=cursor,
    ):
        record_id = record["id"]
        changed_ids.append(record_id)
        if not dry_run:
            producer.send({"target": target.name, "record_id": record_id})

    if not dry_run:
        state_store.set_cursor(target.name, None)

    return {
        "target": target.name,
        "watch_fields": watch_fields,
        "cursor_before": cursor,
        "matched": changed_ids,
        "enqueued": 0 if dry_run else len(changed_ids),
        "cursor_after": None if dry_run else state_store.get_cursor(target.name),
    }


def main() -> None:
    args = parse_args()

    settings = load_airtable_ingestion_settings(config_path=Path(args.config))

    if args.target:
        targets = [settings.target(args.target)]
    else:
        targets = [t for t in settings.targets if t.poll_enabled]

    if not targets:
        _p("No poll_enabled targets found in config — nothing to do.")
        sys.exit(0)

    s3 = s3_client(region_name=settings.aws_region, profile_name=args.profile)
    uploader = S3Uploader(client=s3, bucket=settings.s3_bucket)
    state_store = PipelineStateStore(uploader=uploader)
    airtable = AirtableClient(
        pat_token=settings.airtable_pat_token,
        timeout_seconds=settings.defaults.request_timeout_seconds,
    )

    producer: SqsProducer | None = None
    if not args.dry_run:
        queue_url = (os.environ.get("SQS_QUEUE_URL") or "").strip()
        if not queue_url:
            _p("ERROR: SQS_QUEUE_URL is not set in the environment.")
            sys.exit(1)
        sqs = sqs_client(region_name=settings.aws_region, profile_name=args.profile)
        producer = SqsProducer(client=sqs, queue_url=queue_url)

    _p("=" * 60)
    _p("  Airtable Poller" + ("  (DRY RUN)" if args.dry_run else ""))
    _p(f"  Targets : {', '.join(t.name for t in targets)}")
    _p(f"  Started : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _p("=" * 60)

    results = []
    for target in targets:
        result = _poll_one_target(
            target=target,
            airtable=airtable,
            state_store=state_store,
            producer=producer,
            dry_run=args.dry_run,
        )
        results.append(result)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    for result in results:
        _p("")
        _p(f"  Target        : {result['target']}")
        _p(f"  Watch fields  : {', '.join(result['watch_fields'])}")
        _p(f"  Cursor before : {result['cursor_before'] or '(none — bootstrap run)'}")
        _p(f"  Matched       : {len(result['matched'])} record(s)")
        for record_id in result["matched"][:20]:
            _p(f"    - {record_id}")
        if len(result["matched"]) > 20:
            _p(f"    … and {len(result['matched']) - 20} more")
        if args.dry_run:
            _p("  Enqueued      : 0 (dry run — cursor not advanced)")
        else:
            _p(f"  Enqueued      : {result['enqueued']}")
            _p(f"  Cursor after  : {result['cursor_after']}")

    _p("")
    _p("=" * 60)
    _p("  DONE")
    _p("=" * 60)

    if not args.dry_run:
        # Exact-match token for the CloudWatch heartbeat metric filter
        # (alarm treats missing data as breaching): if this line does not
        # appear within the weekly cadence, the poll silently failed.
        # Dry runs and --json runs (line is absent above the JSON) don't
        # count as heartbeats.
        total_matched = sum(len(r["matched"]) for r in results)
        total_enqueued = sum(r["enqueued"] for r in results)
        _p(
            f"PIPELINE_RUN_COMPLETED component=poller status=success "
            f"targets={len(results)} matched={total_matched} enqueued={total_enqueued}"
        )


if __name__ == "__main__":
    main()
