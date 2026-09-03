"""OpenSearch retrieval adapter — KNN + BM25 hybrid search with parent hydration.

Design
------
- Search runs against the per-source index (e.g. ``mcp-dalberg-profiles``)
  configured in ``config/retrieval_sources.yaml``.
- KNN is always pre-filtered with ``chunk_type = "child"`` so vector search
  never touches parent-only documents (matches the indexer mapping in
  :mod:`pipeline.embedding_pipeline.indexer.mappings`).
- Hybrid mode runs KNN and BM25 in parallel, then merges via Reciprocal
  Rank Fusion. The same fusion is reused by :mod:`retrieval.merger` for
  cross-source merges.
- Parent hydration: child KNN hits are re-keyed to ``parent_chunk_id``
  and fetched in one ``mget`` call so callers see full-context parent text.
- The underlying ``opensearchpy`` client is sync; calls are off-loaded to a
  thread via :func:`asyncio.to_thread` to keep the async router non-blocking.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import structlog

from pipeline.common.opensearch import build_opensearch_client
from retrieval.models import (
    FieldDescriptor,
    Hint,
    SchemaDescriptor,
    SearchResult,
)

if TYPE_CHECKING:
    from retrieval.config import (
        EmbeddingConfig,
        LogicalSource,
        OpenSearchSourceConfig,
        RankingConfig,
    )
    from retrieval.settings import RetrievalRuntimeSettings

log = structlog.get_logger(__name__)

# Standard provenance fields written by the indexer (see
# :mod:`pipeline.embedding_pipeline.indexer.opensearch._to_action`).
_SEMANTIC_METADATA_FIELDS = (
    "table_name",
    "primary_key",
    "column_name",
    "s3_key",
    "s3_bucket",
    "source_url",
    "filename",
    # Original document key (populated by scripts/backfill_source_s3_key.py or
    # future indexing). When present, the S3 citation resolver uses it directly
    # and skips the per-query folder listing.
    "source_s3_key",
    "airtable_record_id",
    "airtable_base_id",
    "airtable_table_id",
    # "record_summary" marks the record-level parent summary chunk.
    "doc_role",
    "position",
    "token_count",
    "embedding_model",
    "indexed_at",
)

# Structured Airtable facets hoisted to keyword/date fields — filterable AND
# carried into result metadata for display.
_FACET_FIELDS = (
    # D.Quals
    "client_organisation",
    "practice_area",
    "project_region",
    "project_location",
    "dalberg_entity",
    "insight_type",
    # Backfilled by scripts/backfill_d_quals_record_meta.py (keyword, no re-embed).
    # Listed here so they are carried into hit metadata (for display / answering
    # "who led it", "who staffed it", "how big was it") and exposed via
    # supported_filters() as term-filterable facets. (D.Quals has no PM or
    # client-contact column, so those glossary fields are intentionally absent.)
    "dalberg_contact_person",
    "dalberg_team_members",
    "total_fees_charged",
    "project_lenses",
    "confidential_project",
    "confidential_client",
    "start_date",
    "end_date",
    # Knowledge Library (union; each index only populates its own)
    "kd_type",
    "country_region",
    "author",
    "team",
    "item_type",
    "client",
    "language",
    "can_be_shared_externally",
    "date_of_publication",
    # Proposal Library (union; each index only populates its own)
    "country",
    "region",
    "project_type",
    "date",
)


def opensearch_only_schema(source: "LogicalSource") -> SchemaDescriptor:
    """Schema descriptor for sources without an Airtable side."""

    fields = [FieldDescriptor(name=name, type="keyword") for name in _SEMANTIC_METADATA_FIELDS]
    return SchemaDescriptor(
        source=source.name,
        display_name=source.display_name,
        description=source.description,
        capabilities=["semantic"],
        identifier_field=source.config.identifier_field,
        fields=fields,
        long_text_fields=[],
        semantic_metadata_fields=list(_SEMANTIC_METADATA_FIELDS),
    )


# Standard English stop words that carry no BM25 signal in a CV index.
# Applied client-side to the BM25 query only — the KNN/embedding query
# keeps the full natural language string so Voyage can use sentence structure.
# OpenSearch's dalberg_english analyser also strips these on the indexed side,
# but stripping them here lets us detect and skip an all-stop-word query
# before it reaches the cluster.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "can", "shall",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "all", "any", "both", "each", "more", "most", "other", "some",
    "such", "no", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "also", "about",
    # Natural-language query prefixes that never appear in CV text.
    "find", "show", "list", "get", "give", "tell", "search", "looking",
    "want", "need", "please", "who", "what", "which", "where", "when",
    "how", "like",
})

_WHITESPACE_RE = re.compile(r"\s+")


def _strip_stop_words(query: str) -> str:
    """Remove stop words from a BM25 query string.

    Returns the cleaned string, or an empty string if only stop words remain
    (caller should skip BM25 in that case).
    """
    tokens = _WHITESPACE_RE.split(query.strip().lower())
    kept = [t for t in tokens if t and t not in _STOP_WORDS]
    return " ".join(kept)


# Curated consultant-term → canonical-expansion map, applied to the BM25 (lexical)
# query ONLY. This is the live-effective home for the glossary's acronyms/aliases:
# the OpenSearch analyzer synonym list only expands on (re)index, so on a live
# index query-side expansion is what actually helps. Properties that keep it
# accuracy-safe: it only ADDS canonical terms to what is searched (never removes,
# never asserts), the KNN vector query is left as the full natural-language string,
# and any noise it admits is re-sorted by rerank-2.5 downstream. Every value is a
# real Dalberg canonical term — nothing invented. Ambiguous/short tokens (e.g.
# "ap") are deliberately excluded to avoid mis-expansion.
_QUERY_EXPANSIONS: dict[str, str] = {
    "pa": "practice area",
    "pm": "project manager",
    "quals": "qualifications credentials",
    "qual": "qualification credential",
    "rfp": "request for proposal",
    "skilling": "education to employment skills training",
    "agri": "agriculture food systems",
    "imm": "monitoring evaluation learning",
    "m&e": "monitoring evaluation",
    "psd": "private sector development",
    "wash": "water sanitation hygiene",
    "dfi": "development finance institution",
}
_EXPANSION_PATTERNS: dict[str, "re.Pattern[str]"] = {
    term: re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
    for term in _QUERY_EXPANSIONS
}


def _expand_bm25_query(original_query: str, cleaned_bm25: str) -> str:
    """Append canonical expansions for any alias/acronym present in the query.

    Detection runs on the ORIGINAL query (word-boundary, ampersand-preserving);
    expansions are appended to the stop-word-stripped BM25 string. Returns the
    (possibly unchanged) BM25 query.
    """
    ql = original_query.lower()
    extra = [
        _QUERY_EXPANSIONS[term]
        for term, pat in _EXPANSION_PATTERNS.items()
        if pat.search(ql)
    ]
    if not extra:
        return cleaned_bm25
    return (cleaned_bm25 + " " + " ".join(extra)).strip()


def _dedup_by_person(results: list["SearchResult"]) -> list["SearchResult"]:
    """Collapse multiple section hits from the same person to their best-scoring one.

    Without this, a profile with N CV sections can occupy N slots in a top-10
    result set, crowding out other candidates entirely.
    """
    best: dict[str, SearchResult] = {}
    no_pk: list[SearchResult] = []
    for r in results:
        pk = r.metadata.get("primary_key")
        if not pk:
            no_pk.append(r)
        elif pk not in best or r.score > best[pk].score:
            best[pk] = r
    deduped = sorted(best.values(), key=lambda r: r.score, reverse=True)
    deduped.extend(no_pk)
    return deduped


class OpenSearchSource:
    """Semantic search adapter for one logical source."""

    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        cfg: "OpenSearchSourceConfig",
        embedding_cfg: "EmbeddingConfig",
        ranking_cfg: "RankingConfig",
        runtime: "RetrievalRuntimeSettings",
    ) -> None:
        if not runtime.opensearch_endpoint:
            msg = (
                "OPENSEARCH_ENDPOINT is not set; OpenSearchSource cannot be built. "
                "Either set the env var or disable the source in retrieval_sources.yaml."
            )
            raise RuntimeError(msg)

        self.name = name
        self.display_name = display_name
        self.index_name = cfg.index_name
        self._cfg = cfg
        self._embedding_cfg = embedding_cfg
        self._ranking_cfg = ranking_cfg
        self._runtime = runtime
        self._client = build_opensearch_client(
            runtime.opensearch_endpoint,
            username=runtime.opensearch_username,
            password=runtime.opensearch_password,
            aws_region=runtime.aws_region,
        )
        # Optional: derive structured facet filters from the NL query, grounded in
        # the index's actual facet values.
        self._facet_planner = None
        if getattr(cfg, "facet_filtering", False):
            from retrieval.facet_planner import FacetPlanner  # noqa: PLC0415

            self._facet_planner = FacetPlanner(
                client=self._client, index_name=self.index_name
            )

    # ------------------------------------------------------------------
    # Capability advertisement
    # ------------------------------------------------------------------

    def supported_filters(self) -> set[str]:
        # Mirrors the indexer's keyword fields (see mappings.py).
        return {
            "table_name",
            "primary_key",
            "column_name",
            "s3_key",
            "s3_bucket",
            "filename",
            "embedding_model",
            "chunk_type",
            "section_canonical",   # top-level field added in the new mapping
            "doc_role",
            *_FACET_FIELDS,        # filterable D.Quals facets
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search_semantic(
        self,
        *,
        query: str,
        embedding: list[float] | None,
        top_k: int,
        filters: dict[str, Any],
    ) -> tuple[list[SearchResult], list[Hint]]:
        if not query and embedding is None:
            return [], []

        # Query understanding: derive index-grounded facet filters from the NL
        # query. Kept separate from caller-supplied filters — only DERIVED
        # facets may soften under facet_mode; explicit filters (and the
        # child-chunk restriction / confidentiality) stay hard in every mode.
        derived: dict[str, Any] = {}
        if self._facet_planner is not None and query:
            try:
                derived = await asyncio.to_thread(self._facet_planner.plan, query) or {}
                # A caller-supplied filter on the same field wins outright; the
                # derived guess must not soften or shadow it.
                derived = {k: v for k, v in derived.items() if k not in filters}
                if derived:
                    log.debug("facet_filters_derived", source=self.name, filters=derived)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning("facet_planning_failed", source=self.name, error=str(exc))
                derived = {}

        facet_mode = self._cfg.facet_mode
        strict_filters = filters if facet_mode == "soft" else {**derived, **filters}

        # Retrieve a wider candidate pool and narrow down after person-level
        # deduplication.  This prevents the final top_k from being exhausted
        # by multiple sections of the same person's CV.
        fetch_k = max(self._cfg.over_fetch_k, top_k)

        mode = self._cfg.search_mode.lower()

        # Strip stop words for BM25 only.  The embedding query keeps the full
        # natural-language string so Voyage can use sentence structure for KNN.
        bm25_query = _strip_stop_words(query) if query else ""
        if bm25_query:
            # Curated query expansion (glossary acronyms/aliases → canonical terms).
            # Recall-additive, reranker-protected; KNN keeps the full NL string.
            expanded = _expand_bm25_query(query, bm25_query)
            if expanded != bm25_query:
                log.debug("bm25_query_expanded", cleaned=bm25_query, expanded=expanded)
                bm25_query = expanded
            log.debug("bm25_query_cleaned", original=query, cleaned=bm25_query)

        async def _run_pass(pass_filters: dict[str, Any]) -> list[dict[str, Any]]:
            knn_hits: list[dict[str, Any]] = []
            bm25_hits: list[dict[str, Any]] = []
            if mode in ("hybrid", "knn") and embedding is not None:
                knn_hits = await asyncio.to_thread(
                    self._knn_search, embedding, fetch_k, pass_filters
                )
            if mode in ("hybrid", "bm25") and bm25_query:
                bm25_hits = await asyncio.to_thread(
                    self._bm25_search, bm25_query, fetch_k, pass_filters
                )
            return self._rrf_merge(knn_hits, bm25_hits, k_max=fetch_k)

        merged = await _run_pass(strict_filters)

        if (
            facet_mode == "fallback"
            and derived
            and len(merged) < self._cfg.facet_fallback_min_results
        ):
            # Strict pass under-delivered: widen by dropping the DERIVED facets
            # from the filter (explicit filters + child restriction stay), then
            # prefer facet-matching docs at the merge stage instead.
            strict_count = len(merged)
            widened = await _run_pass(filters)
            merged = self._merge_passes(merged, widened, k_max=fetch_k)
            self._apply_facet_boost(merged, derived)
            log.info(
                "facet_fallback_widened",
                source=self.name,
                derived_facets=derived,
                strict_count=strict_count,
                widened_count=len(merged),
            )
        elif facet_mode == "soft" and derived:
            self._apply_facet_boost(merged, derived)

        if not merged:
            return [], []

        parent_ids = [
            (hit.get("_source") or {}).get("parent_chunk_id")
            for hit in merged
        ]
        parent_ids = [pid for pid in parent_ids if pid]
        parent_docs = (
            await asyncio.to_thread(self._mget, parent_ids)
            if parent_ids
            else {}
        )

        # Build section-level deduplicated results (one result per parent chunk).
        # Person-level dedup happens below — this pass just collapses multiple
        # child hits that share the same parent.
        pre_dedup: list[SearchResult] = []
        seen_parent_ids: set[str] = set()

        for hit in merged:
            child = hit.get("_source") or {}
            parent_chunk_id = child.get("parent_chunk_id")

            if parent_chunk_id and parent_chunk_id in seen_parent_ids:
                continue
            if parent_chunk_id:
                seen_parent_ids.add(parent_chunk_id)

            parent = parent_docs.get(parent_chunk_id, {}) if parent_chunk_id else {}
            text = (parent.get("text") if parent else None) or child.get("text", "")
            metadata: dict[str, Any] = {
                key: child.get(key)
                for key in _SEMANTIC_METADATA_FIELDS
                if child.get(key) is not None
            }
            # section_canonical is now a top-level field; keep it accessible
            # in metadata for downstream tools that read metadata directly.
            section_canonical = child.get("section_canonical") or (
                (child.get("metadata") or {}).get("section_canonical")
            )
            if section_canonical:
                metadata["section_canonical"] = section_canonical

            # Carry structured facets into result metadata for display/use.
            for fk in _FACET_FIELDS:
                if child.get(fk) is not None:
                    metadata[fk] = child.get(fk)

            extra_metadata = child.get("metadata") or {}
            if isinstance(extra_metadata, dict):
                metadata.update({k: v for k, v in extra_metadata.items() if v is not None})

            pre_dedup.append(
                SearchResult(
                    source=self.name,
                    source_type="semantic",
                    score=float(hit.get("_score") or 0.0),
                    text=text,
                    chunk_id=child.get("chunk_id"),
                    parent_chunk_id=parent_chunk_id,
                    metadata=metadata,
                    payload={
                        "rank": hit.get("_rank"),
                        "rrf_score": hit.get("_rrf_score"),
                        "section": section_canonical,
                        "token_count": child.get("token_count"),
                    },
                )
            )

        # Voyage cross-encoder re-ranking: run after parent hydration so the
        # reranker scores full-context text, and before person dedup so the
        # dedup step uses the improved scores.  Disabled gracefully when the
        # config flag is off or the API call fails.
        if self._ranking_cfg.reranker_enabled and pre_dedup and query:
            try:
                from retrieval.embedding.reranker import get_reranker  # noqa: PLC0415

                pre_dedup = await get_reranker().rerank(query, pre_dedup)
                log.debug(
                    "reranker_applied",
                    source=self.name,
                    model=self._ranking_cfg.reranker_model,
                    candidates=len(pre_dedup),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("reranker_failed_using_rrf_order", source=self.name, error=str(exc))

        # Person-level deduplication: keep only the highest-scoring result per
        # primary_key (email).  Without this, a profile with 6 CV sections can
        # occupy 6 of the 10 result slots, crowding out other candidates.
        results = _dedup_by_person(pre_dedup)[:top_k]

        # Hierarchy hydration: attach each hit's DECK summary (by s3_key) and
        # RECORD summary (by primary_key) so the answer carries deck + project
        # context, not just the matched slide/section.
        record_pks = {
            r.metadata.get("primary_key") for r in results if r.metadata.get("primary_key")
        }
        deck_keys = {r.metadata.get("s3_key") for r in results if r.metadata.get("s3_key")}
        if record_pks or deck_keys:
            try:
                rec_sum, deck_sum = await asyncio.gather(
                    asyncio.to_thread(self._fetch_record_summaries, record_pks),
                    asyncio.to_thread(self._fetch_deck_summaries, deck_keys),
                )
                for r in results:
                    pk = r.metadata.get("primary_key")
                    if pk and rec_sum.get(pk):
                        r.metadata["record_summary"] = rec_sum[pk]
                    sk = r.metadata.get("s3_key")
                    if sk and deck_sum.get(sk):
                        r.metadata["deck_summary"] = deck_sum[sk]
            except Exception as exc:  # noqa: BLE001 — hydration is best-effort
                log.warning("summary_hydration_failed", source=self.name, error=str(exc))

        # Emit one airtable_lookup hint per unique person in the final result set.
        hints: list[Hint] = []
        for r in results:
            pk = r.metadata.get("primary_key")
            if pk:
                hints.append(
                    Hint(
                        tool="airtable_lookup",
                        source=self.name,
                        reason="structured_fields_available",
                        detail={
                            "record_primary_key": pk,
                            "table_name": r.metadata.get("table_name"),
                        },
                    )
                )

        return results, hints

    async def fetch_by_id(self, ids: list[str]) -> list[SearchResult]:
        if not ids:
            return []
        docs = await asyncio.to_thread(self._mget, ids)
        out: list[SearchResult] = []
        for chunk_id, doc in docs.items():
            if not doc:
                continue
            metadata = {key: doc.get(key) for key in _SEMANTIC_METADATA_FIELDS if doc.get(key) is not None}
            out.append(
                SearchResult(
                    source=self.name,
                    source_type="semantic",
                    score=1.0,
                    text=doc.get("text", ""),
                    chunk_id=chunk_id,
                    parent_chunk_id=doc.get("parent_chunk_id"),
                    metadata=metadata,
                    payload={"doc": doc},
                )
            )
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter_clauses(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        supported = self.supported_filters()
        clauses: list[dict[str, Any]] = []
        for key, value in filters.items():
            if key not in supported or value is None:
                continue
            if isinstance(value, list | tuple | set):
                clauses.append({"terms": {key: list(value)}})
            else:
                clauses.append({"term": {key: value}})
        return clauses

    def _build_filter(self, filters: dict[str, Any], *, force_child: bool) -> list[dict[str, Any]]:
        clauses = self._filter_clauses(filters)
        if force_child:
            clauses.append({"term": {"chunk_type": "child"}})
        return clauses

    def _knn_search(
        self,
        embedding: list[float],
        top_k: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        knn_filter = self._build_filter(filters, force_child=True)
        body = {
            "size": top_k,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": top_k,
                        "filter": (
                            {"bool": {"filter": knn_filter}}
                            if knn_filter
                            else None
                        ),
                    }
                }
            },
        }
        if body["query"]["knn"]["embedding"]["filter"] is None:
            del body["query"]["knn"]["embedding"]["filter"]
        try:
            resp = self._client.search(index=self.index_name, body=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("opensearch_knn_failed", source=self.name, error=str(exc))
            return []
        return list(resp.get("hits", {}).get("hits", []))

    def _bm25_search(
        self,
        query: str,
        top_k: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        bm25_filter = self._build_filter(filters, force_child=True)
        # Two-clause should query:
        #  1. Standard term match (recall) — each query term scored independently.
        #  2. Phrase match (precision bonus) — adjacent terms score higher.
        # minimum_should_match:1 means a document needs at least one clause to
        # match, but scoring accumulates when both do.
        bool_query: dict[str, Any] = {
            "should": [
                {"match": {"text": {"query": query, "boost": 2.0}}},
                {"match_phrase": {"text": {"query": query, "boost": 4.0, "slop": 1}}},
                # Lexical match on the backfilled people-name sub-fields (analyzed
                # `.text`). Lets "projects led by / staffed with <name>" retrieve
                # records where the person is the Dalberg contact or a team member
                # even when the name is absent from the deck text. No-op until the
                # `.text` sub-fields are populated (matching a missing field simply
                # scores nothing — never errors).
                {"multi_match": {
                    "query": query,
                    "type": "best_fields",
                    "fields": [
                        "dalberg_contact_person.text",
                        "dalberg_team_members.text",
                    ],
                    "boost": 2.0,
                }},
            ],
            "minimum_should_match": 1,
        }
        if bm25_filter:
            bool_query["filter"] = bm25_filter
        body = {
            "size": top_k,
            "_source": {"excludes": ["embedding"]},
            "query": {"bool": bool_query},
        }
        try:
            resp = self._client.search(index=self.index_name, body=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("opensearch_bm25_failed", source=self.name, error=str(exc))
            return []
        return list(resp.get("hits", {}).get("hits", []))

    def _rrf_merge(
        self,
        knn_hits: list[dict[str, Any]],
        bm25_hits: list[dict[str, Any]],
        *,
        k_max: int,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion across two ranked hit lists.

        RRF score = sum(1 / (rrf_k + rank_in_each_list)). Each hit_list is
        ordered by relevance (rank 0 = best). Hits are de-duplicated by
        ``_id``; the better-ranked source survives with the merged score.
        """
        rrf_k = self._ranking_cfg.rrf_k or 60
        merged: dict[str, dict[str, Any]] = {}

        def _add(hits: list[dict[str, Any]]) -> None:
            for rank, hit in enumerate(hits):
                doc_id = hit.get("_id") or (hit.get("_source") or {}).get("chunk_id")
                if not doc_id:
                    continue
                contribution = 1.0 / (rrf_k + rank + 1)
                if doc_id in merged:
                    merged[doc_id]["_rrf_score"] += contribution
                else:
                    enriched = dict(hit)
                    enriched["_rrf_score"] = contribution
                    enriched["_rank"] = rank
                    merged[doc_id] = enriched

        _add(knn_hits)
        _add(bm25_hits)

        ranked = sorted(
            merged.values(),
            key=lambda h: h["_rrf_score"],
            reverse=True,
        )
        for rank, hit in enumerate(ranked):
            hit["_score"] = hit["_rrf_score"]
            hit["_rank"] = rank
        return ranked[:k_max]

    @staticmethod
    def _merge_passes(
        strict: list[dict[str, Any]],
        widened: list[dict[str, Any]],
        *,
        k_max: int,
    ) -> list[dict[str, Any]]:
        """Combine the strict and widened fallback passes.

        Docs appearing in both keep their best RRF score (never appear twice).
        Ranks are recomputed after the merge.
        """
        by_id: dict[str, dict[str, Any]] = {}
        for hit in [*strict, *widened]:
            doc_id = hit.get("_id") or (hit.get("_source") or {}).get("chunk_id")
            if not doc_id:
                continue
            prev = by_id.get(doc_id)
            if prev is None or hit["_rrf_score"] > prev["_rrf_score"]:
                by_id[doc_id] = hit
        ranked = sorted(by_id.values(), key=lambda h: h["_rrf_score"], reverse=True)
        for rank, hit in enumerate(ranked):
            hit["_score"] = hit["_rrf_score"]
            hit["_rank"] = rank
        return ranked[:k_max]

    def _apply_facet_boost(
        self,
        hits: list[dict[str, Any]],
        derived: dict[str, Any],
    ) -> None:
        """Rank facet-matching docs ahead of comparable non-matching ones.

        Adds up to one top-rank RRF contribution (1/(rrf_k+1)), scaled by the
        fraction of derived facets the doc matches. Calibration: a full facet
        match outranks any non-matching doc of similar relevance, but a doc
        leading BOTH ranked lists still beats a facet-matching straggler — the
        boost prefers, it does not override relevance. Re-sorts in place.
        """
        if not derived or not hits:
            return
        rrf_k = self._ranking_cfg.rrf_k or 60
        full_boost = 1.0 / (rrf_k + 1)
        for hit in hits:
            src = hit.get("_source") or {}
            matched = 0
            for field, wanted in derived.items():
                wanted_set = (
                    set(wanted) if isinstance(wanted, list | tuple | set) else {wanted}
                )
                have = src.get(field)
                have_set = set(have) if isinstance(have, list) else {have}
                if have_set & wanted_set:
                    matched += 1
            if matched:
                hit["_rrf_score"] += full_boost * (matched / len(derived))
                hit["_facet_matched"] = matched
        hits.sort(key=lambda h: h["_rrf_score"], reverse=True)
        for rank, hit in enumerate(hits):
            hit["_score"] = hit["_rrf_score"]
            hit["_rank"] = rank

    def _mget(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch documents by ``chunk_id`` (which is the ``_id``)."""

        if not ids:
            return {}
        unique_ids = list(dict.fromkeys(ids))
        try:
            resp = self._client.mget(
                index=self.index_name,
                body={"ids": unique_ids},
                _source_excludes=["embedding"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("opensearch_mget_failed", source=self.name, error=str(exc))
            return {}
        out: dict[str, dict[str, Any]] = {}
        for doc in resp.get("docs", []) or []:
            if not doc.get("found"):
                continue
            out[doc["_id"]] = doc.get("_source") or {}
        return out

    def _fetch_summaries(self, *, doc_role: str, field: str, values: set[str]) -> dict[str, str]:
        """Return ``{key: summary text}`` for summary vectors of ``doc_role``.

        Summaries are single embedded child chunks (``doc_role`` = record_summary |
        deck_summary). ``field`` is the join key: ``primary_key`` for the record
        summary (spans all a record's files), ``s3_key`` for a deck's summary.
        """
        keys = [v for v in values if v]
        if not keys:
            return {}
        body = {
            "size": len(keys),
            "_source": [field, "text"],
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"doc_role": doc_role}},
                        {"terms": {field: keys}},
                    ]
                }
            },
        }
        try:
            resp = self._client.search(index=self.index_name, body=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("opensearch_summary_failed", source=self.name,
                        doc_role=doc_role, error=str(exc))
            return {}
        out: dict[str, str] = {}
        for hit in resp.get("hits", {}).get("hits", []) or []:
            src = hit.get("_source") or {}
            key = src.get(field)
            if key and key not in out and src.get("text"):
                out[key] = src["text"]
        return out

    def _fetch_record_summaries(self, primary_keys: set[str]) -> dict[str, str]:
        return self._fetch_summaries(
            doc_role="record_summary", field="primary_key", values=primary_keys
        )

    def _fetch_deck_summaries(self, s3_keys: set[str]) -> dict[str, str]:
        return self._fetch_summaries(
            doc_role="deck_summary", field="s3_key", values=s3_keys
        )

    # ------------------------------------------------------------------
    # Schema introspection (used when source has no Airtable side)
    # ------------------------------------------------------------------

    def get_schema(self) -> SchemaDescriptor:
        fields = [FieldDescriptor(name=name, type="keyword") for name in _SEMANTIC_METADATA_FIELDS]
        return SchemaDescriptor(
            source=self.name,
            display_name=self.display_name,
            description=None,
            capabilities=["semantic"],
            identifier_field=None,
            fields=fields,
            long_text_fields=[],
            semantic_metadata_fields=list(_SEMANTIC_METADATA_FIELDS),
        )
