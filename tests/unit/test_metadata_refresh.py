"""No-LLM facet/metadata backfill: in-place update_by_query + pipeline driver."""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.embedding_pipeline.indexer.opensearch import OpenSearchIndexer
from pipeline.embedding_pipeline.pipeline import Pipeline


class _FakeOSClient:
    def __init__(self) -> None:
        self.last_body = None

    def update_by_query(self, index, body, params):  # noqa: ARG002
        self.last_body = body
        return {"updated": 3}


def _indexer() -> OpenSearchIndexer:
    idx = OpenSearchIndexer.__new__(OpenSearchIndexer)
    idx._index = "mcp-d-quals"
    idx._table_index_map = {}
    idx._prefix_index_map = []
    idx._client = _FakeOSClient()
    return idx


def test_update_fields_by_query_builds_term_and_script() -> None:
    idx = _indexer()
    n = idx.update_fields_by_query(
        term_field="primary_key", term_value="p1",
        fields={"practice_area": ["Health"], "project_region": "East Africa"},
    )
    assert n == 3
    body = idx._client.last_body
    assert body["query"]["term"]["primary_key"] == "p1"
    assert body["script"]["params"]["fields"] == {
        "practice_area": ["Health"], "project_region": "East Africa",
    }


def test_update_fields_by_query_noop_on_empty() -> None:
    idx = _indexer()
    assert idx.update_fields_by_query(term_field="primary_key", term_value="p1", fields={}) == 0
    assert idx._client.last_body is None  # never called


# -- pipeline driver --------------------------------------------------------

def _loaded(pk: str, facets: dict) -> SimpleNamespace:
    return SimpleNamespace(document=SimpleNamespace(source=SimpleNamespace(
        primary_key=pk, facets=facets)))


class _FakeLoader:
    def __init__(self, items) -> None:
        self._items = items

    def iter_prefix(self, prefix):  # noqa: ARG002
        return iter(self._items)


class _RecordingIndexer:
    def __init__(self) -> None:
        self.calls = []

    def update_fields_by_query(self, *, term_field, term_value, fields):  # noqa: ARG002
        self.calls.append((term_value, fields))
        return 4


def test_refresh_metadata_one_update_per_record() -> None:
    p = Pipeline.__new__(Pipeline)
    p._loader = _FakeLoader([
        _loaded("p1", {"practice_area": ["Health"]}),
        _loaded("p1", {"practice_area": ["Health"]}),  # same record (e.g. summary chunk) — dedup
        _loaded("p2", {}),                              # no facets — skipped
        _loaded("p3", {"project_region": "South Asia"}),
    ])
    p._indexer = _RecordingIndexer()

    stats = p.refresh_metadata("raw/d.quals/")

    # One update per distinct record that has facets (p1, p3); p2 skipped.
    assert {pk for pk, _ in p._indexer.calls} == {"p1", "p3"}
    assert stats == {"records": 2, "chunks": 8}
