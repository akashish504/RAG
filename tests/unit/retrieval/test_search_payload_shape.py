"""Wire-payload shape tests for the slimmed `search` response.

These tests pin the retrieval-vs-presentation contract from the
payload-slimming plan:

  * Hits, ordering, scores, primary keys, citation_url, references_markdown,
    and the synthesised answer must all be byte-identical regardless of
    response-shape options. Retrieval correctness is non-negotiable.
  * Display-side fields (`hits[].text` length, `markdown_table`,
    per-hit `citations[]`) shrink according to the documented contract.
"""

from __future__ import annotations

import json

from retrieval.formatter import format_response
from retrieval.models import Citation, SearchResponse, SearchResult


# ---------------------------------------------------------------------------
# Fixtures — built fresh in each test to avoid in-memory state bleed.
# ---------------------------------------------------------------------------


_LONG_TEXT = (
    "Jane Doe has fifteen years of consulting experience across health "
    "financing, climate adaptation, and gender equity programmes in West "
    "and East Africa. She has led engagements with the World Bank, the "
    "Gates Foundation, and several African finance ministries. Recent "
    "work focuses on blended finance instruments for adaptation. "
) * 4  # ~1200 chars, well above both 400 and 800 limits.


def _make_hit(*, email: str, score: float, with_citation_url: bool = True) -> SearchResult:
    url = f"https://airtable.com/appX/tblY/rec{email[:6].upper()}" if with_citation_url else None
    return SearchResult(
        source="dalberg_profiles",
        source_type="semantic",
        score=score,
        text=_LONG_TEXT,
        metadata={
            "primary_key": email,
            "Display Name": email.split("@")[0].title(),
            "Job Title": "Senior Partner",
            "Office Location": "Nairobi",
        },
        citation_url=url,
        citations=(
            [
                Citation(
                    cite_id="C1",
                    kind="semantic_chunk",
                    label=f"{email} — CV",
                    url=url,
                    locator={"section": "experience"},
                )
            ]
            if with_citation_url
            else []
        ),
    )


def _two_hit_response(*, with_citation_url: bool = True) -> SearchResponse:
    return SearchResponse(
        ok=True,
        hits=[
            _make_hit(email="jane@dalberg.com", score=0.91, with_citation_url=with_citation_url),
            _make_hit(email="john@dalberg.com", score=0.87, with_citation_url=with_citation_url),
        ],
        markdown_table="| Name | Office |\n|---|---|\n| Jane | Nairobi |",
        answer="Jane works on health financing.",
    )


# ---------------------------------------------------------------------------
# Retrieval invariants — these MUST hold regardless of slim options.
# ---------------------------------------------------------------------------


def test_hits_order_and_identity_preserved_under_slim_options() -> None:
    """Same hits, same order, same scores, same primary keys — slim or not."""
    response = _two_hit_response()
    full = response.to_dict()  # default options
    slim = response.to_dict(text_preview_chars=400)  # search-tool shape

    full_keys = [(h["source"], h["metadata"]["primary_key"], h["score"]) for h in full["hits"]]
    slim_keys = [(h["source"], h["metadata"]["primary_key"], h["score"]) for h in slim["hits"]]
    assert full_keys == slim_keys


def test_citation_url_preserved_on_every_hit() -> None:
    response = _two_hit_response()
    for shape in (response.to_dict(), response.to_dict(text_preview_chars=400)):
        for hit in shape["hits"]:
            assert hit["citation_url"], "citation_url must be present on every hit"


def test_references_markdown_byte_identical_between_shapes() -> None:
    response = _two_hit_response()
    full = response.to_dict()
    slim = response.to_dict(text_preview_chars=400)
    assert full.get("references_markdown") == slim.get("references_markdown")
    assert full.get("references_markdown"), "expected refs_md with citation URLs present"


def test_answer_byte_identical_between_shapes() -> None:
    response = _two_hit_response()
    full = response.to_dict()
    slim = response.to_dict(text_preview_chars=400)
    assert full["answer"] == slim["answer"] == "Jane works on health financing."


# ---------------------------------------------------------------------------
# Display-side contracts — the actual slimming behaviour.
# ---------------------------------------------------------------------------


def test_text_preview_truncated_to_400_when_requested() -> None:
    response = _two_hit_response()
    slim = response.to_dict(text_preview_chars=400)
    for hit in slim["hits"]:
        # 400 chars + 1 ellipsis char when truncated.
        assert len(hit["text"]) <= 401, f"slim text too long: {len(hit['text'])}"
        assert hit["text"].endswith("…"), "truncation marker missing"


def test_text_preview_defaults_to_800_for_semantic_search_shape() -> None:
    response = _two_hit_response()
    full = response.to_dict()  # no text_preview_chars → class default 800.
    for hit in full["hits"]:
        assert len(hit["text"]) <= 801
        # Long-text fixture > 800 chars, so ellipsis must be present.
        assert hit["text"].endswith("…")


def test_citations_array_dropped_when_refs_md_present() -> None:
    response = _two_hit_response(with_citation_url=True)
    out = response.to_dict()
    assert out.get("references_markdown"), "fixture should produce refs_md"
    for hit in out["hits"]:
        assert "citations" not in hit, (
            "citations[] must be dropped on hits when references_markdown is present"
        )


def test_citations_array_kept_when_refs_md_absent() -> None:
    """If no hit has a citation_url, refs_md is empty — keep per-hit citations as fallback."""
    response = _two_hit_response(with_citation_url=False)
    out = response.to_dict()
    assert not out.get("references_markdown")
    # No citation_url means hits also have no citations populated in our
    # fixture, so the array is empty but still present.
    for hit in out["hits"]:
        assert "citations" in hit


# ---------------------------------------------------------------------------
# format_response — markdown_table skip when answer succeeds.
# ---------------------------------------------------------------------------


def _hits_for_formatter() -> list[SearchResult]:
    return [_make_hit(email="jane@dalberg.com", score=0.91)]


def test_markdown_table_omitted_when_answer_present(monkeypatch) -> None:
    monkeypatch.setattr(
        "retrieval.formatter.synthesize_answer",
        lambda **_: "Jane works on health financing.",
    )
    monkeypatch.setattr(
        "retrieval.formatter.classify_response_shape",
        lambda **_: {"response_mode": "full_records", "columns": []},
    )
    resp = format_response(
        question="Who works on health financing?",
        hits=_hits_for_formatter(),
        hints=[],
        plan=None,
        allowed_field_names=["Display Name", "Office Location"],
        diagnostics={},
        include_llm_answer=True,
    )
    out = resp.to_dict()
    assert out.get("answer"), "answer should be present"
    assert "markdown_table" not in out, "markdown_table must be omitted when answer present"


def test_markdown_table_present_when_answer_synth_fails(monkeypatch) -> None:
    def boom(**_):
        raise RuntimeError("synth offline")

    monkeypatch.setattr("retrieval.formatter.synthesize_answer", boom)
    monkeypatch.setattr(
        "retrieval.formatter.classify_response_shape",
        lambda **_: {"response_mode": "full_records", "columns": []},
    )
    resp = format_response(
        question="Who works on health financing?",
        hits=_hits_for_formatter(),
        hints=[],
        plan=None,
        allowed_field_names=["Display Name", "Office Location"],
        diagnostics={},
        include_llm_answer=True,
    )
    out = resp.to_dict()
    # Fallback path: no usable answer, so the table must still be emitted.
    assert "markdown_table" in out


def test_markdown_table_present_when_answer_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "retrieval.formatter.classify_response_shape",
        lambda **_: {"response_mode": "full_records", "columns": []},
    )
    resp = format_response(
        question="Who works on health financing?",
        hits=_hits_for_formatter(),
        hints=[],
        plan=None,
        allowed_field_names=["Display Name", "Office Location"],
        diagnostics={},
        include_llm_answer=False,
    )
    out = resp.to_dict()
    assert "markdown_table" in out


# ---------------------------------------------------------------------------
# Payload-size sanity — the whole point of the change.
# ---------------------------------------------------------------------------


def test_slim_payload_is_smaller_than_full() -> None:
    response = _two_hit_response()
    full = json.dumps(response.to_dict(), default=str)
    slim = json.dumps(response.to_dict(text_preview_chars=400), default=str)
    assert len(slim) < len(full), "slim payload must be strictly smaller than full"
