"""Pre-flight check: does the SQS queue (and its DLQ) exist?

Run this BEFORE wiring the polling worker so a missing queue / DLQ is caught
up front rather than at first message send.

Usage
-----
    # Human-readable (default):
    python scripts/check_sqs.py

    # Machine-readable:
    python scripts/check_sqs.py --json

    # Local dev with a named AWS profile:
    python scripts/check_sqs.py --profile my-sso-profile

What it checks
--------------
1. The main queue exists (resolved by SQS_QUEUE_NAME, falling back to the
   SQS_QUEUE_URL host path). Existence is proven by a successful GetQueueUrl /
   GetQueueAttributes call — not by the presence of an env var.
2. The queue's RedrivePolicy → the wired dead-letter queue, if any, and whether
   that DLQ actually exists.
3. As a convenience, whether a conventionally-named "<name>-dlq" queue exists
   even when it is not yet wired as the redrive target.

Exit code is 0 only when the main queue exists; non-zero otherwise, so this is
safe to gate a deploy step on.
"""

from __future__ import annotations

import argparse
import json
import os
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

from botocore.exceptions import ClientError

from pipeline.common.aws import boto3_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check that the SQS queue and DLQ exist.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    parser.add_argument("--region", default=None, help="AWS region override.")
    parser.add_argument("--profile", default=None, help="AWS profile (local dev only).")
    return parser.parse_args()


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _queue_name_from_url(url: str) -> str | None:
    """Derive the queue name from a queue URL's final path segment."""
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def _describe_queue(sqs, *, name: str) -> dict | None:
    """Return {url, attributes} for a queue by name, or None if it doesn't exist.

    Existence is decided by GetQueueUrl: AWS raises QueueDoesNotExist when the
    queue is absent (or NonExistentQueue on some API versions).
    """
    try:
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"}:
            return None
        raise
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"]).get(
        "Attributes", {}
    )
    return {"url": url, "attributes": attrs}


def _dlq_from_redrive(attributes: dict) -> tuple[str | None, int | None]:
    """Extract (dlq_arn, max_receive_count) from a queue's RedrivePolicy."""
    raw = attributes.get("RedrivePolicy")
    if not raw:
        return None, None
    try:
        policy = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None
    max_receive = policy.get("maxReceiveCount")
    return policy.get("deadLetterTargetArn"), (
        int(max_receive) if max_receive is not None else None
    )


def _name_from_arn(arn: str | None) -> str | None:
    """arn:aws:sqs:region:acct:queue-name → queue-name."""
    if not arn:
        return None
    return arn.rsplit(":", 1)[-1] or None


def main() -> None:
    args = parse_args()

    region = args.region or os.environ.get("AWS_REGION", "eu-west-1")
    # An empty AWS_PROFILE in .env (AWS_PROFILE=) is still read by boto3 and makes
    # it try to load a profile named "" — which fails. Treat empty as unset so the
    # default credential chain (env keys / IAM role) is used instead.
    if not os.environ.get("AWS_PROFILE", "").strip():
        os.environ.pop("AWS_PROFILE", None)
    profile = args.profile or os.environ.get("AWS_PROFILE") or None

    main_name = os.environ.get("SQS_QUEUE_NAME") or _queue_name_from_url(
        os.environ.get("SQS_QUEUE_URL", "")
    )

    report: dict = {
        "region": region,
        "profile": profile,
        "configured_queue_name": main_name,
        "main_queue": {"exists": False},
        "redrive": {"configured": False},
        "dlq": {"exists": False, "wired_as_redrive_target": False},
        "ok": False,
        "errors": [],
    }

    if not main_name:
        report["errors"].append(
            "No queue name: set SQS_QUEUE_NAME or SQS_QUEUE_URL in .env."
        )
        _emit(report, as_json=args.json)
        sys.exit(2)

    try:
        sqs = boto3_session(region_name=region, profile_name=profile).client("sqs")
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"Could not create SQS client: {exc}")
        _emit(report, as_json=args.json)
        sys.exit(2)

    # --- Main queue -------------------------------------------------------
    try:
        main_q = _describe_queue(sqs, name=main_name)
    except ClientError as exc:
        report["errors"].append(f"GetQueueUrl failed for {main_name!r}: {exc}")
        _emit(report, as_json=args.json)
        sys.exit(2)

    if main_q is None:
        report["errors"].append(f"Main queue {main_name!r} does NOT exist.")
        _emit(report, as_json=args.json)
        sys.exit(1)

    attrs = main_q["attributes"]
    report["main_queue"] = {
        "exists": True,
        "name": main_name,
        "url": main_q["url"],
        "arn": attrs.get("QueueArn"),
        "visibility_timeout": attrs.get("VisibilityTimeout"),
        "messages_available": attrs.get("ApproximateNumberOfMessages"),
        "messages_in_flight": attrs.get("ApproximateNumberOfMessagesNotVisible"),
    }

    # --- DLQ via redrive policy ------------------------------------------
    dlq_arn, max_receive = _dlq_from_redrive(attrs)
    dlq_name_candidate = _name_from_arn(dlq_arn)

    if dlq_arn:
        report["redrive"] = {
            "configured": True,
            "dead_letter_target_arn": dlq_arn,
            "max_receive_count": max_receive,
        }
        dlq_q = _describe_queue(sqs, name=dlq_name_candidate) if dlq_name_candidate else None
        if dlq_q is not None:
            report["dlq"] = {
                "exists": True,
                "wired_as_redrive_target": True,
                "name": dlq_name_candidate,
                "url": dlq_q["url"],
                "arn": dlq_q["attributes"].get("QueueArn"),
                "messages_available": dlq_q["attributes"].get(
                    "ApproximateNumberOfMessages"
                ),
            }
        else:
            report["dlq"] = {"exists": False, "wired_as_redrive_target": True}
            report["errors"].append(
                f"RedrivePolicy points at {dlq_arn!r} but that queue does not exist."
            )
    else:
        # No redrive wired — check for a conventionally-named DLQ anyway.
        convention = f"{main_name}-dlq"
        conv_q = _describe_queue(sqs, name=convention)
        if conv_q is not None:
            report["dlq"] = {
                "exists": True,
                "wired_as_redrive_target": False,
                "name": convention,
                "url": conv_q["url"],
                "arn": conv_q["attributes"].get("QueueArn"),
            }

    # ok = main queue exists AND a DLQ exists (wired or by convention).
    report["ok"] = report["main_queue"]["exists"] and report["dlq"]["exists"]

    _emit(report, as_json=args.json)
    # Exit 0 only when the main queue exists. DLQ absence is a warning (exit 3),
    # not a hard failure, so you can decide whether to create one.
    if not report["main_queue"]["exists"]:
        sys.exit(1)
    sys.exit(0 if report["dlq"]["exists"] else 3)


def _emit(report: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return

    _p("=" * 60)
    _p("  SQS Pre-flight Check")
    _p(f"  Region  : {report['region']}")
    _p(f"  Profile : {report['profile'] or '(default credential chain)'}")
    _p(f"  Queue   : {report['configured_queue_name'] or '(none configured)'}")
    _p("=" * 60)

    mq = report["main_queue"]
    if mq.get("exists"):
        _p(f"  [OK ] Main queue EXISTS: {mq['name']}")
        _p(f"        url               : {mq['url']}")
        _p(f"        arn               : {mq['arn']}")
        _p(f"        visibility_timeout: {mq['visibility_timeout']}s")
        _p(f"        messages available: {mq['messages_available']}")
        _p(f"        messages in-flight: {mq['messages_in_flight']}")
    else:
        _p("  [MISS] Main queue does NOT exist.")

    rd = report["redrive"]
    if rd.get("configured"):
        _p(f"  [OK ] RedrivePolicy set → maxReceiveCount={rd['max_receive_count']}")
        _p(f"        dead-letter target: {rd['dead_letter_target_arn']}")
    else:
        _p("  [WARN] No RedrivePolicy on the main queue (no DLQ wired).")

    dq = report["dlq"]
    if dq.get("exists"):
        wired = "wired as redrive target" if dq.get("wired_as_redrive_target") else "exists but NOT wired"
        _p(f"  [OK ] DLQ EXISTS ({wired}): {dq['name']}")
        _p(f"        url: {dq['url']}")
    else:
        _p("  [MISS] No DLQ found (neither via redrive nor '<name>-dlq').")

    if report["errors"]:
        _p("")
        _p(f"  Issues ({len(report['errors'])}):")
        for e in report["errors"]:
            _p(f"    ! {e}")

    _p("")
    verdict = "READY" if report["ok"] else "ACTION NEEDED"
    _p(f"  Verdict: {verdict}")


if __name__ == "__main__":
    main()
