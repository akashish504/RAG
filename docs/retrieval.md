# Retrieval Module (v2)

Source-agnostic retrieval over Dalberg knowledge stores. One MCP server,
five tools, N logical sources defined in YAML.

* MCP mount: **`/mcp/v2/`** (FastMCP, streamable HTTP)
* Code lives under [`src/retrieval/`](../src/retrieval/)
* Per-source config: [`config/retrieval_sources.yaml`](../config/retrieval_sources.yaml)
* Legacy `/mcp/` mount stays online; both legacy tools forward to v2 via
  [`pipeline/api/mcp_server.py`](../src/pipeline/api/mcp_server.py).

## Why

Single-source MCP code couples the tool catalogue to the data layout: the
catalogue grows linearly with sources, and there is no place for hybrid
(structured + semantic) retrieval. The v2 layer fixes that: adding
`d_quals`, `proposal_library`, etc. is a YAML edit, and every tool returns
the same envelope so the MCP client never special-cases.

## The five tools

Every tool returns the same JSON envelope (see *Envelope contract* below).

| Tool | Calls Claude? | Inputs | Use when |
|------|---------------|--------|----------|
| `list_sources()` | no | none | Discover what's available; capabilities + enabled flag |
| `get_schema(source)` | no | source name | Inspect field catalog before writing a `formula` |
| `semantic_search(query, source?, top_k?, filters?)` | no | NL query | Vector search over embedded chunks (KNN + BM25 hybrid) |
| `airtable_lookup(source, formula?, fields?, max_records?)` | no | Airtable filterByFormula | Exact, repeatable structured queries |
| `search(question, sources?, mode?, top_k?, include_answer?)` | yes (planner + answer) | NL question | NL convenience: plans the formula, dispatches primitives, merges, optionally synthesises an answer |

The two primitives (`semantic_search`, `airtable_lookup`) never call
Claude. The composer (`search`) calls Claude twice (planner + answer
synthesis) when there is exactly one source; for multi-source it skips the
planner and runs a cross-source semantic search instead.

## Envelope contract

```json
{
  "ok": true,
  "hits": [
    {
      "source": "dalberg_profiles",
      "source_type": "semantic",      // or "structured"
      "score": 0.82,
      "text": "...",
      "chunk_id": "…",
      "record_id": null,              // populated by Airtable adapter
      "parent_chunk_id": "…",
      "metadata": { "table_name": "Dalberg Profiles", ... },
      "payload": { "child": {...}, "parent": {...} }
    }
  ],
  "hint_count": 1,
  "hints": [
    { "tool": "airtable_lookup", "source": "dalberg_profiles",
      "reason": "structured_fields_available",
      "detail": { "record_primary_key": "...", "table_name": "..." } }
  ],
  "response_mode": "full_records",   // count_only | column_subset | full_records
  "columns": ["Display Name", "Email"],
  "markdown_table": "| Display Name | … |",
  "answer": "There are 12 advisors based in East Africa…",
  "plan": {                          // present only when `search` planned the call
    "mode": "hybrid",
    "airtable_formula": "{Office Region}='East Africa'",
    "semantic_query": "advisors East Africa",
    "max_records": null,
    "top_k": 10,
    "rationale": "...",
    "model": "claude-sonnet-4-6"
  },
  "diagnostics": {
    "elapsed_ms": 412,
    "sources": {
      "dalberg_profiles": { "hits": 7, "hints": 1, "latency_ms": 280, "error": null }
    },
    "embedding": { "supplied_by_caller": false, "computed": true, "error": null },
    "merge": "rrf"
  },
  "error": null
}
```

`ok: false` responses populate `error` and leave `hits` empty.

## Hint protocol

Hints are structured cross-tool follow-ups. They never block; they tell
the MCP client about a more precise tool that fits the unmet need.

| Reason | Emitted by | Detail |
|--------|------------|--------|
| `long_text_field_truncated` | `AirtableSource` after truncating a row's long-text field beyond `long_text_truncate` | `{"field", "rows_truncated", "truncate_at"}` — caller can re-issue via `semantic_search` to get the full content from chunks |
| `structured_fields_available` | `OpenSearchSource` when a child chunk's `primary_key` is set | `{"record_primary_key", "table_name"}` — caller can re-issue via `airtable_lookup` to get the full Airtable row |

Add new hint reasons by adding a `Hint(tool=..., source=..., reason=...,
detail=...)` in the relevant adapter; the envelope passes them through
untouched.

## Multi-source config

```yaml
defaults:
  embedding: { provider: voyage, model: voyage-4, dims: 1024, query_prefix: "query: " }
  ranking:   { fusion: rrf, rrf_k: 60 }
  search_mode: hybrid
  top_k: 10
  airtable: { long_text_truncate: 800 }

sources:
  dalberg_profiles:
    display_name: "Dalberg Profiles"
    enabled: true
    description: "Dalberg employee profiles..."
    chunking_strategy: resume
    identifier_field: "Email"
    airtable:
      base_id: ${BASE_ID}
      table_name: "Dalberg Profiles"
      schema_snapshot_path: config/dalberg_profiles_schema.json
    opensearch:
      index_name: mcp-dalberg-profiles
      k: 10
      search_mode: hybrid
```

Adding a new source:

1. Add a top-level entry under `sources:` with its `base_id`,
   `table_name`, `index_name`, and `chunking_strategy`.
2. Snapshot its Airtable schema:
   `python scripts/export_airtable_schema_snapshot.py --source <name>`.
3. Make sure the writer pipeline's `config/tables.yaml` has a matching
   entry pointing at the same OpenSearch index.
4. Flip `enabled: true`.

The MCP catalogue does not change. `list_sources()` reports the new
source on the next `SourceRegistry.load()`.

`${VAR}` placeholders are expanded from process environment at load time;
missing required vars raise immediately so misconfiguration is loud.

## Mode semantics

`mode` chooses how a source dispatches its query:

* `airtable_only` — run only the Airtable adapter. Returns structured
  rows ranked by Airtable's natural order. Use for exact filters and
  counts.
* `semantic_only` — run only the OpenSearch adapter. Returns
  parent-hydrated child chunks ranked by KNN + BM25 RRF. Use when the
  question is about content / topics that live in long-text fields or
  document chunks.
* `hybrid` (default) — run both adapters concurrently. Per-source results
  are deduplicated by `(source, chunk_id|record_id)`. When more than one
  source is selected, a cross-source RRF re-ranks the merged list.

The `search` planner normalises mode requests against each source's
declared `capabilities`: `airtable_only` against an OpenSearch-only
source becomes `semantic_only`, and vice versa.

## Boundary rule

`src/retrieval/` is allowed to import only:

* `pipeline.common.{aws,ids,opensearch}` — stable cross-cutting utilities.
* `pipeline.airtable_ingestion.data_extract.AirtableConnector` — the only
  Airtable client in the repo.

It MUST NOT import `pipeline.embedding_pipeline.{pipeline,chunker,parser,
embedder,indexer.OpenSearchIndexer}`. The writer evolves independently.

## Lifecycle and concurrency

* `SourceRegistry` is built once per process (cached at module level via
  `retrieval.config.get_registry()`); call `get_registry(reload=True)` to
  pick up YAML changes.
* `OpenSearchSource` and `AirtableSource` are stateless after construction
  — safe to share across requests.
* The router awaits `asyncio.gather` over per-source query coros. Each
  source's sync I/O (Airtable HTTP, OpenSearch HTTP) is offloaded to
  `asyncio.to_thread` so the event loop never blocks.
* Query embedding is computed once per request inside the router and
  reused across sources. Identical queries within a worker reuse the
  cached vector via the `VoyageQueryEmbedder` LRU cache.

## Migration map (v1 → v2)

| v1 file (deleted unless noted) | v2 home |
|--------------------------------|---------|
| `pipeline/api/airtable_profiles_settings.py` | replaced by `retrieval/config.py` (multi-source) |
| `pipeline/api/airtable_profiles_service.py` | `retrieval/sources/airtable.py` (generic) |
| `pipeline/api/nl_airtable_query.py` | split into `retrieval/planner/{nl_planner,prompts}.py` |
| `pipeline/api/nl_response_shape.py` | folded into `retrieval/formatter.py` (`ResponseMode` + classifier) |
| `pipeline/api/nl_query_human_response.py` | folded into `retrieval/formatter.py` (markdown + Claude answer) |
| `pipeline/api/anthropic_query_settings.py` | `retrieval/planner/anthropic_settings.py` |
| `pipeline/api/mcp_server.py` (kept) | shrunk to deprecation forwarder; both v1 tools call into v2 |
| `pipeline/embedding_pipeline/indexer/opensearch.py::build_opensearch_client` | extracted to `pipeline/common/opensearch.py`, re-exported for back-compat |

## Verification (post-deploy checks)

* `python -c "from retrieval.config import SourceRegistry; print(SourceRegistry.load(instantiate_adapters=False).names())"`
  → returns the enabled source names.
* `curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/test/sources`
  → returns the same `list_sources` payload over REST.
* MCP client → `list_sources()` shows enabled sources from YAML; flip
  `enabled: true` on `d_quals` → it appears without code changes.
* MCP client → `airtable_lookup(source="dalberg_profiles", formula="{Office Region}='East Africa'")`
  → returns rows without going through Claude.
* MCP client → `search(question="Who has worked in Kenya?", sources=["dalberg_profiles"])`
  → returns ranked hits + `answer` + `markdown_table` + a non-empty `plan`.
* MCP client → legacy `/mcp/` `query_dalberg_profiles(question)` returns
  the same envelope as v2 `search` plus a `deprecated: true` flag.

## What is intentionally NOT in scope (Phase 2)

* RBAC / per-user filter injection.
* A Postgres metadata source. Adding one only requires a new
  `RetrievalSource` Protocol implementation; no MCP changes.
* Async-native OpenSearch client (`opensearch-py-async`); the current
  thread-pool wrap is fine until contention shows up.
* Webhook → SQS reindex path.
* Per-tenant credential isolation in `SourceRegistry` (single-tenant
  PAT model today).
