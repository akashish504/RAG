# Phase 0 Research: Concurrent Multi-Source Retrieval Planning & Execution

No `NEEDS CLARIFICATION` markers were left in the Technical Context — this is a localized, well-understood refactor of code already read in full. This document records the decisions made and the alternatives rejected.

## Decision 1: Concurrency primitive

**Decision**: Use `asyncio.to_thread(...)` to run the existing synchronous `plan_query()` call off the event loop, and `asyncio.gather(...)` to run one such call (or, in `_search_async`, one full per-source unit of work) per source concurrently.

**Rationale**: This is not a new pattern for this codebase — it's already the established idiom for "blocking call that needs to run concurrently inside an async function":
- `retrieval/router.py:83` — `asyncio.gather(*(self._safe_query(s, per_source_query) for s in sources), ...)`
- `retrieval/embedding/voyage.py:97` — `await asyncio.to_thread(self._embed_sync, norm)`
- `retrieval/embedding/reranker.py:111` — `await asyncio.to_thread(...)`
- `retrieval/sources/airtable.py:258` — `await asyncio.to_thread(self._fetch_rows, kwargs)`
- `retrieval/sources/opensearch.py:279/283/392-393` — multiple `await asyncio.to_thread(...)` calls, including two run concurrently via `asyncio.gather` at :392-393

Following the same idiom keeps the change idiomatic and reviewable, and satisfies Constitution Principle II (Reuse Before Building) directly.

**Alternatives considered**:
- **Introduce `anthropic.AsyncAnthropic`** and make `plan_query()` itself `async def`. Rejected: touches a shared, more widely-used function (`plan_query` is called from at least two sites) and its call signature/import surface, for no benefit over `to_thread` — the SDK call itself is still I/O-bound and gains nothing from a native async client here, while the change becomes larger and riskier than necessary. `to_thread` achieves the same concurrency with a strictly smaller diff.
- **A manual `ThreadPoolExecutor`**. Rejected: `asyncio.to_thread` already does this (it's a thin wrapper over the default executor) and is the pattern already in use everywhere else in this codebase — a second, parallel mechanism would be inconsistent for no gain.

## Decision 2: Scope of concurrency in `_search_async`

**Decision**: Fold the entire per-source unit of work (schema fetch → `plan_query` via `to_thread` → build `RetrievalQuery` → `await router.run(q)` → enrichment `await router.run(enrich_q)`) into one coroutine per source, and gather across sources — not just the `plan_query` step.

**Rationale**: Reading `_search_async` end-to-end (tools.py:506-647) shows the *entire* loop body is sequential today, not just the planner call: each source's `router.run(q)` and its enrichment `router.run(enrich_q)` are `await`ed inline before the loop moves to the next source. `RetrievalRouter.run()` already does internal `asyncio.gather` fan-out (router.py:83), but it is invoked here with exactly one source per call, so that capability is never exercised across sources. Parallelizing only `plan_query()` and leaving the two `router.run()` calls sequential would still serialize the larger share of the work for sources with real data-fetch latency. Confirmed safe: `all_hits` / `all_hints` / `plan_diagnostics` are simple per-source accumulations merged once after the loop (dedup, citation assignment — tools.py:624-646), with no cross-source ordering dependency.

**Alternatives considered**:
- **Parallelize only the planning step, leave retrieval/enrichment sequential**. Rejected: addresses less of the actual latency (per the exploration, `router.run()` and its enrichment call are a meaningful part of each iteration's cost, not just the Claude planner round-trip) for a marginally smaller diff — not a good trade given the fix is equally simple either way.

## Decision 3: Failure isolation strategy

**Decision**: Each per-source coroutine keeps its own internal `try/except`, falling back to the existing default `semantic_only` plan dict on planner failure, exactly as today. `asyncio.gather` is called without `return_exceptions=True` (i.e., with its default of letting exceptions propagate) — but because every exception is already caught and converted to a fallback value *inside* each per-source coroutine before it returns, no coroutine ever raises to the gather layer for this expected failure mode.

**Rationale**: Spec requirement FR-003 demands bit-for-bit equivalent fallback behavior to today. Handling the exception inside each task (rather than via `return_exceptions=True` at the gather boundary) preserves the exact existing fallback *value* (the default plan dict), not just "didn't crash" — matching today's semantics precisely rather than approximating them.

**Alternatives considered**:
- **`asyncio.gather(..., return_exceptions=True)`** with fallback logic applied after gathering. Rejected: would require re-deriving the default-plan-on-failure logic outside the per-source unit, splitting behavior that's currently co-located and easy to review as a single per-source path. Keeping the existing inline `try/except` inside each task is a smaller, more obviously-correct diff.

## Decision 4: Single-source path

**Decision**: No special-case branch for `len(expanded) == 1`; run the same `asyncio.gather` over a single-element iterable.

**Rationale**: `asyncio.gather` over one coroutine has negligible overhead compared to today's single-iteration `for` loop, and avoids a special-cased code path that would need its own testing/maintenance. Satisfies FR-005 (no regression for single-source queries) without added complexity.

**Alternatives considered**:
- **Explicit `if len(expanded) == 1: ... else: gather(...)` branch**. Rejected: adds a second code path to maintain and test for a case that `asyncio.gather` already handles efficiently.
