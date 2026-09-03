---

description: "Task list for concurrent multi-source retrieval planning & execution"

---

# Tasks: Concurrent Multi-Source Retrieval Planning & Execution

**Input**: Design documents from `/specs/004-mcp-retrieval-latency/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/internal-functions.md](./contracts/internal-functions.md), [quickstart.md](./quickstart.md)

**Tests**: Included — this is a concurrency refactor of existing behavior, so regression coverage (single-source unchanged, output shape unchanged, failure isolation preserved) is required to trust the change, per spec FR-003/FR-004/FR-005.

**Organization**: Tasks are grouped by user story (US1 = P1 concurrent execution, US2 = P2 failure isolation) per [spec.md](./spec.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2)

## Path Conventions

Single project. All implementation changes are in `src/retrieval/mcp/tools.py`. Tests live in the existing `tests/unit/retrieval/` tree (`test_plan_retrieval.py`, `test_search_impl.py` already exist and cover these two functions).

---

## Phase 1: Setup

**Purpose**: Establish a known-good baseline before touching anything.

- [X] T001 Run `uv run pytest tests/unit/retrieval/test_plan_retrieval.py tests/unit/retrieval/test_search_impl.py -v` and confirm all existing tests pass before any change (baseline for regression comparison).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Both user stories need multi-source tests whose mock router doesn't assume the sources are called in a fixed global order — a correctness requirement introduced by moving from sequential to concurrent execution (see [research.md](./research.md) Decision 2, and note that `asyncio.gather` preserves *result* order but not *call/interleaving* order across sources).

**⚠️ CRITICAL**: Complete before writing any new multi-source test in Phase 3 or Phase 4.

- [X] T002 In `tests/unit/retrieval/test_search_impl.py`, add an order-independent variant of `_router_with_responses` (e.g. `_router_with_responses_by_source(mapping: dict[str, tuple[SearchResponse, SearchResponse | None]])`) that returns a response based on the `RetrievalQuery.sources[0]` / `mode` of the call rather than call position, so multi-source tests remain correct regardless of which source's coroutine happens to run first. Existing single-source tests and the existing `_router_with_responses` helper are unaffected and stay as-is (single-source ordering — plan → main run → enrich run — is unchanged and still deterministic).

**Checkpoint**: Foundation ready — both user stories can now add reliable multi-source tests.

---

## Phase 3: User Story 1 - Multi-source question answered without added wait per source (Priority: P1) 🎯 MVP

**Goal**: Sources are planned (and, for `search`, retrieved + enriched) concurrently instead of sequentially, so wall-clock time for a multi-source query no longer scales linearly with source count.

**Independent Test**: Ask the same multi-source question before/after and compare wall-clock time (see [quickstart.md](./quickstart.md) §2); assert output content is identical either way.

### Tests for User Story 1

- [X] T003 [P] [US1] In `tests/unit/retrieval/test_plan_retrieval.py`, add a test that patches `plan_query` with a side effect that sleeps briefly via a real blocking call (e.g. `time.sleep(0.05)`), calls `plan_retrieval_impl` with 4 sources, and asserts total wall-clock time is well under 4× a single source's time (e.g. `< 0.15s` for 4 sources at `0.05s` each) — proves concurrent execution, not just correct output.
- [X] T004 [P] [US1] In `tests/unit/retrieval/test_search_impl.py`, add the equivalent timing test for `search_impl`/`_search_async` with 4 sources, using the order-independent mock router from T002 and a sleeping `plan_query` side effect, asserting the same sub-linear wall-clock scaling.
- [X] T005 [P] [US1] In `tests/unit/retrieval/test_search_impl.py`, add a regression test with a single source that asserts the JSON output (hits, hints, diagnostics) is byte-for-byte identical to a fixed expected value — locks in FR-005 (no single-source behavior change).

### Implementation for User Story 1

- [X] T006 [US1] In `src/retrieval/mcp/tools.py`, refactor `plan_retrieval_impl`'s loop (currently ~lines 290-311) into a per-source async coroutine (schema fetch → `await asyncio.to_thread(plan_query, question=question, schema=schema)`, with the existing `try/except` fallback to the default `semantic_only` plan dict kept inside the coroutine) and run one coroutine per source via a single `asyncio.gather(...)`, preserving the existing per-source-order result list before passing each result through the unchanged `_build_source_plan(...)`.
- [X] T007 [US1] In `src/retrieval/mcp/tools.py`, refactor `_search_async`'s loop (currently ~lines 528-622) into a per-source async coroutine covering schema fetch, `await asyncio.to_thread(plan_query, ...)`, `RetrievalQuery` construction, `await router.run(q)`, and the conditional enrichment `await router.run(enrich_q)` — returning that source's `(hits, hints, plan_diagnostics_entry)` — and run one coroutine per source via `asyncio.gather(...)`. After the gather, concatenate all sources' `hits`/`hints`/diagnostics into `all_hits`/`all_hints`/`plan_diagnostics` and continue into the existing unchanged merge step (dedup, citation assignment, `SearchResponse` construction — current lines 624-647).
- [X] T008 [US1] Run `uv run pytest tests/unit/retrieval/test_plan_retrieval.py tests/unit/retrieval/test_search_impl.py -v` and confirm all existing tests (from T001's baseline) plus the new T003-T005 tests pass.

**Checkpoint**: Multi-source `retrieval_planner` and `search` calls run concurrently; single-source behavior is provably unchanged.

---

## Phase 4: User Story 2 - One slow or failing source doesn't penalize the others (Priority: P2)

**Goal**: A planning failure (or slowness) in one source under concurrent execution degrades only that source to its existing default fallback plan, without affecting or delaying any other source's result.

**Independent Test**: Force one of several sources' `plan_query` to raise (or, for slowness, to sleep longer than the others) and confirm the other sources' results are complete, correct, and not delayed by the failing/slow one (see [quickstart.md](./quickstart.md) §4).

### Tests for User Story 2

- [X] T009 [P] [US2] In `tests/unit/retrieval/test_plan_retrieval.py`, add a test with 3 sources where `plan_query`'s side effect raises for exactly one source name and returns a normal plan for the other two; assert all 3 plans are present in the response, the failing source's plan shows `mode: "semantic_only"` / `uncertain: True` (matching today's fallback shape), and the other two sources' plans are unaffected.
- [X] T010 [P] [US2] In `tests/unit/retrieval/test_search_impl.py`, add the equivalent test for `search_impl` using the order-independent mock router (T002): one source's `plan_query` raises, the other sources still return their expected hits, and the failing source's diagnostics entry shows the default fallback plan.
- [X] T011 [P] [US2] In `tests/unit/retrieval/test_search_impl.py`, add a test where one source's `plan_query` side effect sleeps much longer than the others (e.g. `0.2s` vs `0.02s`); assert the overall call completes in roughly the slow source's time (not the sum of all sources' times) — confirms a slow source doesn't serialize behind/ahead of the others.

### Implementation for User Story 2

- [X] T012 [US2] Verify (and adjust if needed) that the per-source coroutines written in T006/T007 catch exceptions strictly inside each source's own coroutine — never letting an exception propagate out to the `asyncio.gather` call — so one source's failure can never cancel or fail sibling coroutines' tasks. This should already hold from T006/T007 if the existing `try/except` blocks were moved as-is; this task is the explicit check + fix if the tests from T009-T011 reveal otherwise.
- [X] T013 [US2] Run `uv run pytest tests/unit/retrieval/test_plan_retrieval.py tests/unit/retrieval/test_search_impl.py -v` and confirm all tests (T001 baseline + T003-T005 + T009-T011) pass.

**Checkpoint**: Both user stories complete — concurrent execution is fast and fault-isolated exactly as the sequential version was.

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: Final validation against the full spec.

- [X] T014 Run the full test suite (`uv run pytest tests/ -v`) to confirm no unrelated regression.
- [X] T015 Execute the manual latency comparison in [quickstart.md](./quickstart.md) §2-3 against a real (or realistically-mocked) multi-source question, recording before/after wall-clock time to confirm SC-001 (≤~1.5x single-source time for 4 sources) and SC-002 (no single-source regression).
- [X] T016 [P] Re-read the final diff of `src/retrieval/mcp/tools.py` end-to-end once more to confirm no behavior other than execution scheduling changed (output shape, prompts, tool contracts all untouched per FR-004).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — run first to establish the regression baseline.
- **Foundational (Phase 2)**: Depends on Phase 1. Blocks Phase 3 and Phase 4 test tasks (T003-T005, T009-T011) since they need the order-independent mock helper from T002.
- **User Story 1 (Phase 3)**: Depends on Phase 2. This is the MVP — delivers the actual concurrency fix.
- **User Story 2 (Phase 4)**: Depends on Phase 2 and, in practice, on Phase 3's implementation (T006/T007) existing, since US2's tests exercise the same per-source coroutines US1 introduces. Not truly independent of US1 at the code level (same functions), but independently *testable* — US2's tests specifically target failure-isolation behavior that US1's tests don't cover.
- **Polish (Phase 5)**: Depends on Phase 3 and Phase 4 both being complete.

### Parallel Opportunities

- T003, T004, T005 (US1 tests, different test functions across two files) can be written in parallel.
- T009, T010, T011 (US2 tests) can be written in parallel.
- T006 and T007 touch the same file (`tools.py`) but different functions — sequential within one session is simplest; not marked `[P]` to avoid merge conflicts from concurrent edits to the same file.

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1: Setup (baseline).
2. Phase 2: Foundational (order-independent mock helper).
3. Phase 3: User Story 1 — the concurrency fix itself. This alone delivers the latency win described in the spec.
4. **STOP and VALIDATE**: run quickstart.md's manual latency comparison.

### Then

5. Phase 4: User Story 2 — lock in failure isolation with dedicated tests (the behavior itself is a byproduct of how US1 is implemented, but US2 makes it explicit and regression-proof).
6. Phase 5: Polish — full suite + final read-through.
