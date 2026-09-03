# Phase 1 Data Model: D.Quals Confidentiality Tagging

> **2026-07-10 revision**: this feature no longer redacts the Client Organisation Name or suppresses links — see spec.md's Revision History. This document describes the current (tag-based) model; the original redaction-target/link-suppression sections have been replaced accordingly.

This feature introduces no new storage schema and no new persisted entities — it reads facet values that already exist on `d_quals` records and prepends a visible tag to what the retrieval layer returns, without altering any other field. The entities below are the conceptual shapes involved, expressed independent of how any one source physically stores them (see "Physical representation" per entity for the two existing storage shapes it must reconcile).

## Project Qualification Record

A description of a past Dalberg engagement: practice area, region, project description, associated client, and (relevant to this feature) two independent confidentiality markers.

**Physical representation**:
- OpenSearch (`mcp-d-quals` index, semantic hits): one logical record maps to multiple chunk documents (parent + child chunks, including dedicated `record_summary`/`deck_summary` chunks); the confidentiality markers and client name are copied onto every chunk's metadata at index time.
- Airtable (`(D.Quals)` table, structured hits): one row per record; fields read verbatim into `SearchResult.metadata` under their raw Airtable names.

## Confidentiality Marker (Project)

**Represents**: Whether the project itself should be flagged as confidential to retrieval callers.

**Values**: `CONFIDENTIAL`, `NON-CONFIDENTIAL`, or blank/unset (Airtable `multipleSelects` field — API returns a list of zero or more of these strings, not a bare string).

**Key names by physical source**: `confidential_project` (semantic/OpenSearch) / `"Confidential Project"` (structured/Airtable).

**Rule**: A value of exactly `CONFIDENTIAL` (case-insensitive) on *any* entry in the list adds the project-confidentiality contribution to the hit's tag (see Confidentiality Tag below). Blank, missing, or `NON-CONFIDENTIAL` → no contribution; record shown exactly as it would be otherwise.

## Confidentiality Marker (Client)

**Represents**: Whether the record's client relationship should be flagged as confidential to retrieval callers.

**Values**: Same shape as the Project marker (`CONFIDENTIAL` / `NON-CONFIDENTIAL` / blank, list-valued).

**Key names by physical source**: `confidential_client` (semantic) / `"Confidential Client"` (structured).

**Rule**: A value of exactly `CONFIDENTIAL` adds the client-confidentiality contribution to the hit's tag. The client's name, all metadata, and all links remain fully visible — this marker only ever adds a tag, never hides anything. Blank/missing/`NON-CONFIDENTIAL` → no contribution.

## Client Organisation Name

**Represents**: The recorded name of the client on a project record.

**Values**: Airtable `multipleSelects` — list of zero or more canonical strings, e.g. `["Charities Aid Foundation America (CAF America)"]`.

**Key names by physical source**: `client_organisation` (semantic) / `"Client Organisation"` (structured).

**Handling**: Always shown in full, unmodified, regardless of the Confidentiality Marker (Client) value. (Prior revision derived masking-target variants from this field; that logic no longer exists.)

## Confidentiality Tag

**Represents**: The visible signal added to a record's text when one or both confidentiality markers are `CONFIDENTIAL`.

**Values**: One of `"**[CONFIDENTIAL PROJECT]**"`, `"**[CONFIDENTIAL CLIENT]**"`, or `"**[CONFIDENTIAL PROJECT & CLIENT]**"` (when both markers are set) — never more than one tag per hit.

**Where it's applied** (prepended, space-separated, to the existing value):
- `SearchResult.text` — always present, so the tag always appears somewhere in every response referencing the record.
- `SearchResult.metadata["record_summary"]` and `SearchResult.metadata["deck_summary"]`, when either is a non-empty string — these are LLM-generated summaries that open with a sentence naming the project (per `record_summary.py`'s prompt instructions), making them the closest thing to a reliable "title" for a semantic hit.

**Not applied to**: `SearchResult.metadata`'s other fields, `SearchResult.payload`, `SearchResult.citation_url`, or `SearchResult.citations` — none of these are modified by this feature.

## What is explicitly NOT touched by this feature (in contrast to the original design)

- `SearchResult.citation_url` / `SearchResult.citations` — always resolved and returned normally, exactly as for any non-confidential hit.
- The Client Organisation Name, and every other metadata field — always shown verbatim.
- Hit count / result set size — nothing is ever dropped, so a query never returns fewer hits because of this feature.
