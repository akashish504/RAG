"""Unit tests for PipelineStateStore (S3-backed cursor + job ledger)."""

from __future__ import annotations

import json
import re

import pytest

from pipeline.common.state_store import PipelineStateStore


class _FakePaginator:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def paginate(self, *, Bucket: str, Prefix: str):  # noqa: N803 — boto3 API shape
        keys = [k for k in sorted(self._store) if k.startswith(Prefix)]
        yield {"Contents": [{"Key": k} for k in keys]} if keys else {}


class _FakeClient:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _FakePaginator(self._store)


class _FakeUploader:
    """Dict-backed stand-in for S3Uploader (only what the store touches)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.bucket = "test-bucket"
        self.client = _FakeClient(self.store)

    def get_text(self, *, key: str) -> str | None:
        return self.store.get(key)

    def upload_json(self, *, payload, key: str) -> None:
        self.store[key] = json.dumps(payload)

    def delete_object(self, *, key: str) -> None:
        self.store.pop(key, None)


@pytest.fixture()
def store() -> PipelineStateStore:
    return PipelineStateStore(uploader=_FakeUploader())


def test_cursor_absent_returns_none(store: PipelineStateStore) -> None:
    assert store.get_cursor("d_quals_sync") is None


def test_cursor_round_trip(store: PipelineStateStore) -> None:
    store.set_cursor("d_quals_sync", "2026-07-22T10:00:00Z")
    assert store.get_cursor("d_quals_sync") == "2026-07-22T10:00:00Z"


def test_cursor_default_is_canonical_utc_z(store: PipelineStateStore) -> None:
    """set_cursor(None) must write the exact YYYY-MM-DDTHH:MM:SSZ shape that
    AirtableClient.iter_changed_records parses with DATETIME_PARSE."""
    store.set_cursor("d_quals_sync")
    value = store.get_cursor("d_quals_sync")
    assert value is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)


def test_job_round_trip_and_delete(store: PipelineStateStore) -> None:
    assert store.get_job("d_quals_sync", "rec1") is None
    store.put_job("d_quals_sync", "rec1", status="failed", attempts=2, last_error="boom")
    job = store.get_job("d_quals_sync", "rec1")
    assert job is not None
    assert job["status"] == "failed"
    assert job["attempts"] == 2
    assert job["last_error"] == "boom"
    store.delete_job("d_quals_sync", "rec1")
    assert store.get_job("d_quals_sync", "rec1") is None


def test_put_job_rejects_invalid_status(store: PipelineStateStore) -> None:
    with pytest.raises(ValueError, match="invalid status"):
        store.put_job("d_quals_sync", "rec1", status="exploded", attempts=1)


def test_list_jobs_filters_by_status_and_target(store: PipelineStateStore) -> None:
    store.put_job("d_quals_sync", "recA", status="failed", attempts=1)
    store.put_job("d_quals_sync", "recB", status="dead", attempts=5)
    store.put_job("other_sync", "recC", status="dead", attempts=5)

    assert len(store.list_jobs()) == 3
    assert {j["record_id"] for j in store.list_jobs(target="d_quals_sync")} == {"recA", "recB"}
    dead = store.list_jobs(status="dead")
    assert {j["record_id"] for j in dead} == {"recB", "recC"}
    assert store.list_jobs(target="d_quals_sync", status="dead")[0]["record_id"] == "recB"
