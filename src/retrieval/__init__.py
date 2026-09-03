"""Retrieval module — source-agnostic MCP retrieval over OpenSearch + Airtable.

Public entry points
-------------------
:class:`retrieval.config.SourceRegistry`
    Loads ``config/retrieval_sources.yaml`` and instantiates per-source
    Airtable + OpenSearch adapters.

:class:`retrieval.router.RetrievalRouter`
    Top-level orchestrator. Fans out queries across one or more sources,
    embeds the query once per request, and merges hits with RRF.

:func:`retrieval.mcp.server.build_mcp`
    FastMCP server exposing five tools: ``list_sources``, ``get_schema``,
    ``semantic_search``, ``airtable_lookup``, ``search``.

Boundary rule
-------------
This package does NOT import ``pipeline.embedding_pipeline`` write paths
(``pipeline``, ``chunker``, ``parser``, ``embedder``, ``indexer.OpenSearchIndexer``).
It may reuse stable utilities: ``pipeline.common.aws``, ``pipeline.common.ids``,
``pipeline.common.opensearch.build_opensearch_client``, and
``pipeline.airtable_ingestion.data_extract.AirtableConnector``.
"""

from __future__ import annotations

__all__ = [
    "__version__",
]

__version__ = "0.1.0"
