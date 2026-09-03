# Environment Configuration

This project reads connectivity values from `.env` in local/dev environments and
from the EC2 instance environment or AWS Secrets Manager in deployed
environments.

`.env` is gitignored. Do not commit secrets.

## Current Dev Infrastructure

### AWS

- Region: `eu-west-1`
- Account ID: `345568587892`

### S3

- Bucket: `claude-mcp-object-store`
- ARN: `arn:aws:s3:::claude-mcp-object-store`
- Default input prefix: `raw/`
- Default smoke-test key: `sample.txt`

The embedding pipeline reads `.txt` documents from S3. Preferred final layout:

```text
raw/{table_name}/{primary_key}/{column_name}.txt
```

For early testing, flat keys like `sample.txt` are also supported by the S3
reader.

### PostgreSQL / RDS

- DB identifier: `mcp-dev-rds-db-eu`
- Engine: `postgresql`
- Region/AZ: `eu-west-1a`
- Instance class: `db.t3.micro`

The RDS endpoint, database name, username, and password are not stored yet.
Those are only needed once we implement the pipeline audit/control-plane layer.

### OpenSearch

- Domain name: `mcp-dev-os-search-eu`
- Domain ARN:
`arn:aws:es:eu-west-1:345568587892:domain/mcp-dev-os-search-eu`
- VPC endpoint:
`https://vpc-mcp-dev-os-search-eu-wpkqzgri7dpehi74tybzbnmx5u.eu-west-1.es.amazonaws.com`
- Dashboards URL:
`https://vpc-mcp-dev-os-search-eu-wpkqzgri7dpehi74tybzbnmx5u.eu-west-1.es.amazonaws.com/_dashboards`
- Default index: `mcp-docs`

### SQS

Live — drives the event-driven ingestion loop (poller → queue → worker):

- `SQS_QUEUE_NAME` / `SQS_QUEUE_URL` / `SQS_QUEUE_ARN` — the `claude-mcp-sqs` queue; `SQS_QUEUE_URL` is required by both `scripts/run_poller.py` and `scripts/run_worker.py`.
- `SQS_VISIBILITY_TIMEOUT` (300s), `SQS_WAIT_TIME_SECONDS` (20), `SQS_MAX_MESSAGES` (10) — worker long-poll tuning.
- `WORKER_MAX_ATTEMPTS` (5) — retries before a record is marked `dead` in the S3 job ledger (`_pipeline_state/jobs/` — the DLQ substitute; inspect with `scripts/list_ingestion_failures.py`).

The poller is cron-driven (weekly — Saturday 09:00 server time, registered by `scripts/reset_deployment.sh`); the worker runs as the `worker` Docker Compose service.

## Still Needed Later

- Confirm the exact S3 key for the current test file if it is not `sample.txt`.
- RDS endpoint and credentials, when audit/control-plane work starts.
- OpenSearch authentication choice, if the domain requires username/password
instead of IAM/SigV4 from EC2.
- Voyage API key, when we replace the stub embedder.

