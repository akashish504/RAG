"""Indexer stage — bulk-write chunks to OpenSearch."""

from pipeline.embedding_pipeline.indexer.base import Indexer
from pipeline.embedding_pipeline.indexer.mappings import EMBEDDING_DIMS, INDEX_MAPPING, INDEX_SETTINGS
from pipeline.embedding_pipeline.indexer.opensearch import OpenSearchIndexer, build_opensearch_client

__all__ = [
    "Indexer",
    "EMBEDDING_DIMS",
    "INDEX_MAPPING",
    "INDEX_SETTINGS",
    "OpenSearchIndexer",
    "build_opensearch_client",
]
