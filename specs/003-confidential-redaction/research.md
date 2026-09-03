# Phase 0 Research: D.Quals Confidentiality Enforcement

No `[NEEDS CLARIFICATION]` markers remained in the Technical Context — all open questions were resolved with the user during the original planning session (see spec.md's process note) before any code was written. This document consolidates those decisions in the standard Decision/Rationale/Alternatives format for traceability.

> **2026-07-10 revision note**: §1-§6 below describe the *original* hide/redact design (drop confidential projects, redact client names, suppress links). That design was superseded by a tag-based approach — see §8 — which shows full record content always and communicates confidentiality via a visible bracketed tag instead. §1-§6 are kept for historical traceability (the chokepoint-location and detection-logic reasoning in §1-§2 and §7 remain fully valid; only the "what to do once detected" decisions in §3-§4-§5 were reversed). §7 (the production incident) remains fully applicable — detection must still be correct regardless of what action is taken afterward.

## 1. Where to enforce: single chokepoint vs. multiple call sites

**Decision**: Enforce inside `RetrievalRouter.run()` (`src/retrieval/router.py`), immediately after the cross-source RRF merge and before citation resolution.

**Rationale**: Tracing every hit's path from each source adapter (`src/retrieval/sources/opensearch.py`, `src/retrieval/sources/airtable.py`) through to the three MCP tools (`semantic_search`, `airtable_lookup`, `search` in `src/retrieval/mcp/tools.py`) showed that `_search_async` (backing the `search` tool) calls `router.run()` multiple times per request (once per source, plus an enrichment call) but never constructs a `SearchResult` outside of a `router.run()` call. One filter placed inside `router.run()` therefore covers 100% of MCP output paths with zero risk of a bypassed second path.

**Alternatives considered**: Filtering inside each source adapter (`opensearch.py`, `airtable.py`) — rejected because it would need to be duplicated in two places and re-duplicated for any future source, whereas the router chokepoint is source-agnostic by construction. Filtering inside `tools.py`'s `_search_async` dedup step — rejected once it was confirmed that step only operates on hits that already passed through `router.run()`, making a second filter there redundant.

## 2. Key-spelling mismatch between semantic and structured hits

**Decision**: Every facet lookup checks both the snake_case key (`confidential_project`, `confidential_client`, `client_organisation` — used by OpenSearch-native/semantic hits) and the raw Title-Case Airtable field name (`"Confidential Project"`, `"Confidential Client"`, `"Client Organisation"` — used by Airtable-native/structured hits), via a small ordered-key-list helper.

**Rationale**: `src/retrieval/sources/opensearch.py`'s `_FACET_FIELDS` hoists facets under snake_case keys at index time, but `src/retrieval/sources/airtable.py`'s `_format_rows` copies raw Airtable field names verbatim into `metadata` (`metadata[fname] = value`). These are two genuinely different key spellings for the same logical field, not a bug to fix — normalizing at read time (in the new policy module) is simpler and safer than changing either source adapter's established metadata shape, which other code already depends on.

**Alternatives considered**: Normalizing keys at the source-adapter layer instead — rejected as higher-risk (touches two files other features depend on) for no benefit over a local two-key lookup in the one new module that needs it.

## 3. Redaction method: deterministic substring vs. LLM verification

**Decision**: Deterministic, case-insensitive substring matching against the record's own recorded `Client Organisation` value, plus two derived variants: the value with any `(...)` parenthetical stripped, and the parenthetical's inner text alone (so `"X (Y)"` also matches standalone `"X"` and `"Y"`). No LLM-based verification pass.

**Rationale**: User-confirmed trade-off. An LLM verification pass would catch more informal aliases/nicknames but adds per-request latency and cost to every confidential-client hit, and is itself probabilistic rather than a hard guarantee — so it doesn't actually deliver a stronger guarantee, just a different failure mode. Deterministic matching is fast, free, auditable, and consistent with this codebase's existing query-time patterns (e.g. `redact_metadata_for_response`'s key-level filtering).

**Alternatives considered**: LLM verification pass (rejected per above); fuzzy/edit-distance matching (rejected as a source of false positives — redacting an unrelated word that happens to be similar — with no corresponding guarantee of catching real aliases).

**Accepted limitation**: An organisation mentioned only by an alias/nickname never recorded as its official `Client Organisation` value will not be masked. Documented in spec.md's Assumptions, not treated as a defect.

## 4. Suppressing confidential-client citations: exclude-before-resolve vs. resolve-then-strip

**Decision**: Confidential-client hits are excluded from `_resolve_citations`'s semantic-hit input entirely, rather than having `citation_url`/`citations` nulled after resolution runs.

**Rationale**: Reading `_resolve_citations` (`router.py`) showed that `S3CitationResolver.resolve_semantic_hits` unconditionally overwrites `citation_url`, `citations`, **and** `metadata["source_s3_key"]`/`["source_s3_url"]` for any semantic hit carrying `s3_key`/`s3_bucket` — and those two metadata keys are explicitly allow-listed for client visibility (`_MCP_LOCATOR_KEYS` in `models.py`), never stripped by the generic `redact_metadata_for_response`. A "resolve then strip `citation_url`" approach would still leak the true S3 path into metadata. Excluding the hit from resolution input entirely closes this cleanly. Structured (Airtable) hits never reach `_resolve_citations` at all, so their `citation_url`/`citations` (set once by the Airtable adapter) are nulled directly by the same policy function, independent of the pre-existing `AIRTABLE_CITATIONS_ENABLED` flag (that flag's default must not be relied upon for this guarantee).

**Alternatives considered**: Resolve then strip afterward — rejected once the `source_s3_key`/`source_s3_url` leak above was found by direct code reading, not assumption.

## 5. Feature flag / kill-switch

**Decision**: No environment variable or settings field. Enforcement is unconditional.

**Rationale**: User-confirmed. Unlike `AIRTABLE_CITATIONS_ENABLED` (default-off; flipping it only *adds* exposure, low-stakes to toggle), a confidentiality toggle would default *on*, and an accidentally-set `FLAG=0` in shared config would silently and durably disable a data-leak control with no audit trail. A code revert (auditable via git/PR) is the correct rollback mechanism for a control like this, not an always-present runtime knob.

**Alternatives considered**: Mirroring `RetrievalRuntimeSettings`'s existing env-var pattern for a new flag — rejected for the reason above; noted as available to add later if the team's ops model changes, but not built pre-emptively.

## 6. Defense-in-depth: `payload["fields"]` redaction

**Decision**: Also redact `hit.payload["fields"]` (Airtable's raw display fields) using the same pattern, in addition to `text` and `metadata`.

**Rationale**: `src/retrieval/formatter.py`'s `build_markdown_table` reads `payload["fields"]` in preference to `metadata` and currently has no production callers (dead code today), but if it's ever wired up it would silently bypass this feature's redaction. Redacting `payload["fields"]` alongside `metadata` is a few lines of cheap insurance against that landmine, given this is a security control.

**Alternatives considered**: Leaving `payload["fields"]` unredacted and only documenting the risk — rejected as unnecessarily leaving a known, cheaply-closeable gap in a security-sensitive feature.

## 7. Post-deployment incident: caller-narrowed `fields` bypassed enforcement entirely

**What happened**: after initial deployment, querying the live MCP server for a genuinely `Confidential Project = CONFIDENTIAL` record (Project Number 3010927, "MCC PSOA Liberia Stage 2") via `airtable_lookup` returned it unredacted. Confirmed live against the real Airtable record and reproduced deterministically: when the caller (here, an LLM planner choosing a token-efficient column subset — `Project Name`, `Client Organisation`, `Project Description`, etc.) does not include `Confidential Project` in the `fields` argument, Airtable's API omits that column from the row entirely. `is_confidential_project`/`is_confidential_client` had no way to distinguish "field not fetched" from "field genuinely blank" — and blank was, by design (see §5's blank-flag decision), treated as "not confidential."

**Root cause**: `airtable_lookup`'s `fields` parameter and `search`'s capped enrichment-field list (`_enrichment_fields_for_schema`, `src/retrieval/mcp/tools.py`) both flow unmodified into `AirtableSource.filter_structured()` (`src/retrieval/sources/airtable.py`), which passes them straight to the Airtable `listRecords` API call. This is a caller-controllable input that the original design didn't account for — the original threat model implicitly assumed "all fields are always fetched," which is Airtable's default behavior but not guaranteed once a caller narrows the request.

**Fix**: `ensure_required_fetch_fields(source_name, fields)` in `src/retrieval/confidentiality.py` — unions `Confidential Project`/`Confidential Client`/`Client Organisation` into any non-empty, caller-narrowed `fields` list for a `CONFIDENTIAL_SOURCES` table, before the Airtable API call is made (wired into `AirtableSource.filter_structured`, the one place that call happens for both `airtable_lookup` and `search`'s enrichment path). A `None`/empty `fields` value (meaning "fetch everything") is left untouched.

**Verified**: reproduced and fixed against the live record via direct Airtable API calls (not just unit tests) — see quickstart.md for the reproduction script. 6 regression tests added to `tests/unit/retrieval/test_confidentiality.py` covering the injection helper and the end-to-end scenario.

**Lesson for future confidentiality-adjacent features**: any enforcement mechanism that reads a field from a data source must also verify it *actually has fetch-time visibility* into that field, not just that its detection logic is correct — a correct check on absent data is silently equivalent to no check at all.

## 8. Revision: switch from hide/redact to tag (2026-07-10)

**Decision**: Replace dropping (confidential projects) and redaction+link-suppression (confidential clients) with a single, uniform action: prepend a bracketed tag (`**[CONFIDENTIAL PROJECT]**` / `**[CONFIDENTIAL CLIENT]**` / `**[CONFIDENTIAL PROJECT & CLIENT]**`) to the hit's `text`, and to its `record_summary`/`deck_summary` metadata when present. Nothing is dropped; nothing is redacted; citations/links are no longer touched.

**Rationale**: User-directed reversal — the business wants full transparency of content with a clear confidentiality signal, not information withheld. This is a legitimate, different tradeoff than the original "must never be visible" requirement, not a bug fix.

**Where the tag goes, given no dedicated "title" field exists**: Investigated whether a discrete "title" field could be tagged directly. Found neither hit type has one: structured (Airtable) hits build `text` from `"fname: value"` lines in raw API response order (not guaranteed to put "Project Name" first — `src/retrieval/sources/airtable.py:293-305`); semantic (OpenSearch) hits' `_FACET_FIELDS`/`_SEMANTIC_METADATA_FIELDS` contain no project-name/title field at all (`metadata["primary_key"]` is the Project Number, a number, not a name — confirmed via `config/retrieval_sources.yaml`'s `identifier_field: "Project Number"`). The closest thing to a reliable title is the opening sentence of `record_summary`/`deck_summary`, which `record_summary.py`'s prompt explicitly instructs to name the project. Decision: prepend the tag to the start of `text` (always present, always the first thing read) and to `record_summary`/`deck_summary` (which specifically open with the project's name) — the practical equivalent of "along the title" given no dedicated title field exists for either hit type. Reordering Airtable fields to force "Project Name" first was considered and rejected as unnecessary risk to a shared adapter for a benefit already achieved more simply.

**What carried over unchanged**: `CONFIDENTIAL_SOURCES`, the key-spelling duality handling, `is_confidential_project`/`is_confidential_client`, and — critically — `ensure_required_fetch_fields` (§7's incident fix). Correct detection matters exactly as much under a tag-based design as under a hide/redact one; only the *action taken upon detection* changed.

**Alternatives considered**: A structured metadata-only flag (e.g. `metadata["confidentiality_tag"]`) instead of/alongside the inline text tag — rejected (or rather, deferred) per explicit user choice: a metadata-only field relies on the MCP client choosing to surface it, which doesn't reliably satisfy "along the title"; an inline text tag is guaranteed to appear in whatever the client actually reads.

## Residual risks (explicitly out of scope for this feature, documented for future follow-up)

- `facet_planner.py`'s vocabulary aggregation (`MATCHABLE_FACETS` including `client_organisation`) runs across the entire index with no confidential-record exclusion. This is no longer a redaction-bypass concern post-revision (nothing is redacted anymore), but is still worth noting as a pre-existing internal-only aggregation.
- Data at rest is unchanged by design (query-time-only enforcement, per explicit scope decision) — direct OpenSearch/Airtable access still shows true values, same as the retrieval layer now does too.
- (Historical, no longer applicable post-revision) The original design's result-count-shrinkage concern from dropping confidential-project hits no longer applies — nothing is dropped anymore.
