# Feature Specification: D.Quals Vector Index Quantization (faiss on-disk)

> **Decision update (2026-07-22):** compression level changed from 32x to **8x**
> by user decision — trades memory headroom for a larger recall margin. At 8x the
> resident graph is ~0.9 GB (fits t3.medium's ~1.5 GiB cache, tight). Fallback:
> rebuild at **16x** (~550 MB) if post-migration hybrid-query latency on
> t3.medium is unsatisfactory. Numbers below that assume 32x/~360 MB are
> superseded accordingly.

**Feature Branch**: `006-dquals-faiss-quantization`

**Created**: 2026-07-22

**Status**: Draft

**Input**: User description: "Migrate the mcp-d-quals OpenSearch index to faiss binary quantization (mode on_disk, compression_level 32x, space_type cosinesimil) so the ~6 GB resident HNSW vector graph (1,292,556 child-chunk vectors, 1024-dim) shrinks to ~360 MB and fits the page cache of the permanent t3.medium.search node, eliminating the 60-second hybrid-search timeouts. Quantization applies only to d-quals indexes; the unused text.keyword sub-field is dropped from d-quals only; a committed migration script performs create/benchmark/reindex/status/finalize/verify against the temporarily scaled-up r7g.4xlarge.search domain; the old index is retained until verification passes; after cutover and deletion of the old index, the domain scales back to t3.medium.search the same day."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - D.Quals search returns reliably (Priority: P1)

An MCP user (consultant querying the Lucie connector) searches D.Quals qualifications. Today these searches frequently hang for 60 seconds and fail because the vector search working set is ~4x larger than the search node's available memory. After migration, the same searches return promptly and consistently on the permanent (small) node.

**Why this priority**: This is the outage being fixed — D.Quals search is effectively broken under load, which blocks the primary use of the MCP server.

**Independent Test**: Run the set of real queries that previously produced 60-second timeouts against the migrated index on the permanent node; all return successfully in under ~1 second at p95.

**Acceptance Scenarios**:

1. **Given** the migrated (quantized) D.Quals index served from the permanent `t3.medium.search` node, **When** a user runs hybrid searches that previously timed out, **Then** results return in under ~1 second p95 with zero 60-second timeouts.
2. **Given** the migrated index, **When** a vector (kNN) search executes, **Then** only child chunks are returned (the child-only pre-filter still holds).
3. **Given** the migrated index, **When** known benchmark queries are run against both old and new indexes side by side (before the old index is deleted), **Then** top-k results are judged equivalent in relevance (same or overlapping top hits; no obviously missing well-known documents).

---

### User Story 2 - Safe, reversible migration (Priority: P2)

The operator migrates the index without risking data. The source index is never modified; every step is observable, cancellable, and gated; and rollback at any point before deletion is a one-line configuration revert.

**Why this priority**: The index represents the full extraction/embedding pipeline output (~2M chunks); rebuilding it from scratch is expensive. The migration must be safe to abort at any step.

**Independent Test**: Each migration step can be run and inspected independently (pre-flight check, create, benchmark, reindex, status, finalize, verify); aborting mid-migration leaves the live search path untouched.

**Acceptance Scenarios**:

1. **Given** the migration is mid-flight, **When** any step fails or is cancelled, **Then** live search continues to serve from the old index unaffected.
2. **Given** the reindex has completed, **When** verification runs, **Then** it reports document-count parity (2,052,432 total; per-`chunk_type` counts equal) and sample-query sanity **before** any cutover happens.
3. **Given** the cutover has happened but a problem is discovered before old-index deletion, **When** the configuration is pointed back at the old index and redeployed, **Then** search behaves exactly as before the migration.
4. **Given** documents were ingested into the old index while the reindex was running, **When** a delta pass runs before cutover, **Then** those documents are present in the new index (no silent data loss from the migration window).

---

### User Story 3 - Cost returns to baseline (Priority: P3)

The large instance is borrowed, not kept. The domain runs on temporarily upgraded hardware only for the duration of the build, then returns to the permanent small instance the same day.

**Why this priority**: The entire point of quantization is avoiding a permanent hardware upgrade (~$980/mo large vs ~$57/mo baseline). Forgetting the scale-down silently burns ~17x the monthly budget.

**Independent Test**: After migration completes, the domain configuration reports the permanent instance type, and search still meets the P1 latency criteria on it.

**Acceptance Scenarios**:

1. **Given** verification passed and the old index is deleted, **When** the domain is scaled back down, **Then** it reports `t3.medium.search` and search still meets SC-001/SC-002 on that node.
2. **Given** the migration finished, **When** the operator reviews the checklist, **Then** the scale-down is recorded as a completed, same-day step (it is part of the migration's definition of done, not a follow-up).

---

### Edge Cases

- **Reindex task reports "completed" while background merges are still running**: completion is defined as task done AND merges drained (`merges.current == 0`); status checks must show both. (This exact misreading previously produced a 100x-wrong time estimate.)
- **Documents ingested during the migration window**: the SQS ingestion pipeline may write to the old index while the copy runs. A delta pass (create-only copy, which skips already-copied documents) must run before cutover, or ingestion must be paused for the window.
- **Recall degradation from quantization**: binary quantization can drop occasional relevant hits. Side-by-side spot checks on known queries happen while both indexes exist; if quality is unacceptable, rollback is exercised instead of cutover.
- **Migration script run against the wrong environment or too early**: the pre-flight check must verify it is talking to the scaled-up node (memory ≈ 128 GB), the cluster is healthy, and no merges are in flight, and refuse to proceed otherwise.
- **New index created accidentally by the ingestion pipeline before this migration ships**: index creation is idempotent (`ensure_index` skips existing indexes), so the pipeline must not fight the migration for index creation; after this change, any *future* recreation of a d-quals index automatically receives the quantized mapping.
- **Disk pressure during the copy**: both indexes coexist (~33 GB total) on the 50 GiB volume; the pre-flight check must confirm free-disk headroom stays below the 85% flood-stage watermark for the duration.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The D.Quals vector index MUST be stored so its memory-resident search working set fits in the permanent node's page cache alongside the text-search working set (at the chosen 8x level: ~6 GB → ~0.9 GB; 16x fallback: ~550 MB).
- **FR-002**: Compression MUST apply only to d-quals indexes. The profiles, knowledge-library, and proposal-library indexes MUST retain their existing full-precision mapping (they fit in memory as-is and would take a recall hit for no benefit).
- **FR-003**: The unused `text.keyword` sub-field MUST be dropped from d-quals indexes only (verified unqueried across the codebase; ~2 KB/doc of dead weight).
- **FR-004**: Migration MUST be performed by a committed, reviewable script with discrete, independently runnable steps: pre-flight check, target creation, timed benchmark, full copy, progress status, finalize, and verify.
- **FR-005**: The migration MUST NOT modify the source index at any point. The source index MUST be retained until verification passes and cutover is confirmed.
- **FR-006**: Verification MUST gate cutover and include: total document-count parity (2,052,432), per-`chunk_type` count parity, child-only kNN filter behavior, and side-by-side result sanity on known queries against both indexes.
- **FR-007**: Cutover MUST be a configuration-only change (`config/retrieval_sources.yaml`), and rollback before old-index deletion MUST be a revert of that same change.
- **FR-008**: Documents ingested into the old index during the migration window MUST NOT be lost: a delta pass MUST run before cutover (or ingestion paused for the window).
- **FR-009**: The domain MUST return to `t3.medium.search` the same day the migration completes; the scale-down is part of the migration's definition of done.
- **FR-010**: Any future (re)creation of a d-quals index through the ingestion pipeline MUST automatically receive the quantized mapping, so this migration cannot be silently undone by an index recreation.
- **FR-011**: Progress reporting MUST distinguish "copy task finished" from "index fully built" (background merges drained), so completion is never declared early.

### Key Entities

- **mcp-d-quals (source index)**: 2,052,432 chunks (1,292,556 child chunks carrying 1024-dim embeddings; parents carry none). Read-only throughout the migration; deleted only after confirmed cutover.
- **mcp-d-quals-v2 (target index)**: Same documents, quantized vector storage (binary 32x, full-precision vectors retained on disk for rescoring), 4 primary shards, no `text.keyword` sub-field.
- **Chunk**: A parent (context) or child (embedded) fragment of a source document; `chunk_type` distinguishes them and the child-only filter guards vector search.
- **Retrieval source configuration**: The registry entry that points D.Quals search at a physical index; the cutover/rollback lever (per Constitution Principle I, retrieval is configured, not hardcoded).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: D.Quals searches that previously timed out return in under ~1 second at p95 when served from the permanent small instance.
- **SC-002**: Zero 60-second timeouts across the full post-migration query battery on the permanent instance.
- **SC-003**: 100% document-count parity between old and new indexes (total and per chunk type), including documents ingested during the migration window.
- **SC-004**: Known-query spot checks show equivalent retrieval quality (overlapping top results, no missing well-known documents), assessed while both indexes exist.
- **SC-005**: Monthly search-infrastructure cost returns to the pre-migration baseline (~$57/mo domain) the same day, with the temporary large-instance window costing under ~$25 total.
- **SC-006**: Memory-resident vector working set on the permanent node shrinks by at least 15x (fits within available page cache with headroom for text search).

## Assumptions

- The domain is a single-node dev domain (`mcp-dev-os-search-eu`, eu-west-1), already temporarily scaled to `r7g.4xlarge.search` (verified today; final blue/green cleanup stage may still be clearing, which blocks config changes but not data operations).
- The quantized mapping (`mode: on_disk`, `compression_level: 32x`, `space_type: cosinesimil`) is supported by the cluster — probe-confirmed directly on this domain on 2026-07-21 (OpenSearch 3.5 ≥ required 2.17).
- Result merging is rank-based (RRF), so the score-scale change from the engine swap does not affect how vector and text results combine.
- EBS stays at gp3 3000 IOPS / 125 MB/s — the optional bump is skipped; the large node's RAM caches the entire source index, making disk speed non-critical.
- Ingestion volume during the migration window is low (dev environment), making the delta-pass approach sufficient versus pausing ingestion.
- Replica count on the target is 0 (single-node domain cannot allocate replicas); build-time settings (refresh disabled) are restored at finalize.
- The r7g.4xlarge instance was chosen over the originally discussed r6g.4xlarge because the R6g family is previous-generation and was not offered; capability is equivalent for this job.
