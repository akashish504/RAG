# Tasks: Event-Driven Airtable Ingestion (SQS Poller + Worker) with Format Allowlist and Deployment Reset

**Input**: Design documents from `/specs/005-sqs-event-ingestion/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/internal-interfaces.md, quickstart.md

**Tests**: Included — the spec's success criteria require verifiable behavior and the plan mandates new unit tests (donor shipped none).

**Organization**: Grouped by user story. Donor source for ports is always `git show 80cbe0d:<path>` (never the `dqual-vllm` tip). Minimal-port variant throughout (no `LinkedFieldResolver`; `documents_failed` counter).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

**Purpose**: Clean substrate for the ported modules

- [x] T001 Remove stale untracked `src/pipeline/queue/__pycache__/` directory (leftover donor bytecode) so old `.pyc` files cannot shadow the new module

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared plumbing every story builds on — ported verbatim from `80cbe0d` in dependency order

**⚠️ CRITICAL**: complete before any user story phase

- [x] T002 Port additive hunk: add `delete_object(*, key)` to `src/pipeline/airtable_ingestion/s3_uploader.py` (donor diff, ~4 lines)
- [x] T003 [P] Port additive hunk: add `sqs_client(*, region_name="eu-west-1", profile_name=None)` to `src/pipeline/common/aws.py` after `secrets_client`
- [x] T004 Port new file verbatim: `src/pipeline/common/state_store.py` (`PipelineStateStore` — depends on T002)
- [x] T005 [P] Port new files verbatim: `src/pipeline/queue/__init__.py` and `src/pipeline/queue/producer.py` (`SqsProducer`)
- [x] T006 [P] Port additive hunk: add `iter_changed_records()` and `get_record()` to `src/pipeline/airtable_ingestion/airtable_client.py` (after `iter_records`)
- [x] T007 Add `poll_enabled: bool = False` (donor comment) AND `allowed_extensions: tuple[str, ...] = ()` (new, per research D3) to `IngestionTargetConfig`, plus `attachments_skipped_disallowed: int = 0` to `IngestionRunReport`, in `src/pipeline/airtable_ingestion/models.py`
- [x] T008 Parse both new target fields in `load_airtable_ingestion_settings` in `src/pipeline/airtable_ingestion/config.py`: `poll_enabled=bool(raw.get("poll_enabled", False))` and `allowed_extensions` normalized lowercase/dot-prefixed per contracts
- [x] T009 Unit test: `tests/unit/test_state_store.py` — cursor round-trip + canonical `…Z` format, job put/get/delete, `list_jobs` status filter, invalid-status `ValueError` (fake uploader dict-store)

**Checkpoint**: state store, queue producer, SQS client, changed-record queries, and config fields all exist and are tested

---

## Phase 3: User Story 1 — Automatic ingestion of new/updated documents (Priority: P1) 🎯 MVP

**Goal**: Airtable change → poller enqueues → worker ingests + embeds → searchable, no manual command

**Independent Test**: quickstart.md "EC2 validation" steps 2–4 (manual enqueue + `--once`, end-to-end record touch, dead-letter path)

- [x] T010 [US1] Hand-port `src/pipeline/airtable_ingestion/pipeline.py` per plan Phase B: `_write_record_summary` returns `str | None`; `_process_record` returns `(report, keys_written)` appending normalized/original/summary keys; `run_target._run_one` adapts to the tuple; new `ingest_one_record(*, target, record) -> list[str]` (NO `linked_resolver` param) raising `RuntimeError` when `report.errors` non-empty; leave HEAD's `normalize_facet_value` code untouched
- [x] T011 [P] [US1] Port `scripts/run_poller.py` verbatim from donor; only edit: docstring cadence "every 30 minutes" → "every 5 minutes (temporary test cadence)"
- [x] T012 [US1] Port `scripts/run_worker.py` from donor with the 6 minimal-port edits (plan Phase C): drop `linked_fields` import, drop `_resolver_for()`, drop `resolver_cache` param/arg/dict, call `ingest_one_record(target=..., record=...)`, replace `failed_documents` block with `documents_failed` count check raising `RuntimeError` with failed keys
- [x] T013 [P] [US1] Port `scripts/check_sqs.py` verbatim from donor
- [x] T014 [P] [US1] Port `scripts/list_ingestion_failures.py` verbatim from donor
- [x] T015 [P] [US1] Enable polling in config: add `poll_enabled: true` (+ donor comment) under `d_quals_sync` in `config/airtable_ingestion.yaml`; add a commented `allowed_extensions` example (leave unset so the default applies)
- [x] T016 [P] [US1] Add `worker` service to `docker-compose.yml` (`<<: *app-base`, `command: python scripts/run_worker.py`, `restart: unless-stopped`, donor comment with cadence edited to 5 min); add `restart: unless-stopped` to the `api` service (research D10)
- [x] T017 [P] [US1] Add `WORKER_MAX_ATTEMPTS=5` with explanatory comment to `.env.example` (constitution Principle V)
- [x] T018 [P] [US1] Unit test: `tests/unit/airtable_ingestion/test_ingest_one_record.py` — returns keys for new normalized text + record summary; `[]` when already processed; `RuntimeError` when report has errors (fake uploader/client fixtures mirroring `test_skip_already_processed.py`)
- [x] T019 [P] [US1] Unit test: `tests/unit/test_run_worker_handling.py` — `_handle_message` outcomes: `ok` deletes ledger+message; failure below cap → `retry`; at cap → `dead`; `documents_failed > 0` → raises → retry; malformed body / unknown target → `bad_message`
- [x] T020 [P] [US1] Unit test: `tests/unit/test_poller_changed_records.py` — `iter_changed_records` builds `IS_AFTER(LAST_MODIFIED_TIME({col1}, {col2}, {col3}), DATETIME_PARSE('<cursor>', 'YYYY-MM-DDTHH:mm:ssZ'))` and full scan when `since_iso=None` (mocked connector)

**Checkpoint**: `python scripts/run_poller.py --dry-run` works locally; worker unit-tested; US1 deliverable complete pending deployment (US3)

---

## Phase 4: User Story 2 — Format allowlist (Priority: P2)

**Goal**: Disallowed formats never downloaded or uploaded; skips visible in reports

**Independent Test**: unit tests + quickstart step 5 (.zip attachment never lands in S3)

- [x] T021 [US2] Add `DEFAULT_ALLOWED_EXTENSIONS` frozenset constant next to `_IMAGE_EXTENSIONS` in `src/pipeline/airtable_ingestion/pipeline.py` and enforce the gate in `_process_record`'s attachment loop BEFORE the image skip / sidecar upload / download: compute per-target effective set (`frozenset(target.allowed_extensions) or DEFAULT_ALLOWED_EXTENSIONS`, hoisted above the loop), skip + `report.attachments_skipped_disallowed += 1` + loud `_p(...)` line for non-allowed or extensionless files
- [x] T022 [US2] Register `"attachments_skipped_disallowed"` in `_REPORT_INT_FIELDS` in `src/pipeline/airtable_ingestion/pipeline.py` and print the skip count in `run_target`'s summary block
- [x] T023 [P] [US2] Unit test: `tests/unit/airtable_ingestion/test_format_filter.py` — `.exe`/`.txt`/extensionless skipped before download (no `download_attachment`/`upload_file` calls) with counter incremented; `.pdf`/`.pptx` pass; per-target `allowed_extensions=(".pdf",)` override respected; config normalization `"PDF"` → `.pdf`

**Checkpoint**: allowlist enforced for sync, batch, and worker paths (all share `_process_record`)

---

## Phase 5: User Story 3 — One-command deployment reset (Priority: P2)

**Goal**: Single idempotent script converges the EC2 box to a healthy deployment with the 5-minute cron

**Independent Test**: quickstart steps 1 and 6 (run twice; converges; exactly one cron entry)

- [x] T024 [US3] Create `scripts/reset_deployment.sh` per contracts/internal-interfaces.md §4: `set -euo pipefail`; repo root from script path; flags `--build`/`--skip-preflight`; pre-flight (docker daemon, flock, `.env`, non-empty `SQS_QUEUE_URL`, `check_sqs.py` with exit-3-as-WARN); `crontab -r || true` (full wipe, user-confirmed); `docker compose down --remove-orphans`; optional `docker compose build api pipeline`; `docker compose up -d api worker`; health checks with ≤60s retries (API `/health` on `${API_HOST_PORT:-80}`, worker running + log banner, `run_poller.py --dry-run`); register the cron line with marker + absolute docker path + flock + log redirect; final `####`-framed temporary-cadence banner; `chmod +x`
- [x] T025 [P] [US3] Add Makefile targets `docker-run-poller` and `reset-deployment` following the existing `docker-*` pattern in `Makefile`
- [x] T026 [US3] Shell-check the script (`bash -n`, and `shellcheck` if available) and verify idempotency logic by dry inspection (no EC2 in this environment)

**Checkpoint**: script ready for EC2 validation per quickstart

---

## Phase 6: Polish & Cross-Cutting

- [x] T027 [P] Port `docs/troubleshooting.md` verbatim from donor, then fix the stale "Worker is still being wired" line and change "every 30 min" references to the 5-minute test cadence (+ retune note)
- [x] T028 [P] Update `docs/docker-ec2.md`: add `worker` to the services list; add `scripts/reset_deployment.sh` as the canonical reset/deploy path with the cron explanation
- [x] T029 [P] Update `docs/environment.md`: replace the "SQS: not configured yet" placeholder with live poller/worker vars incl. `WORKER_MAX_ATTEMPTS`
- [x] T030 [P] Update `docs/architecture.md`: services table + offline section gain the poller→SQS→worker path
- [x] T031 [P] Update `docs/aws-infra-flow.md`: flip SQS→pipeline link from dashed "Planned" to solid Live, add worker node, note `aws-infra-flow.png` is stale pending regeneration
- [x] T032 Run full gates: `pytest tests/unit -q`, `ruff check src tests scripts`, `mypy src`; fix anything they surface

## Phase 7: Amendment — Password-Protected File Gate (2026-07-22)

**Purpose**: Extend US2's "only processable files enter the pipeline" guarantee to encrypted files (FR-007a). Positive detection only; skipped after download but before S3 upload/extraction so locked files never exhaust worker retries into the dead ledger.

- [x] T033 Create `src/pipeline/airtable_ingestion/encryption_check.py` — `is_password_protected(binary, file_name)`: pypdf `is_encrypted` + empty-password decrypt for PDFs (owner-only files pass), CFB magic + UTF-16LE `EncryptionInfo`/`EncryptedPackage` directory-stream markers for Office files (no new dependency)
- [x] T034 Add `attachments_skipped_password_protected` counter to `IngestionRunReport` in `src/pipeline/airtable_ingestion/models.py`; register in `_REPORT_INT_FIELDS` and the run summary in `src/pipeline/airtable_ingestion/pipeline.py`
- [x] T035 Gate in `_process_record` after bytes are acquired, before the original S3 upload: skip + count + loud log; delete the pre-written metadata sidecar and any stale pre-gate original so the locked file leaves no trace
- [x] T036 Unit tests `tests/unit/airtable_ingestion/test_password_protected_filter.py` — detection positives (real encrypted PDF via pypdf importorskip, CFB-marker Office bytes), negatives (owner-only PDF, plain zip, bare CFB legacy .doc, prose false-positive guard, garbage bytes), and `_process_record` integration (counter, no upload, sidecar deleted, clean sibling unaffected)
- [x] T037 Docs: troubleshooting SQS row + docker-ec2 event-driven note; spec FR-007a + edge case

## Dependencies

```text
Phase 1 (T001) → Phase 2 (T002→T004; T003, T005, T006 parallel; T007→T008; T009 after T004)
Phase 2 → US1 (T010 before T012; T011/T013/T014/T015/T016/T017 parallel; tests T018-T020 after their targets)
US1 T010 → US2 (T021→T022→T023)   [same file: pipeline.py edits are sequential]
US1 T013 (check_sqs) → US3 T024 → T025/T026
All → Phase 6 (docs parallel; T032 last)
```

US2 depends on US1's T010 only because both edit `pipeline.py`; conceptually independent. US3 depends only on T013 + T016.

## Implementation Strategy

MVP = Phases 1–3 (US1): poller + worker functional, testable locally with `--dry-run` and `--once`. US2 (filter) and US3 (reset script) are incremental, independently verifiable additions. EC2 validation follows quickstart.md after all phases.
