# Dalberg MCP — Embedding Pipeline

Offline pipeline that reads source text from S3, chunks it (parent-child), embeds
the chunks with Voyage-4, and indexes them into AWS OpenSearch. This repository
currently focuses on the offline embedding/indexing pipeline. The Claude-facing
MCP server, Airtable integration, Nginx deployment config, and document ingestion
will be layered in later as separate modules.

## Current scope (Phase 1 — chunking + indexing)

```
S3 (raw/{table}/{primary_key}/{column}.txt)   <-- .txt only
        │
        ▼
   Reader  ──▶  Chunker (parent + child, tiktoken cl100k_base)
                  │
                  ▼
              Embedder (stub today, Voyage-4 later)
                  │
                  ▼
              Indexer (OpenSearch, int8_hnsw, dims=1024)
```

Format contract: this pipeline only consumes plain text. The upstream
ingestion pipeline is responsible for converting PDF / DOCX / PPTX / etc. into
`.txt` files placed under `raw/{table_name}/{primary_key}/{column_name}.txt`.

Out of scope for now: ingestion pipeline (PDF/DOCX/PPTX → text), MCP FastAPI
server, Nginx deployment, Airtable lookup tool, RBAC, hybrid search, and
runtime retrieval orchestration.

## Airtable Attachment Ingestion (separate pipeline)

This repo also includes a separate Airtable -> S3 attachment ingestion module
for syncing selected Airtable fields into raw S3 layout:

```text
raw/<table_name>/<identifier>/<column_name>/<file>
```

Current target is `Profiles Sync (Claude)` -> `Dalberg Profiles` with
`CV Attachment` and `Bio Attachment` columns. See:

- `config/airtable_ingestion.yaml`
- `scripts/discover_airtable_schema.py`
- `scripts/run_airtable_ingestion.py`
- `docs/airtable-ingestion.md`

### Current focus: chunking

**Reader + parser** are wired for `.txt` in S3 (`TextParser`). Optional
PDF/DOCX/PPTX parsers exist under `pipeline/parser/`; install `dalberg-mcp[parsers]`
and extend `supported_extensions` per table when you need them. Chunking tuning
still lives in **`pipeline/chunker/`**.

## Repository layout

```
dalberg_mcp/
├── config/                  # YAML config + tables.yaml (table → S3 prefix)
├── docs/                    # Architecture notes and folder-structure contract
├── scripts/                 # CLI entrypoints (run pipeline, chunk_s3_object, ...)
├── src/dalberg_mcp/
│   ├── config.py            # Settings loader
│   ├── logging_config.py    # Structured logging
│   ├── pipeline/
│   │   ├── pipeline.py      # Orchestrator: read → chunk → embed → index
│   │   ├── models.py        # Chunk / Document dataclasses
│   │   ├── parser/          # TextParser + optional PDF/DOCX/PPTX ([parsers])
│   │   ├── reader/          # S3 reader, table registry, document loader
│   │   ├── chunker/         # Parent-child chunker + token utilities
│   │   ├── embedder/        # Stub today; Voyage-4 later
│   │   └── indexer/         # OpenSearch bulk indexer + index mapping
│   └── common/
│       ├── ids.py           # chunk_id / document_hash generation
│       └── aws.py           # boto3 S3 + Secrets Manager helpers
├── tests/
│   ├── unit/                # Per-module tests
│   ├── integration/         # End-to-end with localstack / test OpenSearch
│   └── fixtures/samples/    # Sample .txt files for tests
└── data/                    # Local scratch (gitignored)
```

See `docs/folder-structure.md` for the detailed ownership of each folder and
the planned future shape for the MCP runtime server.

## S3 input convention

```
raw/{table_name}/{primary_key}/{column_name}.txt
```

Example: `raw/contracts/recABC123/proposal_text.txt`. The pipeline carries
`table_name`, `primary_key`, and `column_name` through as provenance metadata
on every chunk so we can map back to Airtable later without re-indexing.

## Quick start (after implementation lands)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # fill in AWS + OpenSearch values

python scripts/create_opensearch_index.py    # one-time index setup
python scripts/run_pipeline.py --prefix raw/ # run end-to-end
```

## Docker / EC2

The project includes a reusable Docker image and `docker-compose.yml`. The same
`pipeline` service can run reader tests, chunking, indexing, pytest, or any new
script we add later:

```bash
docker compose build

# 1) Optional: Airtable → S3 (set AIRTABLE_PAT_TOKEN + S3_BUCKET in .env)
docker compose run --rm pipeline python scripts/discover_airtable_schema.py
docker compose run --rm pipeline python scripts/run_airtable_ingestion.py --target profiles_sync

# 2) Embedding pipeline (after raw data exists in S3)
docker compose run --rm pipeline python scripts/run_pipeline.py --prefix raw/

# Shortcuts
make docker-discover-airtable-schema
make docker-run-airtable-ingestion

# S3 smoke / tests use Compose profile `tools`
docker compose --profile tools run --rm s3-smoke
docker compose --profile tools run --rm test
```

We do not need a new Compose file for every feature. Add a new service only for
a genuinely different runtime process, such as the future long-running MCP
server or SQS worker. See `docs/docker-ec2.md`.

## Status

Greenfield. Folder structure scaffolded; module bodies are stubs marked with
`TODO`. See the design reference for architectural decisions already locked in
(chunk-in-OpenSearch, parent-child slides, Voyage-4 passage/query prefixes,
AWS eu-west-1).# tailoredai
# tailoredai
# tailoredai
