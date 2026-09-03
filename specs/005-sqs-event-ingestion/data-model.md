# Data Model: 005-sqs-event-ingestion

Four entities are introduced or extended. All are persisted either in S3 (state store), SQS (message), or YAML/env (config) — no database.

## 1. Poll cursor (new — S3, per target)

**Key**: `_pipeline_state/cursor/<target>.json`

| Field | Type | Rules |
|---|---|---|
| `last_seen` | string, ISO-8601 UTC `YYYY-MM-DDTHH:MM:SSZ` | Canonical shape enforced by `PipelineStateStore.set_cursor`; written as "now" after a successful enqueue pass |

**States**: absent → bootstrap (full-table scan); present → formula-filtered poll (`IS_AFTER(LAST_MODIFIED_TIME({watched cols…}), DATETIME_PARSE(last_seen))`). Never pre-seeded (research D7).

## 2. Queue message (new — SQS)

**Queue**: `claude-mcp-sqs` (env `SQS_QUEUE_URL`)

| Field | Type | Rules |
|---|---|---|
| `target` | string | Must equal an `IngestionTargetConfig.name`; unknown → poison pill (deleted, logged) |
| `record_id` | string | Airtable record id (`rec…`); missing → poison pill |

Body is JSON (`ensure_ascii=False`). One message per changed record per poll. Duplicate deliveries are expected (SQS at-least-once + visibility-timeout redelivery) and safe: ingestion is idempotent per attachment.

## 3. Job ledger entry (new — S3, per record; the DLQ substitute)

**Key**: `_pipeline_state/jobs/<target>/<record_id>.json`

| Field | Type | Rules |
|---|---|---|
| `target` | string | — |
| `record_id` | string | — |
| `status` | enum `queued \| processing \| failed \| dead` | Invalid status → `ValueError` in store |
| `attempts` | int ≥ 0 | Incremented per failed handling; at `WORKER_MAX_ATTEMPTS` (default 5) → `dead` + SQS message deleted |
| `last_error` | string \| null | Truncated exception text |
| `updated_at` | string ISO-8601 UTC | Set on every put |

**Lifecycle**: (absent) → `processing` on pickup → deleted on success; on failure → `failed` (message left for redelivery) → … → `dead` at cap. `list_ingestion_failures.py` lists all non-deleted entries (filter by `--status`/`--target`).

## 4. Ingestion target config (extended — `config/airtable_ingestion.yaml` → `IngestionTargetConfig`)

| Field | Type | Default | Rules |
|---|---|---|---|
| `poll_enabled` (new) | bool | `false` | Opt-in per target for the poller; independent of existing `enabled` (manual sync). Initially `true` only for `d_quals_sync`. |
| `allowed_extensions` (new) | tuple[str, ...] | `()` | Empty → module default `DEFAULT_ALLOWED_EXTENSIONS`. Entries normalized at load: lowercased, dot-prefixed (`"PDF"` → `.pdf`). Non-empty → exact per-target override. |
| `process_images` (existing) | bool | `false` | Unchanged; second gate applied after the allowlist. |

**Default allowlist** (module constant in `pipeline.py`): `.pdf .ppt .pptx .doc .docx .xlsx .xlsm .png .jpg .jpeg .webp .gif` — exactly the extraction-dispatchable set on this branch.

## 5. Ingestion run report (extended — `IngestionRunReport`)

| Field | Type | Rules |
|---|---|---|
| `attachments_skipped_disallowed` (new) | int, default 0 | Incremented per attachment rejected by the allowlist (before download). Registered in `_REPORT_INT_FIELDS` so per-record parallel reports sum into the run total. Printed in `run_target`'s summary. |

## Relationships

```text
IngestionTargetConfig (yaml) ──poll_enabled──▶ Poller ──1 msg/changed record──▶ Queue message (SQS)
        │                                        │ reads/writes
        │ allowed_extensions                     ▼
        ▼                                   Poll cursor (S3)
_process_record gate                             
        ▲                                        
Queue message ──consumed──▶ Worker ──ingest_one_record──▶ S3 docs ──run_one──▶ OpenSearch
                              │ reads/writes attempts
                              ▼
                        Job ledger entry (S3)
```
