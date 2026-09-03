# Internal Interface Contracts: 005-sqs-event-ingestion

No new HTTP/MCP endpoints (Constitution Principle III: nothing to auth-decide). Contracts here are the CLI surfaces, the queue message schema, the state-store layout, and one new library method. Existing interfaces (API container, MCP tools, embedding pipeline) are unchanged.

## 1. SQS message schema (poller → worker)

```json
{"target": "d_quals_sync", "record_id": "recXXXXXXXXXXXXXX"}
```

- Producer: `SqsProducer.send(payload: dict) -> MessageId` (`src/pipeline/queue/producer.py`), JSON body, `ensure_ascii=False`.
- Consumer validation (`run_worker._handle_message`): missing/empty `target` or `record_id`, undecodable JSON, or `target` not in loaded config → outcome `bad_message` → delete message, log, never retry.

## 2. State store layout (S3, prefix `_pipeline_state/`)

- `cursor/<target>.json` → `{"last_seen": "<ISO-8601 Z>"}`
- `jobs/<target>/<record_id>.json` → `{"target", "record_id", "status", "attempts", "last_error", "updated_at"}`

API (`PipelineStateStore`, `src/pipeline/common/state_store.py` — donor-verbatim):
`get_cursor(target) -> str | None`, `set_cursor(target, value: str | None)` (None → now, canonical `…Z`), `get_job(target, record_id) -> dict | None`, `put_job(...)` (status validated against `{queued, processing, failed, dead}`), `delete_job(target, record_id)`, `list_jobs(target=None, status=None) -> list[dict]`.

## 3. Library method: `AirtableAttachmentIngestionPipeline.ingest_one_record`

```python
def ingest_one_record(*, target: IngestionTargetConfig, record: dict[str, Any]) -> list[str]
```

- Returns the S3 keys written/changed for this record (normalized texts, unextracted originals, record summary) — the exact set the worker must re-embed.
- Returns `[]` when everything was already processed (idempotent no-op).
- Raises `RuntimeError` if the per-record report recorded any errors (triggers worker retry path).
- MINIMAL variant: no `linked_resolver` parameter (donor-only construct not ported).

## 4. CLI contracts

### `scripts/run_poller.py` (one-shot; cron entry point)
- Flags: `--target <name>` (override, ignores `poll_enabled`), `--dry-run` (print matches; no enqueue, no cursor advance), `--json`, `--profile <aws-profile>`.
- Env: requires `SQS_QUEUE_URL` (exit 1 if unset); uses `AIRTABLE_PAT_TOKEN`, S3 creds via default chain.
- Exit codes: 0 success (including zero matches); 1 config/env error.

### `scripts/run_worker.py` (long-running; compose `worker` service)
- Flags: `--once` (process at most one receive batch, then exit — used for foreground verification), `--max-attempts N` (default env `WORKER_MAX_ATTEMPTS` or 5).
- Env: `SQS_QUEUE_URL` (required), `SQS_MAX_MESSAGES`, `SQS_WAIT_TIME_SECONDS`, `WORKER_MAX_ATTEMPTS`, `ANTHROPIC_API_KEY` (optional — record summaries skipped when absent), `VOYAGE_API_KEY`.
- Message outcomes: `ok` (ledger deleted + message deleted) / `retry` (ledger `failed`, message left) / `dead` (ledger `dead`, message deleted) / `bad_message` (message deleted).

### `scripts/check_sqs.py` (read-only pre-flight)
- Exit 0: main queue exists (deploy-gate pass). Exit 3: queue exists, no DLQ (reset script treats as WARN — by design, ledger is the DLQ substitute). Other non-zero: abort deploy.

### `scripts/list_ingestion_failures.py`
- Flags: `--status dead|failed|processing`, `--target <name>`, `--json`. Lists ledger entries (empty output = healthy).

### `scripts/reset_deployment.sh` (root on EC2; idempotent)
- Flags: `--build` (rebuild `api` + `pipeline` images first), `--skip-preflight`.
- Sequence (each step loud, PASS/FAIL): pre-flight (docker daemon, flock, `.env` present, non-empty `SQS_QUEUE_URL`, `check_sqs.py`) → **wipe entire crontab** (`crontab -r || true`; user-confirmed) → `docker compose down --remove-orphans` → optional build → `docker compose up -d api worker` → health checks with ≤60s retries (API `GET /health` on `${API_HOST_PORT:-80}`; worker container running + startup banner in logs; `run_poller.py --dry-run` smoke) → register cron → final banner.
- Cron line contract (exactly one entry after any run):
  ```
  */5 * * * * cd <REPO_ROOT> && flock -n /tmp/airtable-poller.lock <ABS_DOCKER> compose run --rm pipeline python scripts/run_poller.py >> /var/log/airtable_poller.log 2>&1 # dalberg-mcp-airtable-poller
  ```
- Final banner MUST include: services started, installed cron line, log path, failure-listing pointer, and a `####`-framed reminder that the 5-minute cadence is a temporary testing value.
- Exit non-zero on any pre-flight or health-check failure; no partial cron registration on failure (cron is registered only after health checks pass — note: crontab wipe happens early by design, so a failed run leaves no cron until re-run succeeds).

## 5. Compose service contract

```yaml
worker:
  <<: *app-base
  command: python scripts/run_worker.py
  restart: unless-stopped
```

`api` gains `restart: unless-stopped` in the same edit (reboot survival). `compose down` remains the only sanctioned stop (via the reset script).
