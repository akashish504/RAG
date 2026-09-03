# Docker and EC2 Workflow

Use Docker Compose on EC2 for Airtable ingestion (S3 raw layout), the embedding
pipeline, and the future MCP runtime server.

## Why Docker Compose

- Keeps Python, native dependencies, and CLI tools consistent across local and
  EC2 environments.
- Lets EC2 use its IAM role through boto3, without storing AWS access keys in
  `.env`.
- Gives one reusable image for reader, chunker, embedder, indexer, tests, and
  future server commands.
- Avoids creating a new deployment mechanism every time the pipeline grows.

## Do We Need A New Compose File For Every Feature?

No.

The same `pipeline` service can run any project command:

```bash
docker compose run --rm pipeline python scripts/read_s3_text.py --bucket claude-mcp-object-store --key sample.txt
docker compose run --rm pipeline python scripts/run_pipeline.py --prefix raw/
docker compose run --rm pipeline pytest
```

As we add functionality, we usually add a Python module or script, not a new
Compose file.

Add a new Compose service only when the process has a different runtime shape,
for example:

- A long-running future MCP/FastAPI server.
- A local-only dependency like LocalStack or a test OpenSearch container.

## Services

Current `docker-compose.yml` services:

- `api`: production HTTP/MCP server (Nginx :80 → Uvicorn), always on,
  `restart: unless-stopped`.
- `worker`: long-running SQS consumer for event-driven Airtable ingestion
  (`scripts/run_worker.py`), always on, `restart: unless-stopped`. Fed by the
  cron poller below.
- `pipeline`: default reusable app container. Use command overrides for most
  work (including Airtable ingestion and the embedding pipeline). Also the
  one-shot container the poller cron runs.
- `s3-smoke`: convenience command for pulling the configured S3 test object
  (requires Compose profile `tools`; see below).
- `test`: runs pytest (profile `tools`).
- `lint`: runs ruff and mypy (profile `tools`).
- `shell`: opens an interactive shell inside the image (profile `tools`).

Shortcuts like `s3-smoke`, `test`, and `lint` are grouped under the `tools`
profile so a plain `docker compose up` only starts the lightweight `pipeline`
service. Activate the profile when you want them:

```bash
docker compose --profile tools run --rm s3-smoke
docker compose --profile tools run --rm test
```

For anything you run often on EC2 (ingestion, embedding), prefer the `pipeline`
service with an explicit command — no profile needed.

## EC2 First-Time Setup

Install Docker and Git:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker "$USER"
```

Log out and back in so the Docker group membership applies.

Clone the repo:

```bash
git clone <github-repo-url>
cd dalberg_mcp
```

Create `.env`:

```bash
cp .env.example .env
```

On EC2, leave `AWS_PROFILE` empty. Boto3 will use the EC2 instance role.

Build:

```bash
docker compose build
```

(Optional) Slimmer image without dev/test tools — faster installs on small EC2:

```bash
docker compose build --build-arg PIP_EXTRAS=parsers,airtable_ingestion
```

### First: Airtable ingestion → S3

The embedding/chunking pipeline expects text under `raw/...` in `S3_BUCKET`.
Populate that bucket by running ingestion on the instance (after `.env` has
`AIRTABLE_PAT_TOKEN` and `S3_BUCKET`):

```bash
# Discover bases/tables/schema (writes under data/metadata/airtable; optional S3 upload per config)
docker compose run --rm pipeline python scripts/discover_airtable_schema.py

# Sync configured targets (default target name: profiles_sync — see config/airtable_ingestion.yaml)
docker compose run --rm pipeline python scripts/run_airtable_ingestion.py --target profiles_sync
```

Equivalent via Make from the repo root:

```bash
make docker-discover-airtable-schema
make docker-run-airtable-ingestion
# Or: AIRTABLE_INGEST_TARGET=other_target make docker-run-airtable-ingestion
```

Run the S3 read smoke test:

```bash
docker compose --profile tools run --rm s3-smoke
```

Or override the key explicitly:

```bash
docker compose run --rm pipeline \
  python scripts/read_s3_text.py \
  --bucket claude-mcp-object-store \
  --key sample.txt \
  --region eu-west-1
```

## Regular Deployment Loop

**Canonical reset/deploy path** — one idempotent script that stops the `api` +
`worker` containers, wipes the crontab, restarts everything, health-checks,
and re-registers the poller cron (weekly — Saturday 09:00 server time):

```bash
git pull
bash scripts/reset_deployment.sh --build   # or: make reset-deployment
```

Manual batch runs still work exactly as before when needed:

```bash
git pull
docker compose build
docker compose --profile tools run --rm test
docker compose run --rm pipeline python scripts/run_airtable_ingestion.py --target profiles_sync
docker compose run --rm pipeline python scripts/run_pipeline.py --prefix raw/
```

### Event-driven ingestion (poller → SQS → worker)

- The crontab entry (installed by `reset_deployment.sh`, marker
  `# dalberg-mcp-airtable-poller`) runs `scripts/run_poller.py` in a one-shot
  `pipeline` container weekly (Saturday 09:00 server time), `flock`-guarded so
  overlapping runs no-op. Output: `/var/log/airtable_poller.log`. For an
  immediate poll between scheduled runs:
  `docker compose run --rm pipeline python scripts/run_poller.py`.
- The `worker` service consumes the queue continuously; per-record failures
  retry up to `WORKER_MAX_ATTEMPTS` (default 5) then park as `dead` in the S3
  job ledger. Inspect with
  `docker compose run --rm pipeline python scripts/list_ingestion_failures.py`.
- Only allowlisted formats are ingested, and password-protected files
  (encrypted PDFs / Office documents) are skipped before S3 upload and
  extraction — both show up as skip counters in the run summary, so a locked
  file never burns worker retries.
- First-ever poll has no cursor → full-table bootstrap scan (slow first cycle;
  idempotent skips keep it cheap).
- Onboard another table by setting `poll_enabled: true` on its target in
  `config/airtable_ingestion.yaml` — no code change.

## AWS Credentials

On EC2:

- Do not set `AWS_ACCESS_KEY_ID`.
- Do not set `AWS_SECRET_ACCESS_KEY`.
- Keep `AWS_PROFILE` blank.
- Attach the correct IAM role to the EC2 instance.

Locally:

- Either run outside Docker with your normal AWS profile, or
- pass credentials/profile into Docker deliberately when needed.

The current Compose file is optimized for EC2 role-based access.
