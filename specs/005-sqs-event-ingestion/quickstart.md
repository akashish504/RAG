# Quickstart Validation: 005-sqs-event-ingestion

How to prove the feature works, locally and on EC2. Interfaces referenced here are defined in [contracts/internal-interfaces.md](contracts/internal-interfaces.md); entities in [data-model.md](data-model.md).

## Prerequisites

- Local: `.env` with Airtable + AWS + `SQS_QUEUE_URL` values (see `.env.example`); `make install` (or the Docker image).
- EC2: repo at `/root/tailoredai`, `.env` populated, IAM instance role with SQS send/receive/delete + S3 rw on the bucket (incl. `_pipeline_state/`).

## Local validation

```bash
# 1. Unit tests (new: format filter, state store, ingest_one_record, worker handling, poller formula)
pytest tests/unit -q

# 2. Lint + types
ruff check src tests scripts && mypy src

# 3. Poller dry run — prints matched record IDs + the Airtable formula; enqueues nothing
python scripts/run_poller.py --dry-run

# 4. Queue pre-flight (real AWS creds)
python scripts/check_sqs.py
```

**Expected**: all tests pass; dry run lists the poll-enabled target(s) (`d_quals_sync`) with a record count and the `IS_AFTER(LAST_MODIFIED_TIME(...))` formula; `check_sqs.py` exits 0 (or 3 = no DLQ, acceptable).

## EC2 validation

```bash
cd /root/tailoredai && git pull

# 1. Full reset (builds images, wipes crontab, starts api+worker, registers */5 cron)
bash scripts/reset_deployment.sh --build
# Expected: every pre-flight/health check prints PASS; final banner shows the cron
# line and the loud "5-minute cadence is TEMPORARY" reminder.
crontab -l          # exactly one entry, marker "# dalberg-mcp-airtable-poller"
docker compose ps   # api + worker running

# 2. Worker single-batch smoke test (foreground)
aws sqs send-message --queue-url "$SQS_QUEUE_URL" \
  --message-body '{"target":"d_quals_sync","record_id":"rec<KNOWN_ID>"}'
docker compose run --rm pipeline python scripts/run_worker.py --once
# Expected: "[OK] ... ingested=<n> embedded=<n>"-style line; ledger entry deleted.

# 3. End-to-end (User Story 1)
#    a. In Airtable, add/replace an attachment on one D.Quals record.
#    b. Within 5 minutes the cron poller enqueues it:
tail -f /var/log/airtable_poller.log
#    c. Worker picks it up:
docker compose logs -f worker
#    d. Verify searchable: query OpenSearch (or the MCP search tool) for the new document.

# 4. Failure path (User Story 1, scenario 3)
aws sqs send-message --queue-url "$SQS_QUEUE_URL" \
  --message-body '{"target":"d_quals_sync","record_id":"recDOESNOTEXIST00"}'
# After ~5 attempts (SQS redeliveries):
docker compose run --rm pipeline python scripts/list_ingestion_failures.py --status dead
# Expected: exactly that record listed as dead; queue no longer redelivers it.

# 5. Format filter (User Story 2)
#    Attach a .zip to a watched record; after its cycle:
#    - run summary shows "attachments skipped (disallowed format): 1"
#    - aws s3 ls the record's prefix: no .zip object present.

# 6. Idempotent re-run (User Story 3)
bash scripts/reset_deployment.sh
# Expected: converges again; still exactly one cron entry.
```

## Known first-run behavior

The first poll has no cursor → full table scan → every record enqueued once (intended backfill; ingestion skips unchanged attachments so most messages are cheap). Expect an initially busy worker and a slow first cycle.

## After validation

Retune the cron cadence (currently `*/5`, temporary): edit the cron line in `scripts/reset_deployment.sh` and re-run it, or `crontab -e` directly. To onboard another table later: set `poll_enabled: true` on its target in `config/airtable_ingestion.yaml` — no code change.
