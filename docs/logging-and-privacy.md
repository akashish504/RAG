# Logging & Privacy Review — MCP Dev CloudWatch Logs

Status: reviewed 2026-07-29 (full audit of every log/print statement in
`src/pipeline/`, `src/retrieval/`, and `scripts/`). This is the explicit
log-content review requested for the monitoring rollout: what each CloudWatch
log group can contain, what was redacted, and what is documented as an
accepted residual for sign-off.

## How logs reach CloudWatch

`docker-compose.cloudwatch.yml` (applied by `scripts/reset_deployment.sh` on
EC2 only; opt out with `MCP_CLOUDWATCH_LOGS=0`) ships container stdout/stderr
via Docker's `awslogs` driver in non-blocking mode. Local dev keeps the
default `json-file` driver — nothing leaves the machine.

| Log group | Source | Retention |
|---|---|---|
| `/mcp-dev/api` | api container: nginx access/error + uvicorn + FastAPI app logs | 30 days |
| `/mcp-dev/worker` | SQS worker: per-record ingestion + embedding | 30 days |
| `/mcp-dev/pipeline` | one-off `pipeline` runs incl. the weekly poller cron | 30 days |

Retention and KMS encryption are properties of the log groups themselves —
see "AWS-side requirements" below.

## What each group contains

### `/mcp-dev/api`
- nginx access lines: **client IPs**, request paths, status codes, user agents.
  Paths are opaque (`/mcp/v2/mcp`, `/cite/<HMAC-token>`); bearer tokens travel
  in the `Authorization` header, which nginx does **not** log.
- Application logs (structlog): source names, index names, counts, error
  strings. Retrieval queries from Claude clients are **not** logged verbatim.

### `/mcp-dev/worker` and `/mcp-dev/pipeline`
- Airtable **record IDs**, S3 keys, sanitized attachment filenames, page/slide/
  chunk/token counts, model names, error strings + stack traces.
- **Never logged:** document body text, extracted CV/bio content, prompts sent
  to Anthropic/Voyage, Airtable field values (with the two caveats below).
  Audit confirmed exception handlers use `exc_info` rather than interpolating
  content, and the normalizers log filenames and char counts only.

## Redacted during this review

- `src/pipeline/airtable_ingestion/pipeline.py` per-record progress line
  previously printed the raw identifier-column value — for `profiles_sync`
  that column is **Email**. It now prints the Airtable record ID plus a
  masked identifier (first two characters), via `mask_identifier()` in
  `src/pipeline/airtable_ingestion/normalizers.py`.

## Accepted residuals — for sign-off

1. **Identifiers inside S3 keys.** The storage layout keys profile documents
   by normalized identifier (`raw/<table>/<identifier>/…`, built in
   `pipeline.py`), so any log line that includes an S3 key for `profiles_sync`
   carries a normalized email address. Removing this means migrating the S3
   layout to record-ID keys — out of scope for the monitoring rollout.
   Mitigation until then: KMS encryption + IAM read-scoping on the log groups
   (below) and 30-day retention.
2. **Attachment filenames.** Uploaded CV/bio filenames commonly embed a
   person's name (`jane_doe_cv.pdf`). They are kept in logs because they are
   the primary handle for debugging ingestion failures. Same mitigations as
   above.

## Expected volume (answers the cost question)

- Steady state ≈ **50–100 MB/month**, dominated by `/mcp-dev/api` access lines
  (ALB health checks ≈ 270 B/request across nginx + uvicorn). Worker/pipeline
  ≈ 15–25 KB per ingested record; the weekly poller run is negligible.
- A full batch re-ingestion adds ≈ 12–25 KB/document (≈ 40–75 MB for a
  3,000-document run).
- At $0.50/GB ingestion: **< $0.05/month** steady state. Blow-up risks are
  crash loops and `LOG_LEVEL=DEBUG` (boto3 wire logs) — the override pins
  `LOG_LEVEL=INFO`, and an `IncomingBytes` alarm (~5× expected) is the
  recommended guard.

## AWS-side requirements (not enforceable from this repo)

- **Pre-create** the three log groups encrypted with a customer-managed KMS
  key *before* first deploy with the override — `awslogs-create-group` makes
  unencrypted groups, and encryption cannot be applied retroactively to
  already-ingested data. Set 30-day retention on each.
- Instance role: `logs:CreateLogStream` + `logs:PutLogEvents` on the three
  groups (plus `kms:GenerateDataKey` grant on the key), and
  `CloudWatchAgentServerPolicy` for the mem/disk agent metrics.
- IAM read-scoping: `logs:GetLogEvents`/`FilterLogEvents` on `/mcp-dev/*`
  restricted to the team's roles, not account-wide readers.
