# Implementation Plan: D.Quals Confidentiality Enforcement

**Branch**: `003-confidential-redaction` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-confidential-redaction/spec.md`

**Note**: This plan was written retroactively — implementation predates this document (see spec.md's process note). It documents the design that was actually built and reviewed against the constitution, rather than driving new implementation work.

## Summary

Two of the D.Quals (Project Qualifications) source's Airtable facet fields — `Confidential Project` and `Confidential Client` — were already being ingested and indexed as ordinary metadata but had no access-control effect on retrieval. This feature adds query/response-time enforcement: records flagged `Confidential Project = CONFIDENTIAL` are dropped from every MCP tool's output; records flagged `Confidential Client = CONFIDENTIAL` are kept but have the client organisation's name deterministically redacted everywhere in returned text/metadata, with all links to the underlying source file/row suppressed. Enforcement is a single new policy module (`retrieval/confidentiality.py`) invoked from the one chokepoint all MCP tools already share (`RetrievalRouter.run()`), requiring no re-indexing and no new configuration surface.

## Technical Context

**Language/Version**: Python >=3.11 (repo pyproject.toml; dev/test run under 3.13)

**Primary Dependencies**: None added. Reuses existing `retrieval` module dependencies (`structlog`, `opensearch-py`, `pyairtable` — all already present); no new third-party package introduced.

**Storage**: OpenSearch (`mcp-d-quals` index) + Airtable (`(D.Quals)` table) — unchanged. This feature reads facet values already present on existing documents/rows; it does not alter storage schema or write new data.

**Testing**: pytest (existing `tests/unit/retrieval/` suite convention — construct `SearchResult` objects directly, no live backend calls)

**Target Platform**: Linux server (existing FastAPI/FastMCP deployment — no platform change)

**Project Type**: Single project — Python backend module within the existing MCP retrieval server (`src/retrieval/`)

**Performance Goals**: No measurable added latency for the common case (non-D.Quals sources, non-confidential D.Quals records) — enforcement is a cheap in-memory filter/regex pass over an already-small per-request hit list (≤ a few dozen hits), not an additional network call.

**Constraints**: Must not require re-indexing or re-processing existing OpenSearch/Airtable data (query/response-time enforcement only, per explicit scope decision); must not introduce a runtime on/off toggle (deliberate — see Constitution Check).

**Scale/Scope**: One new module (~180 lines), two small edits to `src/retrieval/router.py`, one new test file (~20 tests). No new sources, endpoints, or external integrations.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Source-Agnostic Retrieval** — PASS. This feature does not add a new source or hardcode a backend into the tool layer; it adds an access-control policy that is *intentionally* scoped to the one source (`d_quals`) that actually has these Airtable fields today (`CONFIDENTIAL_SOURCES = frozenset({"d_quals"})`, checked explicitly rather than inferred from key presence, per user decision). If a future source needs the same policy, extending `CONFIDENTIAL_SOURCES` is a one-line change — no tool-layer coupling introduced.
- **II. Reuse Before Building** — PASS. Reused the existing Airtable-citation-bypass pattern in `router.run()` (same insertion point, same "iterate `merged`, mutate in place" style) and generalized `facet_planner._word_present`'s boundary-regex technique instead of inventing a new redaction mechanism. Extended, not duplicated, the existing `_REDACTED_METADATA_KEYS` philosophy in `models.py` (kept that one as key-level redaction; this feature's value-level redaction lives in a separate, clearly-scoped module).
- **III. No Endpoint Ships Without an Explicit Auth Decision** — N/A. No new endpoint; this is an output filter on existing MCP tools.
- **IV. Structured, Loud Observability** — PASS (N/A boot check). This principle targets *risky/degraded fallback configurations* needing a loud startup check (e.g. `_log_citation_mode`). This feature has no fallback or degraded mode — it's unconditional enforcement with no configuration branch — so there's nothing analogous to surface at boot.
- **V. Environment-Driven Configuration** — PASS (by omission, deliberately). No new environment variable/config was introduced. This was a considered decision, not an oversight: a runtime kill-switch for an access-control feature is itself the risk (an accidentally-set `FLAG=0` silently disables a data-leak control with no audit trail), whereas a code revert (auditable via git/PR) is the correct rollback mechanism. Nothing here needed to flow through `.env`/settings because there is no configurable behavior.
- **Development Workflow** — **VIOLATED, justified in Complexity Tracking below.** This feature was designed and implemented via an interactive plan-mode session before `/speckit-specify`/`/speckit-plan` ran, not after. See Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/003-confidential-redaction/
├── spec.md               # /speckit-specify output
├── plan.md               # This file (/speckit-plan command output)
├── research.md           # Phase 0 output (/speckit-plan command)
├── data-model.md         # Phase 1 output (/speckit-plan command)
├── quickstart.md         # Phase 1 output (/speckit-plan command)
├── contracts/            # Phase 1 output (/speckit-plan command)
│   └── mcp-tool-output-confidentiality.md
├── checklists/
│   └── requirements.md
└── tasks.md              # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/retrieval/
├── confidentiality.py        # NEW — policy module (this feature)
├── router.py                 # MODIFIED — 2 call sites (merge-time filter, _resolve_citations exclusion)
├── models.py                 # UNCHANGED — existing key-level redact_metadata_for_response() reused as-is
├── sources/
│   ├── opensearch.py         # UNCHANGED — already surfaces confidential_project/confidential_client/client_organisation as facets
│   └── airtable.py           # UNCHANGED — already surfaces the Title-Case equivalents as raw metadata
├── citations.py               # UNCHANGED
└── facet_planner.py           # UNCHANGED (residual risk noted in research.md, not fixed by this feature)

tests/unit/retrieval/
└── test_confidentiality.py    # NEW — module + router-rule-mirror tests
```

**Structure Decision**: Single project (this repo is one Python backend, no frontend/mobile split). The feature is implemented as one new module inside the existing `src/retrieval/` package — the same location as its sibling policy modules (`citations.py`, `facet_planner.py`, `settings.py`) — plus minimal edits to the one router file every MCP tool already funnels through. No new top-level directories, services, or packages were needed.

## Complexity Tracking

> Constitution Check found one violation, justified below.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|---------------------------------------|
| Development Workflow — feature was implemented before `/speckit-specify`/`/speckit-plan` ran, not after | The user asked for this feature through Claude Code's interactive plan-mode (research → AskUserQuestion decisions → approved plan → implementation) in the same session, and only afterward asked for the Speckit artifacts to be generated for traceability. The plan-mode process already performed equivalent rigor — codebase exploration via research agents, an explicit design review, and user sign-off on scope/behavior decisions (blank-flag handling, redaction method, enforcement layer) — before any code was written. | Reverting the already-reviewed, already-tested implementation and redoing it strictly through `/speckit-specify` → `/speckit-plan` → `/speckit-tasks` → `/speckit-implement` would discard working, tested code to re-derive the same design a second time, for process compliance alone. The user was told explicitly, before generating these artifacts, that this run is a retroactive backfill, not the intended forward flow — this is a one-time, disclosed exception, not a proposed change to how future features should be handled. |
