"""Discover pending PPTX jobs from S3.

One paginated LIST of the entire prefix; skip any deck that already has a
normalized.txt sibling (set lookup, no per-file HEAD calls).
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from pipeline.preprocessing.slides.batch import DeckJob

from .s3_store import S3Store

log = logging.getLogger(__name__)

_DECK_SUFFIXES = (".pptx", ".ppt")


def discover_jobs(store: S3Store, prefix: str) -> list[DeckJob]:
    log.info("Scanning s3 prefix: %s", prefix)
    all_keys = list(store.iter_keys(prefix))
    key_set = set(all_keys)
    log.info("  %d object(s) listed; identifying pending decks ...", len(all_keys))

    jobs: list[DeckJob] = []
    for key in all_keys:
        if not key.lower().endswith(_DECK_SUFFIXES):
            continue
        attach_dir = PurePosixPath(key).parent
        att_id = attach_dir.name
        normalized_key = f"{attach_dir.as_posix()}/{att_id}__normalized.txt"
        if normalized_key in key_set:
            continue  # already extracted — skip

        # Stub with S3-path-derived fallback values. The render worker fetches
        # .airtable_meta.json and overwrites these fields before processing —
        # keeping discovery to a single LIST with zero per-deck GETs.
        jobs.append(DeckJob(
            custom_id=att_id,
            identifier=attach_dir.parent.parent.name,
            record_id="",
            column="",
            original_key=key,
            normalized_key=normalized_key,
            metadata_header="",
            base_id="",
            table_id="",
            facets={},
        ))

    log.info("  %d deck(s) pending extraction", len(jobs))
    return jobs
