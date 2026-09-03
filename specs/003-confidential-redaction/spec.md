# Feature Specification: D.Quals Confidentiality Tagging

**Feature Branch**: `003-confidential-redaction`

**Created**: 2026-07-08

**Revised**: 2026-07-10 (see Revision History below — the enforcement approach changed from hide/redact to show-in-full-with-a-tag)

**Status**: Implemented (retroactive spec — see Note below)

**Input**: Original user description: "D.Quals confidentiality enforcement for the MCP retrieval server. Two policies scoped to the d_quals (Project Qualifications) source: (1) When a project record's Confidential Project flag is set to CONFIDENTIAL, the entire record must never appear in any search/lookup result returned to an MCP client, under any query mode. (2) When a project record's Confidential Client flag is set to CONFIDENTIAL, the record may still appear, but the client organisation's name must be redacted (replaced with a fixed masking token) everywhere it appears in the returned text and metadata, and no link to the underlying source file or record (e.g. presigned document link, source row link) may be returned for that record — only the generated text/summary content. Records where either flag is blank or explicitly non-confidential are shown unchanged. Enforcement happens at query/response time (no re-indexing of existing data required)."

Revision input (2026-07-10): "instead of hiding name in confidential client and hiding project itself in case of confidential project, I would like to show all the details in both the cases, but for all the results with either confidential client and confidential project or both I would like to see a separate tag for both of them. This tag should show up along the title of the project." Confirmed as a full replacement of the hide/redact behavior (not additive), with source links restored, and a bracketed text tag prepended to the title/text.

> **Note on process**: this spec was written after implementation, not before (see the original version's process note). It has since been revised in place, again after implementation, to track a deliberate reversal of the original design. Both the original spec and this revision were written retroactively.

## Revision History

- **2026-07-08 (original)**: Confidential Project records were dropped entirely from all output; Confidential Client records were kept but had the client name redacted and all source links suppressed.
- **2026-07-10 (this revision)**: Reversed to a transparency-first approach — every record's full content (including the client's name and a working source link) is always shown. Confidentiality is now communicated via a visible bracketed tag (`**[CONFIDENTIAL PROJECT]**`, `**[CONFIDENTIAL CLIENT]**`, or `**[CONFIDENTIAL PROJECT & CLIENT]**`) prepended to the record's text, rather than by hiding data. The underlying detection logic (which facet values count as confidential, and the fix for a production incident where a caller-narrowed field list could bypass detection) is unchanged — only what happens *after* detection changed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Confidential projects are visibly tagged, not hidden (Priority: P1)

A user (via any MCP client) asks a question that matches a D.Quals project record marked `Confidential Project = CONFIDENTIAL`. The record appears in the response in full, exactly as any other record would, but with a `**[CONFIDENTIAL PROJECT]**` tag prepended to its text so the reader immediately knows the project itself is confidential.

**Why this priority**: This is the primary signal the business wants — readers should never be confused about a project's confidentiality status, and should never be missing information they're otherwise entitled to see.

**Independent Test**: Query with terms matching a known confidential-project record via `semantic_search`, `airtable_lookup`, and `search`; confirm the record appears in every response with the tag visible at the start of its text, and with the same content, metadata, and links it would have without this feature.

**Acceptance Scenarios**:

1. **Given** a D.Quals record with `Confidential Project = CONFIDENTIAL`, **When** a query matches it via semantic search, structured lookup, or the combined search tool, **Then** the record appears in the response with `**[CONFIDENTIAL PROJECT]**` prepended to its text, and every other field (metadata, citation link) unchanged from what it would otherwise be.
2. **Given** a D.Quals record with `Confidential Project` blank or `NON-CONFIDENTIAL`, **When** a query matches it, **Then** the record appears with no tag, unaffected by this feature.

---

### User Story 2 - Confidential clients are visibly tagged, name and links stay visible (Priority: P1)

A user's query matches a D.Quals record marked `Confidential Client = CONFIDENTIAL`. The record's full content — including the client organisation's name and a working link to the source document/row — is shown exactly as it would be otherwise, with a `**[CONFIDENTIAL CLIENT]**` tag prepended to its text.

**Why this priority**: Equally important as User Story 1 — a reader must be able to tell a client relationship is confidential (e.g. so they know not to repeat it externally) without losing access to the underlying information they're authorized to see.

**Independent Test**: Query for a record known to have `Confidential Client = CONFIDENTIAL` and a populated client organisation name. Confirm the record appears with the tag, the client's name fully visible in text/metadata, and a working source link present.

**Acceptance Scenarios**:

1. **Given** a record with `Confidential Client = CONFIDENTIAL` and `Client Organisation = "Acme Corp"`, **When** the record is returned, **Then** `**[CONFIDENTIAL CLIENT]**` is prepended to its text and "Acme Corp" remains fully visible everywhere it already appeared (text, metadata, generated summaries).
2. **Given** the same record, **When** the response is assembled, **Then** its source document/row link is present and working, exactly as it would be without this feature.
3. **Given** a record where both `Confidential Project` and `Confidential Client` are `CONFIDENTIAL`, **When** returned, **Then** the combined tag `**[CONFIDENTIAL PROJECT & CLIENT]**` is prepended instead of two separate tags.
4. **Given** a record where `Confidential Client` is blank or `NON-CONFIDENTIAL`, **When** it is returned, **Then** no tag is added and nothing else changes.

---

### User Story 3 - Non-confidential sources and records are never affected (Priority: P2)

Any record from a source other than the Project Qualifications source, or any D.Quals record with both flags blank/non-confidential, passes through completely unchanged.

**Why this priority**: A confidentiality feature that also alters unrelated data would undermine trust in the whole retrieval system. Needed for safe rollout, hence P2 rather than P1.

**Independent Test**: Run existing search/lookup queries against non-D.Quals sources and against non-confidential D.Quals records; confirm output is byte-for-byte identical to pre-feature behavior.

**Acceptance Scenarios**:

1. **Given** a record from a different source that happens to have fields with the same names as the confidentiality flags, **When** it is returned, **Then** it is not affected by this feature (the policy is scoped to the Project Qualifications source specifically).
2. **Given** a D.Quals record with neither flag set to `CONFIDENTIAL`, **When** returned, **Then** its text, metadata, and links are identical to before this feature existed.

---

### Edge Cases

- What happens when the client organisation name is missing/blank on a record marked `Confidential Client = CONFIDENTIAL`? The tag is still added (the project/client relationship confidentiality status is independent of whether a name happens to be on file); there's simply no name to point at.
- What happens to the record's citation/source link? It is never affected by this feature in either direction — links are shown exactly as they would be without any confidentiality marker.
- What happens if the caller requests only a narrow subset of Airtable columns (e.g. via `airtable_lookup`'s `fields` parameter) that omits the confidentiality columns? The system still fetches the confidentiality columns internally so tagging remains correct, regardless of what the caller explicitly asked for (see the production-incident fix, carried over unchanged from the original design — Assumptions).
- What happens to data accessed directly through the underlying data systems (not through the retrieval/MCP layer)? It is unaffected by this feature, as it always was.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST show every Project Qualifications record in full, regardless of its `Confidential Project`/`Confidential Client` marker values — no record is ever dropped, and no field value is ever redacted or replaced, because of this feature.
- **FR-002**: When a record's `Confidential Project` marker is exactly `CONFIDENTIAL`, the system MUST prepend the tag `**[CONFIDENTIAL PROJECT]**` to that record's returned text.
- **FR-003**: When a record's `Confidential Client` marker is exactly `CONFIDENTIAL`, the system MUST prepend the tag `**[CONFIDENTIAL CLIENT]**` to that record's returned text.
- **FR-004**: When both markers are exactly `CONFIDENTIAL` on the same record, the system MUST prepend the single combined tag `**[CONFIDENTIAL PROJECT & CLIENT]**` instead of two separate tags.
- **FR-005**: The system MUST treat a blank/unset value, or a value of `NON-CONFIDENTIAL`, on either marker as "not confidential" for that marker — no tag contribution from that marker in that case.
- **FR-006**: The system MUST also prepend the applicable tag to a record's generated summary text (`record_summary`/`deck_summary`) when present, since that field is where a project's name/description is most likely to be read from.
- **FR-007**: This feature's rules MUST apply only to the Project Qualifications source; records from any other source are unaffected even if they happen to expose similarly-named fields.
- **FR-008**: Enforcement MUST occur at query/response time against already-stored data — it MUST NOT require re-processing or re-loading existing records into storage.
- **FR-009**: The system MUST always be able to correctly evaluate both confidentiality markers regardless of any caller-supplied field-selection parameter that might otherwise narrow what's fetched from the underlying data store — detection MUST NOT silently degrade to "not confidential" just because a caller didn't ask for those specific columns.

### Key Entities

- **Project Qualification Record**: A description of a past engagement/project, including a practice area, region, description, and an associated client organisation. Carries two independent yes/no-style confidentiality markers: one for the project itself, one for the client's identity.
- **Confidentiality Marker (Project)**: Indicates whether a project record's confidentiality should be flagged to retrieval callers.
- **Confidentiality Marker (Client)**: Indicates whether a record's client relationship confidentiality should be flagged to retrieval callers.
- **Confidentiality Tag**: The visible text marker (`**[CONFIDENTIAL PROJECT]**`, `**[CONFIDENTIAL CLIENT]**`, or `**[CONFIDENTIAL PROJECT & CLIENT]**`) prepended to a record's text/summary when one or both markers are set.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of records marked `Confidential Project = CONFIDENTIAL` and/or `Confidential Client = CONFIDENTIAL` carry the correct tag, prepended to their text, in every response that includes them (verified by automated tests, not sampling).
- **SC-002**: 100% of records — confidential or not — retain their full original content, including client organisation names and source links, with the tag as the only addition.
- **SC-003**: 100% of records unaffected by either confidentiality marker (including all records from other sources) produce identical output to before this feature was introduced, with no measurable added latency for the common case.
- **SC-004**: Rolling out this feature requires zero downtime and zero re-processing of previously stored records.
- **SC-005**: Detection of both confidentiality markers remains correct even when a caller requests a narrowed subset of fields (regression coverage for the prior production incident).

## Assumptions

- The two confidentiality markers and the client organisation name are already being captured as structured data on each Project Qualifications record; this feature only changes how that existing data affects retrieval output, not how it is captured.
- "Blank/unset" confidentiality markers are treated as "not confidential" (no tag) — an explicit `CONFIDENTIAL` value is required to trigger a tag.
- Enforcement is scoped to what the retrieval/MCP layer returns to callers; it does not change or restrict access to the underlying stored data through other means.
- No configurable on/off switch is provided for this feature, consistent with the original design's reasoning — this remains a deliberate, code-level behavior rather than a runtime toggle.
- There is no dedicated "title" field on either a semantic (OpenSearch) or structured (Airtable) D.Quals hit — the tag is prepended to the start of the record's text (and its generated summary, when present) as the practical equivalent of "next to the title," since no separate title field reliably exists for both hit types.
