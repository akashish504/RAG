# Quickstart: Validating D.Quals Confidentiality Enforcement

## Prerequisites

- Repo checked out, virtualenv set up per the project's normal dev workflow (`venv/` already present in this repo).
- No live OpenSearch/Airtable/AWS credentials required — every scenario below runs against in-memory `SearchResult` objects, matching this repo's existing `tests/unit/retrieval/` convention.

## 1. Run the automated test suite

```bash
python -m pytest tests/unit/retrieval/test_confidentiality.py -v
```

**Expected outcome**: all tests pass, covering: confidential-project drop (semantic + structured key spellings), confidential-client redaction (text/metadata/`record_summary`/`deck_summary`/`payload["fields"]`), citation suppression (independent of `AIRTABLE_CITATIONS_ENABLED`), blank/missing-flag pass-through, scalar-vs-list facet values, standalone-acronym redaction, non-`d_quals` source exemption, and the `_resolve_citations` semantic-hit exclusion.

Also run the adjacent suites to confirm no regression in related behavior:

```bash
python -m pytest tests/unit/retrieval/ tests/unit/test_facets.py -q
```

## 2. Manual/interactive check (no network calls)

From the repo root, with the project's Python environment active:

```python
from retrieval.confidentiality import filter_and_redact_confidential
from retrieval.models import SearchResult

confidential_project_hit = SearchResult(
    source="d_quals", source_type="semantic", score=0.9,
    text="A secret initiative.",
    metadata={"confidential_project": ["CONFIDENTIAL"]},
)
confidential_client_hit = SearchResult(
    source="d_quals", source_type="structured", score=1.0,
    text="Engagement with Acme Corp on market entry.",
    metadata={
        "Confidential Client": ["CONFIDENTIAL"],
        "Client Organisation": ["Acme Corp"],
    },
    citation_url="https://airtable.com/appX/tblY/recZ",
)
clean_hit = SearchResult(
    source="d_quals", source_type="semantic", score=0.8,
    text="Engagement with Beta Corp.",
    metadata={"client_organisation": ["Beta Corp"]},
)

result = filter_and_redact_confidential(
    [confidential_project_hit, confidential_client_hit, clean_hit]
)

assert confidential_project_hit not in result          # dropped entirely
assert "Acme Corp" not in result[0].text                # redacted
assert result[0].citation_url is None                   # link suppressed
assert result[1].text == "Engagement with Beta Corp."   # untouched
print("OK — confidentiality enforcement behaves as specified")
```

**Expected outcome**: the script prints `OK — confidentiality enforcement behaves as specified` with no assertion errors.

## 3. End-to-end check against a real `d_quals` deployment (optional, requires credentials)

If you have a configured environment (`AIRTABLE_PAT_TOKEN`, `OPENSEARCH_ENDPOINT`, etc. — see `.env.example`) and know the identifier of a real record with `Confidential Project = CONFIDENTIAL` and one with `Confidential Client = CONFIDENTIAL`:

1. Call the `search` or `semantic_search` MCP tool (via the running MCP server, or directly through `retrieval.mcp.tools.search_impl`) with a query that matches the confidential-project record.
2. Confirm that record is absent from `hits` and from `references`/`references_markdown`.
3. Repeat with a query matching the confidential-client record.
4. Confirm the client's name is replaced with `XXXXXXX` in the returned `text` and `metadata`, and that `citation_url` is `null` and `citations` is `[]` for that hit.

Refer to [contracts/mcp-tool-output-confidentiality.md](contracts/mcp-tool-output-confidentiality.md) for the exact before/after response shape this step should observe.
