"""Unit tests for plan_retrieval_impl.

All tests mock _registry() so no live Airtable / OpenSearch / Anthropic
credentials are required. plan_query is also mocked so no Claude API call
is made.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import patch

import pytest

from retrieval.mcp.tools import _ENRICHMENT_FIELDS_CAP, _MAX_RECORDS_CAP, plan_retrieval_impl
from retrieval.models import FieldDescriptor, SchemaDescriptor


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


def _make_schema(
    source: str,
    *,
    capabilities: list[str] | None = None,
    identifier_field: str = "Email",
    include_profile_fields: bool = False,
) -> SchemaDescriptor:
    if capabilities is None:
        capabilities = ["semantic", "structured"]
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
        display_name=source.replace("_", " ").title(),
        description="Test source",
        capabilities=capabilities,
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
    mode: str = "semantic_only",
    formula: str = "",
    semantic_query: str = "test query",
    top_k: int = 10,
    max_records: int | None = None,
    rationale: str = "test rationale",
    uncertain: bool = False,
    query_kind: str = "",
) -> dict[str, Any]:
    return {
        "mode": mode,
        "airtable_formula": formula,
        "semantic_query": semantic_query,
        "top_k": top_k,
        "max_records": max_records,
        "rationale": rationale,
        "uncertain": uncertain,
        "query_kind": query_kind,
        "lookup_fields": None,
    }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_question_returns_error() -> None:
    result = json.loads(plan_retrieval_impl(question=""))
    assert result["ok"] is False
    assert "question" in result["error"].lower()


def test_whitespace_only_question_returns_error() -> None:
    result = json.loads(plan_retrieval_impl(question="   "))
    assert result["ok"] is False


def test_unknown_source_returns_error() -> None:
    registry = _MockRegistry({})
    with patch("retrieval.mcp.tools._registry", return_value=registry):
        result = json.loads(
            plan_retrieval_impl(question="Who has health experience?", sources=["nonexistent"])
        )
    assert result["ok"] is False
    assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Response shape invariants
# ---------------------------------------------------------------------------


def test_response_is_valid_json() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        raw = plan_retrieval_impl(question="Who speaks French?")
    # Must not raise
    result = json.loads(raw)
    assert isinstance(result, dict)


def test_response_envelope_shape() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        result = json.loads(plan_retrieval_impl(question="Who speaks French?"))

    assert result["ok"] is True
    assert result["question"] == "Who speaks French?"
    assert isinstance(result["plans"], list)
    assert len(result["plans"]) == 1


def test_plan_entry_has_required_keys() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        result = json.loads(plan_retrieval_impl(question="Find experts in health."))

    plan = result["plans"][0]
    for key in ("source", "mode", "semantic_query", "airtable_formula", "top_k", "suggested_calls"):
        assert key in plan, f"Missing key {key!r} in plan"


# ---------------------------------------------------------------------------
# semantic_only mode
# ---------------------------------------------------------------------------


def test_semantic_only_has_two_suggested_calls() -> None:
    """semantic_only + Airtable adapter → step 1 semantic + step 2 enrichment."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Who has climate experience?"))

    calls = result["plans"][0]["suggested_calls"]
    assert len(calls) == 2
    assert calls[0]["tool"] == "semantic_search"
    assert calls[0]["step"] == 1
    assert calls[1]["tool"] == "airtable_lookup"
    assert calls[1].get("purpose") == "enrichment"


def test_semantic_only_step_args() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(mode="semantic_only", semantic_query="climate finance", top_k=15),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="Climate finance experience?"))

    sem_args = result["plans"][0]["suggested_calls"][0]["args"]
    assert sem_args["query"] == "climate finance"
    assert sem_args["source"] == "profiles"
    assert sem_args["top_k"] == 15


def test_semantic_only_no_main_airtable_lookup() -> None:
    """Semantic-only should NOT have a main airtable_lookup (only enrichment)."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Who speaks Portuguese?"))

    calls = result["plans"][0]["suggested_calls"]
    main_at_calls = [c for c in calls if c["tool"] == "airtable_lookup" and c.get("purpose") != "enrichment"]
    assert main_at_calls == [], "No main airtable_lookup expected in semantic_only mode"


# ---------------------------------------------------------------------------
# hybrid mode with formula
# ---------------------------------------------------------------------------


def test_hybrid_with_formula_has_three_suggested_calls() -> None:
    """hybrid + non-empty formula → semantic + main airtable + enrichment."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(
                mode="hybrid",
                formula='FIND("Portuguese", {Languages})',
                semantic_query="health experience",
            ),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="Portuguese speakers with health experience?"))

    calls = result["plans"][0]["suggested_calls"]
    assert len(calls) == 3
    tools = [c["tool"] for c in calls]
    assert tools[0] == "semantic_search"
    assert tools[1] == "airtable_lookup"
    assert tools[2] == "airtable_lookup"
    assert calls[1].get("purpose") != "enrichment"
    assert calls[2].get("purpose") == "enrichment"


def test_hybrid_formula_passed_to_main_airtable_call() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    formula = 'FIND("Portuguese", {Languages})'
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(mode="hybrid", formula=formula),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="Portuguese speakers?"))

    main_at = result["plans"][0]["suggested_calls"][1]
    assert main_at["args"]["formula"] == formula


def test_hybrid_without_formula_has_two_calls() -> None:
    """hybrid with empty formula → only semantic + enrichment (no main airtable)."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="hybrid", formula="")),
    ):
        result = json.loads(plan_retrieval_impl(question="Health expertise?"))

    calls = result["plans"][0]["suggested_calls"]
    assert len(calls) == 2
    assert calls[0]["tool"] == "semantic_search"
    assert calls[1]["tool"] == "airtable_lookup"
    assert calls[1].get("purpose") == "enrichment"


# ---------------------------------------------------------------------------
# airtable_only mode
# ---------------------------------------------------------------------------


def test_airtable_only_has_one_suggested_call() -> None:
    """airtable_only → single airtable_lookup, no semantic, no enrichment."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(
                mode="airtable_only",
                formula='{Office Location}="Nairobi"',
            ),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="List everyone in Nairobi."))

    calls = result["plans"][0]["suggested_calls"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "airtable_lookup"
    assert calls[0].get("purpose") != "enrichment"


def test_airtable_only_no_semantic_search_call() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(mode="airtable_only", formula='{Seniority Band}="Partner"'),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="List all partners."))

    tools = [c["tool"] for c in result["plans"][0]["suggested_calls"]]
    assert "semantic_search" not in tools


# ---------------------------------------------------------------------------
# max_records cap
# ---------------------------------------------------------------------------


def test_max_records_capped_at_50() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(
                mode="hybrid",
                formula='{Office Location}="Nairobi"',
                max_records=500,
            ),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="People in Nairobi?"))

    plan = result["plans"][0]
    assert plan["max_records"] == _MAX_RECORDS_CAP

    # The main airtable_lookup call must also be capped
    at_call = next(c for c in plan["suggested_calls"] if c["tool"] == "airtable_lookup" and c.get("purpose") != "enrichment")
    assert at_call["args"]["max_records"] <= _MAX_RECORDS_CAP


def test_enrichment_call_always_capped() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Climate experts?"))

    enrichment = next(
        c for c in result["plans"][0]["suggested_calls"] if c.get("purpose") == "enrichment"
    )
    assert enrichment["args"]["max_records"] == _MAX_RECORDS_CAP


# ---------------------------------------------------------------------------
# Source capability checks
# ---------------------------------------------------------------------------


def test_semantic_only_source_no_enrichment_step() -> None:
    """Source with no Airtable adapter must not generate an enrichment step."""
    registry = _MockRegistry({
        "opensearch_only": _MockLogical("opensearch_only", has_airtable=False, has_opensearch=True),
    })
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Who has climate experience?"))

    calls = result["plans"][0]["suggested_calls"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "semantic_search"
    enrichment_calls = [c for c in calls if c.get("purpose") == "enrichment"]
    assert enrichment_calls == []


def test_airtable_only_source_no_semantic_search() -> None:
    """Source with no OpenSearch adapter must not generate a semantic_search step."""
    registry = _MockRegistry({
        "airtable_only": _MockLogical("airtable_only", has_airtable=True, has_opensearch=False),
    })
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(mode="hybrid", formula='{Status}="Active"'),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="Active records?"))

    tools = [c["tool"] for c in result["plans"][0]["suggested_calls"]]
    assert "semantic_search" not in tools


# ---------------------------------------------------------------------------
# Planner failure fallback
# ---------------------------------------------------------------------------


def test_planner_failure_falls_back_gracefully() -> None:
    """If plan_query raises, a semantic_only fallback plan is returned (no error)."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})

    def _boom(*_, **__):
        raise RuntimeError("Anthropic API timeout")

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_boom),
    ):
        result = json.loads(plan_retrieval_impl(question="Who speaks Portuguese?"))

    assert result["ok"] is True
    plan = result["plans"][0]
    assert plan["mode"] == "semantic_only"
    assert "fallback" in plan["rationale"].lower() or "failed" in plan["rationale"].lower()
    # Must still have a semantic_search call
    assert any(c["tool"] == "semantic_search" for c in plan["suggested_calls"])
    # Uncertain fallback should suggest get_schema first
    assert any(c["tool"] == "get_schema" and c["step"] == 0 for c in plan["suggested_calls"])


# ---------------------------------------------------------------------------
# Literal keyword classification (rule-based, no Claude)
# ---------------------------------------------------------------------------


def test_guitar_query_plans_airtable_only_with_find_formula() -> None:
    """Who plays guitar? → airtable_only on Skills via rule-based classifier."""
    schema = _make_schema("dalberg_profiles", include_profile_fields=True)
    logical = _MockLogical("dalberg_profiles", schema=schema)
    registry = _MockRegistry({"dalberg_profiles": logical})

    with patch("retrieval.mcp.tools._registry", return_value=registry):
        result = json.loads(
            plan_retrieval_impl(question="Who at Dalberg plays guitar?", sources=["dalberg_profiles"])
        )

    assert result["ok"] is True
    plan = result["plans"][0]
    assert plan["mode"] == "airtable_only"
    assert plan["airtable_formula"] == 'FIND("guitar", LOWER({Skills}))'
    assert plan["query_kind"] == "literal_keyword"

    calls = plan["suggested_calls"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "airtable_lookup"
    fields = calls[0]["args"]["fields"]
    assert "Display Name" in fields
    assert "Job Title" in fields
    assert "Office Location" in fields
    assert "Skills" in fields
    assert "Interests" in fields
    assert "semantic_search" not in [c["tool"] for c in calls]


def test_enrichment_includes_skills_interests_languages_when_present() -> None:
    schema = _make_schema("profiles", include_profile_fields=True)
    logical = _MockLogical("profiles", schema=schema)
    registry = _MockRegistry({"profiles": logical})

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Climate finance experts?"))

    enrichment = next(
        c for c in result["plans"][0]["suggested_calls"] if c.get("purpose") == "enrichment"
    )
    fields = enrichment["args"]["fields"]
    assert "Skills" in fields
    assert "Interests" in fields
    assert "Languages" in fields


def test_uncertain_hybrid_adds_get_schema_step_zero() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch(
            "retrieval.mcp.tools.plan_query",
            return_value=_mock_plan(mode="hybrid", formula="", uncertain=True, query_kind="uncertain"),
        ),
    ):
        result = json.loads(plan_retrieval_impl(question="Ambiguous people question?"))

    calls = result["plans"][0]["suggested_calls"]
    assert calls[0]["step"] == 0
    assert calls[0]["tool"] == "get_schema"
    assert calls[0]["args"]["source"] == "profiles"


# ---------------------------------------------------------------------------
# Wildcard / multi-source expansion
# ---------------------------------------------------------------------------


def test_wildcard_sources_expands_to_all() -> None:
    registry = _MockRegistry({
        "source_a": _MockLogical("source_a"),
        "source_b": _MockLogical("source_b"),
    })
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        result = json.loads(plan_retrieval_impl(question="Who has health experience?"))

    assert result["ok"] is True
    assert len(result["plans"]) == 2
    source_names = {p["source"] for p in result["plans"]}
    assert source_names == {"source_a", "source_b"}


def test_none_sources_expands_to_all() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        result = json.loads(plan_retrieval_impl(question="Health expertise?", sources=None))

    assert result["ok"] is True
    assert len(result["plans"]) == 1


def test_explicit_source_list() -> None:
    registry = _MockRegistry({
        "source_a": _MockLogical("source_a"),
        "source_b": _MockLogical("source_b"),
    })
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
    ):
        result = json.loads(
            plan_retrieval_impl(question="Health expertise?", sources=["source_a"])
        )

    assert len(result["plans"]) == 1
    assert result["plans"][0]["source"] == "source_a"


# ---------------------------------------------------------------------------
# Enrichment fields — schema-derived, source-agnostic
# ---------------------------------------------------------------------------


def test_enrichment_fields_derived_from_schema_non_long_text() -> None:
    """Enrichment fields must be derived from the schema: only non-long-text, non-attachment."""
    from retrieval.models import FieldDescriptor, SchemaDescriptor

    schema = SchemaDescriptor(
        source="profiles",
        display_name="Profiles",
        description=None,
        capabilities=["semantic", "structured"],
        identifier_field="Email",
        fields=[
            FieldDescriptor(name="Email", type="email"),
            FieldDescriptor(name="Display Name", type="singleLineText"),
            FieldDescriptor(name="CV Text", type="multilineText", is_long_text=True),
            FieldDescriptor(name="CV File", type="multipleAttachments", is_attachment=True),
            FieldDescriptor(name="Office Location", type="singleSelect"),
        ],
    )
    logical = _MockLogical("profiles", schema=schema)
    registry = _MockRegistry({"profiles": logical})

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Find climate experts."))

    enrichment = next(
        c for c in result["plans"][0]["suggested_calls"] if c.get("purpose") == "enrichment"
    )
    fields = enrichment["args"].get("fields", [])
    # Must include structured fields
    assert "Email" in fields
    assert "Display Name" in fields
    assert "Office Location" in fields
    # Must exclude long-text and attachment fields
    assert "CV Text" not in fields
    assert "CV File" not in fields


def test_enrichment_fields_capped_at_limit() -> None:
    """Enrichment fields list must not exceed _ENRICHMENT_FIELDS_CAP."""
    from retrieval.models import FieldDescriptor, SchemaDescriptor

    # Create a schema with many non-long-text fields
    many_fields = [FieldDescriptor(name=f"Field{i}", type="singleLineText") for i in range(30)]
    schema = SchemaDescriptor(
        source="big_source",
        display_name="Big",
        description=None,
        capabilities=["semantic", "structured"],
        identifier_field="Field0",
        fields=many_fields,
    )
    logical = _MockLogical("big_source", schema=schema)
    registry = _MockRegistry({"big_source": logical})

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Find something."))

    enrichment = next(
        c for c in result["plans"][0]["suggested_calls"] if c.get("purpose") == "enrichment"
    )
    fields = enrichment["args"].get("fields", [])
    assert len(fields) <= _ENRICHMENT_FIELDS_CAP


def test_enrichment_uses_source_identifier_field() -> None:
    """Enrichment step must reference the source's actual identifier_field, not hardcode Email."""
    from retrieval.models import FieldDescriptor, SchemaDescriptor

    schema = SchemaDescriptor(
        source="knowledge_library",
        display_name="Knowledge Library",
        description=None,
        capabilities=["semantic", "structured"],
        identifier_field="Doc ID",
        fields=[
            FieldDescriptor(name="Doc ID", type="singleLineText"),
            FieldDescriptor(name="Title", type="singleLineText"),
            FieldDescriptor(name="Document Type", type="singleSelect"),
            FieldDescriptor(name="Body", type="multilineText", is_long_text=True),
        ],
    )
    logical = _MockLogical("knowledge_library", schema=schema)
    registry = _MockRegistry({"knowledge_library": logical})

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(mode="semantic_only")),
    ):
        result = json.loads(plan_retrieval_impl(question="Find research on climate."))

    enrichment = next(
        c for c in result["plans"][0]["suggested_calls"] if c.get("purpose") == "enrichment"
    )
    # identifier_field must be exposed so the host knows which field to use
    assert enrichment.get("identifier_field") == "Doc ID"
    # formula template must reference Doc ID, not Email
    formula_template = enrichment["args"]["formula"]
    assert "Doc ID" in formula_template
    assert "Email" not in formula_template
    # note must reference the actual identifier field
    assert "Doc ID" in enrichment["note"]
    # long-text Body field must be excluded from enrichment fields
    fields = enrichment["args"].get("fields", [])
    assert "Body" not in fields


# ---------------------------------------------------------------------------
# top_k clamping
# ---------------------------------------------------------------------------


def test_top_k_clamped_to_1_min() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(top_k=0)),
    ):
        result = json.loads(plan_retrieval_impl(question="Anyone?"))

    plan = result["plans"][0]
    assert plan["top_k"] >= 1
    sem_call = next(c for c in plan["suggested_calls"] if c["tool"] == "semantic_search")
    assert sem_call["args"]["top_k"] >= 1


def test_top_k_clamped_to_50_max() -> None:
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan(top_k=200)),
    ):
        result = json.loads(plan_retrieval_impl(question="Anyone?"))

    sem_call = next(c for c in result["plans"][0]["suggested_calls"] if c["tool"] == "semantic_search")
    assert sem_call["args"]["top_k"] <= 50


# ---------------------------------------------------------------------------
# No retrieval is executed
# ---------------------------------------------------------------------------


def test_plan_retrieval_does_not_call_router() -> None:
    """plan_retrieval must never invoke the router (pure planner — no retrieval)."""
    registry = _MockRegistry({"profiles": _MockLogical("profiles")})
    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", return_value=_mock_plan()),
        patch("retrieval.mcp.tools._router") as mock_router,
    ):
        plan_retrieval_impl(question="Who has health experience?")

    mock_router.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrency: sources are planned in parallel, not sequentially (spec 004)
# ---------------------------------------------------------------------------


def test_multi_source_planning_runs_concurrently() -> None:
    """4 sources, each with a slow plan_query, should take ~1 slow call's
    time, not 4x — proves per-source planning is concurrent, not sequential."""
    registry = _MockRegistry({f"source_{i}": _MockLogical(f"source_{i}") for i in range(4)})

    def _slow_plan(*_: Any, **__: Any) -> dict[str, Any]:
        time.sleep(0.05)
        return _mock_plan()

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_slow_plan),
    ):
        start = time.perf_counter()
        result = json.loads(plan_retrieval_impl(question="Who has health experience?"))
        elapsed = time.perf_counter() - start

    assert result["ok"] is True
    assert len(result["plans"]) == 4
    # Sequential would take ~4 * 0.05s = 0.2s; concurrent should be close to 0.05s.
    assert elapsed < 0.15, f"expected concurrent planning, took {elapsed:.3f}s"


def test_one_source_planner_failure_does_not_affect_others() -> None:
    """One source's plan_query raises; the other sources must still get a
    normal plan, unaffected by the failure or by running concurrently with it."""
    registry = _MockRegistry({
        "profiles": _MockLogical("profiles"),
        "broken_source": _MockLogical("broken_source"),
        "proposals": _MockLogical("proposals"),
    })

    def _maybe_boom(*, question: str, schema: Any) -> dict[str, Any]:
        if schema.source == "broken_source":
            raise RuntimeError("Anthropic timeout")
        return _mock_plan(mode="hybrid", formula='FIND("x", {Skills})', semantic_query=question)

    with (
        patch("retrieval.mcp.tools._registry", return_value=registry),
        patch("retrieval.mcp.tools.plan_query", side_effect=_maybe_boom),
    ):
        result = json.loads(plan_retrieval_impl(question="Who has health experience?"))

    assert result["ok"] is True
    plans_by_source = {p["source"]: p for p in result["plans"]}
    assert set(plans_by_source) == {"profiles", "broken_source", "proposals"}

    broken = plans_by_source["broken_source"]
    assert broken["mode"] == "semantic_only"
    assert "Planning failed" in broken["rationale"]

    for name in ("profiles", "proposals"):
        assert plans_by_source[name]["mode"] == "hybrid"
        assert "Planning failed" not in plans_by_source[name]["rationale"]
