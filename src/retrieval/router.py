"""Top-level retrieval orchestrator.

Responsibilities
----------------
* Resolve source names (``"*"`` -> all enabled sources).
* Embed the query once per request and reuse the vector across sources.
* Dispatch to each :class:`retrieval.config.LogicalSource` concurrently
  via :func:`asyncio.gather`.
* Merge per-source hit lists via Reciprocal Rank Fusion.
* Return a :class:`retrieval.models.SearchResponse` envelope (without the
  optional NL ``answer`` field; that is filled by ``search`` -> formatter).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from retrieval.config import LogicalSource, SourceRegistry, get_registry
from retrieval.merger import group_by_source, rrf_merge
from retrieval.models import (
    Hint,
    ResponseMode,
    RetrievalQuery,
    SearchResponse,
    SearchResult,
)

log = structlog.get_logger(__name__)


class RetrievalRouter:
    """Composes the registry, the query embedder, and the cross-source merger."""

    def __init__(self, registry: SourceRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, q: RetrievalQuery) -> SearchResponse:
        sources = self._resolve_sources(q.sources)
        if not sources:
            return SearchResponse(
                ok=False,
                error="No matching enabled sources for query.sources={!r}".format(q.sources),
                diagnostics={"requested_sources": q.sources},
            )

        # Compute the query embedding once if any source needs it.
        embedding = q.embedding
        embedding_error: str | None = None
        wants_semantic = q.mode in ("semantic_only", "hybrid") and q.question
        any_semantic_capable = any(s.opensearch is not None for s in sources)
        if wants_semantic and any_semantic_capable and embedding is None:
            try:
                embedding = await self._embed_query(q.question or "")
            except Exception as exc:  # noqa: BLE001
                embedding_error = str(exc)
                log.warning("query_embed_failed", error=embedding_error)

        per_source_query = RetrievalQuery(
            question=q.question,
            embedding=embedding,
            formula=q.formula,
            fields=q.fields,
            filters=dict(q.filters),
            sources=[s.name for s in sources],
            mode=q.mode,
            top_k=q.top_k,
            max_records=q.max_records,
        )

        started = time.perf_counter()
        results = await asyncio.gather(
            *(self._safe_query(s, per_source_query) for s in sources),
            return_exceptions=False,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        all_hits: list[SearchResult] = []
        all_hints: list[Hint] = []
        per_source_diag: dict[str, Any] = {}
        per_source_hits: dict[str, list[SearchResult]] = {}

        for source, (hits, hints, source_elapsed_ms, error) in zip(sources, results):
            per_source_hits[source.name] = hits
            per_source_diag[source.name] = {
                "hits": len(hits),
                "hints": len(hints),
                "latency_ms": source_elapsed_ms,
                "error": error,
            }
            all_hits.extend(hits)
            all_hints.extend(hints)

        merged = (
            rrf_merge(per_source_hits, top_k=q.top_k * max(1, len(sources)))
            if len(per_source_hits) > 1
            else _flatten_per_source(per_source_hits)
        )

        # Confidentiality tagging (d_quals only) — every hit is kept in full;
        # a bracketed tag is prepended to text/record_summary/deck_summary
        # when confidential_project/confidential_client is set. No dropping,
        # no redaction, no citation changes.
        from retrieval.confidentiality import tag_confidential_hits  # noqa: PLC0415

        merged = tag_confidential_hits(merged)

        diagnostics = {
            "elapsed_ms": elapsed_ms,
            "sources": per_source_diag,
            "embedding": {
                "supplied_by_caller": q.embedding is not None,
                "computed": embedding is not None and q.embedding is None,
                "error": embedding_error,
            },
            "merge": "rrf" if len(per_source_hits) > 1 else "passthrough",
        }
        if not all_hits and embedding_error:
            return SearchResponse(
                ok=False,
                hits=[],
                hints=all_hints,
                diagnostics=diagnostics,
                error=f"No hits and embedding failed: {embedding_error}",
            )

        citation_diag = await self._resolve_citations(sources, merged)
        if citation_diag:
            diagnostics["citations"] = citation_diag

        # PRODUCT DECISION: citations come ONLY from S3, in EVERY mode (search,
        # hybrid, AND airtable_lookup) — unconditionally. The Airtable adapter no
        # longer builds airtable.com links; this strip is defense in depth so no
        # code path (present or future) can leak one. Structured rows keep their
        # DATA; only citation_url/citations are dropped (the references block
        # reads h.citations). The AIRTABLE_CITATIONS_ENABLED env flag is inert.
        for h in merged:
            if h.source_type == "structured":
                h.citation_url = None
                h.citations = []

        from retrieval.citations import assign_cite_ids_to_hits, build_references_from_hits  # noqa: PLC0415

        assign_cite_ids_to_hits(merged)
        references = build_references_from_hits(merged)

        return SearchResponse(
            ok=True,
            hits=merged,
            hints=all_hints,
            references=references,
            response_mode=ResponseMode.FULL_RECORDS,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_sources(self, requested: list[str]) -> list[LogicalSource]:
        if not requested or any(s == "*" for s in requested):
            return self._registry.enabled_sources()
        out: list[LogicalSource] = []
        for name in requested:
            try:
                out.append(self._registry.get(name))
            except KeyError as exc:
                log.warning("unknown_source", requested=name, error=str(exc))
        return out

    async def _resolve_citations(
        self,
        sources: list[LogicalSource],
        hits: list[SearchResult],
    ) -> dict[str, Any]:
        """Resolve citations for all hits.

        Semantic hits  → S3 presigned URLs (primary path, no Airtable call needed).
        Structured hits → Airtable record URLs already set by the Airtable adapter;
                          nothing extra to do here.

        The Airtable ``CitationResolver`` is intentionally preserved and importable
        for callers that explicitly need Airtable record URLs (e.g. structured queries).
        """
        from retrieval.citations import S3CitationResolver  # noqa: PLC0415
        from retrieval.settings import get_runtime_settings  # noqa: PLC0415

        runtime = get_runtime_settings()
        s3_resolver = S3CitationResolver(
            expiry_seconds=runtime.citation_expiry_seconds,
            aws_region=runtime.aws_region,
            signing_secret=runtime.citation_signing_secret,
            public_base_url=runtime.citation_public_base_url,
            link_ttl_seconds=runtime.citation_link_ttl_seconds,
        )

        semantic_hits = [h for h in hits if h.source_type == "semantic"]
        if not semantic_hits:
            return {}

        diag = s3_resolver.resolve_semantic_hits(semantic_hits)
        return {"s3": diag}

    async def _embed_query(self, question: str) -> list[float]:
        from retrieval.embedding.voyage import get_query_embedder  # noqa: PLC0415

        embedder = get_query_embedder()
        return await embedder.embed_query(question)

    async def _safe_query(
        self,
        source: LogicalSource,
        q: RetrievalQuery,
    ) -> tuple[list[SearchResult], list[Hint], int, str | None]:
        started = time.perf_counter()
        try:
            hits, hints = await source.query(q)
            latency_ms = int((time.perf_counter() - started) * 1000)
            return hits, hints, latency_ms, None
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - started) * 1000)
            log.warning(
                "source_query_failed",
                source=source.name,
                mode=q.mode,
                error=str(exc),
            )
            return [], [], latency_ms, str(exc)


def _flatten_per_source(per_source_hits: dict[str, list[SearchResult]]) -> list[SearchResult]:
    # Single-source path: preserve adapter ordering.
    if not per_source_hits:
        return []
    only_source = next(iter(per_source_hits.values()))
    return list(only_source)


# Re-export so callers can `from retrieval.router import group_by_source`
__all__ = ["RetrievalRouter", "group_by_source"]
