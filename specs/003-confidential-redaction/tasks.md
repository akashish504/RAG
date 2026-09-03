# Tasks: D.Quals Confidentiality Enforcement

**Input**: Design documents from `/specs/003-confidential-redaction/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Status note**: This tasks list was generated retroactively, after implementation (see spec.md's process note). Every task below is marked `[x]` because it was already completed during the original plan-mode implementation session — this document exists for traceability, not to drive new work. Re-open a task (`[ ]`) if a future change needs to redo or extend it.

**Tests**: Included — this feature's original approval explicitly called for unit test coverage.

**Organization**: Tasks are grouped by user story per spec.md's priorities (US1, US2 = P1; US3 = P2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Could have run in parallel (different files/functions, no dependency on an incomplete task)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- File paths are exact

## Path Conventions

Single project — `src/`, `tests/` at repository root (per plan.md's Structure Decision).

---

## Phase 1: Setup

**Purpose**: Project initialization and basic structure

- [x] T001 Confirm no new dependencies, config, or scaffolding are required — the feature is additive to the existing `src/retrieval/` package (no new project/service to initialize)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared detection/normalization logic that both User Story 1 and User Story 2 depend on

**⚠️ CRITICAL**: Both user stories below build directly on this phase

- [x] T002 [P] Create `src/retrieval/confidentiality.py` with `CONFIDENTIAL_SOURCES` (scoped to `d_quals`), key-alias tuples for both snake_case (semantic) and Title-Case (structured) spellings, and `_as_list`/`_facet_values` normalization helpers (handles list- and string-valued Airtable `multipleSelects` facets)
- [x] T003 [P] Implement `is_confidential_project(hit)` and `is_confidential_client(hit)` in `src/retrieval/confidentiality.py` (source-gated; case-insensitive exact `"CONFIDENTIAL"` match; blank/missing = not confidential)
- [x] T004 Add foundational tests in `tests/unit/retrieval/test_confidentiality.py`: `test_non_d_quals_source_untouched`, `test_is_confidential_helpers_respect_source_gating` (proves source-gating, not mere key-presence-gating)

**Checkpoint**: Detection logic ready — both user stories can now be implemented

---

## Phase 3: User Story 1 - Confidential projects never surface (Priority: P1) 🎯 MVP

**Goal**: A record with `Confidential Project = CONFIDENTIAL` never appears in any MCP tool's output, in any query mode.

**Independent Test**: Query with terms matching a known confidential-project record via `semantic_search`, `airtable_lookup`, and `search`; confirm it's absent from every response.

### Tests for User Story 1

- [x] T005 [P] [US1] Add `test_confidential_project_dropped_semantic_hit`, `test_confidential_project_dropped_structured_hit`, `test_confidential_project_non_confidential_value_kept`, `test_confidential_project_blank_or_missing_kept`, `test_confidential_project_case_insensitive` in `tests/unit/retrieval/test_confidentiality.py`

### Implementation for User Story 1

- [x] T006 [US1] Implement the drop rule in `filter_and_redact_confidential()` (`src/retrieval/confidentiality.py`): exclude every hit where `is_confidential_project(hit)` is true
- [x] T007 [US1] Wire `merged = filter_and_redact_confidential(merged)` into `RetrievalRouter.run()` (`src/retrieval/router.py`), immediately after the RRF merge and before `_resolve_citations`, so the drop happens before any citation-resolution work is wasted on a hit about to be discarded

**Checkpoint**: User Story 1 fully functional and independently testable — confidential projects are hidden across all three MCP tools (they all funnel through `RetrievalRouter.run()`)

---

## Phase 4: User Story 2 - Confidential client names are masked (Priority: P1)

**Goal**: A record with `Confidential Client = CONFIDENTIAL` still appears, but its client organisation's name is redacted everywhere in returned text/metadata, and no link to the source file/row is returned.

**Independent Test**: Query for a record with `Confidential Client = CONFIDENTIAL` and a populated client name; confirm the record appears with the name masked and `citation_url`/`citations` empty.

### Tests for User Story 2

- [x] T008 [P] [US2] Add `test_confidential_client_redacts_text_and_metadata_semantic_hit`, `test_confidential_client_redacts_structured_metadata_fields`, `test_confidential_client_redacts_record_summary_and_deck_summary`, `test_confidential_client_redacts_payload_fields`, `test_scalar_string_facet_values_handled`, `test_acronym_in_parens_redacted_standalone` in `tests/unit/retrieval/test_confidentiality.py`
- [x] T009 [P] [US2] Add `test_confidential_client_suppresses_citation_url_and_citations`, `test_confidential_client_suppression_independent_of_airtable_citations_enabled`, `test_confidential_client_blank_or_missing_kept_as_is` in `tests/unit/retrieval/test_confidentiality.py`
- [x] T010 [P] [US2] Add `test_resolve_citations_excludes_confidential_client_semantic_hits` (router-rule mirror, matching the existing `test_citation_policy.py` idiom) in `tests/unit/retrieval/test_confidentiality.py`

### Implementation for User Story 2

- [x] T011 [US2] Implement `_client_name_variants(hit)` in `src/retrieval/confidentiality.py`: canonical `client_organisation` value(s) + parenthetical-stripped base + standalone parenthetical content (e.g. an acronym), deduplicated case-insensitively
- [x] T012 [US2] Implement `_build_redaction_pattern(names)`, `_redact_value(value, pattern)`, `_redact_dict_in_place(d, pattern)` in `src/retrieval/confidentiality.py` (case-insensitive, word-boundary-ish alternation over escaped names; generalizes `facet_planner._word_present`'s boundary technique into a substitutable pattern)
- [x] T013 [US2] Extend `filter_and_redact_confidential()` to, for each remaining confidential-client hit: redact `hit.text`, every metadata value via `_redact_dict_in_place`, and `hit.payload["fields"]` if present (defense-in-depth for the currently-unwired `formatter.build_markdown_table` path); then force `hit.citation_url = None` and `hit.citations = []` unconditionally (`src/retrieval/confidentiality.py`)
- [x] T014 [US2] Exclude confidential-client hits from the semantic-hit list passed into S3 resolution inside `RetrievalRouter._resolve_citations()` (`src/retrieval/router.py`) — required because `S3CitationResolver.resolve_semantic_hits` unconditionally overwrites `citation_url`/`citations`/`metadata["source_s3_key"]`/`["source_s3_url"]`, so exclusion-before-resolve is necessary; stripping fields after resolution would still leak the true S3 path into metadata

**Checkpoint**: User Stories 1 AND 2 both work independently — confidential projects are hidden, confidential clients are masked with no source links

---

## Phase 5: User Story 3 - Non-confidential sources and records are unaffected (Priority: P2)

**Goal**: Records from other sources, and D.Quals records with both flags blank/non-confidential, pass through byte-for-byte unchanged.

**Independent Test**: Run existing queries against non-D.Quals sources and non-confidential D.Quals records; confirm output is identical to pre-feature behavior.

### Tests for User Story 3

- [x] T015 [P] [US3] Add `test_non_d_quals_source_untouched` variant covering both flags simultaneously set (already added in Phase 2 as T004 covers the single-flag case; this task extended it to the combined case) and `test_mixed_batch_drops_and_redacts_independently` (drop + redact + clean hit together in one batch) in `tests/unit/retrieval/test_confidentiality.py`

### Implementation for User Story 3

- [x] T016 [US3] No production code changes required for this story — it validates that T006/T013's source-gating (via `is_confidential_project`/`is_confidential_client`, both checking `hit.source in CONFIDENTIAL_SOURCES` first) already guarantees non-`d_quals` hits and non-flagged `d_quals` hits are left untouched
- [x] T017 [US3] Run `python -m pytest tests/unit/retrieval/ tests/unit/test_facets.py -q` to confirm zero regressions in existing citation/facet behavior (128 tests passed)

**Checkpoint**: All three user stories independently functional and verified together

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T018 [P] Generate Speckit documentation trail (`spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/mcp-tool-output-confidentiality.md`, `quickstart.md`, `checklists/requirements.md`) in `specs/003-confidential-redaction/` for traceability (retroactive per this feature's disclosed process deviation)
- [x] T019 Manually execute `quickstart.md`'s step 2 validation script end-to-end and confirm it prints the expected success line
- [x] T020 Document residual risks not fixed by this feature (facet_planner vocabulary aggregation, `formatter.py` dead-code path, expected result-count shrinkage, query-time-only scope) in `research.md` and `spec.md`'s Assumptions

---

## Phase 7: Post-Deployment Incident Fix (discovered after initial deployment)

**What happened**: after the initial deploy, querying the live server for Project Number 3010927 ("MCC PSOA Liberia Stage 2", genuinely `Confidential Project = CONFIDENTIAL`) via `airtable_lookup` returned it unredacted — reproduced live against the real Airtable record. See research.md §7 for full root-cause analysis.

- [x] T021 Reproduce the leak live against the real Airtable record via direct API calls (not just a hypothesis) — confirmed `is_confidential_project` returns `False` when the caller's `fields` list omits `Confidential Project`
- [x] T022 Implement `ensure_required_fetch_fields(source_name, fields)` in `src/retrieval/confidentiality.py` — unions the confidentiality + client-org columns into any caller-narrowed `fields` list for a `CONFIDENTIAL_SOURCES` table
- [x] T023 Wire `ensure_required_fetch_fields` into `AirtableSource.filter_structured()` in `src/retrieval/sources/airtable.py` (the single fetch-call site shared by `airtable_lookup` and `search`'s enrichment path)
- [x] T024 [P] Add regression tests in `tests/unit/retrieval/test_confidentiality.py`: `test_missing_confidentiality_key_is_indistinguishable_from_blank` (documents the hazard), `test_ensure_required_fetch_fields_injects_confidentiality_columns`, `test_ensure_required_fetch_fields_noop_when_fields_not_narrowed`, `test_ensure_required_fetch_fields_noop_for_non_confidential_source`, `test_ensure_required_fetch_fields_closes_the_reproduced_leak`
- [x] T025 Re-verify live against the real record with and without the fix (before: leaked; after: dropped) and re-run full `tests/unit/retrieval/` + `tests/unit/test_facets.py` suite (139 tests, zero regressions)
- [ ] T026 **Pending user action**: redeploy (pull + rebuild + restart) and re-confirm Project Number 3010927 is no longer returned by the live server

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS both user stories (both need `is_confidential_project`/`is_confidential_client`)
- **User Story 1 (Phase 3)**: Depends on Foundational only — independently shippable as the MVP
- **User Story 2 (Phase 4)**: Depends on Foundational only — does not depend on User Story 1's drop logic (both live in the same function but are additive, not sequential business logic)
- **User Story 3 (Phase 5)**: Depends on User Story 1 + User Story 2 being implemented (it verifies both leave everything else untouched — nothing to verify before they exist)
- **Polish (Phase 6)**: Depends on all user stories being complete

### Parallel Opportunities

- T002/T003 (foundational helpers) could have been built in parallel (different functions, same new file — minor same-file contention only)
- T005 and T008/T009/T010 (tests for US1 vs US2) could run in parallel — different test functions, no shared state
- US1's implementation (T006-T007) and US2's implementation (T011-T014) touch the same two files (`confidentiality.py`, `router.py`) but different functions/call sites — parallelizable by a team with care, sequential in this single-session build

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (trivial) + Phase 2 (Foundational)
2. Complete Phase 3 (User Story 1) → confidential projects are already fully hidden — this alone closes the highest-severity gap ("must never be visible under any circumstances")
3. **STOP and VALIDATE**: run T005's tests independently

### Incremental Delivery (what actually happened, in this order)

1. Foundational helpers → User Story 1 (drop) → User Story 2 (redact + suppress links) → User Story 3 (regression verification) → Polish/documentation
2. Each phase's tests passed before moving to the next; full suite (128 tests) re-run at the end with zero regressions

---

## Phase 8: Revision — Switch from Hide/Redact to Tag (2026-07-10)

**What changed**: the user reversed the original design's core behavior — instead of dropping confidential projects and redacting confidential clients, every record now shows in full (name, links, everything) with a visible bracketed tag (`**[CONFIDENTIAL PROJECT]**` / `**[CONFIDENTIAL CLIENT]**` / `**[CONFIDENTIAL PROJECT & CLIENT]**`) prepended to its text. See spec.md's Revision History and research.md §8 for the full rationale.

- [x] T027 Rewrite `src/retrieval/confidentiality.py`: remove `_client_name_variants`, `_build_redaction_pattern`, `_redact_value`, `_redact_dict_in_place`; replace `filter_and_redact_confidential` with `tag_confidential_hits` (prepends the correct tag to `text`/`record_summary`/`deck_summary`, drops nothing, touches no citation fields). `CONFIDENTIAL_SOURCES`, key-spelling handling, `is_confidential_project`/`is_confidential_client`, and `ensure_required_fetch_fields`/`REQUIRED_FETCH_FIELDS` (the production-incident fix) carried over unchanged
- [x] T028 Update `src/retrieval/router.py`: swap the `filter_and_redact_confidential(merged)` call for `tag_confidential_hits(merged)`; revert `_resolve_citations` to its pre-incident form (`semantic_hits = [h for h in hits if h.source_type == "semantic"]`, no confidential-client exclusion) since links are no longer suppressed
- [x] T029 [P] Rewrite `tests/unit/retrieval/test_confidentiality.py`: remove drop/redaction/citation-suppression tests; add tagging tests (project-only, client-only, both-flags-combined, blank/missing untouched, tag on `record_summary`/`deck_summary`, citations/links explicitly asserted as preserved, mixed-batch nothing-dropped); keep all detection/`ensure_required_fetch_fields` tests unchanged
- [x] T030 Run `pytest tests/unit/retrieval/ tests/unit/test_facets.py -q` — 135 tests passed, zero regressions
- [x] T031 Verify live against real Airtable data: Project Number 3010927 (`Confidential Project = CONFIDENTIAL`) now tags instead of dropping; Project Number 3110071 ("The Coca-Cola Company", `Confidential Client = CONFIDENTIAL` only) now tags with the client name and citation link both fully visible
- [x] T032 Sync `spec.md`, `research.md`, `data-model.md`, `contracts/mcp-tool-output-confidentiality.md` to describe the new tag-based contract, with revision notes preserving the original design's history rather than deleting it
- [ ] T033 **Pending user action**: redeploy (pull + rebuild + restart) and spot-check a known confidential-project and confidential-client record on the live server
