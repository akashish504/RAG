"""Indexer routing: a dotted S3 slug (raw/d.quals/) must still route chunks to the
registered table's index (mcp-d-quals), not the fallback (mcp-docs)."""

from __future__ import annotations

from pipeline.embedding_pipeline.indexer.opensearch import OpenSearchIndexer
from pipeline.embedding_pipeline.models import Document, SourceMetadata
from pipeline.embedding_pipeline.parser.registry import ParserRegistry, default_parsers
from pipeline.embedding_pipeline.reader.document_loader import DocumentLoader
from pipeline.embedding_pipeline.reader.tables import TableRegistry


def _loader_and_registry():
    reg = TableRegistry.from_yaml("config/tables.yaml")
    loader = DocumentLoader(
        reader=object(),
        parser_registry=ParserRegistry(parsers=default_parsers()),
        table_registry=reg,
    )
    return loader, reg


def test_dotted_slug_table_name_normalized_to_registry_name() -> None:
    loader, reg = _loader_and_registry()
    table = reg.get("d_quals")
    doc = Document(
        document_hash="h",
        text="DOCUMENT_SUMMARY: x\n\n## Slide 1: Intro\n- a",
        source=SourceMetadata(
            s3_bucket="b",
            s3_key="raw/d.quals/1010012/c/attX__normalized.txt",
            table_name="d.quals",            # raw dotted path segment
            primary_key="1010012", column_name="c", filename="attX__normalized.txt",
        ),
    )
    loaded = loader._load(doc, table)
    assert loaded.document.source.table_name == "d_quals"   # resolved registry name


def _indexer_with_prefix_routing():
    reg = TableRegistry.from_yaml("config/tables.yaml")
    idx = OpenSearchIndexer.__new__(OpenSearchIndexer)
    idx._index = "mcp-docs"  # fallback
    idx._table_index_map = {t.name: t.index_name for t in reg.list()}
    idx._prefix_index_map = sorted(
        ((t.s3_prefix, t.index_name) for t in reg.list()), key=lambda kv: len(kv[0]), reverse=True
    )
    return idx


def test_routes_by_s3_prefix_even_with_dotted_slug() -> None:
    idx = _indexer_with_prefix_routing()
    from types import SimpleNamespace
    # Authoritative: routes by the chunk's actual S3 location, immune to slug quirks.
    src = SimpleNamespace(s3_key="raw/d.quals/1010012/c/attX__normalized.txt", table_name="d.quals")
    assert idx._get_index(src) == "mcp-d-quals"
    kl = SimpleNamespace(s3_key="raw/knowledge_library/Brief/c/att__normalized.txt", table_name="d.quals")
    assert idx._get_index(kl) == "mcp-knowledge-library"   # prefix wins over wrong table_name


def test_unknown_prefix_falls_back_and_warns() -> None:
    idx = _indexer_with_prefix_routing()
    from types import SimpleNamespace
    src = SimpleNamespace(s3_key="raw/something_unconfigured/x.txt", table_name="")
    assert idx._get_index(src) == "mcp-docs"
