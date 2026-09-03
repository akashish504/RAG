---

description: "Task list template for feature implementation"
---

# Tasks: Fix OpenSearch IAM Auth Expiring Under Load

**Input**: Design documents from `/specs/002-fix-opensearch-sigv4-auth/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/build-opensearch-client.md, quickstart.md

**Tests**: Included — this is a production-incident fix where the acceptance criteria (credentials survive rotation, other auth paths unaffected) are only verifiable through tests, per `quickstart.md`.

**Organization**: Tasks are grouped by user story (US1 = P1 incident fix, US2 = P2 regression guard) per `spec.md`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2)
- All file paths are exact and relative to repo root

## Path Conventions

Single project. Fix lands in `src/pipeline/common/opensearch.py`; new tests in
`tests/unit/pipeline/common/test_opensearch.py`; dependency change in
`pyproject.toml`.

---

## Phase 1: Setup

**Purpose**: Confirm the environment already supports the target implementation — no new dependency versions needed.

- [X] T001 Confirm `opensearch-py>=2.6` (already pinned in `pyproject.toml`) exposes `opensearchpy.AWSV4SignerAuth` (`python -c "from opensearchpy import AWSV4SignerAuth"`); no dependency version bump required.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared test scaffolding both user stories' tests depend on.

**⚠️ CRITICAL**: Both user story test phases below write into the same new test file — create it first to avoid conflicting file-creation.

- [X] T002 Create `tests/unit/pipeline/common/test_opensearch.py` with: a fake boto3 "live credentials" test double whose `.get_frozen_credentials()` can be configured to return different values on successive calls (simulating rotation), and a fixture/helper to build it. No assertions yet — just the shared scaffold both US1 and US2 tests import.

**Checkpoint**: Test file exists with shared fixtures — user story test tasks can now proceed.

---

## Phase 3: User Story 1 - Long-running service survives IAM credential rotation (Priority: P1) 🎯 MVP

**Goal**: `build_opensearch_client()`'s SigV4 path signs every request from the live, auto-refreshing boto3 credentials object instead of a one-time frozen snapshot, so authentication never goes stale mid-process.

**Independent Test**: Build a client via `build_opensearch_client()` in SigV4 mode with the fake rotating-credentials double from T002; sign two requests with different simulated credential values in between; assert both signatures reflect the *current* value at sign time, not the value at construction time.

### Tests for User Story 1 ⚠️

> Write these first; they must FAIL against the current (buggy) implementation before T006 fixes it.

- [X] T003 [P] [US1] In `tests/unit/pipeline/common/test_opensearch.py`, test that when `username`/`password` are unset, `build_opensearch_client()` builds `http_auth` as an `opensearchpy.AWSV4SignerAuth` constructed from the **live** object returned by `boto3.Session().get_credentials()` (assert the factory never calls `.get_frozen_credentials()` itself — that the object passed to `AWSV4SignerAuth` is the live, mockable credentials object).
- [X] T004 [P] [US1] In `tests/unit/pipeline/common/test_opensearch.py`, using the rotating-credentials double from T002, test that two signing operations performed at different simulated times each reflect the credentials value current *at that time* — i.e., a credential change after client construction is observed by later requests without rebuilding the client.
- [X] T005 [P] [US1] In `tests/unit/pipeline/common/test_opensearch.py`, test that `build_opensearch_client()` still raises `ValueError` with the existing message when `boto3.Session().get_credentials()` returns `None` (no credentials resolvable at all) — unchanged behavior.

### Implementation for User Story 1

- [X] T006 [US1] In `src/pipeline/common/opensearch.py`, replace the `requests_aws4auth.AWS4Auth` + `raw_creds.get_frozen_credentials()` construction with `opensearchpy.AWSV4SignerAuth(boto3.Session().get_credentials(), aws_region, "es")`, passing the **live** credentials object (delete the `get_frozen_credentials()` call and the `requests_aws4auth` import). Makes T003–T005 pass.
- [X] T007 [US1] In `src/pipeline/common/opensearch.py`, add a `structlog` log line in `build_opensearch_client()` reporting which auth mode was selected (`"basic"` vs `"sigv4"`) at construction time, per Constitution Principle IV (loud boot-time visibility for auth configuration, matching the existing `_log_citation_mode` pattern).

**Checkpoint**: User Story 1 is fully functional and independently testable — the incident's root cause is fixed and verified without needing User Story 2.

---

## Phase 4: User Story 2 - Other auth paths keep working unchanged (Priority: P2)

**Goal**: Confirm the dev/local basic-auth path and any static-AWS-key (non-role) environments see zero behavior change from the SigV4 fix.

**Independent Test**: Run the basic-auth and static-credential test cases in isolation; both pass regardless of whether US1's tests have been run.

### Tests for User Story 2 ⚠️

- [X] T008 [P] [US2] In `tests/unit/pipeline/common/test_opensearch.py`, test that when `username` and `password` are both set, `build_opensearch_client()` still returns a client with `http_auth=(username, password)`, correct `use_ssl`/`verify_certs` per host (`localhost`/`127.*` vs. remote), and does **not** touch `boto3` at all — unchanged from pre-fix behavior.
- [X] T009 [P] [US2] In `tests/unit/pipeline/common/test_opensearch.py`, test that a static (non-role) AWS credentials object returned by `boto3.Session().get_credentials()` (e.g. representing env-var or profile-based keys, not a `RefreshableCredentials`) still works with the new `AWSV4SignerAuth` construction — no assumption in the fix that credentials must be role-based/refreshable.

### Implementation for User Story 2

- [ ] T010 [US2] Manually verify per `quickstart.md` §3 against a real or staging OpenSearch endpoint with basic auth (local Docker) that query behavior is byte-for-byte unchanged from before the fix. **Not performed by this implementation pass** — requires a live staging environment this agent does not have access to; T008/T009's unit coverage is the automated substitute. Recommended before deploying to production.

**Checkpoint**: Both auth modes verified independently — User Story 1 and User Story 2 each pass without depending on the other.

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: Remove the now-unused dependency and confirm no regressions elsewhere.

- [X] T011 [P] Remove `"requests-aws4auth>=1.3"` from the `dependencies` list in `pyproject.toml`.
- [X] T012 [P] Run `grep -rn "requests_aws4auth\|requests-aws4auth" src/ pyproject.toml` per `quickstart.md` §4 and confirm zero matches. (Also fixed a stray docstring reference in `src/pipeline/embedding_pipeline/indexer/opensearch.py` found by this grep.)
- [X] T013 Run `pytest tests/unit -v` to confirm the full existing unit test suite still passes with no regressions from this change. (331 passed, 3 pre-existing unrelated failures + 2 pre-existing errors confirmed present before this change too via `git stash`; 1 pre-existing skip.)
- [X] T014 Walk through `quickstart.md` end-to-end (sections 1, 2, 4 automated via the new test suite; section 3 is the manual staging check deferred in T010).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS both user stories (shared test file).
- **User Story 1 (Phase 3)**: Depends on Foundational. No dependency on US2.
- **User Story 2 (Phase 4)**: Depends on Foundational. Independent of US1 — can run in parallel with Phase 3 if staffed, though T010's manual check is more meaningful once T006 has landed.
- **Polish (Phase 5)**: Depends on both user stories being complete (T011/T012 assume the `requests_aws4auth` import is already gone, i.e. T006 done).

### Within Each User Story

- Tests (T003–T005, T008–T009) MUST be written and FAIL/pass-as-expected before/after the corresponding implementation task.
- US1: T006 before T007 (both edit the same function; T007 is additive logging on top of T006's change).
- US2 has no implementation task beyond verification — it exists to catch regressions in T006.

### Parallel Opportunities

- T003, T004, T005 (all US1 tests, same file but independent test functions) can be drafted in parallel by different people, then merged.
- T008, T009 (US2 tests) can run in parallel with T003–T005 (US1 tests) since both only *read* the pre-fix code and assert on different branches (`if username and password` vs. the SigV4 branch).
- T011 and T012 (Polish) can run in parallel — different files.

---

## Parallel Example: User Story 1

```bash
# After T002 (shared test scaffold) is done, draft all US1 tests together:
Task: "Test AWSV4SignerAuth built from live credentials object, not frozen, in tests/unit/pipeline/common/test_opensearch.py"
Task: "Test signing reflects rotated credentials without client rebuild, in tests/unit/pipeline/common/test_opensearch.py"
Task: "Test ValueError still raised when no credentials resolve, in tests/unit/pipeline/common/test_opensearch.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational).
2. Complete Phase 3 (User Story 1) — this alone resolves the production incident.
3. **STOP and VALIDATE**: run T003–T005; confirm they fail against the pre-fix code, then pass after T006–T007.
4. This is a legitimate, shippable fix on its own — US2 only adds regression confidence.

### Incremental Delivery

1. Setup + Foundational → shared scaffold ready.
2. User Story 1 → incident fixed, independently tested, mergeable/deployable.
3. User Story 2 → regression guard added, confirms no collateral damage.
4. Polish → dependency removed, full suite green.

---

## Notes

- This is a small, single-file production fix — phases are intentionally lean (no parallel-team split is really necessary; sequential T001→T014 is the realistic path for one person).
- Per Constitution Principle II (Reuse Before Building), T006 is itself a "reuse the library's own signer instead of a bolted-on one" change — no new abstraction is introduced anywhere in this task list.
- Commit after Phase 3 (US1) at the latest — that commit alone fixes the incident.
