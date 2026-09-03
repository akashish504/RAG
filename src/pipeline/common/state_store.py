"""S3-backed state for the event-driven ingestion pipeline.

Two small pieces of state live here, both as plain JSON objects under a
reserved S3 prefix (``_pipeline_state/`` by default — never collides with
``raw/`` or ``metadata/``):

Cursor
    One object per target: the ISO timestamp of the last successful poll.
    Read by the poller before querying Airtable; written after a batch of
    changed records has been fully enqueued.

Job ledger
    One object per (target, record_id), present only while that record is
    NOT fully processed. A successful worker run deletes the object; a
    failing one writes status/attempts/last_error. This is the failure
    surface described in the plan (no RDS, no DLQ — the S3 object list under
    ``jobs/<target>/`` IS the queryable "what's stuck / what's dead" view).

Deliberately not using RDS/Postgres: the ``pipeline_runs`` /
``document_index_log`` tables in ``pipeline.common.postgres`` are dead code
today (never called with ``--postgres`` in any script/compose/Makefile) and
RDS is being retired, so this pipeline must not add a new dependency on it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pipeline.airtable_ingestion.s3_uploader import S3Uploader

_VALID_STATUSES = frozenset({"queued", "processing", "failed", "dead"})

# Canonical cursor shape: UTC, second precision, "Z" suffix — no microseconds,
# no "+00:00" offset. AirtableClient.iter_changed_records() parses cursors
# with the matching explicit format 'YYYY-MM-DDTHH:mm:ssZ'; anything else
# risks Airtable's DATETIME_PARSE silently mis-parsing the value.
def _format_cursor(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cursor_key(prefix: str, target: str) -> str:
    return f"{prefix.rstrip('/')}/cursor/{target}.json"


def _job_key(prefix: str, target: str, record_id: str) -> str:
    return f"{prefix.rstrip('/')}/jobs/{target}/{record_id}.json"


def _job_prefix(prefix: str, target: str | None) -> str:
    if target is None:
        return f"{prefix.rstrip('/')}/jobs/"
    return f"{prefix.rstrip('/')}/jobs/{target}/"


class PipelineStateStore:
    """Cursor + failure-ledger storage on top of the existing S3Uploader."""

    def __init__(self, *, uploader: S3Uploader, prefix: str = "_pipeline_state") -> None:
        self._uploader = uploader
        self._prefix = prefix

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def get_cursor(self, target: str) -> str | None:
        """Return the last-seen ISO timestamp for ``target``, or None if unset.

        None means "no prior poll" — callers should treat this as "process
        everything" (a bootstrap run), not "nothing changed".
        """
        raw = self._uploader.get_text(key=_cursor_key(self._prefix, target))
        if raw is None:
            return None
        try:
            return json.loads(raw)["last_seen"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def set_cursor(self, target: str, iso_ts: str | None = None) -> None:
        """Persist the cursor for ``target``. Defaults to the current UTC time.

        ``iso_ts``, if given explicitly, MUST already be in the canonical
        ``YYYY-MM-DDTHH:MM:SSZ`` shape (see ``_format_cursor``) — this is what
        ``AirtableClient.iter_changed_records`` parses on the next poll.
        """
        iso_ts = iso_ts or _format_cursor(datetime.now(timezone.utc))
        self._uploader.upload_json(
            payload={"last_seen": iso_ts},
            key=_cursor_key(self._prefix, target),
        )

    # ------------------------------------------------------------------
    # Job / failure ledger
    # ------------------------------------------------------------------

    def get_job(self, target: str, record_id: str) -> dict[str, Any] | None:
        """Return the job record for (target, record_id), or None if absent
        (absent means: never failed, or already succeeded and cleared)."""
        raw = self._uploader.get_text(key=_job_key(self._prefix, target, record_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def put_job(
        self,
        target: str,
        record_id: str,
        *,
        status: str,
        attempts: int,
        last_error: str | None = None,
    ) -> None:
        """Write/overwrite the job record for (target, record_id)."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}")
        payload = {
            "target": target,
            "record_id": record_id,
            "status": status,
            "attempts": attempts,
            "last_error": last_error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._uploader.upload_json(payload=payload, key=_job_key(self._prefix, target, record_id))

    def delete_job(self, target: str, record_id: str) -> None:
        """Clear the job record — called on successful processing."""
        self._uploader.delete_object(key=_job_key(self._prefix, target, record_id))

    def list_jobs(
        self, target: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List job records, optionally filtered by target and/or status.

        This is the entire "let the user check failures" mechanism for now:
        every key under jobs/ is a record that has NOT yet succeeded. Lists
        via the same S3 client/credentials as everything else in this store
        (not a fresh default-credential-chain client) and lets errors
        propagate — a permissions/network failure here must surface as an
        error, not silently report "no failures".
        """
        prefix = _job_prefix(self._prefix, target)
        keys: list[str] = []
        paginator = self._uploader.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._uploader.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))

        jobs: list[dict[str, Any]] = []
        for key in keys:
            raw = self._uploader.get_text(key=key)
            if raw is None:
                continue
            try:
                job = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if status is not None and job.get("status") != status:
                continue
            jobs.append(job)
        return jobs
