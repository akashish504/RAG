"""Record-summary parent hydration: fetch the record-level summary chunk by
primary_key and the filters it queries on. The OpenSearch client is faked.
"""

from __future__ import annotations

from pipeline.embedding_pipeline.indexer.mappings import INDEX_MAPPING
from retrieval.sources.opensearch import OpenSearchSource


def test_index_mapping_has_doc_role_keyword() -> None:
    # doc_role must be a filterable keyword for the hydration query.
    assert INDEX_MAPPING["properties"]["doc_role"] == {"type": "keyword"}


class _FakeClient:
    def __init__(self) -> None:
        self.last_body = None

    def search(self, index, body):  # noqa: ARG002
        self.last_body = body
        return {
            "hits": {
                "hits": [
                    {"_source": {"primary_key": "p1", "text": "Summary of record p1."}},
                    {"_source": {"primary_key": "p1", "text": "dup ignored"}},
                    {"_source": {"primary_key": "p3", "text": ""}},  # empty skipped
                ]
            }
        }


def _bare_source() -> OpenSearchSource:
    src = OpenSearchSource.__new__(OpenSearchSource)
    src.name = "d_quals"
    src.index_name = "mcp-d-quals"
    src._client = _FakeClient()
    return src


def test_fetch_record_summaries_filters_and_dedups() -> None:
    src = _bare_source()
    out = src._fetch_record_summaries({"p1", "p2", "p3"})

    # Only the first non-empty summary per primary_key is kept.
    assert out == {"p1": "Summary of record p1."}

    # The query filters on doc_role=record_summary + the pks (summary is a single
    # embedded child vector, so no chunk_type=parent filter).
    filters = src._client.last_body["query"]["bool"]["filter"]
    assert {"term": {"doc_role": "record_summary"}} in filters
    terms = next(f for f in filters if "terms" in f)["terms"]["primary_key"]
    assert set(terms) == {"p1", "p2", "p3"}


def test_fetch_deck_summaries_uses_s3_key() -> None:
    src = _bare_source()
    src._fetch_deck_summaries({"raw/d.quals/p1/.../deck.txt"})
    filters = src._client.last_body["query"]["bool"]["filter"]
    assert {"term": {"doc_role": "deck_summary"}} in filters
    assert any("terms" in f and "s3_key" in f["terms"] for f in filters)


def test_fetch_record_summaries_empty_input() -> None:
    src = _bare_source()
    assert src._fetch_record_summaries(set()) == {}
