# Implementation Plan: Concurrent Multi-Source Retrieval Planning & Execution

**Branch**: `004-mcp-retrieval-latency` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-mcp-retrieval-latency/spec.md`

## Summary

`plan_retrieval_impl` and `_search_async` in `src/retrieval/mcp/tools.py` each loop over requested sources one at a time. Every iteration calls the blocking, synchronous `plan_query()` (`src/retrieval/planner/nl_planner.py:116`, a blocking `anthropic.Anthropic().messages.create()`), and `_search_async` additionally does `await router.run(q)` plus an enrichment `await router.run(enrich_q)` per source inside the same loop — so a query touching N sources pays for N sequential Claude/router round-trips even though each source's plan and result are already independent of every other source's.

The fix reuses the concurrency idiom already established throughout this codebase (`asyncio.to_thread` wrapping a blocking call, gathered with `asyncio.gather` — see `retrieval/router.py:83`, `retrieval/embedding/voyage.py:97`, `retrieval/embedding/reranker.py:111`, `retrieval/sources/airtable.py:258`, `retrieval/sources/opensearch.py:279/283/392-393`): wrap each source's blocking `plan_query()` call in `asyncio.to_thread`, and for `_search_async` also fold the subsequent `router.run` + enrichment `router.run` into the same per-source async unit of work, then run one such unit per source via `asyncio.gather`. Per-source exception handling (fallback to the existing default `semantic_only` plan) stays inside each per-source unit, so `asyncio.gather` never sees a source-level exception and other sources are unaffected. No new dependency, no change to prompts, tool contracts, or output shape.

## Technical Context

**Language/Version**: Python 3.11 (`requires-python = ">=3.11"`, pyproject.toml:10)

**Primary Dependencies**: `anthropic>=0.45` (existing, sync client — reused as-is, not replaced with an async client), `asyncio` (stdlib) — no new dependency added

**Storage**: N/A (no persistence change)

**Testing**: `pytest`, `pytest-asyncio` (existing dev dependencies, pyproject.toml:56-58)

**Target Platform**: Linux server (existing FastMCP/FastAPI deployment, `src/pipeline/api/main.py`)

**Project Type**: Single project — backend retrieval/MCP service (`src/retrieval/`, `src/pipeline/`)

**Performance Goals**: Multi-source query wall-clock time approaches single-source wall-clock time (currently scales ~linearly with source count); see spec SC-001 (≤~1.5x single-source time for a 4-source query, down from ~4x today)

**Constraints**: Zero behavior change to output shape/content (spec FR-004), zero regression for single-source queries (spec FR-005), no new dependencies (spec FR-006), per-source failure isolation must be bit-for-bit equivalent to today's fallback behavior (spec FR-003)

**Scale/Scope**: Two functions in one file (`src/retrieval/mcp/tools.py`): `plan_retrieval_impl` (~line 263) and `_search_async` (~line 506). No other files require changes — `plan_query()`, `RetrievalRouter.run()`, `_build_source_plan()`, and the merge/dedup/citation logic are all reused unmodified.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Principle I (Source-Agnostic Retrieval)**: PASS. No source-specific logic is added; the change is purely about *when* the existing per-source calls execute (concurrently vs. sequentially), not *what* they do. All sources continue to flow through the registry exactly as today.
- **Principle II (Reuse Before Building)**: PASS — this is the principle driving the whole design. The plan explicitly reuses the existing `asyncio.to_thread` + `asyncio.gather` idiom already used in five other places in this codebase rather than introducing a new async Anthropic client, a thread pool, or any other new concurrency mechanism.
- **Principle III (Explicit Auth Decision)**: N/A. No new endpoint is introduced; the existing MCP tool surface and its auth gating (Entra OAuth, specs/001) are untouched.
- **Principle IV (Structured, Loud Observability)**: PASS. Existing `structlog` warning calls on planner failure (`log.warning("search_plan_failed", ...)`, `log.warning("planner_failed", ...)`) are preserved unchanged inside each per-source unit of work — failures remain loud, just from within a concurrently-scheduled task instead of a sequential loop iteration.
- **Principle V (Environment-Driven Configuration)**: N/A. No new configuration, credentials, or environment variables are introduced.

No violations — Complexity Tracking table is not needed.

## Project Structure

### Documentation (this feature)

```text
specs/004-mcp-retrieval-latency/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md         # Phase 1 output
├── quickstart.md         # Phase 1 output
├── contracts/            # Phase 1 output (internal function contracts, no external API)
└── tasks.md              # Phase 2 output (/speckit-tasks - not created by this command)
```

### Source Code (repository root)

```text
src/
└── retrieval/
    ├── mcp/
    │   └── tools.py          # MODIFIED: plan_retrieval_impl loop (~:290), _search_async loop (~:528)
    ├── planner/
    │   └── nl_planner.py     # UNCHANGED: plan_query() reused as-is via asyncio.to_thread
    └── router.py             # UNCHANGED: RetrievalRouter.run() reused as-is (reference pattern for asyncio.gather)

tests/
└── unit/
    └── pipeline/              # existing test location (currently untracked/new per git status);
                                # add/extend tests for plan_retrieval_impl and _search_async here
```

**Structure Decision**: Single project, no new modules or directories. This is a localized concurrency refactor inside `src/retrieval/mcp/tools.py`; no other part of the codebase needs to move or be restructured. Tests land in the existing `tests/unit/pipeline/` tree alongside other pipeline unit tests.

## Complexity Tracking

*No constitution violations — table not needed.*
