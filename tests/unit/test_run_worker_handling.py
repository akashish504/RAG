"""Tests for scripts/run_worker._handle_message outcome semantics.

Outcomes: ok (ledger cleared), retry (below attempt cap, message left for SQS
redelivery), dead (cap reached — DLQ substitute), bad_message (poison pill).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.run_worker import _handle_message


class _FakeStateStore:
    def __init__(self, existing: dict | None = None) -> None:
        self.jobs: dict[tuple[str, str], dict] = {}
        if existing:
            self.jobs[(existing["target"], existing["record_id"])] = existing
        self.deleted: list[tuple[str, str]] = []

    def get_job(self, target: str, record_id: str):
        return self.jobs.get((target, record_id))

    def put_job(self, target, record_id, *, status, attempts, last_error=None):
        self.jobs[(target, record_id)] = {
            "target": target,
            "record_id": record_id,
            "status": status,
            "attempts": attempts,
            "last_error": last_error,
        }

    def delete_job(self, target, record_id):
        self.deleted.append((target, record_id))
        self.jobs.pop((target, record_id), None)


class _FakeSettings:
    def __init__(self, target) -> None:
        self._target = target

    def target(self, name: str):
        if self._target is not None and name == self._target.name:
            return self._target
        raise KeyError(name)


class _FakeIngestionPipeline:
    def __init__(self, keys=None, error: Exception | None = None) -> None:
        self._keys = keys or []
        self._error = error

    def ingest_one_record(self, *, target, record):  # noqa: ARG002
        if self._error is not None:
            raise self._error
        return list(self._keys)


class _FakeEmbeddingPipeline:
    def __init__(self, documents_failed: int = 0) -> None:
        self._failed = documents_failed
        self.ran: list[str] = []

    def run_one(self, key: str):
        self.ran.append(key)
        return SimpleNamespace(documents_failed=self._failed)


_TARGET = SimpleNamespace(name="d_quals_sync", database_id="app1", table_name="T")
_AIRTABLE = SimpleNamespace(
    get_record=lambda *, base_id, table_name, record_id: {"id": record_id, "fields": {}}
)
_BODY = {"target": "d_quals_sync", "record_id": "rec1"}


def _handle(
    *,
    body=_BODY,
    ingestion_error: Exception | None = None,
    documents_failed: int = 0,
    prior_attempts: int = 0,
    keys=("raw/k1",),
    max_attempts: int = 5,
):
    state = _FakeStateStore(
        existing=(
            {"target": "d_quals_sync", "record_id": "rec1", "attempts": prior_attempts}
            if prior_attempts
            else None
        )
    )
    embedding = _FakeEmbeddingPipeline(documents_failed=documents_failed)
    outcome, detail = _handle_message(
        body=body,
        ingestion_settings=_FakeSettings(_TARGET),
        airtable=_AIRTABLE,
        uploader=object(),
        record_summarizer=None,
        embedding_pipeline=embedding,
        state_store=state,
        pipeline_cache={
            "d_quals_sync": _FakeIngestionPipeline(keys=list(keys), error=ingestion_error)
        },
        max_attempts=max_attempts,
    )
    return outcome, detail, state, embedding


def test_ok_path_embeds_keys_and_clears_ledger() -> None:
    outcome, _, state, embedding = _handle()
    assert outcome == "ok"
    assert embedding.ran == ["raw/k1"]
    assert state.deleted == [("d_quals_sync", "rec1")]
    assert state.jobs == {}


def test_failure_below_cap_is_retry() -> None:
    outcome, detail, state, _ = _handle(ingestion_error=RuntimeError("boom"))
    assert outcome == "retry"
    job = state.jobs[("d_quals_sync", "rec1")]
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert "attempt 1/5" in detail


def test_failure_at_cap_is_dead() -> None:
    outcome, _, state, _ = _handle(
        ingestion_error=RuntimeError("boom"), prior_attempts=4
    )
    assert outcome == "dead"
    assert state.jobs[("d_quals_sync", "rec1")]["status"] == "dead"
    assert state.jobs[("d_quals_sync", "rec1")]["attempts"] == 5


def test_embedding_failure_counter_triggers_retry() -> None:
    outcome, detail, state, _ = _handle(documents_failed=2)
    assert outcome == "retry"
    assert "embedding failed for 2 document(s)" in state.jobs[
        ("d_quals_sync", "rec1")
    ]["last_error"]
    assert "raw/k1" in detail or "raw/k1" in state.jobs[("d_quals_sync", "rec1")]["last_error"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"target": "d_quals_sync"},
        {"record_id": "rec1"},
        {"target": "who_dis", "record_id": "rec1"},
    ],
)
def test_bad_messages_are_poison_pills(body) -> None:
    outcome, _, state, embedding = _handle(body=body)
    assert outcome == "bad_message"
    assert state.jobs == {}, "no ledger entry for undeliverable messages"
    assert embedding.ran == []
