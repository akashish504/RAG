# Contracts: internal function behavior

This feature has no external/public API surface change — the MCP tool contracts (`search`, `retrieval_planner`, `semantic_search`) are explicitly required to stay identical (spec FR-004). The only "contract" worth pinning down is the internal per-source unit of work each entry point will gather over, since that's the new seam being introduced.

## `plan_retrieval_impl` — per-source planning unit

Conceptually, one async unit of work per source, replacing one loop iteration:

- **Input**: `source_name: str`, `question: str`, `registry` (existing `SourceRegistry`)
- **Behavior**:
  1. Resolve `logical = registry.get(source_name)`, then `schema` via `logical.get_schema()` falling back to `_schema_from_snapshot(...)` on exception — unchanged from today.
  2. Call `plan_query(question=question, schema=schema)` via `asyncio.to_thread` instead of directly.
  3. On any exception from steps 1-2, return the existing default fallback `raw_plan` dict (`mode: "semantic_only"`, `uncertain: True`, etc.) — unchanged values from today's `except` block.
  4. Return `_build_source_plan(source_name, question, raw_plan, registry, schema=schema)` — unchanged function, called with the same arguments as today.
- **Output**: one plan dict, identical shape to today's per-iteration result.
- **Failure isolation**: any exception raised inside this unit MUST be caught internally (never propagate out) so that `asyncio.gather` across all sources never sees an exception from this path — matching the existing per-source `try/except` exactly, just relocated into a coroutine.
- **Caller contract**: `plan_retrieval_impl` gathers one such unit per source in `expanded`, preserving source order in the returned `plans` list (order matters for readability/reproducibility of the JSON response, even though it doesn't affect correctness).

## `_search_async` — per-source plan-and-execute unit

Conceptually, one async unit of work per source, replacing one loop iteration:

- **Input**: `source_name: str`, `question: str`, `top_k: int`, `registry`, `router: RetrievalRouter`
- **Behavior** (identical sequence to today's loop body, just isolated per source):
  1. Resolve `logical`/`schema` (same fallback as above).
  2. Call `plan_query(...)` via `asyncio.to_thread`; on exception, use the existing default fallback `raw_plan`.
  3. Derive `mode`, `semantic_query`, `formula`, `plan_top_k`, `max_records`, `enrichment_fields` — unchanged derivation logic (tools.py:548-562).
  4. Build `RetrievalQuery` and `await router.run(q)` — unchanged.
  5. If applicable, build the enrichment formula and `await router.run(enrich_q)` — unchanged condition and logic (tools.py:580-613).
  6. Return this source's `(hits, hints, plan_diagnostics_entry)` tuple.
- **Output**: one `(hits: list[SearchResult], hints: list[Hint], diagnostics: dict)` tuple per source.
- **Failure isolation**: same requirement as above — internal exceptions must resolve to the existing fallback path, not propagate to the gather.
- **Caller contract**: `_search_async` gathers one such unit per source, then concatenates all `hits`/`hints`/`diagnostics` (order across sources does not need to be preserved, since the existing merge step — dedup + citation assignment — does not depend on source order) before running the unchanged merge/dedup/citation/envelope logic (tools.py:624-647).

## Non-goals for this contract

- No change to the `search`, `retrieval_planner`, or `semantic_search` MCP tool signatures, argument names, or return JSON shape.
- No change to `plan_query()`'s signature, `RetrievalRouter.run()`'s signature, or `_build_source_plan()`'s signature — all three are called with the same arguments as today, just from a concurrently-scheduled caller.
