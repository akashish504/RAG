"""Soft-facet fallback (spec 007): config knob parsing, fallback-pass merging,
and the facet-match ranking boost. Pure-unit — no OpenSearch client involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from retrieval.config import _parse_opensearch
from retrieval.sources.opensearch import OpenSearchSource

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_facet_mode_defaults_to_hard() -> None:
    cfg = _parse_opensearch({"index_name": "idx"}, default_k=10)
    assert cfg.facet_mode == "hard"
    assert cfg.facet_fallback_min_results == 3


def test_facet_mode_fallback_parses_with_threshold() -> None:
    cfg = _parse_opensearch(
        {"index_name": "idx", "facet_mode": "FALLBACK", "facet_fallback_min_results": 5},
        default_k=10,
    )
    assert cfg.facet_mode == "fallback"
    assert cfg.facet_fallback_min_results == 5


def test_invalid_facet_mode_rejected() -> None:
    with pytest.raises(ValueError, match="facet_mode"):
        _parse_opensearch({"index_name": "idx", "facet_mode": "sometimes"}, default_k=10)


# ---------------------------------------------------------------------------
# _merge_passes — combining strict + widened fallback passes
# ---------------------------------------------------------------------------


def _hit(doc_id: str, rrf: float, **src: object) -> dict:
    return {"_id": doc_id, "_rrf_score": rrf, "_source": {"chunk_id": doc_id, **src}}


def test_merge_passes_dedupes_keeping_best_score() -> None:
    strict = [_hit("a", 0.030), _hit("b", 0.020)]
    widened = [_hit("a", 0.010), _hit("c", 0.025)]
    merged = OpenSearchSource._merge_passes(strict, widened, k_max=10)
    ids = [h["_id"] for h in merged]
    assert ids == ["a", "c", "b"]              # each doc once, ordered by best score
    assert merged[0]["_rrf_score"] == 0.030    # strict's better score for 'a' survived
    assert [h["_rank"] for h in merged] == [0, 1, 2]


def test_merge_passes_truncates_to_k_max() -> None:
    strict = [_hit(f"s{i}", 0.03 - i * 0.001) for i in range(3)]
    widened = [_hit(f"w{i}", 0.02 - i * 0.001) for i in range(3)]
    merged = OpenSearchSource._merge_passes(strict, widened, k_max=4)
    assert len(merged) == 4


# ---------------------------------------------------------------------------
# _apply_facet_boost — facet-matching docs rank ahead
# ---------------------------------------------------------------------------


def _bare_source(rrf_k: int = 60) -> OpenSearchSource:
    src = OpenSearchSource.__new__(OpenSearchSource)
    src._ranking_cfg = SimpleNamespace(rrf_k=rrf_k)
    return src


def test_facet_match_outranks_equal_nonmatch() -> None:
    hits = [
        _hit("plain", 0.0200),
        _hit("match", 0.0199, project_region="East Africa"),
    ]
    _bare_source()._apply_facet_boost(hits, {"project_region": ["East Africa"]})
    assert [h["_id"] for h in hits] == ["match", "plain"]
    assert hits[0]["_facet_matched"] == 1


def test_strong_relevance_lead_still_wins() -> None:
    # A doc leading both ranked lists (~2/(k+1)) must beat a facet-matching
    # straggler — the boost prefers, it does not override relevance.
    both_lists_leader = _hit("leader", 2 / 61)
    straggler = _hit("straggler", 1 / 100, project_region="East Africa")
    hits = [both_lists_leader, straggler]
    _bare_source()._apply_facet_boost(hits, {"project_region": ["East Africa"]})
    assert hits[0]["_id"] == "leader"


def test_partial_match_scales_boost() -> None:
    derived = {"project_region": ["East Africa"], "practice_area": ["Health"]}
    hits = [
        _hit("full", 0.010, project_region="East Africa", practice_area=["Health"]),
        _hit("half", 0.010, project_region="East Africa"),
        _hit("none", 0.010),
    ]
    _bare_source()._apply_facet_boost(hits, derived)
    assert [h["_id"] for h in hits] == ["full", "half", "none"]


def test_list_valued_doc_facets_intersect() -> None:
    hits = [_hit("multi", 0.010, project_region=["West Africa", "East Africa"])]
    _bare_source()._apply_facet_boost(hits, {"project_region": ["East Africa"]})
    assert hits[0]["_facet_matched"] == 1


def test_no_derived_facets_is_noop() -> None:
    hits = [_hit("a", 0.020), _hit("b", 0.010)]
    before = [(h["_id"], h["_rrf_score"]) for h in hits]
    _bare_source()._apply_facet_boost(hits, {})
    assert [(h["_id"], h["_rrf_score"]) for h in hits] == before
