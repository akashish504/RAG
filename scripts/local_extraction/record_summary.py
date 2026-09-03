"""Record-level summary — written incrementally after each deck finishes.

Each time a deck completes, update_record_summary() is called. It:
  1. Reads all normalized.txt files already in S3 for that record
  2. Merges them with the just-finished deck (deduplicates by att_id)
  3. Rewrites __record_summary.txt atomically

This means the record summary is always up to date and survives crashes.
"""

from __future__ import annotations

import logging
import threading
from pathlib import PurePosixPath
from typing import Any

from pipeline.airtable_ingestion.record_summary import (
    extract_file_summary,
    usable_sections,
)
from pipeline.preprocessing.slides.batch import DeckJob

from .deck_state import DeckState
from .s3_store import S3Store
from .vlm_client import VLMClient, RECORD_DIGEST_MAX_CHARS

log = logging.getLogger(__name__)

# Per-record lock to prevent two threads writing the same record summary simultaneously
_record_locks: dict[str, threading.Lock] = {}
_record_locks_lock = threading.Lock()


def _get_record_lock(identifier: str) -> threading.Lock:
    with _record_locks_lock:
        if identifier not in _record_locks:
            _record_locks[identifier] = threading.Lock()
        return _record_locks[identifier]


def update_record_summary(
    job: DeckJob,
    store: S3Store,
    vlm: VLMClient,
) -> None:
    """Called after each deck finishes. Rewrites __record_summary.txt for the record."""
    identifier = job.identifier
    if not identifier:
        return

    # record_dir is three levels up from the att_id folder:
    # raw/d.quals/{record_pk}/{column_slug}/{att_id}/
    record_dir = PurePosixPath(job.normalized_key).parent.parent.parent.as_posix()

    with _get_record_lock(identifier):
        # Collect all normalized.txt keys already in S3 for this record
        existing_keys = list(store.iter_keys(f"{record_dir}/"))
        normalized_keys = [
            k for k in existing_keys
            if k.endswith("__normalized.txt")
        ]

        # Always include the just-finished deck's key (may not be listed yet if S3 is eventually consistent)
        if job.normalized_key not in normalized_keys:
            normalized_keys.append(job.normalized_key)

        # Deduplicate by att_id (last segment before __normalized.txt)
        seen_att_ids: set[str] = set()
        deduped_keys: list[str] = []
        for k in normalized_keys:
            att_id = PurePosixPath(k).name.replace("__normalized.txt", "")
            if att_id not in seen_att_ids:
                seen_att_ids.add(att_id)
                deduped_keys.append(k)

        # Extract per-file summaries
        sections: list[tuple[str, str]] = []
        for key in deduped_keys:
            att_id = PurePosixPath(key).name.replace("__normalized.txt", "")
            try:
                text = store.get_bytes(key).decode("utf-8", errors="replace")
                s = extract_file_summary(text)
                if s:
                    sections.append((att_id, s))
            except Exception:
                log.warning("[record] %s — could not read %s", identifier, key, exc_info=True)

        usable = usable_sections(sections)
        if not usable:
            log.warning("[record] %s — no usable summaries found", identifier)
            return

        if len(usable) == 1:
            record_summary = usable[0][1]
        else:
            digest = "\n\n".join(f"## {label}\n{text}" for label, text in usable)
            digest = digest[:RECORD_DIGEST_MAX_CHARS]
            try:
                record_summary = vlm.summarize_record(digest)
            except Exception:
                log.warning("[record] %s — vLLM call failed, concatenating", identifier, exc_info=True)
                record_summary = " ".join(t for _, t in usable)

        manifest = "\n".join(f"- {label}" for label, _ in usable)
        body = f"DOCUMENT_SUMMARY: {record_summary}\n\nFiles in this record:\n{manifest}\n"

        try:
            store.put_text(
                f"{record_dir}/__record_summary.txt",
                job.metadata_header + body,
            )
        except Exception:
            log.warning("[record] %s — write failed", identifier, exc_info=True)
            return

        # .airtable_meta.json sidecar
        if job.record_id and job.table_id:
            payload: dict[str, Any] = {
                "airtable_record_id": job.record_id,
                "airtable_base_id": job.base_id,
                "airtable_table_id": job.table_id,
                "identifier": identifier,
                "column_name": "",
                "doc_role": "record_summary",
            }
            if job.facets:
                payload["facets"] = job.facets
            try:
                store.put_json(f"{record_dir}/.airtable_meta.json", payload)
            except Exception:
                log.warning("[record] %s — meta sidecar write failed", identifier, exc_info=True)

        log.info("[record] %s — summary updated (%d file(s))", identifier, len(usable))
