"""Vocabulary-grounded facet extraction from NL queries."""

from __future__ import annotations

from retrieval.facet_planner import (
    SOFT_FACETS,
    FacetPlanner,
    _normalize_match,
    _word_present,
    split_soft,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, index, body):  # noqa: ARG002
        self.calls += 1
        # Two facet fields with their distinct indexed values.
        return {
            "aggregations": {
                "client_organisation": {"buckets": [
                    {"key": "Gates Foundation"}, {"key": "USAID"},
                ]},
                "project_region": {"buckets": [
                    {"key": "East Africa"}, {"key": "South Asia"},
                ]},
                "practice_area": {"buckets": [{"key": "Health"}]},
                "project_location": {"buckets": []},
                "dalberg_entity": {"buckets": []},
                "insight_type": {"buckets": []},
            }
        }


def _planner() -> FacetPlanner:
    return FacetPlanner(client=_FakeClient(), index_name="mcp-d-quals")


def test_word_present_respects_boundaries() -> None:
    assert _word_present("Mali", "work in mali on health")
    assert not _word_present("Mali", "we formalise the process")  # not inside a word


def test_plan_extracts_grounded_facets() -> None:
    p = _planner()
    out = p.plan("health work in East Africa for the Gates Foundation")
    assert out["client_organisation"] == ["Gates Foundation"]
    assert out["project_region"] == ["East Africa"]
    assert out["practice_area"] == ["Health"]
    assert "USAID" not in out.get("client_organisation", [])  # not mentioned


def test_plan_empty_when_no_match_or_no_query() -> None:
    p = _planner()
    assert p.plan("") == {}
    assert p.plan("something with no known facet value") == {}


def test_vocab_is_cached() -> None:
    p = _planner()
    p.plan("Health in East Africa")
    p.plan("USAID work")
    assert p._client.calls == 1  # second query reuses cached vocab


# --- Soft vs hard facet partition (miss #1 gate fix) ------------------------


def test_split_soft_partitions_fuzzy_vs_categorical() -> None:
    derived = {
        "practice_area": ["Talent & Leadership"],   # fuzzy → soft
        "insight_type": ["Case Study"],             # fuzzy → soft
        "client_organisation": ["UNHCR"],           # categorical → hard
        "project_region": ["East Africa"],          # categorical → hard
    }
    hard, soft = split_soft(derived)
    assert soft == {
        "practice_area": ["Talent & Leadership"],
        "insight_type": ["Case Study"],
    }
    assert hard == {
        "client_organisation": ["UNHCR"],
        "project_region": ["East Africa"],
    }


def test_soft_facets_are_the_fuzzy_taxonomies() -> None:
    # Regression anchor for miss #1: practice_area must NOT be a hard filter.
    assert "practice_area" in SOFT_FACETS
    assert "insight_type" in SOFT_FACETS
    # Genuine categorical constraints stay hard (absent from the soft set).
    for categorical in ("client_organisation", "project_region", "start_date"):
        assert categorical not in SOFT_FACETS


def test_split_soft_empty() -> None:
    assert split_soft({}) == ({}, {})


# --- Unicode / ampersand normalisation + curated alias map ------------------


class _AmpersandClient:
    """Index vocab that mixes ampersand encodings and has a D. Capital entity."""

    def search(self, index, body):  # noqa: ARG002
        return {
            "aggregations": {
                "client_organisation": {"buckets": []},
                # full-width ampersand U+FF06 as actually stored in the index
                "practice_area": {"buckets": [{"key": "Cities ＆ Infrastructure"}]},
                "project_region": {"buckets": []},
                "project_location": {"buckets": []},
                "dalberg_entity": {"buckets": [{"key": "D. Capital"}]},
                "insight_type": {"buckets": []},
            }
        }


def _amp_planner() -> FacetPlanner:
    return FacetPlanner(client=_AmpersandClient(), index_name="mcp-d-quals")


def test_normalize_match_folds_fullwidth_ampersand() -> None:
    assert _normalize_match("Cities ＆ Infrastructure") == _normalize_match(
        "cities & infrastructure"
    )


def test_word_present_matches_across_ampersand_encodings() -> None:
    # value uses full-width ＆; query uses ASCII & — must still match
    assert _word_present(
        "Cities ＆ Infrastructure", _normalize_match("work in cities & infrastructure")
    )


def test_plan_matches_fullwidth_ampersand_value_from_ascii_query() -> None:
    p = _amp_planner()
    out = p.plan("our work in cities & infrastructure")
    assert out["practice_area"] == ["Cities ＆ Infrastructure"]  # real stored value


def test_ampersand_no_space_encoding_also_matches_and_returns_original() -> None:
    # A no-space "cities＆infrastructure" query must still match the spaced index
    # value, and the RETURNED value must be the ORIGINAL index value (filter-safe).
    p = _amp_planner()
    out = p.plan("cities＆infrastructure work")
    assert out["practice_area"] == ["Cities ＆ Infrastructure"]


def test_alias_map_adds_canonical_entity_value() -> None:
    p = _amp_planner()
    out = p.plan("what has D.Capital done in energy")
    assert out["dalberg_entity"] == ["D. Capital"]


def test_alias_map_absent_when_term_not_present() -> None:
    p = _amp_planner()
    out = p.plan("cities & infrastructure work")
    assert "dalberg_entity" not in out
