"""Offline batch extraction for D.Quals decks (Anthropic Batch API, ~50% cheaper).

Reads deck originals uploaded by ``run_airtable_ingestion.py --batch`` from S3,
renders + parses them locally, runs three dependent batch rounds (enrich → deck
summary → record summary), and writes ``normalized.txt`` + ``__record_summary.txt``
back to S3.

Usage:
    python scripts/run_batch_extraction.py --prefix raw/d_quals/ --wave-size 750
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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
from pipeline.preprocessing.slides.batch import (
    BatchExtractor,
    DeckJob,
    DocJob,
    duplicate_custom_ids,
    find_record_conflicts,
    group_by_record,
)
from pipeline.preprocessing.slides.render import PptxSlideRenderer

_DECK_SUFFIXES = (".pptx", ".ppt")
_DOC_SUFFIXES = (".pdf", ".docx", ".doc", ".xlsx", ".xlsm")


def _p(msg: str) -> None:
    print(msg, flush=True)


class S3Store:
    """Thin S3 adapter for the batch extractor (list / get / put / exists)."""

    def __init__(self, *, client, bucket: str) -> None:
        self._c = client
        self._bucket = bucket

    def iter_keys(self, prefix: str):
        paginator = self._c.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                yield obj["Key"]

    def key_exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self._c.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def get_bytes(self, key: str) -> bytes:
        return self._c.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def get_json(self, key: str) -> dict:
        try:
            return json.loads(self.get_bytes(key).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def put_text(self, key: str, text: str) -> None:
        self._c.put_object(
            Bucket=self._bucket, Key=key,
            Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8",
        )

    def put_json(self, key: str, payload: dict) -> None:
        self._c.put_object(
            Bucket=self._bucket, Key=key,
            Body=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )


def discover_jobs(store: S3Store, prefix: str) -> list:
    """Build a DeckJob (pptx/ppt) or DocJob (pdf/docx/xlsx) per original lacking a
    normalized.txt sibling. Returns a mixed list."""
    # One paginated LIST gives us every key — so "does normalized.txt exist?" is a
    # set lookup, NOT an S3 HEAD per file (which made the scan take minutes silently).
    all_keys = list(store.iter_keys(prefix))
    key_set = set(all_keys)
    _p(f"  listed {len(all_keys)} object(s); building jobs ...")

    jobs: list = []
    scanned = 0
    for key in all_keys:
        low = key.lower()
        is_deck = low.endswith(_DECK_SUFFIXES)
        is_doc = low.endswith(_DOC_SUFFIXES)
        if not (is_deck or is_doc):
            continue
        attach_dir = PurePosixPath(key).parent
        att_id = attach_dir.name
        normalized_key = f"{attach_dir.as_posix()}/{att_id}__normalized.txt"
        if normalized_key in key_set:
            continue  # already extracted (set lookup, no S3 call)
        meta = store.get_json(f"{attach_dir.as_posix()}/.airtable_meta.json")
        scanned += 1
        if scanned % 100 == 0:
            _p(f"    {scanned} pending file(s) found ...")
        common = dict(
            custom_id=att_id,
            identifier=str(meta.get("identifier") or attach_dir.parent.parent.name),
            record_id=str(meta.get("airtable_record_id") or ""),
            column=str(meta.get("column_name") or ""),
            original_key=key,
            normalized_key=normalized_key,
            metadata_header=str(meta.get("metadata_header") or ""),
            base_id=str(meta.get("airtable_base_id") or ""),
            table_id=str(meta.get("airtable_table_id") or ""),
            facets=meta.get("facets") or {},
        )
        jobs.append(DeckJob(**common) if is_deck else DocJob(**common))
    return jobs


def drop_conflicts(jobs: list[DeckJob]) -> list[DeckJob]:
    """Exclude (and report) any decks that could merge another's data, so the rest
    of the library still extracts cleanly instead of the whole run aborting."""
    bad_ids = duplicate_custom_ids(jobs)
    conflicts = find_record_conflicts(jobs)
    if bad_ids:
        _p(f"  ! SKIP {len(bad_ids)} duplicate custom_id(s): {sorted(bad_ids)}")
    if conflicts:
        _p(f"  ! SKIP {len(conflicts)} identifier(s) mapping to multiple records "
           f"(fix Project Number uniqueness): {sorted(conflicts)}")
    bad_identifiers = set(conflicts)
    return [
        j for j in jobs
        if j.custom_id not in bad_ids and j.identifier not in bad_identifiers
    ]


def pack_waves(jobs: list[DeckJob], wave_size: int) -> list[list[DeckJob]]:
    """Group by record, then pack whole records into waves (never split a record)."""
    waves: list[list[DeckJob]] = []
    current: list[DeckJob] = []
    for group in group_by_record(jobs).values():
        if current and len(current) + len(group) > wave_size:
            waves.append(current)
            current = []
        current.extend(group)
    if current:
        waves.append(current)
    return waves


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-API deck extraction")
    parser.add_argument(
        "--prefix",
        default="raw/d.quals/",  # slugify_table_name("(D.Quals)") -> "d.quals" (dotted)
        help="S3 prefix to scan",
    )
    parser.add_argument("--wave-size", type=int, default=750, help="decks per wave")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Render+upload this many decks concurrently per wave (the bottleneck). "
        "Try 6-8 to match your core count.",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    args = parser.parse_args()

    # Stream the orchestrator's progress logs (render/upload/poll) to stdout —
    # otherwise the run looks frozen during long renders and batch polling.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
    )

    bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not bucket:
        raise SystemExit("Missing S3_BUCKET in environment")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Missing ANTHROPIC_API_KEY in environment")

    import anthropic  # noqa: PLC0415

    store = S3Store(client=s3_client(region_name=args.region), bucket=bucket)
    extractor = BatchExtractor(
        client=anthropic.Anthropic(api_key=api_key),
        store=store,
        renderer=PptxSlideRenderer(),
        workers=args.workers,
    )

    _p(f"Scanning s3://{bucket}/{args.prefix} ...")
    jobs = drop_conflicts(discover_jobs(store, args.prefix))
    waves = pack_waves(jobs, args.wave_size)
    n_decks = sum(1 for j in jobs if isinstance(j, DeckJob))
    n_docs = len(jobs) - n_decks
    _p(f"{len(jobs)} files ({n_decks} decks, {n_docs} docs) across {len(waves)} wave(s)")

    for i, wave in enumerate(waves, start=1):
        decks = [j for j in wave if isinstance(j, DeckJob)]
        docs = [j for j in wave if isinstance(j, DocJob)]
        _p(f"── Wave {i}/{len(waves)} — {len(decks)} decks, {len(docs)} docs ──")
        extractor.process_wave(decks, docs)
        _p(f"   wave {i} done")

    _p("Batch extraction complete.")


if __name__ == "__main__":
    main()
