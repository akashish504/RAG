# Phase 1 Data Model: Concurrent Multi-Source Retrieval Planning & Execution

No new persistent data or schema is introduced. This documents the existing in-memory structures the concurrent execution flows through, and confirms none of them require a shape change.

## Source plan

Produced by `plan_query()` (`retrieval/planner/nl_planner.py:116`) or its per-source failure fallback, then shaped by `_build_source_plan()` (`retrieval/mcp/tools.py:346-483`) in `plan_retrieval_impl`, or consumed directly in `_search_async`.

| Field | Type | Notes |
|---|---|---|
| `mode` | `"semantic_only" \| "airtable_only" \| "hybrid"` | Unchanged |
| `airtable_formula` | `str` | Unchanged |
| `semantic_query` | `str` | Unchanged |
| `top_k` | `int` | Unchanged |
| `max_records` | `int \| None` | Unchanged |
| `uncertain` | `bool` | Set `True` on planner failure fallback |
| `rationale` | `str` | Present only via live planner call; fallback sets a fixed rationale string |

**Independence**: one instance per requested source; no field references another source's plan. This is what makes concurrent computation safe — confirmed by reading `_build_source_plan`, which takes only `(source_name, question, raw_plan, registry, schema)` and returns a value derived solely from those inputs.

**No change**: field set, types, and fallback values are identical before and after this feature — only the *scheduling* of when each instance is computed changes (concurrently vs. sequentially).

## Source result

Produced by `RetrievalRouter.run()` per source in `_search_async`, accumulated into `all_hits` / `all_hints` / `plan_diagnostics`.

| Field | Type | Notes |
|---|---|---|
| `hits` | `list[SearchResult]` | Unchanged shape (`retrieval/models.py`) |
| `hints` | `list[Hint]` | Unchanged shape |
| `plan_diagnostics` entry | `dict` (`source`, `mode`, `airtable_formula`, `semantic_query`) | Unchanged shape |

**Independence**: each source's hits/hints/diagnostics entry is self-contained; the only cross-source step is the final merge (dedup by `(source, source_type, text[:80])`, citation ID assignment, `SearchResponse` construction — tools.py:624-647), which already runs once, after all per-source results are available, and is unaffected by whether those results arrived sequentially or concurrently.

**No change**: merge logic, dedup key, citation assignment, and final `SearchResponse` shape are untouched by this feature.

## Summary

This feature changes *execution scheduling* only. No entity gains, loses, or renames a field; no new entity is introduced. The data model documented here exists purely to confirm — as required by spec FR-004 — that concurrent computation cannot alter output shape, because every structure above is already independent per source and merged only once, after the fact, exactly as today.
