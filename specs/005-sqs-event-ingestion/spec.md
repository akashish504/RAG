# Feature Specification: Event-Driven Airtable Ingestion (SQS Poller + Worker) with Format Allowlist and Deployment Reset

**Feature Branch**: `005-sqs-event-ingestion`

**Created**: 2026-07-22

**Status**: Draft

**Input**: User description: "Port event-driven Airtable ingestion (SQS poller + worker) from dqual-vllm commit 80cbe0d to this branch, add an allowed-file-format filter that skips unsupported attachments before download/S3 upload, and add a one-shot deployment reset script that tears down the MCP API and worker containers, wipes the crontab, restarts everything, and registers an every-5-minute poller cron (temporary test cadence)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - New/updated Airtable documents are ingested automatically (Priority: P1)

A consultant adds or replaces a document attachment on a D.Quals record in Airtable. Without anyone running a manual pipeline command, the document is extracted, embedded, and becomes searchable through the MCP retrieval tools within minutes.

**Why this priority**: This is the core value of the feature — today ingestion only happens when someone manually runs a batch sync, so search results go stale between runs.

**Independent Test**: Modify one attachment column on one Airtable record; verify the record's document appears in search results within one polling cycle plus processing time, with no manual command issued.

**Acceptance Scenarios**:

1. **Given** the scheduled poller and background worker are running, **When** a record's watched attachment column is modified in Airtable, **Then** within one polling cycle the record is queued and the worker extracts, embeds, and indexes its documents.
2. **Given** a record was already fully ingested, **When** the poller re-queues it without any content change, **Then** the worker recognizes the unchanged content and completes quickly without duplicate indexing.
3. **Given** processing of a record fails (e.g., transient network error), **When** the message is retried, **Then** the system retries up to a configured maximum before marking the record as dead and recording the failure for operator review.
4. **Given** more Airtable tables need this behavior later, **When** an operator enables polling for another configured table, **Then** that table participates with no code changes.

---

### User Story 2 - Only supported file formats are processed (Priority: P2)

Attachments whose file type the pipeline cannot extract (e.g., `.zip`, `.mp4`, `.csv`) are skipped up front — never downloaded from Airtable, never stored in S3 — and the skip is visible in run reports.

**Why this priority**: Prevents wasted storage, bandwidth, and confusing "uploaded but never extracted" leftovers; keeps the corpus clean as new tables come online.

**Independent Test**: Ingest a record carrying one supported and one unsupported attachment; verify the unsupported one is counted as skipped and no trace of it exists in object storage.

**Acceptance Scenarios**:

1. **Given** a record with a `.pdf` and a `.zip` attachment, **When** it is ingested (manual sync or via the worker), **Then** the `.pdf` is processed normally and the `.zip` is skipped before download, with a skip counter and log line recording it.
2. **Given** a target configured with a custom allowlist, **When** ingestion runs, **Then** only the listed extensions are processed for that target; other targets keep the default allowlist.
3. **Given** an attachment with no file extension, **When** ingestion runs, **Then** it is skipped and counted.

---

### User Story 3 - One-command deployment reset on the server (Priority: P2)

An operator runs a single script on the EC2 box that stops the API server and worker containers, clears the crontab, brings everything back up fresh, re-registers the every-5-minute poller cron, and verifies health — ending with a loud reminder that the 5-minute cadence is temporary.

**Why this priority**: Deployment today is a sequence of manual commands with no established procedure for the new worker/cron pieces; a single idempotent script removes operator error.

**Independent Test**: Run the script twice in a row on the server; both runs converge to the same healthy state (API responding, worker running, exactly one poller cron entry).

**Acceptance Scenarios**:

1. **Given** old containers and stale cron entries exist, **When** the operator runs the reset script, **Then** all project containers are stopped, the crontab is wiped, the API and worker start fresh, and exactly one poller cron entry (every 5 minutes) is registered.
2. **Given** the environment is misconfigured (e.g., queue URL missing or queue unreachable), **When** the script runs, **Then** it fails fast during pre-flight with a clear message and changes nothing.
3. **Given** the script completed, **When** the operator reads its output, **Then** health-check results and an unmissable reminder about the temporary 5-minute cadence are shown.

---

### Edge Cases

- **First-ever poll (no cursor)**: the poller performs a full table scan and queues every record — accepted as a backfill; per-record idempotency makes re-processing cheap. Documented, not prevented.
- **Poll cycle longer than 5 minutes**: overlapping cron fires must not run concurrently (lock; the later fire exits silently).
- **Record modified while a poll is in flight**: may be picked up in the next cycle rather than the current one — acceptable at this cadence.
- **Worker job exceeding the queue's visibility timeout**: the message may be redelivered mid-job; idempotent processing plus the attempt ledger keeps the duplicate cheap and bounded.
- **Malformed / unknown-target queue message**: discarded as a poison pill (with a log), never retried forever.
- **Record marked dead**: remains listed by the failure-inspection tool until an operator intervenes; the queue message is removed so it cannot loop.
- **Server reboot**: worker and API containers restart automatically; the cron entry persists.
- **Password-protected attachment** *(amendment 2026-07-22)*: skipped with a dedicated counter before upload/extraction instead of failing extraction and exhausting worker retries into the dead ledger. Legacy pre-2007 `.doc`/`.ppt` password schemes are not detectable this way and still take the failure path.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST detect Airtable records whose watched attachment columns changed since the last poll and queue exactly one message per changed record.
- **FR-002**: Change detection MUST be scoped per configured target table via a per-target opt-in flag, so additional tables can be enabled through configuration alone (initially only the D.Quals target is enabled).
- **FR-003**: A long-running worker MUST consume queued messages and, per record: fetch the record, ingest its attachments (same extraction behavior as the existing manual sync, Claude-based extraction unchanged), then embed and index every new/changed document.
- **FR-004**: The worker MUST retry failed records up to a configured maximum number of attempts (default 5), tracking attempts in a persistent ledger; on reaching the cap the record is marked dead and its message removed.
- **FR-005**: Operators MUST be able to list not-yet-succeeded / failed / dead records via a command-line tool.
- **FR-006**: Ingestion MUST enforce a positive file-format allowlist per attachment, applied before download or object-storage upload, in both manual sync and worker paths. Default allowlist: `.pdf .docx .doc .pptx .ppt .xlsx .xlsm .png .jpg .jpeg .webp .gif` (everything the pipeline can extract today). Skips MUST be counted in run reports and logged.
- **FR-007**: The allowlist MUST be overridable per target in the ingestion configuration; when omitted, the default applies. Existing image handling (`process_images` per target) remains a second, unchanged gate.
- **FR-007a** *(amendment 2026-07-22)*: Ingestion MUST skip attachments that are provably password-protected (encrypted PDFs and encrypted Office documents), after download (protection is only detectable from file bytes) but before object-storage upload and extraction, in both manual sync and worker paths. Skips MUST be counted in run reports and logged; detection MUST be positive-only so ambiguous or corrupt files continue to the existing failure handling. PDFs restricted only by an owner password (openable without a prompt) remain processable.
- **FR-008**: XLSX/XLSM extraction stays on the existing deterministic spreadsheet path; all other formats continue to use Claude-based extraction (no extraction-engine changes).
- **FR-009**: A single idempotent reset script MUST: verify prerequisites (Docker, env file, queue reachability) and abort cleanly on failure; stop the API, worker, and orphaned project containers; wipe the entire crontab; optionally rebuild images; start API + worker; health-check the API endpoint, worker process, and poller (dry run); register the every-5-minute poller cron entry (with a lock to prevent overlap and output redirected to a log file); and print a prominent reminder that the 5-minute cadence is temporary.
- **FR-010**: The poller MUST run as a scheduled one-shot process (not a resident service) and MUST support a dry-run mode that reports what would be queued without side effects.
- **FR-009a** *(amendment 2026-07-23)*: Validation complete — the temporary every-5-minute cadence is retired. The production schedule is weekly: Saturday 09:00 server time (`0 9 * * 6`). The overlap lock and one-shot design remain unchanged.
- **FR-011**: All new environment variables MUST be documented in `.env.example` with comments; all new components MUST emit structured logs and surface degraded configuration loudly at startup (constitution Principles IV & V).
- **FR-012**: The worker MUST treat messages that are undecodable or reference unknown targets as poison pills: log and remove them without retry.

### Key Entities

- **Poll cursor**: per-target timestamp of the last successful poll; absence means "scan everything" (bootstrap).
- **Queue message**: `{target, record_id}` — the unit of work handed from poller to worker.
- **Job ledger entry**: per-record processing state (`queued | processing | failed | dead`), attempt count, last error, updated-at; persisted in object storage; serves as the dead-letter substitute.
- **Ingestion target**: existing per-table configuration, extended with `poll_enabled` and `allowed_extensions`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A document added or replaced in Airtable is searchable without any manual command within 10 minutes (one 5-minute cycle + processing) for a typical record.
- **SC-002**: Zero disallowed-format attachments appear in object storage after ingestion runs that include them; every skip is visible in the run report.
- **SC-003**: The reset script converges to a healthy deployment (API healthy, worker running, exactly one cron entry) from any prior state, in a single invocation, and is safe to re-run.
- **SC-004**: A record that fails processing is retried and either succeeds or is visible as dead in the failure-listing tool after at most 5 attempts; no message loops indefinitely.
- **SC-005**: Enabling event-driven ingestion for an additional table requires only a configuration edit (no code change).

## Assumptions

- The SQS queue (`claude-mcp-sqs`) already exists in AWS with credentials/IAM available via the EC2 instance role; the queue's visibility timeout is managed queue-side (current default 300s).
- The reset script runs as root on the EC2 box; **wiping the entire crontab is intentional and confirmed by the user** (the poller entry is the only intended cron job on this host).
- Minimal port confirmed: the worker matches current-branch batch-ingestion behavior — no linked-record field resolution, and embedding failures are detected via the existing per-run failure counter.
- The 5-minute cadence is a temporary testing value; the user will retune it after validation (a reminder is printed by the script and tracked separately).
- First-run full-table backfill is acceptable (idempotent skips make re-processing cheap); the poll cursor is not pre-seeded.
- Donor code originates from commit `80cbe0d` on the `dqual-vllm` branch; its runtime behavior is trusted as already proven on the EC2 deployment.
