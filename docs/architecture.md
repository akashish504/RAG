# Dalberg MCP — Full Architecture & Data Flow

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Infrastructure Layer](#2-infrastructure-layer)
3. [Claude ↔ MCP Connection](#3-claude--mcp-connection)
4. [MCP Tools Reference](#4-mcp-tools-reference)
5. [Query Execution Flow](#5-query-execution-flow)
6. [Retrieval Layer Deep-Dive](#6-retrieval-layer-deep-dive)
7. [Ingestion Pipeline](#7-ingestion-pipeline)
8. [OpenSearch Schema & Indexing](#8-opensearch-schema--indexing)
9. [Configuration Surface](#9-configuration-surface)
10. [Semantic Search: Current Implementation & How to Change It](#10-semantic-search-current-implementation--how-to-change-it)
11. [Adding a New Data Source](#11-adding-a-new-data-source)

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  OFFLINE (runs manually / on schedule)                              │
│                                                                     │
│  Airtable ──► run_airtable_ingestion.py ──► S3 raw/                │
│  S3 raw/  ──► run_pipeline.py ────────────► OpenSearch index        │
│                                                                     │
│  EVENT-DRIVEN (cron poller + always-on worker, d_quals only today) │
│  cron (weekly) ─► run_poller.py ──► SQS ──► run_worker.py (container)  │
│               (changed records)          ├─► ingest_one_record → S3│
│                                          └─► Pipeline.run_one → OS │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  ONLINE (always running on EC2)                                     │
│                                                                     │
│  Claude.ai ──► ALB (HTTPS 443) ──► EC2:80 ──► Nginx                │
│                                                ──► Uvicorn:8000     │
│                                                    ──► FastAPI app  │
│                                                        ──► FastMCP  │
│                                                            ──► 5 MCP tools
│                                                                ──► Airtable
│                                                                ──► OpenSearch
└─────────────────────────────────────────────────────────────────────┘
```

There are two completely separate phases:

- **Ingestion** — batch CLI jobs that pull data from Airtable, normalize it with Claude, chunk and embed it, and push it to OpenSearch. This runs on demand (or a schedule). The API container does **not** run these. Since feature 005, poll-enabled targets (`poll_enabled` in `config/airtable_ingestion.yaml`) also ingest **event-driven**: a cron poller (`scripts/run_poller.py`, weekly — Saturday 09:00 server time) enqueues changed records to SQS, and the `worker` container (`scripts/run_worker.py`) ingests + embeds them continuously. Only formats in the allowlist (`DEFAULT_ALLOWED_EXTENSIONS` / per-target `allowed_extensions`) are processed; everything else is skipped before download.
- **Serving** — the always-on Docker container that exposes the MCP endpoint, which Claude calls in real time.

---

## 2. Infrastructure Layer

### Physical path of every request from Claude

```
Claude.ai browser/API
    │
    │  HTTPS POST https://mcp.dev.dalberg.com/mcp/v2/mcp
    │  Host: mcp.dev.dalberg.com
    │
    ▼
AWS ALB  (mcp.dev.dalberg.com)
    │  ZeroSSL DV certificate terminates TLS
    │  Forwards to EC2 target group on port 80
    │  Host header preserved
    │
    ▼
EC2 instance  (:80)
    │
    ▼
Docker container  dalberg-mcp-api
    │  Port mapping: host:80 → container:80
    │
    ▼
Nginx  (:80)  [docker/nginx/nginx.conf]
    │
    │  location /mcp {
    │      proxy_set_header Host "localhost";     ← rewrites host header
    │      proxy_set_header X-Forwarded-Host $host;
    │      proxy_buffering off;                   ← streaming-friendly
    │      proxy_read_timeout 120s;
    │      proxy_pass http://127.0.0.1:8000;
    │  }
    │
    ▼
Uvicorn  (127.0.0.1:8000)
    │
    ▼
FastAPI app  [src/pipeline/api/main.py]
    │
    │  app.mount("/mcp/v2/", _mcp_v2.streamable_http_app())
    │
    ▼
FastMCP  — "Dalberg Retrieval"  (stateless_http=True)
    │  Transport: Streamable HTTP (MCP protocol over HTTP/SSE)
    │  DNS rebinding protection: disabled (safe behind nginx)
    │
    ▼
5 registered tools
```

### Why nginx rewrites the Host header

MCP SDK v1.23+ added DNS rebinding protection that validates the incoming `Host` header against an allowlist (`["127.0.0.1:*", "localhost:*"]`). The ALB passes `Host: mcp.dev.dalberg.com`, which is not in the list. Nginx rewrites it to `Host: localhost` before forwarding to Uvicorn. The real external hostname is preserved in `X-Forwarded-Host` for logging.

Since MCP SDK v1.23 also requires `host:port` format (e.g. `localhost:8000`) to match `localhost:*`, we explicitly disable the protection in Python via `TransportSecuritySettings(enable_dns_rebinding_protection=False)`. Nginx's rate limiting (10 req/s, burst 20) provides the external access boundary instead.

### Docker services

| Service | Purpose | Always starts? |
|---------|---------|----------------|
| `api` | HTTP server on host:80 | Yes (no profile, `restart: unless-stopped`) |
| `worker` | SQS consumer — event-driven ingestion + embedding | Yes (no profile, `restart: unless-stopped`) |
| `pipeline` | CLI runner for ingestion/embedding (also the cron poller's one-shot container) | No (run manually / by cron) |
| `s3-smoke`, `test`, `lint`, `shell` | Dev utilities | No (`tools` profile) |

### Startup sequence (inside container)

1. `entrypoint-api.sh` checks `API_BEARER_TOKEN` is set (fails fast otherwise)
2. Launches Uvicorn on `127.0.0.1:8000`
3. Health-polls `/health` up to 30×0.2s
4. `exec nginx -g "daemon off;"` — nginx takes over as PID 1

---

## 3. Claude ↔ MCP Connection

### How the connection is established

Claude.ai uses the **Model Context Protocol** (MCP) over Streamable HTTP. The connector is configured in:

> Claude.ai → Settings → Integrations → Add custom connector
> - URL: `https://mcp.dev.dalberg.com/mcp/v2/mcp`
> - Authentication: None (Bearer middleware disabled for now)

When a Claude chat loads, or when tools are first needed, Claude performs the MCP handshake:

```
Claude ──POST /mcp/v2/mcp──►  FastMCP
        {
          "jsonrpc": "2.0",
          "method": "initialize",
          "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Claude", "version": "..."}
          }
        }

FastMCP ◄──200 SSE event──
        {
          "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "Dalberg Retrieval", "version": "1.27.1"},
            "capabilities": {"tools": {"listChanged": false}, ...},
            "instructions": "Source-agnostic retrieval over Dalberg..."
          }
        }

Claude ──POST /mcp/v2/mcp──► FastMCP
        {"method": "tools/list"}

FastMCP ◄──200 SSE event──
        {"result": {"tools": [
          {name: "list_sources", inputSchema: {...}},
          {name: "get_schema",   inputSchema: {...}},
          {name: "semantic_search", inputSchema: {...}},
          {name: "airtable_lookup", inputSchema: {...}},
          {name: "search",       inputSchema: {...}}
        ]}}
```

After this handshake Claude knows which tools exist, their parameter names, types, and descriptions. It uses this to decide which tool to call and with what arguments — entirely autonomously based on the user's question.

### Tool call flow

```
Claude ──POST /mcp/v2/mcp──►  FastMCP
        {
          "method": "tools/call",
          "params": {
            "name": "search",
            "arguments": {
              "question": "Who works on climate finance in East Africa?",
              "sources": ["dalberg_profiles"]
            }
          }
        }

FastMCP ◄──200 SSE event──
        {
          "result": {
            "content": [{"type": "text", "text": "{...JSON response...}"}]
          }
        }
```

### Transport: Streamable HTTP vs SSE

FastMCP uses **Streamable HTTP** (not SSE). Each tool call is a single HTTP POST. The response is returned as a Server-Sent Events stream with a single `event: message` frame containing the JSON result. This is why `proxy_buffering off` is set in nginx and the `Accept: text/event-stream` header is required.

### Authentication status

| Path | Auth today | Auth in production |
|------|------------|-------------------|
| `GET /health` | None | None |
| `POST /mcp/v2/mcp` | **None** (disabled for dev) | Bearer token middleware |
| `GET /v1/*` test routes | Bearer (`API_BEARER_TOKEN`) | Bearer |

The `mcp_bearer_middleware` function was removed from `main.py` during development. Before production: add a FastAPI middleware that reads `Authorization: Bearer <token>`, validates with `secrets.compare_digest`, and returns 401 if missing/invalid.

---

## 4. MCP Tools Reference

All five tools are registered in `src/retrieval/mcp/tools.py` and bound to the FastMCP instance in `register_tools()`. Every tool returns a JSON string (the MCP `text` content block).

### `list_sources()`

**Purpose:** Discover what data sources are available.

**Input:** none

**Returns:**
```json
{
  "ok": true,
  "sources": [
    {
      "name": "dalberg_profiles",
      "display_name": "Dalberg Profiles",
      "enabled": true,
      "capabilities": ["semantic", "structured"],
      "description": "..."
    }
  ]
}
```

**Internal path:** `SourceRegistry.describe_all()` — reads `config/retrieval_sources.yaml`, no adapter I/O.

---

### `get_schema(source: str)`

**Purpose:** Get all field names, types, and select options for a source before writing a formula.

**Input:** `source` — name from `list_sources()`

**Returns:** `SchemaDescriptor` serialized — all fields with `name`, `type`, `select_choices`, `is_long_text`, etc.

**Internal path:** `LogicalSource.get_schema()` → `AirtableSource._load_snapshot()` → reads local JSON snapshot from `data/metadata/airtable/`.

---

### `semantic_search(query, source?, top_k?, filters?)`

**Purpose:** Pure KNN + BM25 hybrid vector search over embedded chunks. No Claude call.

**Parameters:**
- `query` (str, required) — NOT `question`
- `source` (str, optional, singular) — NOT `sources` (plural)
- `top_k` (int, default 10, max 50)
- `filters` (dict, optional)

**Returns:** Raw ranked chunks with provenance metadata. No synthesis.

**Internal path:** `RetrievalQuery(mode="semantic_only")` → `RetrievalRouter.run()` → `OpenSearchSource.search_semantic()`.

---

### `airtable_lookup(source, formula?, fields?, max_records?)`

**Purpose:** Exact structured query using Airtable `filterByFormula`. No Claude call.

**Parameters:**
- `source` (str, required)
- `formula` (str, optional) — Airtable formula string, e.g. `{Region} = "East Africa"`
- `fields` (list[str], optional) — field subset to return
- `max_records` (int, optional)

**Returns:** Structured rows from Airtable. Long-text fields truncated at 800 chars with a `semantic_search` hint.

**Internal path:** `RetrievalQuery(mode="airtable_only")` → `AirtableSource.filter_structured()` → `pyairtable`.

---

### `search(question, sources?, mode?, top_k?, include_answer?)`

**Purpose:** Full NL pipeline — Claude plans the query, dispatches primitives, merges with RRF, synthesizes an answer.

**Parameters:**
- `question` (str, required) — NOT `query`
- `sources` (list[str], optional, plural) — NOT `source`
- `mode` (`"hybrid"` | `"semantic_only"` | `"airtable_only"`, default `"hybrid"`)
- `top_k` (int, default 10)
- `include_answer` (bool, default True)

**Returns:** Full `SearchResponse` with hits, markdown table, synthesized answer, and cross-tool hints.

**Internal path:** `plan_query()` (Claude) → `RetrievalRouter.run()` → `format_response()` (Claude again for shape + synthesis).

---

## 5. Query Execution Flow

### `semantic_search` flow

```
Claude calls semantic_search(query="climate finance East Africa", source="dalberg_profiles")
    │
    ▼
semantic_search_impl()  [tools.py]
    │  builds RetrievalQuery(mode="semantic_only", question=query, sources=["dalberg_profiles"])
    ▼
RetrievalRouter.run(q)  [router.py]
    │
    ├─ _resolve_sources(["dalberg_profiles"]) → [LogicalSource]
    │
    ├─ _embed_query("climate finance East Africa")
    │       └─ VoyageEmbedder.embed(["query: climate finance East Africa"])
    │              → list[float]  (1024 dims)
    │
    ├─ q.embedding = [0.021, -0.034, ...]
    │
    └─ LogicalSource.query(q)
           │
           └─ OpenSearchSource.search_semantic(query=..., embedding=..., top_k=10)
                  │
                  ├─ _knn_search()   → top-20 child chunks  (KNN over embedding field)
                  ├─ _bm25_search()  → top-20 child chunks  (BM25 over text field)
                  ├─ _rrf_merge()    → fused top-20 by RRF
                  ├─ _mget()         → hydrate parent chunks via parent_chunk_id
                  ├─ _dedup_by_person()  → collapse multiple chunks from same person
                  └─ returns list[SearchResult] (top 10)
    │
    ▼
SearchResponse(ok=True, hits=[...10 results...], hints=[airtable_lookup hints])
    │
    ▼  (JSON serialized)
Claude receives raw chunks + provenance metadata
```

### `search` flow (full NL pipeline)

```
Claude calls search(question="Who leads climate finance work in East Africa?")
    │
    ▼
search_impl()  [tools.py]
    │
    ├─ plan_query(question, schema)  [nl_planner.py]
    │       │
    │       │  Claude (claude-sonnet-4-6) receives:
    │       │   - PLANNER_SYSTEM prompt
    │       │   - question
    │       │   - compact field catalog from SchemaDescriptor
    │       │
    │       └─ returns plan dict:
    │              {
    │                "mode": "hybrid",
    │                "airtable_formula": "{Region} = \"East Africa\"",
    │                "semantic_query": "climate finance leadership",
    │                "max_records": null,
    │                "top_k": 10
    │              }
    │
    ├─ RetrievalQuery from plan
    │
    ├─ RetrievalRouter.run(q)
    │       │
    │       ├─ embed("climate finance leadership")  → [float x 1024]
    │       │
    │       └─ LogicalSource.query(q)  [mode=hybrid]
    │               │
    │               ├─ AirtableSource.filter_structured(formula='{Region}="East Africa"')
    │               │       └─ pyairtable → rows from Airtable API
    │               │
    │               └─ OpenSearchSource.search_semantic(query, embedding)
    │                       └─ KNN + BM25 + RRF + parent hydration
    │
    │       └─ rrf_merge({"dalberg_profiles": [at_hits + os_hits]})
    │              → merged ranked list
    │
    └─ format_response(question, hits, plan, ...)  [formatter.py]
            │
            ├─ classify_response_shape()  (Claude — shape: FULL_RECORDS / COLUMN_SUBSET / COUNT_ONLY)
            ├─ build_markdown_table(hits)
            └─ synthesize_answer()  (Claude — NL answer over top-15 hits)
    │
    ▼
SearchResponse(ok=True, hits=[...], markdown_table="| Name | ...", answer="Based on...")
    │
    ▼  (JSON serialized)
Claude renders answer to user
```

### Claude calls in a single `search` invocation

| Call # | Model | Purpose |
|--------|-------|---------|
| 1 | `claude-sonnet-4-6` | NL planner — generate `airtable_formula` + `semantic_query` |
| 2 | `claude-sonnet-4-6` | Shape classifier — decide response format |
| 3 | `claude-sonnet-4-6` | Answer synthesis — produce NL answer from top-15 hits |

All three happen inside the tool call before the response returns to Claude.ai. The Claude model used is configured via `ANTHROPIC_MODEL` env var (default `claude-sonnet-4-6`).

---

## 6. Retrieval Layer Deep-Dive

### Class hierarchy

```
SourceRegistry
    └── LogicalSource  (one per enabled source in retrieval_sources.yaml)
            ├── AirtableSource    (protocol: AirtableSourceProtocol)
            └── OpenSearchSource  (protocol: OpenSearchSourceProtocol)

RetrievalRouter
    └── uses SourceRegistry
    └── uses VoyageEmbedder  (lazy-loaded)
    └── uses rrf_merge (merger.py)

format_response  (formatter.py)
    └── uses Anthropic API (3 calls)
```

### RetrievalRouter.run() in detail

```python
async def run(self, q: RetrievalQuery) -> SearchResponse:
    sources = self._resolve_sources(q.sources)     # "*" → all enabled

    # Embed once, share across all sources
    if needs_embedding(q):
        q.embedding = await self._embed_query(q.question)

    # Fan out concurrently
    tasks = [self._safe_query(src, q) for src in sources]
    results = await asyncio.gather(*tasks)

    # Collect hits per source
    per_source_hits = {src.name: hits for src, hits, ... in results}

    # Merge
    if len(sources) > 1:
        merged = rrf_merge(per_source_hits, rrf_k=60)
    else:
        merged = _flatten_per_source(per_source_hits)

    return SearchResponse(ok=True, hits=merged, hints=all_hints, diagnostics=...)
```

### OpenSearchSource.search_semantic() in detail

```python
async def search_semantic(self, *, query, embedding, top_k, filters):
    fetch_k = max(self.cfg.over_fetch_k, top_k)  # e.g. 50

    # Run KNN and BM25 in parallel
    knn_hits, bm25_hits = await asyncio.gather(
        self._knn_search(embedding, fetch_k, filters),
        self._bm25_search(query, fetch_k, filters)
    )

    # Internal RRF merge (KNN + BM25 within this index)
    fused = self._rrf_merge(knn_hits, bm25_hits, fetch_k)

    # Hydrate parent documents via mget
    parent_ids = [h.parent_chunk_id for h in fused if h.parent_chunk_id]
    parents = await self._mget(parent_ids)
    merged = _attach_parents(fused, parents)

    # Dedup: multiple child chunks from same parent → keep top-scored parent
    deduped = _dedup_by_parent(merged)
    # Dedup: multiple parents from same person → keep top-scored person
    final = _dedup_by_person(deduped)[:top_k]

    # Emit hints for structured lookup
    hints = [Hint(tool="airtable_lookup", reason="structured_fields_available", ...)
             for person in unique_people(final)]

    return final, hints
```

### KNN query (current implementation)

```json
{
  "knn": {
    "embedding": {
      "vector": [0.021, -0.034, ...],
      "k": 50,
      "filter": {
        "bool": {
          "must": [{"term": {"chunk_type": "child"}}],
          "filter": [ ...user_filters... ]
        }
      }
    }
  },
  "_source": ["text", "chunk_id", "parent_chunk_id", "table_name", "primary_key", ...]
}
```

Index settings: HNSW Lucene engine, cosine similarity, `m=16`, `ef_construction=100`, `ef_search=512`.

### BM25 query (current implementation)

```json
{
  "bool": {
    "must": [{"term": {"chunk_type": "child"}}],
    "should": [
      {"match": {"text": {"query": "climate finance", "boost": 1}}},
      {"match_phrase": {"text": {"query": "climate finance", "boost": 2}}}
    ],
    "minimum_should_match": 1
  }
}
```

Custom analyzer `dalberg_english` applied to `text` field: domain synonyms + stemming.

### RRF merge (two levels)

**Level 1 — within OpenSearch (KNN + BM25 fusion):**
```
rrf_score(doc) = Σ 1 / (rrf_k + rank_in_list)
               where rrf_k = 60
```

**Level 2 — cross-source (router):**
Same formula applied across different source hit lists. The `score_weight=0.001` adds a small bonus for the original adapter score to break ties.

---

## 7. Ingestion Pipeline

### Stage 1: Airtable → S3

**Command:** `docker compose run --rm pipeline python scripts/run_airtable_ingestion.py --target profiles_sync`

**Config:** `config/airtable_ingestion.yaml`

```
Airtable base (app0ZvoNuWDMx4NeC)
    └── "Dalberg Profiles" table
            ├── CV Attachment  (PDF/DOCX)
            │       ├── Download original binary
            │       ├── Run llm_cv normalizer (Claude Haiku → structured plain text)
            │       └── Upload to S3:
            │           raw/dalberg_profiles/{email}/cv_attachment/{id}__resume.pdf
            │           raw/dalberg_profiles/{email}/cv_attachment/{email}__normalized.txt
            │
            └── Bio Attachment (PDF/DOCX)
                    ├── Download original binary
                    ├── Run llm_bio normalizer (Claude Haiku → structured plain text)
                    └── Upload to S3:
                        raw/dalberg_profiles/{email}/bio_attachment/{id}__bio.pdf
                        raw/dalberg_profiles/{email}/bio_attachment/{email}__normalized.txt
```

**Idempotency:** If `{id}__filename` already exists in S3, the record is skipped. This means re-running is safe.

### Stage 2: S3 → chunk → embed → OpenSearch

**Command:** `docker compose run --rm pipeline python scripts/run_pipeline.py --prefix raw/ --embedder voyage`

```
S3Reader
    └── Lists all objects under raw/
    └── For each directory with __normalized.txt: skips raw binaries in same dir
    └── Parses .txt, .pdf, .docx via ParserRegistry

DocumentLoader
    └── Extracts text + provenance (table_name, primary_key, column_name, s3_key)

ChunkerRegistry  [strategy: "resume" for dalberg_profiles]
    └── Splits by CV section headers (NAME, EXPERIENCE, EDUCATION, SKILLS...)
    └── parent chunks: up to 1500 tokens  (section-level)
    └── child chunks:  up to  350 tokens  (overlapping windows within section)
    └── Each child carries parent_chunk_id reference

VoyageEmbedder
    └── Embeds ONLY child chunks
    └── Prefix: "passage: {text}"
    └── Model: voyage-4, 1024 dimensions
    └── Batch size 32, retries on 429/5xx

OpenSearchIndexer  [index: mcp-dalberg-profiles]
    └── Check document_hash across all managed indexes
    └── If changed: delete_by_s3_key() then bulk upsert
    └── Bulk size: 256 docs/batch
    └── _id = chunk_id (idempotent)
    └── Parents indexed without embedding field
    └── Children indexed with embedding field
```

### S3 key format

```
raw/{table_slug}/{primary_key}/{column_slug}/{attachment_id}__{filename}
raw/{table_slug}/{primary_key}/{column_slug}/{primary_key}__normalized.txt
```

Example:
```
raw/dalberg_profiles/jane.doe@dalberg.com/cv_attachment/attXYZ__resume.pdf
raw/dalberg_profiles/jane.doe@dalberg.com/cv_attachment/jane.doe@dalberg.com__normalized.txt
```

---

## 8. OpenSearch Schema & Indexing

### Index name

`mcp-dalberg-profiles` (configured in `config/tables.yaml` and `config/retrieval_sources.yaml`)

### Mapping (key fields)

| Field | Type | Purpose |
|-------|------|---------|
| `embedding` | `knn_vector` (1024 dim, HNSW cosine) | KNN search target |
| `text` | `text` with `dalberg_english` analyzer | BM25 search target |
| `chunk_id` | `keyword` | Unique chunk identifier |
| `chunk_type` | `keyword` | `"parent"` or `"child"` |
| `parent_chunk_id` | `keyword` | Links child → parent |
| `document_hash` | `keyword` | SHA-256 of source file (skip-if-unchanged) |
| `table_name` | `keyword` | e.g. `"dalberg_profiles"` |
| `primary_key` | `keyword` | Email address |
| `column_name` | `keyword` | e.g. `"cv_attachment"` |
| `s3_key` | `keyword` | Full S3 path |
| `section_canonical` | `keyword` | Top-level CV section |
| `metadata` | `object` (dynamic) | Extra provenance |

### Index settings

- 1 shard, 1 replica
- `refresh_interval: 30s`
- `knn.algo_param.ef_search: 512`
- HNSW: `m=16`, `ef_construction=100`

### Connection modes

| Mode | When active | Auth |
|------|------------|------|
| AWS SigV4 | No `OPENSEARCH_USERNAME` in env | IAM role / env credentials |
| Basic auth | `OPENSEARCH_USERNAME` + `OPENSEARCH_PASSWORD` set | Username/password |

---

## 9. Configuration Surface

### `config/retrieval_sources.yaml`

The single YAML file that controls what the MCP API serves. Adding a source here (with `enabled: true`) makes it immediately available via `list_sources()` — no code change needed.

Key sections per source:
```yaml
sources:
  dalberg_profiles:
    enabled: true
    display_name: "Dalberg Profiles"
    airtable:
      base_id: "${BASE_ID}"          # env var substitution
      table_name: "Dalberg Profiles"
      schema_snapshot_path: "data/metadata/airtable/dalberg_profiles_schema.json"
    opensearch:
      index_name: "mcp-dalberg-profiles"
      k: 10
      over_fetch_k: 50
      search_mode: "hybrid"          # knn_only | bm25_only | hybrid
    embedding:
      model: "voyage-4"
      dims: 1024
    ranking:
      fusion: "rrf"
      rrf_k: 60
```

### Environment variables (`.env` on EC2)

| Variable | Used by | Required |
|----------|---------|---------|
| `API_BEARER_TOKEN` | FastAPI Bearer auth for `/v1/*` routes | Yes |
| `AIRTABLE_PAT_TOKEN` | Airtable adapter + ingestion | Yes |
| `BASE_ID` | YAML env expansion for Airtable base | Yes |
| `ANTHROPIC_API_KEY` | Planner + formatter (Claude calls) | Yes |
| `VOYAGE_API_KEY` | VoyageEmbedder | Yes |
| `OPENSEARCH_URL` | OpenSearch connection | Yes |
| `OPENSEARCH_USERNAME` | Basic auth (if not using SigV4) | Conditional |
| `OPENSEARCH_PASSWORD` | Basic auth | Conditional |
| `S3_BUCKET` | Ingestion S3 upload | Yes (ingestion) |
| `AWS_REGION` | AWS SDK (default `eu-west-1`) | No |

---

## 10. Semantic Search: Current Implementation & How to Change It

### Where it lives

Everything is in one file: `src/retrieval/sources/opensearch.py`

| Method | What it does |
|--------|-------------|
| `_knn_search()` | Builds and runs the KNN (vector similarity) OpenSearch query |
| `_bm25_search()` | Builds and runs the BM25 (keyword) OpenSearch query |
| `_rrf_merge()` | Fuses KNN and BM25 results using Reciprocal Rank Fusion |
| `search_semantic()` | Orchestrates the above three; hydrates parents; deduplicates |

### Current technique: KNN + BM25 hybrid with RRF

```
query text + embedding
    │                   │
    ▼                   ▼
BM25 query          KNN query
(keyword match)     (cosine similarity over 1024-dim vectors)
    │                   │
    └─────── RRF ────────┘
             │
        fused ranking
             │
        parent hydration (mget)
             │
        dedup by person
```

### How to change the retrieval technique

**Option A — KNN only (disable BM25)**

In `config/retrieval_sources.yaml`:
```yaml
opensearch:
  search_mode: "knn_only"   # was: "hybrid"
```
No code change needed — `_bm25_search` is skipped when `search_mode != "hybrid"`.

**Option B — BM25 only (disable KNN)**

In `config/retrieval_sources.yaml`:
```yaml
opensearch:
  search_mode: "bm25_only"
```

**Option C — Change the KNN algorithm (HNSW → IVF, or different similarity)**

In `src/pipeline/embedding_pipeline/indexer/mappings.py`, change the `knn_vector` mapping:
```python
# Current (HNSW cosine):
"embedding": {
    "type": "knn_vector",
    "dimension": 1024,
    "method": {
        "name": "hnsw",
        "space_type": "cosinesimil",
        "engine": "lucene",
        "parameters": {"m": 16, "ef_construction": 100}
    }
}

# Change to innerproduct (dot product):
"space_type": "innerproduct"

# Change to Faiss engine:
"engine": "faiss",
"parameters": {"m": 16, "ef_construction": 256}
```
Note: changing the mapping requires recreating the index and re-running the embedding pipeline.

**Option D — Change the fusion algorithm (RRF → weighted score)**

In `src/retrieval/sources/opensearch.py`, replace `_rrf_merge()`:
```python
def _rrf_merge(self, knn_hits, bm25_hits, top_k):
    # Current: RRF
    # Replace with weighted score fusion:
    knn_weight = 0.7
    bm25_weight = 0.3
    scores = {}
    for rank, hit in enumerate(knn_hits):
        scores[hit.chunk_id] = (hit, knn_weight * hit.score)
    for rank, hit in enumerate(bm25_hits):
        if hit.chunk_id in scores:
            scores[hit.chunk_id] = (scores[hit.chunk_id][0],
                                    scores[hit.chunk_id][1] + bm25_weight * hit.score)
        else:
            scores[hit.chunk_id] = (hit, bm25_weight * hit.score)
    return [hit for hit, _ in sorted(scores.values(), key=lambda x: -x[1])][:top_k]
```

**Option E — OpenSearch native hybrid query (score-based fusion in the query)**

Instead of two separate queries + Python-level merge, use OpenSearch's native hybrid query:
```python
def _hybrid_search(self, query, embedding, top_k):
    body = {
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {"embedding": {"vector": embedding, "k": top_k}}},
                    {"match": {"text": query}}
                ]
            }
        },
        "search_pipeline": {"phase_results_processors": [{"normalization-processor": {
            "normalization": {"technique": "min_max"},
            "combination": {"technique": "arithmetic_mean", "parameters": {"weights": [0.7, 0.3]}}
        }}]}
    }
```
This requires the OpenSearch hybrid search plugin and a search pipeline configured on the cluster.

**Option F — Re-ranking with a cross-encoder**

Add a re-ranking step after the initial retrieval in `search_semantic()`:
```python
# After fused = self._rrf_merge(...)
# Add cross-encoder re-ranking:
from sentence_transformers import CrossEncoder
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
pairs = [(query, hit.text) for hit in fused]
scores = reranker.predict(pairs)
reranked = sorted(zip(fused, scores), key=lambda x: -x[1])
fused = [hit for hit, _ in reranked][:top_k]
```

### Summary: which file to edit for each change

| What to change | File |
|---------------|------|
| Enable/disable KNN or BM25 | `config/retrieval_sources.yaml` (`search_mode`) |
| KNN vector similarity (cosine → dot) | `src/pipeline/embedding_pipeline/indexer/mappings.py` |
| KNN HNSW parameters (m, ef_construction) | `src/pipeline/embedding_pipeline/indexer/mappings.py` |
| KNN ef_search (query-time accuracy) | `config/default.yaml` (`ef_search`) |
| KNN ↔ BM25 fusion logic | `src/retrieval/sources/opensearch.py` → `_rrf_merge()` |
| Add cross-encoder re-ranking | `src/retrieval/sources/opensearch.py` → `search_semantic()` |
| Embedding model (voyage-4 → other) | `config/retrieval_sources.yaml` (`embedding.model`) + re-run pipeline |

---

## 11. Adding a New Data Source

To add a new table (e.g. `proposal_library`) — **zero code changes required**:

1. **Add Airtable sync target** in `config/airtable_ingestion.yaml`
2. **Add chunking config** in `config/tables.yaml` (s3_prefix, index_name, chunker_strategy)
3. **Add retrieval source** in `config/retrieval_sources.yaml`:
   ```yaml
   proposal_library:
     enabled: true
     display_name: "Proposal Library"
     airtable:
       base_id: "${PROPOSALS_BASE_ID}"
       table_name: "Proposals"
       schema_snapshot_path: "data/metadata/airtable/proposal_library_schema.json"
     opensearch:
       index_name: "mcp-proposal-library"
       search_mode: "hybrid"
   ```
4. **Run ingestion:** `run_airtable_ingestion.py --target proposals_sync`
5. **Run pipeline:** `run_pipeline.py --prefix raw/proposal_library/`
6. **Restart API:** `docker compose up -d --build`

Claude will automatically discover it via `list_sources()` and start using it.
