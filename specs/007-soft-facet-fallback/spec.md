# Feature Specification: Soft-Facet Fallback for Retrieval Search

**Feature Branch**: `007-soft-facet-fallback`

**Created**: 2026-07-23

**Status**: Draft

**Input**: User description: "Add a soft-facet fallback to retrieval search so natural-language-derived facet filters stop over-narrowing results. Run the search with hard derived-facet filters as today; if the filtered search returns fewer than a threshold of results, automatically re-run with the derived facets applied as ranking boosts instead of filters. Scope: all facet-filtered sources (D.Quals, Proposal Library, Knowledge Library), configured per source via a facet_mode knob. Explicitly caller-supplied structured filters always remain hard; confidentiality filtering always remains hard."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Loosely-worded queries stop losing results (Priority: P1)

A consultant searches "financial inclusion work in East Africa". The system detects "East Africa" as a region facet and today filters to only documents tagged exactly that way — hiding strong, relevant projects tagged "Sub-Saharan Africa" or "Kenya", sometimes returning nothing at all. With the fallback, when the strict interpretation yields too few results, the search widens automatically: region-matching documents still rank first, but strong matches with different tags become visible again.

**Why this priority**: This is the observed failure being fixed — derived facets silently discard relevant work, and users have no way to know results were hidden or how to rephrase to recover them.

**Independent Test**: Take real queries that today return zero/very few results due to a derived facet; verify they now return relevant results, with facet-matching documents ranked above equally-relevant non-matching ones.

**Acceptance Scenarios**:

1. **Given** a query whose derived facet filter yields zero results, **When** the search runs in fallback mode, **Then** results are returned from the widened search and documents matching the derived facets rank ahead of comparable non-matching documents.
2. **Given** a query whose derived facet filter yields fewer results than the fallback threshold, **When** the search runs, **Then** the widened result set supplements the strict one rather than replacing relevant strict matches.
3. **Given** a query whose derived facet filter yields ample results (at or above the threshold), **When** the search runs, **Then** behavior and results are identical to today's strict filtering — no widening occurs.

---

### User Story 2 - Guardrails: explicit filters and confidentiality never soften (Priority: P1)

A caller (a tool invocation or downstream integration) passes structured filters explicitly — e.g. `client_organisation: "Gates Foundation"` — or the system applies confidentiality restrictions. These are commands, not guesses: they must exclude non-matching documents in every mode, including during a fallback widening.

**Why this priority**: Ties with P1 in importance because it is a correctness/safety boundary — softening an explicit filter breaks caller trust, and softening confidentiality filtering would be a data-exposure incident.

**Independent Test**: Run searches with explicit filters and confidential-record scenarios in every facet mode; verify exclusion semantics are bit-identical to today's behavior.

**Acceptance Scenarios**:

1. **Given** a caller-supplied structured filter, **When** the search widens via fallback, **Then** the explicit filter still excludes non-matching documents from the widened set.
2. **Given** confidentiality restrictions applicable to the requesting context, **When** any facet mode (hard, soft, fallback) processes any query, **Then** confidentiality exclusions apply unchanged — the fallback can never surface a document that strict mode would have withheld for confidentiality reasons.
3. **Given** a query that produces both derived facets and explicit filters, **When** fallback widening triggers, **Then** only the derived facets soften; the explicit filters remain hard.

---

### User Story 3 - Per-source configuration (Priority: P2)

An operator enables or tunes the behavior per retrieval source in the source registry configuration — turning fallback on for D.Quals, Proposal Library, and Knowledge Library — without code changes, consistent with how sources are already configured.

**Why this priority**: Needed for safe rollout and consistent with the project's source-registry principle, but the feature delivers value with a single hardcoded-equivalent default if configuration shipped later.

**Independent Test**: Change a source's facet mode in configuration only; verify the source's search behavior changes accordingly after redeploy, with no code modifications.

**Acceptance Scenarios**:

1. **Given** a source configured `facet_mode: hard`, **When** searches run, **Then** behavior is exactly today's strict filtering.
2. **Given** a source configured `facet_mode: fallback`, **When** a strict search under-delivers, **Then** widening occurs per User Story 1.
3. **Given** a source configured `facet_mode: soft`, **When** searches run, **Then** derived facets are never applied as exclusions, only as ranking preferences.
4. **Given** a source with no facet mode configured, **When** searches run, **Then** the mode defaults to today's behavior (hard).

---

### Edge Cases

- **Derived facets only, no threshold shortfall ambiguity**: exactly at the threshold count → no widening (threshold is a minimum-acceptable count; meeting it means strict results stand).
- **Query derives no facets at all**: fallback logic must be a no-op — no second search, no behavior change.
- **Widened search still returns nothing**: the query genuinely has no matches; return the empty result as today (fallback widens facets, it does not loosen the query itself).
- **Duplicate results across strict and widened passes**: a document matching both must appear once, at its best rank — never twice.
- **Facet-derived and explicit filter on the same field**: explicit wins and stays hard; the derived one is redundant and must not soften the explicit one.
- **Latency budget**: fallback adds a second search round-trip only when the strict pass under-delivers; sources must not pay the cost on well-matched queries.
- **Result provenance**: when widening occurred, the response should make that discoverable (e.g. in result metadata/logging) so quality issues can be traced to widened searches.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Each facet-filtered retrieval source MUST support three facet modes: `hard` (today's strict filtering), `soft` (derived facets only ever influence ranking), and `fallback` (strict first, widen when under-delivering).
- **FR-002**: In fallback mode, the widened pass MUST trigger when the strict pass returns fewer results than a configurable minimum-results threshold (default: fewer than 3 results).
- **FR-003**: In any widened or soft pass, documents matching the derived facets MUST rank ahead of otherwise-equally-ranked documents that do not match.
- **FR-004**: Caller-supplied structured filters MUST remain exclusionary in every mode and every pass.
- **FR-005**: Confidentiality filtering MUST remain exclusionary in every mode and every pass; no mode may surface a document that hard mode would withhold for confidentiality reasons.
- **FR-006**: The facet mode and fallback threshold MUST be configurable per source in the existing source registry configuration, defaulting to `hard` when unset; enabling `fallback` for D.Quals, Proposal Library, and Knowledge Library is part of this feature's rollout.
- **FR-007**: When a query derives no facets, all modes MUST behave identically to today with no additional search cost.
- **FR-008**: Results appearing in both the strict and widened passes MUST be deduplicated, keeping the best rank.
- **FR-009**: Searches where widening occurred MUST be observable (logged with the derived facets and both pass result counts) for quality tracing.
- **FR-010**: A strict pass meeting the threshold MUST return exactly what it returns today — byte-for-byte behavioral compatibility in the well-matched case.

### Key Entities

- **Derived facet**: A structured filter guessed from the user's query text, grounded in values actually present in the index. The only kind of filter the fallback may soften.
- **Explicit filter**: A structured filter supplied by the caller. Never softened.
- **Confidentiality restriction**: Access-based exclusion applied by the system. Never softened; out of scope for any mode logic beyond "unchanged".
- **Facet mode**: Per-source setting (`hard` | `soft` | `fallback`) governing how derived facets are applied.
- **Fallback threshold**: Per-source minimum strict-pass result count below which widening triggers.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Benchmark queries that today return zero results due to over-narrow derived facets return at least one relevant result under fallback mode.
- **SC-002**: For queries with well-matched facets (strict pass meets threshold), results are identical to pre-feature behavior — 100% parity on a regression battery.
- **SC-003**: Facet-matching documents rank above equally-relevant non-matching documents in 100% of widened searches on the test battery.
- **SC-004**: Zero instances, across all modes and tests, of an explicit filter or confidentiality restriction being bypassed.
- **SC-005**: Well-matched queries incur no measurable latency increase; widened queries stay within ~2x a single search's latency (one extra pass).
- **SC-006**: Operators can change a source's facet mode through configuration alone, verified by a config-only change taking effect after redeploy.

## Assumptions

- The existing facet derivation (vocabulary-grounded planner) is unchanged — this feature only changes how its output is applied, not how facets are guessed.
- The ranking preference for facet-matching documents is applied at the result-merge stage in application code (not inside the search engine queries), keeping the vector and text search paths untouched — relevant because the vector index was just migrated (spec 006) and should not be perturbed.
- Default fallback threshold of 3 is a starting point; it is per-source configurable and expected to be tuned from the observability data (FR-009).
- The child-chunk restriction on vector search is structural (not a facet) and remains hard in all modes.
- "Soft" mode ships for completeness of the mode set but is not enabled for any source in this rollout; the three named sources get `fallback`.
