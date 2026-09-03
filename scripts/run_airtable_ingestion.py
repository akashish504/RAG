"""Run Airtable attachment ingestion for one target.

Usage
-----
    # Human-readable output (default):
    python scripts/run_airtable_ingestion.py --target profiles_sync

    # Machine-readable JSON output:
    python scripts/run_airtable_ingestion.py --target profiles_sync --json
"""

from __future__ import annotations

import argparse
import json
import os
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

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.config import (
    DEFAULT_INGESTION_CONFIG_PATH,
    load_airtable_ingestion_settings,
)
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline
from pipeline.airtable_ingestion.record_summary import RecordSummarizer
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.common.aws import s3_client
from pipeline.preprocessing.normalizers.registry import get_normalizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Airtable attachments into S3 raw layout")
    parser.add_argument(
        "--target",
        default="profiles_sync",
        help="Target name from config/airtable_ingestion.yaml (default: profiles_sync)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable ingestion config",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final report as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: upload originals + sidecars only; defer extraction and "
        "the record summary to scripts/run_batch_extraction.py (no inline Claude).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Process this many records concurrently (default 1). Records are "
        "independent; render (subprocess) and Claude calls (I/O) parallelize well. "
        "Try 6-8 to speed up a large sync run.",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS profile (local only)")
    return parser.parse_args()


def _p(msg: str = "") -> None:
    """Print with immediate flush — essential for SSM / non-TTY sessions."""
    print(msg, flush=True)


def main() -> None:
    args = parse_args()

    settings = load_airtable_ingestion_settings(config_path=Path(args.config))
    target = settings.target(args.target)
    if not target.enabled:
        _p(f"ERROR: Target '{target.name}' is disabled in config.")
        sys.exit(1)

    _p("=" * 60)
    _p(f"  Airtable Ingestion Pipeline")
    _p(f"  Target  : {target.name}  ({target.database_name})")
    _p(f"  Table   : {target.table_name}")
    _p(f"  S3 dest : s3://{settings.s3_bucket}/{target.s3_prefix}/")
    _p(f"  Started : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _p("=" * 60)

    s3 = s3_client(region_name=settings.aws_region, profile_name=args.profile)
    uploader = S3Uploader(client=s3, bucket=settings.s3_bucket)
    airtable = AirtableClient(
        pat_token=settings.airtable_pat_token,
        timeout_seconds=settings.defaults.request_timeout_seconds,
    )
    # Build default normalizer (fallback for columns without per-column config)
    normalizer = get_normalizer(target.normalizer)
    if target.normalizer:
        _p(f"  Normalizer (default): {target.normalizer}")

    # Build per-column normalizer map from attachment_column_normalizers config
    column_normalizers: dict = {}
    for col_name, norm_name in target.attachment_column_normalizers:
        column_normalizers[col_name] = get_normalizer(norm_name)
        _p(f"  Normalizer for '{col_name}': {norm_name or 'passthrough'}")

    # Record-level summarizer (one parent summary per multi-file record). Built
    # only when an Anthropic key is available; absent → multi-file records fall
    # back to concatenated per-file summaries and single-file records reuse theirs.
    record_summarizer = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        record_summarizer = RecordSummarizer(api_key=api_key)

    pipeline = AirtableAttachmentIngestionPipeline(
        airtable=airtable,
        uploader=uploader,
        metadata_local_dir=settings.defaults.metadata_local_dir,
        metadata_s3_prefix=settings.defaults.metadata_s3_prefix,
        upload_schema_to_s3_enabled=settings.defaults.upload_schema_to_s3,
        page_size=settings.defaults.page_size,
        normalizer=normalizer,
        column_normalizers=column_normalizers,
        record_summarizer=record_summarizer,
        batch_mode=args.batch,
        workers=args.workers,
    )

    report = pipeline.run_target(target=target)

    if args.json:
        print(
            json.dumps(
                {
                    "target": report.target_name,
                    "records_seen": report.records_seen,
                    "records_skipped_no_identifier": report.records_skipped_no_identifier,
                    "attachment_fields_seen": report.attachment_fields_seen,
                    "attachments_downloaded": report.attachments_downloaded,
                    "attachments_uploaded": report.attachments_uploaded,
                    "attachments_skipped": report.attachments_skipped,
                    "normalizations_done": report.normalizations_done,
                    "record_summaries_done": report.record_summaries_done,
                    "attachments_unextracted": report.attachments_unextracted,
                    "text_fields_uploaded": report.text_fields_uploaded,
                    "text_fields_skipped": report.text_fields_skipped,
                    "errors": report.errors,
                    "started_at": report.started_at.isoformat(),
                    "finished_at": report.finished_at.isoformat() if report.finished_at else None,
                },
                indent=2,
            )
        )
    else:
        _p("")
        status = "COMPLETED" if not report.errors else f"COMPLETED WITH {len(report.errors)} ERROR(S)"
        _p("=" * 60)
        _p(f"  {status}")
        _p("=" * 60)

    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
