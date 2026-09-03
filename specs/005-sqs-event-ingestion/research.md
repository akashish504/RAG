# Research & Decisions: 005-sqs-event-ingestion

All decisions below were resolved before planning (three-way codebase exploration + donor-branch delta analysis + direct user confirmation). No open NEEDS CLARIFICATION items remain.

## D1. Port strategy: selective port of commit `80cbe0d`, not cherry-pick

**Decision**: Copy from `git show 80cbe0d:<path>` file-by-file; hand-port only `src/pipeline/airtable_ingestion/pipeline.py` and `scripts/run_worker.py`. Never source from the `dqual-vllm` branch tip (`fea9f5f` carries unrelated extras: proposal_library enablement, local-extraction service, facet changes).

**Rationale**: Verified against merge-base `6039fa3`: all 7 new files and 7 additive hunks apply cleanly to HEAD; only `pipeline.py` conflicts (HEAD's `normalize_facet_value` divergence + donor hunks anchored on the absent `LinkedFieldResolver` refactor). A raw cherry-pick would require `ad63a4f` → `c148221` → `80cbe0d`, dragging in large unrelated payloads (backfill scripts, OpenSearch mapping changes, embedding rewrites) that conflict with HEAD.

**Alternatives considered**: (a) cherry-pick chain — rejected: conflicts + unrelated payload; (b) merge `dqual-vllm` — rejected: pulls the whole vLLM experiment surface onto a production branch.

## D2. Minimal port variant (user-confirmed)

**Decision**: Strip the two donor-only dependencies: no `LinkedFieldResolver` (remove `linked_resolver` param from `ingest_one_record`, remove `_resolver_for`/`resolver_cache` from the worker) and replace the worker's `report.failed_documents` (list, absent on HEAD) with HEAD's `report.documents_failed` (int) — raise `RuntimeError` listing the S3 keys whose runs reported failures.

**Rationale**: Keeps cron-driven ingestion byte-for-byte consistent with this branch's manual batch ingestion (HEAD never had resolver-enriched facets). Loses only per-document failure detail in the error message (count + keys instead of names).

**Alternatives considered**: full-fidelity port (also bring `linked_fields.py` + `failed_documents` field) — rejected by user: extra code from unrelated commits, behavior drift vs. manual sync.

## D3. Format allowlist: positive gate at the pre-download choke point

**Decision**: New `allowed_extensions: tuple[str, ...]` per target (empty → module default `DEFAULT_ALLOWED_EXTENSIONS = {.pdf,.ppt,.pptx,.doc,.docx,.xlsx,.xlsm,.png,.jpg,.jpeg,.webp,.gif}`), enforced in `_process_record`'s attachment loop **before** the existing image skip, sidecar upload, Airtable download, and S3 upload. Extensionless files are skipped. New `attachments_skipped_disallowed` counter on `IngestionRunReport` (registered in `_REPORT_INT_FIELDS` for parallel-report summing) + loud skip line + run-summary line.

**Rationale**: That loop is the single choke point shared by sync and batch modes; filtering there guarantees disallowed bytes never reach S3 (SC-002). The default set is exactly what the extraction dispatchers handle today (`llm_content.py` suffix sets; `slides_deck` delegates non-pptx there). User confirmed the full set including spreadsheets and images.

**Alternatives considered**: (a) filter inside normalizers — rejected: runs after download/upload, leaves un-extracted originals in S3 (today's behavior, explicitly unwanted); (b) filter in `run_batch_extraction.discover_jobs` — rejected: batch-only and post-upload; (c) MIME-based filtering — rejected: Airtable `content_type` is only used as S3 metadata today; extension routing is the established convention.

## D4. XLSX/XLSM stays on openpyxl (user-confirmed)

**Decision**: No extraction-engine change. Spreadsheets keep the deterministic openpyxl→markdown path; every other format keeps its existing Claude path (`claude-haiku-4-5` via `llm_content`/`slides_deck`/`record_summary`/CV/Bio normalizers).

**Rationale**: "Use Claude for all extraction" is already the production reality except spreadsheets, where openpyxl is lossless, free, and deterministic. User chose to keep it.

## D5. Reset script wipes the entire crontab (user-confirmed)

**Decision**: `scripts/reset_deployment.sh` runs `crontab -r || true` (full wipe) before registering the single poller entry. Marker comment `# dalberg-mcp-airtable-poller` is still appended to the entry for identification.

**Rationale**: User explicitly chose full wipe over project-filtered removal; the poller is the only intended cron job on this host. Recorded in spec Assumptions as intentional.

**Alternatives considered**: project-filtered removal (grep -v marker) — recommended for safety but declined by user.

## D6. Cron cadence 5 minutes + flock; overlap and cursor semantics

**Decision**: `*/5 * * * * cd <repo> && flock -n /tmp/airtable-poller.lock <abs-docker> compose run --rm pipeline python scripts/run_poller.py >> /var/log/airtable_poller.log 2>&1 # dalberg-mcp-airtable-poller`. Loud script banner + saved reminder: cadence is temporary for testing; retune after validation (donor design used 30 min).

**Rationale**: `flock -n` makes overlapping fires exit silently (bootstrap scans can exceed 5 min). Absolute docker path because cron's PATH is minimal. Donor cursor semantics kept (cursor → "now" after enqueue): a record modified mid-poll may wait one extra cycle — acceptable at this cadence; documented, not fixed.

## D7. No cursor seeding; first run is a full-table backfill

**Decision**: The reset script does not touch `_pipeline_state/`. First poll with no cursor triggers `iter_changed_records(since_iso=None)` → full scan → every record enqueued once.

**Rationale**: Doubles as the initial backfill; `_already_processed`/`key_exists` skip markers make replays cheap. Seeding the cursor would silently skip records changed between the last manual sync and the seed time.

## D8. S3 job ledger is the DLQ substitute

**Decision**: Keep donor failure handling: retry via SQS redelivery while attempts < `WORKER_MAX_ATTEMPTS` (default 5, env-tunable, to be documented in `.env.example`); at cap → ledger `status="dead"` + message deleted. `bad_message` (undecodable/unknown target) → delete immediately. `check_sqs.py`'s "no DLQ" result stays a WARN (exit 3) in the reset script pre-flight, not a failure.

**Rationale**: Proven donor design; `list_ingestion_failures.py` gives the operator surface. A real SQS DLQ remains a future infra option, orthogonal to this port.

## D9. Visibility-timeout risk accepted and documented

**Decision**: Queue-side `SQS_VISIBILITY_TIMEOUT` stays 300s. Document in `docs/troubleshooting.md` that a long slides_deck record (LibreOffice render + Claude escalations) can exceed it, causing mid-job redelivery; idempotent skips + attempt ledger bound the cost; raising the queue's visibility timeout (~900s) is the tuning knob if `--once` timing tests show it's needed.

## D10. Compose worker service + restart policy

**Decision**: `worker` service via `<<: *app-base`, `command: python scripts/run_worker.py`, `restart: unless-stopped` (survives reboots/daemon restarts; does not survive `compose down` — the reset script is the sanctioned stop/start path and always brings it back). Also add `restart: unless-stopped` to `api` in the same edit so the API survives reboots too (currently missing; flagged by design review).

## D11. Stale artifact cleanup

**Decision**: Delete the untracked leftover `src/pipeline/queue/__pycache__/` (stale bytecode from a prior donor checkout) when creating the real `queue/` module files.
