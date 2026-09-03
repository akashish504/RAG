"""Structured Airtable facets: extraction at ingestion + hoisting at index time."""

from __future__ import annotations

from pipeline.airtable_ingestion.pipeline import _build_facets
from pipeline.embedding_pipeline.indexer.opensearch import OpenSearchIndexer
from pipeline.embedding_pipeline.models import Chunk, ChunkType, SourceMetadata


def test_build_facets_slugs_keys_and_keeps_types() -> None:
    fields = {
        "Client Organisation": "Gates Foundation",
        "Practice Area": ["Financial Services", "Health"],   # multi-select → list
        "Project Region": "East Africa",
        "Start Date": "2023-01-15",
        "Empty Field": "",                                    # dropped
        "Missing": None,                                      # dropped
    }
    facets = _build_facets(
        fields,
        ("Client Organisation", "Practice Area", "Project Region", "Start Date",
         "Empty Field", "Missing"),
    )
    assert facets == {
        "client_organisation": "Gates Foundation",
        "practice_area": ["Financial Services", "Health"],
        "project_region": "East Africa",
        "start_date": "2023-01-15",
    }


def test_indexer_hoists_facets_to_top_level() -> None:
    indexer = OpenSearchIndexer.__new__(OpenSearchIndexer)
    indexer._index = "mcp-d-quals"
    indexer._table_index_map = {}
    indexer._prefix_index_map = []
    indexer._embedding_model = None

    chunk = Chunk(
        chunk_id="c1", chunk_type=ChunkType.CHILD, text="x", token_count=1, position=0,
        document_hash="h",
        source=SourceMetadata(
            s3_bucket="b", s3_key="k", table_name="d_quals", primary_key="p",
            column_name="c", filename="f",
            facets={"practice_area": ["Health"], "project_region": "East Africa"},
        ),
    )
    action = indexer._to_action(chunk)
    src = action["_source"]
    # Facets are filterable top-level fields, not buried in metadata.
    assert src["practice_area"] == ["Health"]
    assert src["project_region"] == "East Africa"
