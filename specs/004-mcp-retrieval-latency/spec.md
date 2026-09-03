# Feature Specification: Concurrent Multi-Source Retrieval Planning & Execution

**Feature Branch**: `004-mcp-retrieval-latency`

**Created**: 2026-07-08

**Status**: Draft

**Input**: User description: "Fix MCP retrieval latency caused by sequential per-source planning and execution. Two entry points in src/retrieval/mcp/tools.py — plan_retrieval_impl and _search_async — currently loop over sources one at a time, each iteration making a blocking planner call (and, in _search_async, blocking retrieval + enrichment calls too), serializing work across sources that is otherwise independent. Desired outcome: per-source work happens concurrently so multi-source query latency drops from ~O(N sequential calls) to ~O(1), with per-source failures still isolated exactly as today. No change to tool contracts, prompts, or output shape. No new dependencies. Aggregate token budgets, cross-query caching, and tool-docstring trimming are explicitly out of scope."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Multi-source question answered without added wait per source (Priority: P1)

A person (or an agent acting on their behalf) asks a question that the retrieval system needs to answer by consulting several knowledge sources at once (e.g. profiles, proposal library, knowledge library). Today, the more sources involved, the longer they wait, because each source is planned and queried one after another. They should instead experience a wait time that stays roughly flat regardless of how many sources are consulted, because sources are handled independently and at the same time.

**Why this priority**: This is the entire point of the feature — it's the only user-visible effect, and it's the most common query shape (most questions touch more than one source).

**Independent Test**: Ask the same multi-source question before and after the change and compare wall-clock time to receive the final result. Can be tested independently of any other change by holding the question, sources, and environment fixed.

**Acceptance Scenarios**:

1. **Given** a question that spans 4 enabled sources, **When** the question is submitted, **Then** the total time to produce the combined result is close to the time a single source would take on its own, not four times that.
2. **Given** a question that spans only 1 source, **When** the question is submitted, **Then** behavior and timing are unchanged from today (no regression for the common single-source case).

---

### User Story 2 - One slow or failing source doesn't penalize the others (Priority: P2)

One of the consulted sources is temporarily slow to plan against, or its planning step fails outright (e.g. a schema lookup problem). The person asking the question should still get timely, complete answers from the other sources, with the failing source falling back to its existing safe default behavior instead of blocking or breaking the whole answer.

**Why this priority**: Without this guarantee, making sources run concurrently could turn one bad source into a bottleneck or a hard failure for every query — a regression, not an improvement. This preserves today's fault-isolation behavior under the new concurrent execution.

**Independent Test**: Force one source's planning step to fail (or delay) while others succeed normally, and confirm the other sources' results are returned complete and on time, with the failing source present only as its existing default fallback plan.

**Acceptance Scenarios**:

1. **Given** one of several sources has a planning failure, **When** the question is submitted, **Then** the other sources' results are unaffected and returned as normal, and the failing source appears with the same default "semantic only" fallback behavior used today.
2. **Given** one of several sources is slower than the rest to plan against, **When** the question is submitted, **Then** the overall response time is governed by the slowest source, not by the sum of all sources.

---

### Edge Cases

- What happens when only one source is requested? Behavior and latency must match today exactly (no unnecessary concurrency overhead or behavior change for the single-source path).
- What happens when every requested source fails planning at once? All sources fall back to their existing default plan independently; the overall call still succeeds and returns results the same way it does today when a single source fails.
- What happens when a source's failure is slow to surface (e.g. a hang) rather than an immediate error? The other sources must still complete and be returned without waiting on the hung one beyond whatever timeout/behavior already governs that call today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST plan retrieval for all requested sources concurrently rather than one at a time, for both the planning-only entry point and the plan-and-execute entry point.
- **FR-002**: The system MUST execute each source's retrieval (and any enrichment lookups) concurrently across sources, not just the planning step, for the plan-and-execute entry point.
- **FR-003**: The system MUST preserve today's per-source failure isolation: a planning or execution failure for one source MUST result in that source using its existing default fallback plan, and MUST NOT prevent, delay, or alter the results of any other source.
- **FR-004**: The system MUST produce identical output (same response shape, same fields, same merge/dedup/citation behavior) for a given question and source set as it does today — this is purely a change in how results are computed, not what is returned.
- **FR-005**: The system MUST NOT change the observable behavior or timing of single-source queries.
- **FR-006**: The system MUST NOT introduce new external dependencies to achieve concurrency.

### Key Entities

- **Source plan**: the per-source retrieval strategy (mode, semantic query, structured lookup formula, top-k, etc.) produced by the planning step for one logical source; independent of every other source's plan.
- **Source result**: the hits, hints, and diagnostics produced by executing one source's plan (including its enrichment lookup); independent of every other source's result until the final merge step.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a question spanning 4 enabled sources, total time to final result is no more than roughly 1.5x the time a single source takes alone (down from roughly 4x today), measured on the same environment and question set before/after.
- **SC-002**: Single-source query timing shows no measurable regression (within normal run-to-run variance) compared to today.
- **SC-003**: When one of several sources fails planning, 100% of the other sources' results are still returned correctly and on time, matching today's fallback behavior for the failing source.
- **SC-004**: Output content (hits, hints, citations, diagnostics) for a fixed question and source set is unchanged before/after, verified by direct comparison.

## Assumptions

- "Concurrently" means the existing per-source independence already present in the code (each source's plan and result do not depend on another source's plan or result) is exploited via concurrent execution; no new coordination or shared state between sources is introduced.
- The existing default-fallback-plan behavior on planning failure (semantic-only mode) is the correct and sufficient failure-isolation mechanism to preserve — no new retry, circuit-breaker, or timeout policy is being requested here.
- Scope is limited to the two identified entry points (planning-only, and plan-and-execute) in the MCP retrieval tool layer; no other latency sources (embedding, reranking, external source I/O) are addressed by this feature.
- No new environment variables, feature flags, or configuration are needed — this is an internal execution-strategy change, not a user-facing toggle.
