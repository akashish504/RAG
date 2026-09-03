"""Unit tests for search_impl.

All tests mock _registry() and RetrievalRouter so no live credentials needed.
plan_query is also mocked to avoid Claude API calls.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from retrieval.mcp.tools import search_impl
from retrieval.models import FieldDescriptor, SchemaDescriptor, SearchResponse, SearchResult


# ---------------------------------------------------------------------------
# Shared stubs (mirrors test_plan_retrieval.py)
# ---------------------------------------------------------------------------


def _make_schema(
    source: str,
    *,
    identifier_field: str = "Email",
    include_profile_fields: bool = False,
) -> SchemaDescriptor:
    fields = [
        FieldDescriptor(name="Email", type="email"),
        FieldDescriptor(name="Display Name", type="singleLineText"),
        FieldDescriptor(name="Office Location", type="singleSelect"),
        FieldDescriptor(name="Languages", type="multipleSelects"),
    ]
    if include_profile_fields:
        fields.extend([
            FieldDescriptor(name="Job Title", type="singleLineText"),
            FieldDescriptor(name="Skills", type="multilineText"),
            FieldDescriptor(name="Interests", type="multilineText"),
        ])
    return SchemaDescriptor(
        source=source,
        display_name=source.title(),
        description="Test source",
        capabilities=["semantic", "structured"],
        identifier_field=identifier_field,
        fields=fields,
    )


class _MockAirtable:
    pass


class _MockOpenSearch:
    pass


class _MockLogical:
    def __init__(
        self,
        name: str,
        *,
        has_airtable: bool = True,
        has_opensearch: bool = True,
        schema: SchemaDescriptor | None = None,
    ) -> None:
        self.name = name
        self.airtable = _MockAirtable() if has_airtable else None
        self.opensearch = _MockOpenSearch() if has_opensearch else None
        self._schema = schema or _make_schema(name)

    def get_schema(self) -> SchemaDescriptor:
        return self._schema


class _MockRegistry:
    def __init__(self, sources: dict[str, _MockLogical]) -> None:
        self._sources = sources

    def names(self) -> list[str]:
        return list(self._sources.keys())

    def get(self, name: str) -> _MockLogical:
        if name not in self._sources:
            raise KeyError(f"Unknown source: {name!r}")
        return self._sources[name]

    def enabled_sources(self) -> list[_MockLogical]:
        return list(self._sources.values())


def _mock_plan(
    mode: str = "hybrid",
    formula: str = "",
    semantic_query: str = "test query",
    top_k: int = 10,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "airtable_formula": formula,
        "semantic_query": semantic_query,
        "top_k": top_k,
        "max_records": None,
        "rationale": "test",
        "uncertain": False,
        "query_kind": mode,
        "lookup_fields": None,
    }


def _semantic_hit(source: str = "profiles", pk: str = "alice@dalberg.com") -> SearchResult:
    return SearchResult(
        source=source,
        source_type="semantic",
        score=0.9,
        text="Has experience in health financing in East Africa.",
        metadata={"primary_key": pk},
    )


def _structured_hit(source: str = "profiles") -> SearchResult:
    return SearchResult(
        source=source,
        source_type="structured",
        score=1.0,
        text="Alice Smith | Partner | Nairobi",
        metadata={"Display Name": "Alice Smith", "Skills": "Guitar, Piano"},
    )


def _empty_response() -> SearchResponse:
    return SearchResponse(ok=True, hits=[], hints=[])


def _router_with_responses(*responses: SearchResponse) -> MagicMock:
    """Return a mock RetrievalRouter whose run() returns responses in order."""
    mock = MagicMock()
    mock.run = AsyncMock(side_effect=list(responses))
    return mock


def _router_with_responses_by_source(
    mapping: dict[str, tuple[SearchResponse, SearchResponse | None]],
) -> MagicMock:
    """Return a mock RetrievalRouter whose run() replies based on the query's
    source/mode rather than call position.

    Sources now run concurrently, so the *order* in which different sources'
    `router.run()` calls arrive is no longer deterministic (only each single
    source's own main-then-enrichment order is preserved). A responses-in-order
    side_effect list would silently hand one source's response to another.
    `mapping` is `{source_name: (main_response, enrich_response_or_None)}`;
    the first call for a source returns its main response, a second call for
    the same source (the enrichment lookup, always `mode="airtable_only"`)
    returns its enrich response.
    """
    calls_seen: dict[str, int] = {}

    async def _run(query: Any) -> SearchResponse:
        source = query.sources[0]
        main_response, enrich_response = mapping[source]
        seen = calls_seen.get(source, 0)
        calls_seen[source] = seen + 1
        if seen == 0:
            return main_response
        assert enrich_response is not None, f"unexpected second call for source {source!r}"
        return enrich_response

    mock = MagicMock()
    mock.run = AsyncMock(side_effect=_run)
    return mock


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_question_returns_error() -> None:
    result = json.loads(search_impl(question=""))
    assert result["ok"] is False
    assert "question" in result["error"].lower()


def test_whitespace_question_returns_error() -> None:
    result = json.loads(search_impl(question="   "))
    assert result["ok"] is False


def test_unknown_source_returns_error() -> None:
    registry = _MockRegistry({})
    with patch("retrieval.mcp.tools._registry", return_value=registry):
        result = json.loads(search_impl(question="Who has health experience?", sources=["nonexistent"]))
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_response_is_valid_json() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    router = _router_with_responses(_empty_response(), _empty_response())
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        raw = search_impl(question="Who has health experience?")
    result = json.loads(raw)
    assert isinstance(result, dict)


def test_response_ok_true_on_success() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    router = _router_with_responses(_empty_response(), _empty_response())
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who speaks French?"))
    assert result["ok"] is True


def test_response_has_hits_list() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    router = _router_with_responses(_empty_response(), _empty_response())
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Climate experts?"))
    assert "hits" in result


def test_diagnostics_include_plans() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    router = _router_with_responses(_empty_response(), _empty_response())
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="hybrid", formula='FIND("x", {Skills})')),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has X?"))
    plans = result.get("diagnostics", {}).get("plans", [])
    assert len(plans) == 1
    assert plans[0]["source"] == "profiles"
    assert plans[0]["mode"] == "hybrid"
    assert plans[0]["airtable_formula"] == 'FIND("x", {Skills})'


# ---------------------------------------------------------------------------
# literal-keyword query (guitar) → search runs BOTH semantic + structured
# ---------------------------------------------------------------------------


def test_guitar_query_runs_hybrid_with_structured_formula() -> None:
    """A literal-keyword query (classifier → airtable_only) is upgraded to hybrid
    so search() runs the structured FIND() formula AND a semantic pass — the
    exact structured match is never missed even when search() is called directly.
    """
    schema = _make_schema("dalberg_profiles", include_profile_fields=True)
    registry = _MockRegistry({"dalberg_profiles": _MockLogical("dalberg_profiles", schema=schema)})
    main_response = SearchResponse(ok=True, hits=[_structured_hit("dalberg_profiles")], hints=[])
    enrich_response = SearchResponse(ok=True, hits=[], hints=[])
    router = _router_with_responses(main_response, enrich_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(
            search_impl(question="Who at Dalberg plays guitar?", sources=["dalberg_profiles"])
        )

    assert result["ok"] is True
    # Formula present → search forces hybrid (both passes run), giving the
    # structured lookup equal weight to semantic search.
    main_q = router.run.call_args_list[0][0][0]
    assert main_q.mode == "hybrid"
    assert "guitar" in (main_q.formula or "").lower()


# ---------------------------------------------------------------------------
# semantic_only / hybrid — enrichment triggered
# ---------------------------------------------------------------------------


def test_semantic_hits_trigger_enrichment_call() -> None:
    """semantic hits with primary_key → second router.run call for enrichment."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("profiles", "alice@dalberg.com")], hints=[])
    enrich_response = SearchResponse(ok=True, hits=[_structured_hit("profiles")], hints=[])
    router = _router_with_responses(main_response, enrich_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has health experience?"))

    assert result["ok"] is True
    assert router.run.call_count == 2
    enrich_q = router.run.call_args_list[1][0][0]
    assert enrich_q.mode == "airtable_only"
    assert "alice@dalberg.com" in (enrich_q.formula or "")


def test_enrichment_uses_identifier_field_from_schema() -> None:
    schema = _make_schema("profiles", identifier_field="Email")
    registry = _MockRegistry({"profiles": _MockLogical("profiles", schema=schema)})
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("profiles", "bob@d.com")], hints=[])
    router = _router_with_responses(main_response, _empty_response())

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        search_impl(question="Expert in climate?")

    enrich_q = router.run.call_args_list[1][0][0]
    assert "{Email}" in (enrich_q.formula or "")
    assert "bob@d.com" in (enrich_q.formula or "")


def test_no_enrichment_when_no_airtable_adapter() -> None:
    """Source with no airtable adapter → no enrichment call."""
    registry = _MockRegistry({"os_only": _MockLogical("os_only", has_airtable=False)})
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("os_only")], hints=[])
    router = _router_with_responses(main_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has climate experience?"))

    assert result["ok"] is True
    assert router.run.call_count == 1


def test_no_enrichment_when_no_semantic_hits() -> None:
    """Only structured hits (airtable_only mode) → no enrichment call."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    main_response = SearchResponse(ok=True, hits=[_structured_hit("profiles")], hints=[])
    router = _router_with_responses(main_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="airtable_only", formula='{Office}="Nairobi"')),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="People in Nairobi?"))

    assert result["ok"] is True
    assert router.run.call_count == 1


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_duplicate_hits_deduplicated() -> None:
    """Same structured hit returned from both main query and enrichment → deduped."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    hit = _structured_hit("profiles")
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("profiles"), hit], hints=[])
    enrich_response = SearchResponse(ok=True, hits=[hit], hints=[])  # same hit again
    router = _router_with_responses(main_response, enrich_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="hybrid")),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has experience?"))

    hit_texts = [h["text"] for h in result["hits"]]
    assert hit_texts.count(hit.text) == 1


# ---------------------------------------------------------------------------
# Planner failure fallback
# ---------------------------------------------------------------------------


def test_planner_failure_falls_back_to_semantic_only() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("profiles")], hints=[])
    router = _router_with_responses(main_response, _empty_response())

    def _boom(*_, **__):
        raise RuntimeError("Anthropic timeout")

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_boom),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has health experience?"))

    assert result["ok"] is True
    fallback_q = router.run.call_args_list[0][0][0]
    assert fallback_q.mode == "semantic_only"


# ---------------------------------------------------------------------------
# Multi-source wildcard expansion
# ---------------------------------------------------------------------------


def test_wildcard_sources_queries_all_sources() -> None:
    registry = _MockRegistry({
        "source_a": _MockLogical("source_a"),
        "source_b": _MockLogical("source_b"),
    })
    router = _router_with_responses(
        _empty_response(), _empty_response(),  # source_a main + enrich
        _empty_response(), _empty_response(),  # source_b main + enrich
    )

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has climate experience?"))

    assert result["ok"] is True
    plans = result["diagnostics"]["plans"]
    sources_planned = {p["source"] for p in plans}
    assert sources_planned == {"source_a", "source_b"}


# ---------------------------------------------------------------------------
# Concurrency: sources are planned + retrieved in parallel, not sequentially
# (spec 004)
# ---------------------------------------------------------------------------


def test_single_source_output_unchanged() -> None:
    """Locks in the exact output for a single-source query — proves the
    concurrency refactor changes only execution scheduling, not results
    (spec FR-004/FR-005)."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    main_response = SearchResponse(ok=True, hits=[_semantic_hit("profiles", "alice@dalberg.com")], hints=[])
    enrich_response = SearchResponse(ok=True, hits=[_structured_hit("profiles")], hints=[])
    router = _router_with_responses(main_response, enrich_response)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(search_impl(question="Who has health experience?"))

    assert result["ok"] is True
    assert len(result["hits"]) == 2
    assert result["diagnostics"]["plans"] == [{
        "source": "profiles",
        "mode": "semantic_only",
        "airtable_formula": "",
        "semantic_query": "test query",
    }]


def test_multi_source_search_runs_concurrently() -> None:
    """4 sources, each with a slow plan_query, should take ~1 slow call's
    time, not 4x — proves per-source plan+execute is concurrent."""
    registry = _MockRegistry({f"source_{i}": _MockLogical(f"source_{i}") for i in range(4)})
    router = _router_with_responses_by_source({
        f"source_{i}": (_empty_response(), None) for i in range(4)
    })

    def _slow_plan(*_: Any, **__: Any) -> dict[str, Any]:
        time.sleep(0.05)
        return _mock_plan()

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_slow_plan),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        start = time.perf_counter()
        result = json.loads(search_impl(question="Who has climate experience?"))
        elapsed = time.perf_counter() - start

    assert result["ok"] is True
    # Sequential would take ~4 * 0.05s = 0.2s; concurrent should be close to 0.05s.
    assert elapsed < 0.15, f"expected concurrent execution, took {elapsed:.3f}s"


def test_one_source_failure_does_not_affect_other_search_results() -> None:
    """One source's plan_query raises; the other source's results must be
    complete and correct, unaffected by the failure or by running
    concurrently with it."""
    registry = _MockRegistry({
        "profiles": _MockLogical("profiles"),
        "broken_source": _MockLogical("broken_source"),
    })
    router = _router_with_responses_by_source({
        "profiles": (SearchResponse(ok=True, hits=[_structured_hit("profiles")], hints=[]), None),
        "broken_source": (_empty_response(), None),
    })

    def _maybe_boom(*, question: str, schema: Any) -> dict[str, Any]:
        if schema.source == "broken_source":
            raise RuntimeError("Anthropic timeout")
        return _mock_plan(mode="airtable_only", formula='{Office}="Nairobi"')

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_maybe_boom),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        result = json.loads(
            search_impl(question="People in Nairobi?", sources=["profiles", "broken_source"])
        )

    assert result["ok"] is True
    plans_by_source = {p["source"]: p for p in result["diagnostics"]["plans"]}
    assert plans_by_source["broken_source"]["mode"] == "semantic_only"
    # A non-empty formula always upgrades to hybrid (search() runs both
    # passes) — same override documented by test_guitar_query_runs_hybrid_...
    assert plans_by_source["profiles"]["mode"] == "hybrid"
    hit_sources = {h["source"] for h in result["hits"]}
    assert "profiles" in hit_sources


def test_slow_source_does_not_serialize_others() -> None:
    """One very slow source must not add its latency on top of the others'
    — proves concurrent scheduling, not just independent per-source code."""
    names = ["slow_source", "fast_a", "fast_b", "fast_c", "fast_d"]
    registry = _MockRegistry({name: _MockLogical(name) for name in names})
    router = _router_with_responses_by_source({name: (_empty_response(), None) for name in names})

    def _plan(*, question: str, schema: Any) -> dict[str, Any]:
        time.sleep(0.15 if schema.source == "slow_source" else 0.02)
        return _mock_plan()

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_plan),
        patch("retrieval.mcp.tools.RetrievalRouter", return_value=router),
    ):
        start = time.perf_counter()
        result = json.loads(search_impl(question="Who has climate experience?"))
        elapsed = time.perf_counter() - start

    assert result["ok"] is True
    # Sequential would take ~0.15 + 4*0.02 = 0.23s; concurrent should be
    # close to the slowest source's own 0.15s.
    assert elapsed < 0.2, f"expected concurrent scheduling, took {elapsed:.3f}s"
