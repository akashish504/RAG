# airtable_lookup integrity after search removal

This document traces the complete call chain of `airtable_lookup`, explains where
schema loading and Claude calls actually live, and confirms that removing `search`
and adding `plan_retrieval` left `airtable_lookup` entirely intact.

---

## The concern

The question was: does removing `search` break `airtable_lookup`? Specifically —
*"it first calls get schema and then generates the query using Claude"* — did that
behaviour live inside `airtable_lookup`, or somewhere else?

**Short answer: it lived inside `search_impl`, not inside `airtable_lookup_impl`.**
`airtable_lookup` never called Claude and never fetched the schema per request.
It is unchanged.

---

## Complete call chain for airtable_lookup

```
Host calls: airtable_lookup(source, formula?, fields?, max_records?)
│
├─ airtable_lookup_impl()                    [tools.py]
│   ├─ _registry().get(source)               in-memory dict lookup, no I/O
│   ├─ check logical.airtable is not None    presence guard
│   ├─ build RetrievalQuery(mode="airtable_only", formula, fields, max_records)
│   └─ _run_async(_router().run(q))
│       │
│       └─ RetrievalRouter.run(q)            [router.py]
│           ├─ mode="airtable_only"
│           │   → wants_semantic = False      NO embedding computation
│           │   → no Voyage API call
│           ├─ asyncio.gather → LogicalSource.query(q)
│           │   ├─ run_at = True   (mode is airtable_only, adapter present)
│           │   ├─ run_os = False  (mode is airtable_only)
│           │   └─ AirtableSource.filter_structured(formula, fields, max_records)
│           │       ├─ build kwargs: {formula?, fields?, max_records?}
│           │       ├─ asyncio.to_thread → table.all(**kwargs)  ← pyairtable HTTP
│           │       └─ _format_rows() → SearchResult[], Hint[]
│           ├─ assign_cite_ids_to_hits()      citation building, no I/O
│           └─ build_references_from_hits()
│
└─ _envelope(response) → JSON string
```

**Claude API calls in this path: 0**
**Schema fetches in this path: 0**
**Embedding calls in this path: 0**

The only network call is `table.all(**kwargs)` — a single pyairtable HTTP request to
the Airtable REST API.

---

## Where schema loading actually happens

Schema loading is a **one-time cold-start operation**, not per-request.

```
AirtableSource.__init__()                    called once when the adapter is wired
└─ _load_snapshot()
    ├─ if config/dalberg_profiles_schema.json exists:
    │   └─ json.loads(file)                  disk read, no network
    └─ if file is missing (fresh Docker image):
        └─ build_schema_snapshot_from_meta_api()
            └─ AirtableConnector.fetch_tables_metadata(base_id)
                └─ Airtable Meta API GET /v0/meta/bases/{id}/tables  ← one-time HTTP
```

After init, `AirtableSource._field_index` is an in-memory dict. The `get_schema()`
method just reads that dict — no I/O, no Claude.

`SourceRegistry._wire_adapters()` is called once when `get_registry()` first runs
(module-global singleton). Subsequent requests reuse the cached registry.

---

## Where the "schema → Claude → formula" flow lived

This flow was **entirely inside `search_impl`**, which is now removed:

```
OLD search_impl (removed)
│
├─ registry.get(source).get_schema()         ← schema read (from in-memory _field_index)
├─ plan_query(question, schema)              ← Claude API call #1
│   └─ Anthropic messages.create(PLANNER_SYSTEM + schema JSON)
│       → {"mode": "...", "airtable_formula": "...", "semantic_query": "...", ...}
├─ RetrievalRouter.run(q with formula)
├─ _enrich_hits_with_airtable()              ← second airtable_lookup call (removed)
├─ classify_response_shape()                 ← Claude API call #2 (removed)
└─ synthesize_answer()                       ← Claude API call #3 (removed)
```

This same schema + Claude planning step now lives in `plan_retrieval_impl` (one
Claude call), and the host then calls `airtable_lookup` with the formula from the
plan. The planning and execution are separated.

```
NEW plan_retrieval_impl
│
├─ registry.get(source).get_schema()         ← schema read (same in-memory dict)
└─ plan_query(question, schema)              ← Claude API call #1 (only one)
    → {"mode": "...", "airtable_formula": "...", "suggested_calls": [...]}

Host executes suggested_calls:
├─ semantic_search(query, source, top_k)     ← KNN + BM25, no Claude
└─ airtable_lookup(source, formula, ...)     ← pyairtable, no Claude
```

---

## What _enrich_hits_with_airtable was and why it is gone

`_enrich_hits_with_airtable` was a private function called inside `search_impl`.
It was NOT a public tool and had no external callers. It:

1. Extracted unique email primary_keys from semantic hits
2. Built `OR(LOWER({Email})='a@x.com', ...)` formula + appended any planner formula
3. Called `airtable_lookup_impl()` internally as a second pass
4. Merged structured fields (Display Name, Job Title, etc.) onto semantic hits

**Why it caused the 535-row bug:** when the planner produced a broad formula (or one
that matched many rows), the OR formula could pull the entire table. More critically,
`filter_structured` is called with `max_records=max(len(emails)+20, 50)` but the
*planner formula* branch could still return all rows if the formula matched everything.

**Its replacement:** the enrichment is now an explicit step in `plan_retrieval`
`suggested_calls`. The host builds the OR formula from semantic hit primary_keys and
calls `airtable_lookup` with `max_records=50` (hard-capped). The 535-row scenario is
structurally prevented because:
- The formula must be built from actual semantic hit emails (bounded by top_k ≤ 50)
- `max_records` is always set to `_MAX_RECORDS_CAP = 50` in the enrichment call

---

## airtable_lookup edge cases that still exist

These are pre-existing behaviours, not introduced by the search removal:

### 1. No formula → full table fetch
`airtable_lookup(source="dalberg_profiles")` without a `formula` calls `table.all()`
with no filter, returning all ~535 rows. This is intentional (the docstring says
"Omit to fetch all rows — use max_records to cap"). The tool docstring and routing
prompt both warn: *"never call airtable_lookup without a formula unless intentionally
listing all rows"*.

**Mitigation:** the routing prompt now explicitly states this anti-pattern. The
`plan_retrieval` enrichment step always generates an explicit formula and caps at 50.

### 2. max_records=None with a broad formula
If the host omits `max_records` and supplies a formula that matches many rows, all
matching rows are returned. This is correct behaviour for explicit listing queries
("give me everyone where Status=Active") but expensive for accidental broad formulas.

**Mitigation:** `plan_retrieval` always sets `max_records` in airtable_lookup
`suggested_calls`. Direct host calls should always pass `max_records`.

### 3. Bad formula field names cause Airtable 422
If `{FieldName}` in a formula does not match the exact Airtable field name,
`table.all()` raises an error. This is caught by the `try/except` in
`AirtableSource.filter_structured` → logged + re-raised → `RetrievalRouter._safe_query`
catches it → returns empty hits with `error` in diagnostics.

**Mitigation:** `plan_query` (inside `plan_retrieval`) validates formula field names
against the schema via `_validate_formula_fields()`. Direct `airtable_lookup` calls
without `plan_retrieval` must rely on `get_schema()` first.

### 4. Long-text fields truncated at 800 chars
`AirtableSource._format_rows` truncates any field in `_long_text_fields` at
`cfg.long_text_truncate` (default 800 chars) and emits a `Hint(tool="semantic_search",
reason="long_text_field_truncated")`. This is unchanged and correct — the hint guides
the host to use `semantic_search` for full CV/bio text.

---

## Schema snapshot vs live Meta API

| Condition | Schema source | Frequency |
|-----------|--------------|-----------|
| Snapshot file present (`config/*.json`) | Disk read at init | Once per process |
| Snapshot file missing | Airtable Meta API at init | Once per process, auto-saved |
| `get_schema_impl()` called | In-memory `_field_index` | Per-call, zero I/O |
| `plan_retrieval_impl()` called | In-memory `_field_index` via `get_schema()` | Per-call, zero I/O |
| `airtable_lookup_impl()` called | Does NOT call `get_schema()` | Never |

---

## Summary: what changed, what did not

| Behaviour | Before (search) | After (plan_retrieval) |
|-----------|----------------|----------------------|
| Schema load on cold start | Once at `AirtableSource.__init__` | Same — unchanged |
| Claude call for formula | Inside `search_impl` | Inside `plan_retrieval_impl` |
| Formula validation | Inside `plan_query` | Inside `plan_query` (same code) |
| `airtable_lookup` per-request logic | `filter_structured` → `table.all()` | Same — unchanged |
| `airtable_lookup` Claude calls | 0 | 0 |
| Second Airtable pass | `_enrich_hits_with_airtable` (auto) | Explicit enrichment step in `suggested_calls` (host executes) |
| Max records cap in enrichment | `max(len(emails)+20, 50)` — could exceed 50 if planner formula was broad | Hard cap `_MAX_RECORDS_CAP = 50` always |
| 535-row scenario | Possible when planner formula was broad + enrichment ran | Structurally prevented (host builds OR from actual hit emails, capped at 50) |

**`airtable_lookup` is intact. No regression was introduced.**

---

## Files involved

| File | Role | Changed? |
|------|------|----------|
| `src/retrieval/mcp/tools.py` | `airtable_lookup_impl` lives here | Yes — `search_impl` removed, `plan_retrieval_impl` added; `airtable_lookup_impl` unchanged |
| `src/retrieval/sources/airtable.py` | `AirtableSource.filter_structured` | No |
| `src/retrieval/config.py` | `LogicalSource.query`, `SourceRegistry` | No |
| `src/retrieval/router.py` | `RetrievalRouter.run` | No |
| `src/retrieval/planner/nl_planner.py` | `plan_query` (Claude call) | No |
| `src/retrieval/mcp/routing_prompts.py` | Host instructions | Yes — updated to reference `plan_retrieval` |
| `src/pipeline/api/main.py` | `/v1/test/airtable/nl-query` route | Yes — swapped `search_impl` → `plan_retrieval_impl` |
