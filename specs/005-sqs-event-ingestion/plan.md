# Implementation Plan: Event-Driven Airtable Ingestion (SQS Poller + Worker) with Format Allowlist and Deployment Reset

**Branch**: `005-sqs-event-ingestion` | **Date**: 2026-07-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-sqs-event-ingestion/spec.md`

## Summary

Selectively port the proven event-driven ingestion system from donor commit `80cbe0d` (branch `dqual-vllm`): a cron-driven one-shot **poller** that detects Airtable records whose watched attachment columns changed and enqueues `{target, record_id}` messages to SQS, and a long-running **worker** that consumes them — re-using the existing `AirtableAttachmentIngestionPipeline` per-record logic (Claude extraction unchanged) and the existing embedding composition root (`build_pipeline` → `Pipeline.run_one`). Add a new positive **format allowlist** enforced before download/S3 upload, and a new idempotent **deployment reset script** that wipes the crontab, restarts `api` + `worker` containers, health-checks, and registers the `*/5` poller cron.

Port strategy is **selective, minimal-variant** (decided with the user): copy donor files verbatim where they apply cleanly, hand-port only `pipeline.py` (which conflicts) and `run_worker.py` (which references two donor-only constructs — `LinkedFieldResolver` and `RunReport.failed_documents` — that are stripped/adapted). Full details in [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.11 (Docker images `python:3.11-slim`); Bash for the reset script

**Primary Dependencies**: boto3 (SQS/S3 — `sqs_client` added to existing `src/pipeline/common/aws.py` helpers), pyairtable (via existing `AirtableClient`), anthropic (existing normalizers, unchanged), voyageai + opensearch-py (existing embedding path, unchanged), Docker Compose (project `dalberg-mcp`), cron + flock (host scheduling)

**Storage**: S3 (`claude-mcp-object-store`) — documents under `raw/`, new pipeline state under `_pipeline_state/` (cursor + job ledger); SQS queue `claude-mcp-sqs`; OpenSearch (existing indexes, unchanged)

**Testing**: pytest (unit tests with fake uploader/client fixtures mirroring `tests/unit/airtable_ingestion/`), ruff + mypy gates

**Target Platform**: EC2 (Ubuntu, root) running Docker Compose; local macOS for development

**Project Type**: Existing single-project Python pipeline + ops script

**Performance Goals**: Changed record searchable within ~10 min (5-min poll + processing); poller run cost independent of table size after bootstrap (formula-filtered scan)

**Constraints**: Poller must be one-shot (API container must not run ingestion); no concurrent poller runs (flock); worker retry bounded (default 5 attempts) with no infinite message loops; disallowed formats must never reach S3

**Scale/Scope**: One target table today (`d_quals_sync`, ~thousands of records bootstrap scan); config-driven expansion to more tables

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.0 — PASS (pre-Phase-0 and re-checked post-design).*

- **I. Source-Agnostic Retrieval**: PASS — no retrieval/MCP-layer changes; ingestion-side only.
- **II. Reuse Before Building**: PASS — reuses `S3Uploader`, `pipeline/common/aws.py` client factories (extends with `sqs_client` in the same module), `AirtableAttachmentIngestionPipeline._process_record` idempotency, `build_pipeline`/`Pipeline.run_one`, existing normalizer registry. No new abstractions beyond the donor's `PipelineStateStore`/`SqsProducer` (already proven).
- **III. Explicit Auth Decision (NON-NEGOTIABLE)**: PASS — no new HTTP/MCP endpoints. AWS access via existing IAM instance role; no secrets added to code or chat.
- **IV. Structured, Loud Observability**: PASS — worker/poller log via the donor's loud console pattern + structlog where present; reset script prints explicit PASS/FAIL health checks and an unmissable temporary-cadence banner; `check_sqs.py` surfaces missing-DLQ as a loud WARN.
- **V. Environment-Driven Configuration**: PASS — all knobs via env (`SQS_*` already in `.env.example`; `WORKER_MAX_ATTEMPTS` added with comment) and `config/airtable_ingestion.yaml` (`poll_enabled`, `allowed_extensions`).

No violations → Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/005-sqs-event-ingestion/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0: port-strategy + design decisions
├── data-model.md        # Phase 1: cursor / message / ledger / config entities
├── quickstart.md        # Phase 1: local + EC2 validation guide
├── contracts/
│   └── internal-interfaces.md   # message schema, state-store layout, CLI contracts
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
scripts/
├── run_poller.py                # NEW (donor verbatim + cadence docstring edit)
├── run_worker.py                # NEW (donor minus resolver/failed_documents — 6 edits)
├── check_sqs.py                 # NEW (donor verbatim)
├── list_ingestion_failures.py   # NEW (donor verbatim)
└── reset_deployment.sh          # NEW (greenfield ops script)

src/pipeline/
├── queue/
│   ├── __init__.py              # NEW (donor)
│   └── producer.py              # NEW (donor: SqsProducer)
├── common/
│   ├── aws.py                   # EDIT: + sqs_client()
│   └── state_store.py           # NEW (donor: PipelineStateStore)
└── airtable_ingestion/
    ├── airtable_client.py       # EDIT: + iter_changed_records(), get_record()
    ├── models.py                # EDIT: + poll_enabled, allowed_extensions, attachments_skipped_disallowed
    ├── config.py                # EDIT: parse poll_enabled + allowed_extensions
    ├── s3_uploader.py           # EDIT: + delete_object()
    └── pipeline.py              # EDIT (hand-port): keys_written plumbing, ingest_one_record, format gate

config/airtable_ingestion.yaml   # EDIT: d_quals_sync poll_enabled + allowed_extensions example
docker-compose.yml               # EDIT: + worker service (restart: unless-stopped)
Makefile                         # EDIT: + docker-run-poller, reset-deployment targets
.env.example                     # EDIT: + WORKER_MAX_ATTEMPTS
docs/                            # EDIT: docker-ec2, environment, architecture, aws-infra-flow, troubleshooting (NEW from donor)

tests/unit/
├── airtable_ingestion/
│   ├── test_format_filter.py        # NEW
│   └── test_ingest_one_record.py    # NEW
├── test_state_store.py              # NEW
├── test_run_worker_handling.py      # NEW
└── test_poller_changed_records.py   # NEW (formula construction)
```

**Structure Decision**: Existing single-project layout is preserved; every addition lands in an established directory (`scripts/`, `src/pipeline/queue|common|airtable_ingestion`, `tests/unit/`). No new top-level structure.
