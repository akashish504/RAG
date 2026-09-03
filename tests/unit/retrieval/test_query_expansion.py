"""BM25 query expansion (glossary acronyms/aliases → canonical terms).

Recall-additive and word-boundary safe; never rewrites the KNN vector query.
"""

from __future__ import annotations

from retrieval.sources.opensearch import _expand_bm25_query


def test_expands_known_alias_appends_canonical_terms() -> None:
    out = _expand_bm25_query("what is the PA for this", "pa")
    assert out.startswith("pa")
    assert "practice area" in out


def test_no_alias_leaves_query_unchanged() -> None:
    assert _expand_bm25_query("health financing in kenya", "health financing kenya") \
        == "health financing kenya"


def test_word_boundary_prevents_spurious_expansion() -> None:
    # "pa" must NOT expand inside "japan"; "pm" must NOT expand inside "impmact".
    assert _expand_bm25_query("projects in japan", "projects japan") == "projects japan"
    assert "project manager" not in _expand_bm25_query("company report", "company report")


def test_ampersand_alias_expands() -> None:
    out = _expand_bm25_query("m&e frameworks", "m&e frameworks")
    assert "monitoring evaluation" in out


def test_multiple_aliases_all_appended() -> None:
    out = _expand_bm25_query("skilling quals for agri", "skilling quals agri")
    assert "education to employment" in out
    assert "qualifications" in out
    assert "agriculture food systems" in out
