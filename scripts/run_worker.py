"""Long-running SQS consumer: ingest one Airtable record, then embed its keys.

Usage
-----
    # Run forever, long-polling SQS (the normal deployment mode):
    python scripts/run_worker.py

    # Process at most one receive_message batch, then exit (used for the
    # foreground single-record verification pass — see the plan's step 2):
    python scripts/run_worker.py --once

Composition
-----------
Stage 1 (per SQS message): fetch the one Airtable record named in the
message, then ``AirtableAttachmentIngestionPipeline.ingest_one_record()``
(same idempotent per-record logic ``run_target()`` uses for full syncs — no
changes to it here).

Stage 2 (per S3 key returned by stage 1): ``Pipeline.run_one(key)``, the same
embedding composition root ``scripts/run_pipeline.py`` builds for batch runs.

Failure handling (no DLQ — application-owned instead):
  - On success: delete the job's S3 ledger entry (if any) and the SQS message.
  - On failure: increment the attempt count in the S3 ledger
    (``_pipeline_state/jobs/<target>/<record_id>.json``). Below
    ``WORKER_MAX_ATTEMPTS`` (default 5), leave the message alone so SQS
    redelivers it after the queue's visibility timeout. At/above the cap,
    mark the job ``status=\"dead\"`` and delete the message — this stops the
    redelivery loop and IS the DLQ substitute.
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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
from pipeline.airtable_ingestion.models import IngestionTargetConfig
from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline
from pipeline.airtable_ingestion.record_summary import RecordSummarizer
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.common.aws import s3_client, sqs_client
from pipeline.common.state_store import PipelineStateStore
from pipeline.config import DEFAULT_TABLES_PATH, load_settings as load_embedding_settings
from pipeline.preprocessing.normalizers.registry import get_normalizer
from scripts.run_pipeline import build_pipeline

_DEFAULT_MAX_ATTEMPTS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SQS worker: ingest one Airtable record + embed its S3 keys"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one receive_message batch, then exit "
        "(instead of looping forever). Used for foreground verification runs.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable ingestion config",
    )
    parser.add_argument(
        "--tables-config",
        default=str(DEFAULT_TABLES_PATH),
        help="Path to tables.yaml (default: config/tables.yaml).",
    )
    parser.add_argument(
        "--embedder",
        choices=["stub", "voyage"],
        default="voyage",
        help="Embedding provider for stage 2 (default: voyage).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(os.environ.get("WORKER_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)),
        help="Attempts before a job is marked 'dead' and its SQS message is "
        "deleted (default: 5, env WORKER_MAX_ATTEMPTS).",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS profile (local only)")
    return parser.parse_args()


def _p(msg: str = "") -> None:
    """Print with immediate flush — essential for non-TTY / systemd logs."""
    print(msg, flush=True)


def _ingestion_pipeline_for(
    target: IngestionTargetConfig,
    *,
    settings,
    airtable: AirtableClient,
    uploader: S3Uploader,
    record_summarizer: RecordSummarizer | None,
    cache: dict[str, AirtableAttachmentIngestionPipeline],
) -> AirtableAttachmentIngestionPipeline:
    """One pipeline instance per target, cached for the process lifetime.

    Normalizer / column_normalizers are constructor-level (not per-call), so
    each target — which may map its attachment columns to different
    normalizers (e.g. d_quals_sync's ``slides_deck`` on all three attachment
    columns) — needs its own instance. Mirrors the per-target construction
    ``run_airtable_ingestion.py`` does for a manual/batch run.
    """
    pipeline = cache.get(target.name)
    if pipeline is not None:
        return pipeline

    normalizer = get_normalizer(target.normalizer)
    column_normalizers = {
        col_name: get_normalizer(norm_name)
        for col_name, norm_name in target.attachment_column_normalizers
    }
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
    )
    cache[target.name] = pipeline
    return pipeline


def _handle_message(
    *,
    body: dict,
    ingestion_settings,
    airtable: AirtableClient,
    uploader: S3Uploader,
    record_summarizer: RecordSummarizer | None,
    embedding_pipeline,
    state_store: PipelineStateStore,
    pipeline_cache: dict[str, AirtableAttachmentIngestionPipeline],
    max_attempts: int,
) -> tuple[str, str]:
    """Process one SQS message body. Returns (outcome, detail) for logging.

    outcome is one of: "ok", "retry", "dead", "bad_message".
    """
    target_name = body.get("target")
    record_id = body.get("record_id")
    if not target_name or not record_id:
        return "bad_message", f"malformed payload: {body!r}"

    try:
        target = ingestion_settings.target(target_name)
    except KeyError:
        return "bad_message", f"unknown target {target_name!r}"

    existing = state_store.get_job(target_name, record_id)
    attempts_so_far = existing["attempts"] if existing else 0
    state_store.put_job(
        target_name, record_id, status="processing", attempts=attempts_so_far
    )

    try:
        record = airtable.get_record(
            base_id=target.database_id, table_name=target.table_name, record_id=record_id
        )
        ingestion_pipeline = _ingestion_pipeline_for(
            target,
            settings=ingestion_settings,
            airtable=airtable,
            uploader=uploader,
            record_summarizer=record_summarizer,
            cache=pipeline_cache,
        )
        new_keys = ingestion_pipeline.ingest_one_record(target=target, record=record)

        # RunReport on this branch exposes only the documents_failed counter
        # (no per-document failure list) — collect the S3 keys whose runs
        # reported failures so the error message stays actionable.
        failed_count = 0
        failed_keys: list[str] = []
        for key in new_keys:
            report = embedding_pipeline.run_one(key)
            if report.documents_failed:
                failed_count += report.documents_failed
                failed_keys.append(key)
        if failed_count:
            raise RuntimeError(
                f"embedding failed for {failed_count} document(s): {failed_keys}"
            )

        state_store.delete_job(target_name, record_id)
        return "ok", f"{target_name}/{record_id}: {len(new_keys)} key(s) ingested + embedded"
    except Exception as exc:  # noqa: BLE001
        attempts = attempts_so_far + 1
        if attempts >= max_attempts:
            state_store.put_job(
                target_name, record_id, status="dead", attempts=attempts, last_error=str(exc)
            )
            # RECORD_DEAD is an exact-match token for the CloudWatch
            # error-spike metric filter — keep it stable.
            return "dead", f"RECORD_DEAD {target_name}/{record_id}: {exc}"
        state_store.put_job(
            target_name, record_id, status="failed", attempts=attempts, last_error=str(exc)
        )
        return "retry", f"{target_name}/{record_id}: attempt {attempts}/{max_attempts}: {exc}"


def main() -> None:
    args = parse_args()

    ingestion_settings = load_airtable_ingestion_settings(config_path=Path(args.config))
    embedding_settings = load_embedding_settings(tables_path=Path(args.tables_config))

    queue_url = (os.environ.get("SQS_QUEUE_URL") or "").strip()
    if not queue_url:
        _p("ERROR: SQS_QUEUE_URL is not set in the environment.")
        sys.exit(1)
    wait_time = int(os.environ.get("SQS_WAIT_TIME_SECONDS", "20"))
    max_messages = int(os.environ.get("SQS_MAX_MESSAGES", "10"))

    s3 = s3_client(region_name=ingestion_settings.aws_region, profile_name=args.profile)
    uploader = S3Uploader(client=s3, bucket=ingestion_settings.s3_bucket)
    state_store = PipelineStateStore(uploader=uploader)
    airtable = AirtableClient(
        pat_token=ingestion_settings.airtable_pat_token,
        timeout_seconds=ingestion_settings.defaults.request_timeout_seconds,
    )
    record_summarizer = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        record_summarizer = RecordSummarizer(api_key=api_key)
    pipeline_cache: dict[str, AirtableAttachmentIngestionPipeline] = {}
    embedding_pipeline = build_pipeline(
        settings=embedding_settings,
        tables_config=args.tables_config,
        embedder_name=args.embedder,
        dry_run=False,
        skip_unchanged=True,
        aws_region=embedding_settings.aws_region,
        aws_profile=args.profile,
    )

    sqs = sqs_client(region_name=ingestion_settings.aws_region, profile_name=args.profile)

    _p("=" * 60)
    _p("  Airtable Ingestion Worker" + ("  (--once)" if args.once else ""))
    _p(f"  Queue        : {queue_url}")
    _p(f"  Embedder     : {args.embedder}")
    _p(f"  Max attempts : {args.max_attempts}")
    _p(f"  Started      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _p("=" * 60)

    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_time,
        )
        messages = response.get("Messages", [])
        if not messages:
            if args.once:
                _p("No messages received.")
                break
            continue

        for message in messages:
            try:
                body = json.loads(message["Body"])
            except json.JSONDecodeError:
                _p(f"  [BAD] undecodable message body — deleting: {message['Body']!r}")
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
                continue

            outcome, detail = _handle_message(
                body=body,
                ingestion_settings=ingestion_settings,
                airtable=airtable,
                uploader=uploader,
                record_summarizer=record_summarizer,
                embedding_pipeline=embedding_pipeline,
                state_store=state_store,
                pipeline_cache=pipeline_cache,
                max_attempts=args.max_attempts,
            )

            if outcome in ("ok", "dead", "bad_message"):
                # ok: fully processed. dead/bad_message: stop the redelivery
                # loop (dead = DLQ substitute; bad_message = poison pill).
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])

            tag = {"ok": "OK", "retry": "RETRY", "dead": "DEAD", "bad_message": "BAD"}[outcome]
            _p(f"  [{tag}] {detail}")

        if args.once:
            break

    _p("")
    _p("=" * 60)
    _p("  DONE" if args.once else "  STOPPED")
    _p("=" * 60)


if __name__ == "__main__":
    main()
