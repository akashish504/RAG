# Folder Structure

The repository hosts two cooperating modules:

* `src/pipeline/` — offline batch pipeline that prepares OpenSearch
  (`reader → parser → chunker → embedder → indexer`) plus the FastAPI
  shell that mounts MCP servers.
* `src/retrieval/` — runtime, source-agnostic retrieval over the indexed
  data and over Airtable. Exposes the v2 MCP server with five tools
  (`list_sources`, `get_schema`, `semantic_search`, `airtable_lookup`,
  `search`).

The two modules talk to each other only through three stable surfaces:
the `config/` YAML files, OpenSearch (the writer's index is the reader's
index), and `pipeline/common/` utilities. The retrieval module never
imports the writer's pipeline/chunker/embedder/indexer code (see boundary
rule below).

## Layout

```text
dalberg_mcp/
├── config/
│   ├── default.yaml                  # writer pipeline (chunking, embedding, OS endpoint)
│   ├── tables.yaml                   # writer: table -> S3 prefix + chunker config
│   ├── retrieval_sources.yaml        # reader: per-source Airtable + OpenSearch + chunking_strategy
│   ├── airtable_ingestion.yaml
│   └── dalberg_profiles_schema.json  # frozen Airtable schema snapshot
├── docs/
│   ├── folder-structure.md           # this file
│   └── retrieval.md                  # v2 retrieval contract + tool docs
├── scripts/
│   ├── run_pipeline.py
│   ├── create_opensearch_index.py
│   ├── chunk_s3_object.py
│   ├── discover_airtable_schema.py
│   ├── export_airtable_schema_snapshot.py   # now reads retrieval_sources.yaml
│   ├── run_airtable_ingestion.py
│   ├── test_nl_profiles_query.py            # exercises v2 `search` tool
│   └── upload_sample_data.py
├── src/
│   ├── pipeline/
│   │   ├── config.py
│   │   ├── logging_config.py
│   │   ├── common/
│   │   │   ├── aws.py
│   │   │   ├── ids.py
│   │   │   └── opensearch.py        # shared client factory (writer + reader)
│   │   ├── airtable_ingestion/
│   │   │   ├── data_extract.py
│   │   │   ├── airtable_client.py
│   │   │   ├── config.py
│   │   │   ├── normalizers.py
│   │   │   ├── s3_uploader.py
│   │   │   ├── schema.py
│   │   │   └── pipeline.py
│   │   ├── embedding_pipeline/
│   │   │   ├── models.py
│   │   │   ├── pipeline.py
│   │   │   ├── parser/
│   │   │   ├── reader/
│   │   │   ├── chunker/
│   │   │   ├── embedder/             # passage-side (writer)
│   │   │   └── indexer/
│   │   └── api/                      # FastAPI shell + v1 deprecation forwarder
│   │       ├── main.py               # mounts /mcp/ (v1) and /mcp/v2/ (v2)
│   │       ├── auth.py
│   │       ├── settings.py
│   │       ├── paths.py
│   │       └── mcp_server.py         # v1 forwarder; both tools call into retrieval/*
│   └── retrieval/                    # NEW: source-agnostic retrieval module
│       ├── __init__.py
│       ├── paths.py                  # repo paths used by retrieval-only code
│       ├── settings.py               # secrets bundle (PAT, Voyage, OS, region)
│       ├── config.py                 # SourceRegistry + LogicalSource (YAML loader)
│       ├── models.py                 # RetrievalQuery, SearchResult, Hint, SchemaDescriptor, ResponseMode
│       ├── router.py                 # asyncio.gather fan-out across sources
│       ├── merger.py                 # cross-source RRF
│       ├── formatter.py              # response shape classifier + markdown + Claude answer
│       ├── sources/
│       │   ├── base.py               # protocols
│       │   ├── airtable.py           # generic over (base, table)
│       │   └── opensearch.py         # KNN + BM25 + intra-source RRF + parent hydration
│       ├── embedding/
│       │   ├── base.py
│       │   └── voyage.py             # query-side ("query: " prefix), request-scoped cache
│       ├── planner/
│       │   ├── nl_planner.py         # Claude planner driven by SchemaDescriptor
│       │   ├── prompts.py
│       │   └── anthropic_settings.py
│       └── mcp/
│           ├── server.py             # build_mcp() — five tools
│           └── tools.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/samples/
└── data/                             # local scratch (gitignored)
```

## Package Responsibilities

### `src/pipeline/embedding_pipeline/`

Offline writer. Same as before: `reader → parser → chunker → embedder →
indexer`. Indexer's `_to_action` writes the per-table `mcp-<table>` index
that the retriever later reads. Parent and child chunks share
`document_hash`; child chunks store `parent_chunk_id` so the retriever can
hydrate parent context after a KNN hit.

### `src/pipeline/common/`

Cross-cutting utilities used by both writer and reader.

* `ids.py` — deterministic `document_hash` and `chunk_id` generation.
* `aws.py` — boto3 helpers for S3 and Secrets Manager.
* `opensearch.py` — shared OpenSearch client factory (basic-auth or
  SigV4); used by `embedding_pipeline.indexer.opensearch.OpenSearchIndexer`
  and by `retrieval.sources.opensearch.OpenSearchSource`.

### `src/pipeline/airtable_ingestion/`

Stable Airtable PAT-based connector + S3 uploader. The retrieval module
reuses `data_extract.AirtableConnector` for runtime queries.

### `src/pipeline/api/`

FastAPI shell. Hosts `/health`, `/v1/status`, the bearer-protected
`/v1/test/*` diagnostic routes, and the two MCP mounts:

* `/mcp/`    — v1 legacy. Two tools that forward to v2 (deprecation
  forwarder in `mcp_server.py`). Will be removed once v2 is verified.
* `/mcp/v2/` — current. Five source-agnostic tools.

The diagnostic routes (`/v1/test/airtable/{ping,schema,records}`,
`/v1/test/airtable/nl-query`, `/v1/test/sources`) are thin wrappers that
call into `retrieval/`.

### `src/retrieval/`

New module — see `docs/retrieval.md` for the full contract. High-level:

* `config.py` loads `config/retrieval_sources.yaml` and instantiates one
  `LogicalSource` per enabled entry, each bundling its Airtable adapter
  and its OpenSearch adapter.
* `router.RetrievalRouter` embeds the query once per request and fans out
  across sources via `asyncio.gather`.
* `merger.rrf_merge` reorders cross-source hits with Reciprocal Rank
  Fusion. The OpenSearch adapter does its own intra-source KNN + BM25 RRF.
* `mcp/server.py` registers the five MCP tools.

### Boundary rule

`src/retrieval/` MUST NOT import the writer pipeline:

* No `pipeline.embedding_pipeline.pipeline`
* No `pipeline.embedding_pipeline.chunker.*`
* No `pipeline.embedding_pipeline.parser.*`
* No `pipeline.embedding_pipeline.embedder.*`
* No `pipeline.embedding_pipeline.indexer.OpenSearchIndexer`

`src/retrieval/` MAY import:

* `pipeline.common.aws`, `pipeline.common.ids`, `pipeline.common.opensearch`
* `pipeline.airtable_ingestion.data_extract.AirtableConnector`

This keeps the writer free to evolve (new chunker strategies, new
embedders) without breaking retrieval, and vice versa.

### `config/`

* `default.yaml` — writer pipeline (S3 bucket/prefix, chunker, OpenSearch
  endpoint).
* `tables.yaml` — writer's table → S3 prefix mapping + per-table
  chunker config.
* `retrieval_sources.yaml` — **reader's** per-source bundle: each entry
  groups its Airtable side (`base_id`, `table_name`,
  `schema_snapshot_path`) and its OpenSearch side (`index_name`, `k`,
  `search_mode`) plus a shared `chunking_strategy` and
  `identifier_field`. Adding a new source is YAML-only.
* `dalberg_profiles_schema.json` — frozen Airtable schema snapshot. Re-
  generated by `scripts/export_airtable_schema_snapshot.py`.

### `scripts/`

Operational entrypoints. Notable updates:

* `export_airtable_schema_snapshot.py` now reads its `base_id` /
  `table_name` / output path from `config/retrieval_sources.yaml`. Use
  `--source <name>` to snapshot a non-default source.
* `test_nl_profiles_query.py` exercises the v2 `search` tool from the CLI.

### `tests/`

* `unit/` — chunking, ID generation, model serialisation, mapping
  generation, and the new retrieval models / merger / config loader.
* `integration/` — S3 reader, OpenSearch indexing, and end-to-end MCP
  tool tests once a populated index is available.

### `data/`

Local scratch only. Gitignored except for `.gitkeep`.

## Production data flow (read path)

```text
MCP client (Claude / Cursor)
  -> POST /mcp/v2/  (streamable HTTP)
  -> retrieval.mcp.server.build_mcp() tool dispatch
  -> retrieval.mcp.tools.{search_impl, semantic_search_impl, airtable_lookup_impl, ...}
  -> RetrievalRouter.run(RetrievalQuery)
      ├─ query embedding (VoyageQueryEmbedder, "query: " prefix; cached)
      ├─ asyncio.gather(per LogicalSource.query)
      │     ├─ AirtableSource.filter_structured(formula, fields, max_records)
      │     └─ OpenSearchSource.search_semantic(query, embedding, top_k, filters)
      │           ├─ KNN child search (chunk_type=child filter)
      │           ├─ BM25 child search
      │           ├─ Intra-source RRF
      │           └─ Parent hydration via mget(parent_chunk_id)
      ├─ Cross-source RRF (when multiple sources requested)
      └─ formatter.format_response → response_mode + markdown_table + (optional) Claude answer
  -> SearchResponse JSON envelope back to MCP client
```

## Production data flow (write path) — unchanged

```text
S3 .txt input
  -> reader + parser (TextParser -> ParsedDocument)
  -> chunker (parent/child)
  -> embedder (Voyage "passage: " prefix)
  -> OpenSearch indexer (per-table mcp-<table> index)
```

## Deployment

Production deployment shape did not change:

```text
Claude
  -> HTTPS
  -> Nginx on EC2
  -> FastAPI on localhost (mounts /mcp/ and /mcp/v2/)
  -> OpenSearch / Airtable / S3 / Voyage / Anthropic
```

`auth.py` enforces bearer auth on `/v1/*` REST routes. The MCP mounts
inherit transport-level auth from the surrounding network (Nginx →
localhost) and from the upstream MCP client's own bearer token.
