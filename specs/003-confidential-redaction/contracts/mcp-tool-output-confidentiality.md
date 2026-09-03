# Contract: MCP Tool Output Behavior for D.Quals Confidentiality

> **2026-07-10 revision**: this contract was revised — the original version (dropping/redacting) is superseded below. See spec.md's Revision History.

This feature does not add a new endpoint or tool. It changes the *contract* of what the three existing MCP retrieval tools may return, whenever a result comes from the `d_quals` (Project Qualifications) source. This document describes that behavior change as a contract addendum to each tool's existing response shape.

Affected tools (all defined in `src/retrieval/mcp/server.py`, backed by `src/retrieval/mcp/tools.py`): `semantic_search`, `airtable_lookup`, `search`.

## Rule 1 — Confidential Project tag

**Applies to**: every hit from the `d_quals` source, in every tool and every query mode (`semantic_only`, `airtable_only`, `hybrid`).

**Precondition**: the underlying record's `Confidential Project` facet is exactly `CONFIDENTIAL` (case-insensitive; list-valued facets checked for membership).

**Guarantee**: the hit appears in the response exactly as it would without this feature, except that `"**[CONFIDENTIAL PROJECT]**"` is prepended (space-separated) to `text`, and to `metadata["record_summary"]`/`metadata["deck_summary"]` when either is present. Nothing is dropped, nothing else is modified.

## Rule 2 — Confidential Client tag

**Applies to**: every hit from the `d_quals` source, in every tool and every query mode.

**Precondition**: the underlying record's `Confidential Client` facet is exactly `CONFIDENTIAL`.

**Guarantee**: `"**[CONFIDENTIAL CLIENT]**"` is prepended (space-separated) to `text` and to `record_summary`/`deck_summary` when present. The client organisation's name remains fully visible everywhere it already appeared — no substitution, masking, or redaction of any kind. `citation_url` and `citations` are resolved and returned exactly as they would be for a non-confidential hit.

## Rule 3 — Both markers set

**Applies to**: a hit where both `Confidential Project` and `Confidential Client` are exactly `CONFIDENTIAL`.

**Guarantee**: a single combined tag `"**[CONFIDENTIAL PROJECT & CLIENT]**"` is prepended — never two separate tags on the same hit.

## Rule 4 — No effect outside scope

**Applies to**: any hit from any source other than `d_quals`, and any `d_quals` hit whose `Confidential Project`/`Confidential Client` facets are blank, unset, or `NON-CONFIDENTIAL`.

**Guarantee**: response shape, field values, and citation behavior are byte-for-byte identical to the tool's behavior before this feature existed — no tag, no other change.

## Example — before/after for a Confidential Client hit

Before this feature (or for a non-confidential hit):

```json
{
  "source": "d_quals",
  "text": "Engagement with The Coca-Cola Company on a social impact toolkit...",
  "metadata": {"client_organisation": ["The Coca-Cola Company"]},
  "citation_url": "https://dalberg-bucket.s3.amazonaws.com/raw/.../deck.pptx?X-Amz-Signature=...",
  "citations": [{"cite_id": "c1", "kind": "document_section", "label": "...", "url": "https://..."}]
}
```

After this feature (same underlying record, `Confidential Client = CONFIDENTIAL`, `Confidential Project = NON-CONFIDENTIAL`) — verified live against real Airtable data (Project Number 3110071):

```json
{
  "source": "d_quals",
  "text": "**[CONFIDENTIAL CLIENT]** Engagement with The Coca-Cola Company on a social impact toolkit...",
  "metadata": {"client_organisation": ["The Coca-Cola Company"]},
  "citation_url": "https://dalberg-bucket.s3.amazonaws.com/raw/.../deck.pptx?X-Amz-Signature=...",
  "citations": [{"cite_id": "c1", "kind": "document_section", "label": "...", "url": "https://..."}]
}
```

Only `text` (and `record_summary`/`deck_summary`, when present) changed — everything else is untouched.
