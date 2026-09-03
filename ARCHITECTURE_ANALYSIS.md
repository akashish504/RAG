# Dalberg MCP — Deep Architecture Analysis
> Generated: 2026-05-21 | Reviewer: Claude (Cowork) | Graphify nodes: 773, edges: 1130

---

## 1. Executive Summary

`dalberg_mcp` is an **enterprise AI retrieval system** for Dalberg Group, a global consulting firm. It gives Claude (via the Model Context Protocol) access to Dalberg's knowledge stores — employee profiles, project qualifications, proposals, and knowledge-library documents. The system has two physically separate subsystems that share AWS infrastructure:

1. **Offline Embedding Pipeline** — A batch job that ingests documents from Airtable/S3, chunks them with a parent-child strategy, embeds them via Voyage-4, and indexes them into AWS OpenSearch.
2. **Runtime Retrieval (FastAPI + MCP)** — A long-running HTTP service that exposes five MCP tools to Claude. It plans queries with Claude, dispatches to Airtable (structured) and OpenSearch (semantic), merges results via RRF, and synthesises a natural-language answer.

The project is **actively evolving** — v1 MCP is deprecated, v2 is live, and three sources (`d_quals`, `proposal_library`, `knowledge_library`) are configured but disabled awaiting data.

---

## 2. System Architecture & Data Flow

### 2.1 End-to-End Flow

```
WRITE PATH (offline, batch)
══════════════════════════
Airtable (Profiles / D-Quals / Proposals)
  └─► AirtableAttachmentIngestionPipeline
         ├─ downloads CV / Bio attachments
         └─ uploads to S3: raw/{table}/{email}/{column}/{file}
                │
                ▼
        AWS S3 (claude-mcp-object-store)
                │  raw/{table}/{pk}/{col}.txt
                ▼
        Pipeline (run() or run_one())
          1. S3Reader + DocumentLoader
                │  ─ lists objects under prefix
                │  ─ resolves table via TableRegistry
                ▼
          2. ParserRegistry → ParsedDocument
                │  ─ TextParser (primary)
                │  ─ PDFParser / DOCXParser / PPTXParser (optional[parsers])
                ▼
          3. ChunkerRegistry → list[Chunk]
                │  ─ parent_child: ParentChildChunker (500/150/50 tok)
                │  ─ resume:       ResumeChunker (CV-aware canonical sections)
                ▼
          4. VoyageEmbedder (child chunks only)
                │  ─ "passage: " prefix + voyage-4 API
                │  ─ 1024-dim vectors, batch=32, tenacity retries
                ▼
          5. OpenSearchIndexer (all chunks)
                │  ─ bulk upsert, chunk_id as _id (idempotent)
                │  ─ content-addressed skip (SHA-256 document_hash)
                │  ─ delete_by_s3_key before re-indexing changed docs
                ▼
        AWS OpenSearch (mcp-dev-os-search-eu, eu-west-1)
          ─ per-table indexes: mcp-dalberg-profiles, mcp-d-quals, ...
          ─ KNN int8_hnsw, cosine similarity, dims=1024

READ PATH (runtime, per-request)
═════════════════════════════════
Claude / MCP Client
  └─► FastAPI :8000 (Nginx :80 → host :8080)
        ├─ Bearer auth middleware (all /mcp/* paths)
        │
        ├─ /mcp/v2/mcp  ← FastMCP (v2, current)
        │     5 tools: list_sources · get_schema · semantic_search
        │               airtable_lookup · search
        │
        └─ /mcp/mcp      ← FastMCP (v1, deprecated)
               query_dalberg_profiles (forwards to v2 search)

search(question) flow:
  1. Expand sources (["*"] → all enabled)
  2. plan_query() → Claude API call → {mode, formula, semantic_query, top_k}
  3. RetrievalRouter.run(q)
       ├─ Voyage query embedder (embed once, share across sources)
       └─ asyncio.gather across enabled sources
             ├─ AirtableSource.filter_structured(formula) → structured rows
             └─ OpenSearchSource.search_semantic()
                   ├─ _knn_search(embedding, top_k)  ─┐
                   ├─ _bm25_search(query, top_k)     ─┤ parallel
                   ├─ _rrf_merge(knn, bm25, k)        ┘
                   └─ _mget(parent_chunk_ids) → parent hydration
  4. rrf_merge (cross-source if >1 source)
  5. format_response()
       ├─ classify_response_shape() → Claude API call
       ├─ build_markdown_table(hits)
       └─ synthesize_answer() → Claude API call
  6. SearchResponse JSON → MCP client
```

### 2.2 Module Map

| Module | Path | Role |
|---|---|---|
| `pipeline.embedding_pipeline.pipeline` | `src/pipeline/embedding_pipeline/pipeline.py` | Orchestrator (read→chunk→embed→index) |
| `pipeline.embedding_pipeline.reader` | `src/pipeline/embedding_pipeline/reader/` | S3Reader, DocumentLoader, TableRegistry |
| `pipeline.embedding_pipeline.parser` | `src/pipeline/embedding_pipeline/parser/` | TextParser + optional PDF/DOCX/PPTX |
| `pipeline.embedding_pipeline.chunker` | `src/pipeline/embedding_pipeline/chunker/` | ParentChildChunker, ResumeChunker, registry |
| `pipeline.embedding_pipeline.embedder` | `src/pipeline/embedding_pipeline/embedder/` | VoyageEmbedder, StubEmbedder |
| `pipeline.embedding_pipeline.indexer` | `src/pipeline/embedding_pipeline/indexer/` | OpenSearchIndexer, index mappings |
| `pipeline.airtable_ingestion` | `src/pipeline/airtable_ingestion/` | Airtable → S3 attachment sync |
| `pipeline.api` | `src/pipeline/api/` | FastAPI app, MCP v1, auth, settings |
| `pipeline.common` | `src/pipeline/common/` | AWS clients, OpenSearch client, PostgreSQL, IDs |
| `retrieval` | `src/retrieval/` | Full retrieval subsystem |
| `retrieval.config` | `src/retrieval/config.py` | SourceRegistry, LogicalSource, YAML parser |
| `retrieval.router` | `src/retrieval/router.py` | RetrievalRouter — async multi-source dispatch |
| `retrieval.sources.opensearch` | `src/retrieval/sources/opensearch.py` | KNN + BM25 + parent hydration |
| `retrieval.sources.airtable` | `src/retrieval/sources/airtable.py` | filterByFormula adapter |
| `retrieval.planner.nl_planner` | `src/retrieval/planner/nl_planner.py` | Claude NL → retrieval plan |
| `retrieval.merger` | `src/retrieval/merger.py` | Cross-source RRF merge |
| `retrieval.formatter` | `src/retrieval/formatter.py` | Response shape + markdown + answer synthesis |
| `retrieval.mcp` | `src/retrieval/mcp/` | FastMCP v2 server, 5 tools |

---

## 3. APIs, Authentication, and Integrations

### 3.1 HTTP API (FastAPI)

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | None | Liveness probe for load balancer / Nginx |
| `GET /v1/status` | Bearer | API status + MCP mounts |
| `GET /v1/test/airtable/ping` | Bearer | Smoke-test Airtable connectivity |
| `GET /v1/test/airtable/schema` | Bearer | Returns merged schema descriptor |
| `GET /v1/test/airtable/records` | Bearer | Raw airtable_lookup for debugging |
| `POST /v1/test/airtable/nl-query` | Bearer | Full search() for debugging |
| `GET /v1/test/mcp` | Bearer | Lists MCP mounts |
| `GET /v1/test/sources` | Bearer | Lists all configured sources |
| `/mcp/v2/mcp` | Bearer (middleware) | FastMCP v2 — 5 tools |
| `/mcp/mcp` | Bearer (middleware) | FastMCP v1 — deprecated |

**Authentication:** `secrets.compare_digest` constant-time comparison against `API_BEARER_TOKEN` env var. Applied via ASGI middleware for all `/mcp/*` paths; FastAPI `Depends(require_api_token)` for `/v1/*` routes.

### 3.2 MCP Tools (v2)

```
list_sources()                     → JSON: source names, capabilities, enabled state
get_schema(source)                 → JSON: field catalog (name, type, choices, is_long_text)
semantic_search(query, source?, top_k?, filters?)  → SearchResponse (KNN+BM25)
airtable_lookup(source, formula?, fields?, max_records?)  → SearchResponse (structured rows)
search(question, sources?, mode?, top_k?, include_answer?)  → SearchResponse (NL, full pipeline)
```

### 3.3 External Integrations

| Service | Library | Auth | Purpose |
|---|---|---|---|
| Airtable REST API | `pyairtable` | PAT token (`AIRTABLE_PAT_TOKEN`) | Read profiles, metadata, attachments |
| AWS S3 | `boto3` | IAM role (EC2) / profile (local) | Source document store |
| AWS Secrets Manager | `boto3` | IAM role | Secrets retrieval |
| AWS OpenSearch | `opensearch-py` | SigV4 (prod) / basic auth (dev) | Vector + full-text search |
| Voyage AI | `voyageai` | `VOYAGE_API_KEY` | Embedding (passage + query) |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY` | NL planning + shape + answer synthesis |
| PostgreSQL (RDS) | `psycopg2` (optional) | env vars | Observability; future session store |

---

## 4. Infrastructure and Deployment

### 4.1 AWS (eu-west-1)

- **S3:** `claude-mcp-object-store` — source documents under `raw/`, schema snapshots
- **OpenSearch:** `mcp-dev-os-search-eu` — VPC-private domain, SigV4 auth in prod
- **RDS Postgres:** `mcp-dev-rds-db-eu` — optional observability (pipeline_runs, document_index_log)
- **EC2:** Docker Compose on EC2; IAM instance role (no embedded credentials)

### 4.2 Docker / Compose Profiles

| Profile | Service | Command |
|---|---|---|
| `api` | `api` | Dockerfile.api → Nginx (`:80`) + Uvicorn (`:8000`) |
| (default) | `pipeline` | One-off scripts via command override |
| `tools` | `s3-smoke`, `test`, `lint`, `shell` | Development utilities |

### 4.3 OpenSearch Index Mapping

- **KNN:** `int8_hnsw`, `m=16`, `ef_construction=100`, `cosine similarity`, `dims=1024`
- **Chunk fields:** `chunk_id`, `chunk_type` (parent/child), `text`, `token_count`, `position`, `document_hash`, `embedding`, `embedding_model`, `indexed_at`
- **Provenance fields:** `table_name`, `primary_key`, `column_name`, `s3_key`, `s3_bucket`, `source_url`, `filename`, `parent_chunk_id`, `metadata` (JSONB)

### 4.4 Config Files

| File | Purpose |
|---|---|
| `config/default.yaml` | S3 bucket, chunking params, embedding provider, OpenSearch endpoint |
| `config/tables.yaml` | Per-table: S3 prefix, chunking strategy, index name |
| `config/retrieval_sources.yaml` | Per-source: Airtable base/table, OpenSearch index, search mode |
| `config/airtable_ingestion.yaml` | Ingestion targets (profiles_sync, future targets) |
| `config/dalberg_profiles_schema.json` | Frozen Airtable schema snapshot |
| `.env` | All runtime secrets (never committed; `.env.example` as template) |

---

## 5. Chunking Strategy (Business-Critical)

### 5.1 ParentChildChunker (proposals, generic docs)

```
ParsedDocument.sections (headings as boundaries)
  └─ section → "heading\n\ntext" composite
       ├─ if section_tokens ≤ 500 → 1 parent
       └─ if > 500 → overlapping windows (500 tok, overlap=0 default)
            └─ each parent → children (150 tok, 50 tok overlap, min=30 tok)
                 ─ chunk_id = sha256(doc_hash + type + parent_pos + child_pos)
                 ─ children carry parent_chunk_id for hydration at query time
```

### 5.2 ResumeChunker (Dalberg Profiles — CV-aware)

- Recognises canonical CV sections: EDUCATION, EXPERIENCE, SKILLS, PUBLICATIONS, etc.
- Each canonical section → one parent; bullet/list entries within → children
- Pipe-delimited table rows detected as section separators
- Date-range patterns used for entry splitting
- Fallback ALL-CAPS line detection when DOCX heading styles unavailable

### 5.3 Token Counting

- Tokenizer: `tiktoken` with `cl100k_base` (GPT-4 / Voyage-compatible)
- LRU-cached encoder per encoding name
- `count_tokens`, `split_to_token_window` — both in `chunker/tokens.py`

---

## 6. Retrieval Search Logic

### 6.1 OpenSearch Hybrid Search (per source)

```
KNN search (child chunks only, chunk_type="child")
  └─ {"knn": {"embedding": {"vector": q_vec, "k": top_k}}}
     parallel with
BM25 search (child chunks only)
  └─ {"match": {"text": {"query": question}}}

  ↓
Per-source RRF merge  (rrf_k=60)
  score = 1/(60+rank+1), dedup by _id

  ↓
Parent hydration (_mget on parent_chunk_id values)
  ↓ 
Dedup by parent_chunk_id (only highest-ranking child per parent)
  → SearchResult with parent text, child metadata
```

### 6.2 Cross-Source RRF (merger.py)

```
score = 1/(60+rank+1) + 0.001 × raw_score
dedup by (source, chunk_id or record_id)
stable sort descending → top_k
```

### 6.3 Airtable filterByFormula

- `pyairtable` client with async dispatch via `asyncio.to_thread`
- Long-text fields truncated to 800 chars + `Hint(tool="semantic_search")` added
- `filter_structured(formula, fields, max_records)` — results as `SearchResult(source_type="airtable")`

---

## 7. Performance Bottlenecks

### 7.1 Critical (High Impact)

**[P0] 3 Claude API calls per `search()` request:**
Every `search()` invocation with `include_answer=True` triggers:
1. `plan_query()` — NL → retrieval plan
2. `classify_response_shape()` — how to format results
3. `synthesize_answer()` — final narrative answer

These run sequentially, adding ~2–5 seconds of LLM latency per query. The shape classifier could be merged with the planner in a single prompt.

**[P0] `_run_async` creates a new ThreadPoolExecutor per call:**
In `retrieval/mcp/tools.py`, when called inside an existing event loop:
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    return pool.submit(asyncio.run, coro).result()
```
This spawns a new thread + event loop for every tool invocation. Under concurrent MCP sessions this is expensive.

**[P1] `document_hash_exists` queries ALL managed indexes every run:**
The content-addressed skip check runs a `count` query across all table indexes concatenated (`"idx1,idx2,idx3"`). For large index sets this fan-out is unnecessary — only the target table's index needs to be checked.

**[P1] `delete_by_s3_key` uses `refresh: true`:**
Synchronous index flush before continuing. Under high-volume ingestion this adds significant latency per document.

### 7.2 Moderate Impact

**[P2] Global `_REGISTRY` singleton is not thread-safe for reload:**
```python
_REGISTRY: SourceRegistry | None = None
def get_registry(*, reload=False, ...):
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = SourceRegistry.load(...)
```
No locking. Concurrent first-requests race to initialize adapters.

**[P2] NL planner only fires for single-source queries:**
Multi-source queries skip planning entirely and fall back to semantic-only. This means structured Airtable lookup never runs in multi-source mode.

**[P2] Each `VoyageEmbedder` call is a new retry-decorated closure:**
The `@retry` decorator is rebuilt on every `_embed_batch_with_retry` call inside the loop because it's a nested function. The decorator itself is cheap but the closure allocation adds up at high batch counts.

**[P2] Airtable ingestion has no parallelism within a target:**
Records are fetched and processed sequentially. For large Airtable bases (1000s of profiles) this is slow.

**[P3] No connection pooling for the OpenSearch client across requests:**
Each `OpenSearchSource` owns its own `opensearchpy.OpenSearch` client. The client does pool internally but there's no shared pool across sources.

**[P3] `_mget` for parent hydration is per-source, not batched across sources:**
In cross-source hybrid mode each source mgets its own parents independently. A merged mget would reduce round-trips.

---

## 8. Technical Debt

### 8.1 Structural

1. **v1 MCP (`/mcp/`) still mounted** — `query_dalberg_profiles` just forwards to v2 `search`. Every v1 call adds an extra hop. Should be removed once all clients migrate to v2.

2. **`pipeline.api` still contains legacy modules** — `nl_airtable_query.py` and `nl_response_shape.py` have been superseded by `retrieval.planner` and `retrieval.formatter` but may still exist as imports. Verify before removal.

3. **`_schema_from_snapshot` is duplicated logic** — Both `get_schema_impl` (when adapter wiring fails) and `LogicalSource.get_schema` contain fallback paths that read the same JSON snapshot. This should be a single shared function.

4. **`_run_async` bridge is an architectural smell** — FastMCP tools are sync but the retrieval layer is async. The bridge works but creates subtle issues (nested event loop detection, thread pool churn). The cleaner fix is to register async tools or use `anyio.from_thread.run_sync`.

5. **3 disabled sources** — `d_quals`, `proposal_library`, `knowledge_library` have full YAML config but no schema snapshots and are disabled. Each has a placeholder schema path that doesn't exist (`config/d_quals_schema.json`). This will raise `FileNotFoundError` if enabled without the snapshot.

6. **`table_index_map` not populated in `build_pipeline()`** — The `OpenSearchIndexer` supports per-table index routing but `scripts/run_pipeline.py` likely doesn't wire this up, defaulting everything to the fallback `mcp-docs` index instead of `mcp-dalberg-profiles`.

### 8.2 Testing Gaps

- No integration tests for the retrieval path (router, sources, formatter)
- No tests for `VoyageEmbedder` (mocked), `OpenSearchSource`, `AirtableSource`
- `conftest.py` notes planned moto-backed S3 fixtures but they appear empty
- No async test infrastructure (`pytest-asyncio` not in dev deps)

### 8.3 Documentation

- README still says "Greenfield. Folder structure scaffolded; module bodies are stubs" — no longer accurate; most stubs are implemented
- `docs/retrieval.md` content should be verified against actual implementation

---

## 9. Security Considerations

### 9.1 Good Practices Present

- `secrets.compare_digest` for constant-time token comparison — protects against timing attacks
- IAM instance roles on EC2 — no embedded credentials in containers
- Env vars for all secrets; `.env.example` with placeholders; `.gitignore` for `.env`
- Bearer token required on all `/mcp/*` paths before reaching FastMCP sub-apps
- OpenSearch SigV4 in prod (basic auth only in dev)

### 9.2 Risks / Gaps

| Risk | Severity | Notes |
|---|---|---|
| No rate limiting on API endpoints | Medium | Under active MCP sessions, `search()` triggers 3 Claude API calls each. A misbehaving client can run up Anthropic costs rapidly. |
| No CORS policy | Low | API is not browser-facing today but could become one. |
| `API_BEARER_TOKEN` logged in plain text on startup? | Medium | Verify structlog config doesn't log env vars on startup. |
| Airtable PAT token in env — rotation not automated | Medium | A leaked PAT has broad table access. |
| OpenSearch basic auth in dev matches prod endpoint | High | `default.yaml` contains the production OpenSearch endpoint. A misconfigured dev run could write to prod. |
| `_REGISTRY` singleton is module-global | Low | If `reload=True` is called mid-request, another request sees a partially constructed registry. |
| No audit log for MCP tool invocations | Medium | There's no record of which Claude session called what tool with what args. |
| `synthesize_answer` sends up to 15 hit snippets to Claude | Low | If hits contain PII (emails, personal data), they transit Anthropic's API. Dalberg should verify their DPA with Anthropic covers this. |

---

## 10. Error Handling and Logging

### 10.1 Patterns Used

- **Structlog** with JSON output in prod (`MCP_ENV != dev`), pretty in dev
- `structlog.contextvars.bound_contextvars(run_id=...)` threads run ID through pipeline logs
- Per-document error isolation in `Pipeline.run()` — one failed doc doesn't abort the run
- Stage-level error collection (`EmbedReport.errors`, `IndexReport.errors`) without raising
- Voyage/OpenSearch: tenacity retries (5 attempts, exponential backoff, 1–60s, jitter)
- PostgreSQL helpers: all write ops catch and log exceptions without re-raising
- Retrieval router: `_safe_query` catches source errors, returns empty hits + error string in diagnostics

### 10.2 Gaps

- No distributed tracing (no `trace_id` threaded through MCP → router → sources)
- No metrics / instrumentation (Prometheus, CloudWatch — future work)
- `OpenSearchIndexer.document_hash_exists` returns `False` on exception (silently re-indexes)
- `_run_async` failure inside MCP tools surfaced as opaque Python exceptions to MCP client
- No dead-letter queue for failed SQS messages (SQS integration is planned, not yet built)

---

## 11. Coding Conventions

- **Python 3.11+** — uses `match`, `X | Y` union types, `from __future__ import annotations`
- **Pydantic v2** for settings; dataclasses (stdlib + `slots=True`) for config objects
- **Protocol-based interfaces**: `Embedder`, `Indexer`, `Chunker` — all pluggable with stubs for testing
- **`__all__` explicitly defined** on public modules
- **Ruff** for linting (line-length=100, py311); **mypy** for static typing
- **Lazy imports** for optional deps (`voyageai`, `psycopg2`, `pdfplumber`) — `ImportError` raised with helpful install instructions
- **`_p()` helper** (flush-print) consistently used instead of `print()` for non-TTY CI/SSM compatibility
- **No ORM** — plain psycopg2 with parameterised queries
- Chunk IDs are deterministic: `sha256(doc_hash + type + parent_pos + child_pos)` → fully idempotent re-runs

---

## 12. Recommended Optimizations (Prioritised for 4-Week Plan)

### Week 1 — Quick Wins

| # | Change | File | Expected Gain |
|---|---|---|---|
| 1 | Merge `classify_response_shape` into `plan_query` — one Claude call instead of two | `formatter.py`, `nl_planner.py` | ~40% latency reduction on `search()` |
| 2 | Fix `_run_async` — replace `ThreadPoolExecutor` bridge with `anyio.from_thread.run_sync` or declare FastMCP tools as `async` | `mcp/tools.py` | Eliminates thread churn under concurrent sessions |
| 3 | Add a lock around `_REGISTRY` initialisation | `retrieval/config.py` | Prevents race on cold start |
| 4 | Scope `document_hash_exists` to target table index only | `indexer/opensearch.py` | Reduces fan-out on every skip check |

### Week 2 — Pipeline Throughput

| # | Change | File | Expected Gain |
|---|---|---|---|
| 5 | Async Airtable ingestion — use `asyncio.gather` per record batch | `airtable_ingestion/pipeline.py` | 3–5× ingestion speed |
| 6 | Remove `refresh: true` from `delete_by_s3_key`, use `wait_for` or run async | `indexer/opensearch.py` | Reduces per-doc latency during re-index |
| 7 | Wire `table_index_map` in `build_pipeline()` from `tables.yaml` | `scripts/run_pipeline.py` | Correct per-table index routing |
| 8 | Add `pytest-asyncio` + integration tests for retrieval router | `tests/integration/` | Test coverage for most-used code path |

### Week 3 — Retrieval Quality

| # | Change | Expected Gain |
|---|---|---|
| 9 | Enable NL planner for multi-source queries (plan per source in parallel) | Structured Airtable lookup in multi-source mode |
| 10 | Batch parent `_mget` across sources in cross-source mode | Fewer round-trips, lower latency |
| 11 | Add `synthesize_answer` streaming via `anthropic.stream()` | Better UX; faster time-to-first-token |
| 12 | Enable `d_quals` source — create schema snapshot + index | Expand retrieval coverage |

### Week 4 — Observability & Security

| # | Change | Expected Gain |
|---|---|---|
| 13 | Add rate limiting on `/mcp/*` (e.g. `slowapi`) | Cost protection |
| 14 | Emit structured metrics per tool call (latency, tokens, source) to CloudWatch | Operational visibility |
| 15 | Remove v1 MCP mount once clients migrated | Reduces surface area |
| 16 | Add MCP tool invocation audit log to PostgreSQL | Compliance / debugging |

---

## 13. Environment Variables Reference

```bash
# AWS
AWS_REGION=eu-west-1
AWS_PROFILE=                      # blank on EC2 (uses IAM role)
S3_BUCKET=claude-mcp-object-store

# OpenSearch
OPENSEARCH_ENDPOINT=https://vpc-mcp-dev-os-search-eu-....es.amazonaws.com
OPENSEARCH_USERNAME=              # dev only
OPENSEARCH_PASSWORD=              # dev only

# Airtable
AIRTABLE_PAT_TOKEN=               # Personal Access Token
BASE_ID=                          # Airtable base ID (substituted in YAML)

# Voyage
VOYAGE_API_KEY=

# Anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-opus-4-6   # or claude-sonnet-4-6

# FastAPI
API_BEARER_TOKEN=
MCP_ENV=dev                       # dev → pretty logs; anything else → JSON logs

# PostgreSQL (optional)
POSTGRES_HOST=
POSTGRES_PORT=5432
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
```

---

## 14. Key Files Quick-Reference

```
src/
├── pipeline/
│   ├── embedding_pipeline/
│   │   ├── pipeline.py          ← Orchestrator (START HERE for write path)
│   │   ├── models.py            ← Chunk, Document, RunReport dataclasses
│   │   ├── reader/
│   │   │   ├── s3_reader.py     ← S3 listing + object download
│   │   │   ├── document_loader.py ← Wires reader + parser + table registry
│   │   │   └── tables.py        ← TableRegistry (tables.yaml → TableConfig)
│   │   ├── parser/
│   │   │   ├── text.py          ← Primary: section detection, ALL-CAPS headings
│   │   │   ├── docx.py          ← python-docx + fallback text mode
│   │   │   ├── pdf.py           ← pdfplumber
│   │   │   └── registry.py      ← extension → parser dispatch
│   │   ├── chunker/
│   │   │   ├── parent_child.py  ← ParentChildChunker (500/150/50 tok)
│   │   │   ├── resume.py        ← ResumeChunker (CV-aware)
│   │   │   ├── tokens.py        ← tiktoken helpers
│   │   │   └── registry.py      ← "parent_child" | "resume" → class
│   │   ├── embedder/
│   │   │   ├── voyage.py        ← VoyageEmbedder (tenacity retries)
│   │   │   └── stub.py          ← Zero-vector stub for tests / dry-run
│   │   └── indexer/
│   │       ├── opensearch.py    ← OpenSearchIndexer (bulk upsert)
│   │       └── mappings.py      ← KNN index mapping + settings
│   ├── airtable_ingestion/
│   │   ├── pipeline.py          ← AirtableAttachmentIngestionPipeline
│   │   ├── airtable_client.py   ← Thin wrapper around pyairtable
│   │   ├── data_extract.py      ← AirtableConnector (schema + attachment download)
│   │   ├── s3_uploader.py       ← S3Uploader
│   │   ├── normalizers.py       ← slugify_table_name, normalize_identifier
│   │   └── config.py            ← load_airtable_ingestion_settings()
│   ├── api/
│   │   ├── main.py              ← FastAPI app, middleware, routes, MCP mounts
│   │   ├── mcp_server.py        ← build_mcp() for v1 FastMCP
│   │   ├── auth.py              ← require_api_token dependency
│   │   └── settings.py          ← ApiSettings (bearer token)
│   └── common/
│       ├── aws.py               ← boto3_session, s3_client, get_secret
│       ├── opensearch.py        ← build_opensearch_client (SigV4 / basic auth)
│       ├── postgres.py          ← get_connection, ensure_schema, log helpers
│       └── ids.py               ← chunk_id(), document_hash()
└── retrieval/
    ├── config.py                ← SourceRegistry, LogicalSource, YAML parser
    ├── router.py                ← RetrievalRouter (async multi-source dispatch)
    ├── merger.py                ← rrf_merge (cross-source)
    ├── formatter.py             ← classify_shape, markdown table, synthesize_answer
    ├── models.py                ← RetrievalQuery, SearchResult, SearchResponse, Hint
    ├── settings.py              ← RetrievalRuntimeSettings
    ├── paths.py                 ← REPO_ROOT, DEFAULT_RETRIEVAL_SOURCES_PATH
    ├── sources/
    │   ├── opensearch.py        ← OpenSearchSource (KNN+BM25+hydration)
    │   └── airtable.py          ← AirtableSource (filterByFormula)
    ├── embedding/
    │   └── voyage.py            ← get_query_embedder() (async, shared singleton)
    ├── planner/
    │   ├── nl_planner.py        ← plan_query() → Claude API
    │   ├── prompts.py           ← PLANNER_SYSTEM, build_planner_user_message()
    │   └── anthropic_settings.py ← AnthropicQuerySettings
    └── mcp/
        ├── server.py            ← build_mcp() for v2 FastMCP (5 tools)
        └── tools.py             ← list_sources_impl, search_impl, etc.
```

---

## 15. Graphify Analysis Summary

Graphify produced 773 nodes and 1130 edges. The most connected (highest betweenness centrality) nodes are:

- `build_pipeline()` (0.145) — cross-community bridge connecting all 5 pipeline stages
- `s3_client()` (0.132) — central to both ingestion and embedding pipeline
- `AirtableClient` (0.096) — bridge between ingestion and retrieval
- `ParserRegistry` — 23 edges (most connected single node)
- `ParsedDocument` — 20 edges (core data type flowing through all parser/chunker/embedder stages)
- `DocumentLoader` — 19 edges
- `ChunkingConfig` — 19 edges

262 isolated nodes were detected — mostly docstring fragments and standalone constants. The graphify report is at `graphify-out/GRAPH_REPORT.md`.

---

*This document is the canonical project context for the 4-week optimisation engagement. Update it as architectural decisions are made.*
