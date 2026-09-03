# Retrieval Pipeline — how a query becomes results

How a natural-language query is turned into ranked, cited results for the document
sources (`d_quals`, `knowledge_library`). This is the **search flow**; for the MCP
tools/envelope see [`retrieval.md`](retrieval.md). Code: `src/retrieval/sources/opensearch.py`.

## The pipeline at a glance

```
query string
  │
  ├─ Voyage embed ("query: " prefix) ────────────► KNN  (semantic — child vectors, HNSW cosine)   ─┐
  │                                                                                                 │  PARALLEL
  └─ stop-word strip ─────────────────────────────► BM25 (lexical — dalberg_english analyzer +     ─┤
  │                                                        synonym expansion: ESG, USAID, AfDB…)     │
  │                                                                                                  │
  │   facet planning (optional): match the query against the index's ACTUAL facet                   │
  │   values → filter both KNN & BM25 (e.g. practice_area=Health, project_region=East Africa)        │
  │                                                                                                  │
  ▼                                                                                                  │
  RRF fusion (reciprocal rank fusion, k=60) ◄───────────────────────────────────────────────────────┘
  ▼
  parent hydration — each child hit re-keyed to parent_chunk_id, fetched in one mget → FULL SLIDE text
  ▼
  Voyage reranker (rerank-2.5) — cross-encoder re-scores query × full-slide text (truncated to 5k chars)
  ▼
  dedup by primary_key — one best result per record (so one project can't fill all slots)
  ▼
  summary hydration — attach the deck summary (by s3_key) + record summary (by primary_key) as context
  ▼
  top_k results  (text, slide_number, source_s3_key citation, facets, deck/record summary)
```

## What's actually searched vs. hydrated

- **Searched (KNN + BM25):** only `chunk_type = "child"` — the intra-slide blocks (bullets,
  tables, visual descriptions) **and** the one-vector deck/record summaries (also children).
- **Hydrated (fetched by id, never searched):** the slide **parent** (full slide text) via
  `parent_chunk_id`; the deck summary (by `s3_key`) and record summary (by `primary_key`).

This is why precision and recall both work: KNN/BM25 match the fine-grained block, and the
caller gets the full slide + deck/project context around it.

## The chunk structure it operates on

```
record_summary  (1 vector, doc_role=record_summary)   ── project gist   [join: primary_key]
  deck_summary  (1 vector/deck, doc_role=deck_summary) ── deck gist      [join: s3_key]
    slide       (parent — full slide text, NOT embedded; hydrated by parent_chunk_id)
      block     (child vector — bullet group / table / visual; the retrieval unit)
```
Built by `pptx_slide` chunker (`src/pipeline/embedding_pipeline/chunker/pptx_slide.py`):
slide = parent, intra-slide blocks = children (tables & visuals kept atomic), child embeddings
are prefixed with `Slide N: Title` for context while the stored/cited text stays the raw block.

## Step detail

1. **Embed the query** — Voyage `voyage-4`, 1024-dim, `query: ` prefix (`embedding/`).
2. **Hybrid search (parallel)** — `_knn_search` (HNSW cosine, pre-filtered `chunk_type=child`)
   and `_bm25_search` (`dalberg_english` analyzer: lowercase → domain synonyms → stop → stem).
3. **Facet filtering (optional, `facet_filtering: true`)** — `FacetPlanner`
   (`src/retrieval/facet_planner.py`) pulls the index's distinct facet values (cached 5 min) and
   word-matches the query against them, so a derived filter can never reference a value that
   isn't in the data. Applied to both KNN and BM25; caller-supplied filters always win.
4. **RRF fusion** — `_rrf_merge`, `rrf_k=60`. No score-weight tuning needed.
5. **Parent hydration** — `_mget(parent_chunk_id…)` → each result's `text` becomes the full slide.
6. **Rerank** — `VoyageReranker` (`rerank-2.5`) re-scores on the hydrated slide text. Runs after
   hydration (scores full context) and before dedup. **Graceful**: if it fails or the API key is
   missing, falls back to RRF order (logs `reranker_failed_using_rrf_order`).
7. **Dedup by `primary_key`** — `_dedup_by_person`: highest-scoring result per record.
8. **Summary hydration** — attach `metadata.deck_summary` (by `s3_key`) and
   `metadata.record_summary` (by `primary_key`) so answers carry deck + project context.

## Configuration (`config/retrieval_sources.yaml`)

```yaml
defaults:
  ranking: { fusion: rrf, rrf_k: 60, reranker_enabled: true, reranker_model: rerank-2.5 }
  search_mode: hybrid          # hybrid | semantic_only | airtable_only
sources:
  d_quals:
    opensearch:
      index_name: mcp-d-quals
      k: 10                     # results returned
      over_fetch_k: 50          # candidates fetched per method before dedup
      search_mode: hybrid       # hybrid | knn | bm25  (BM25 + KNN + RRF)
      facet_filtering: true     # NL query → structured facet filters
```
- **Always hybrid + reranked** for `d_quals` and `knowledge_library` (defaults inherited).
- Reranker requires **`VOYAGE_API_KEY` in the retrieval/MCP server's env** (a different process
  from indexing). Missing key → silent fallback to RRF order (still hybrid, no rerank).

## Verifying / debugging via logs

The retrieval server logs (structlog) tell you exactly what ran per query:

```bash
# follow the MCP/retrieval server logs (adjust to how you run it)
docker compose logs -f api            # or: docker compose logs -f <retrieval-service>
# if run standalone:  tail -f <your-server>.log

# confirm the reranker actually ran (vs silently falling back)
docker compose logs api | grep -E "reranker_applied|reranker_failed_using_rrf_order|voyage_reranker_failed"
#   reranker_applied                 → cross-encoder rerank ran ✅
#   reranker_failed_using_rrf_order  → fell back to RRF (check VOYAGE_API_KEY) ⚠️

# confirm hybrid is doing BM25 (the cleaned lexical query)
docker compose logs api | grep "bm25_query_cleaned"

# confirm facet filters were auto-derived from a query
docker compose logs api | grep "facet_filters_applied"
```

Index-side (which index, how many docs) — from the indexing container:
```bash
docker compose run --rm pipeline python scripts/validate_opensearch.py --table d_quals
docker compose run --rm pipeline python scripts/validate_opensearch.py \
  --test-query "financial inclusion in East Africa" --table d_quals      # end-to-end KNN test
```

## Summary
Every `d_quals`/`knowledge_library` query runs **BM25 + semantic KNN, fused by RRF, parent-hydrated,
cross-encoder reranked, deduped per record, with facet filtering and deck/record-summary context**.
It is the configured default, not optional per query — provided the retrieval server has `VOYAGE_API_KEY`.
