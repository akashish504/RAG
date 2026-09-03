"""Unit tests for citation building and Airtable URL resolution helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from retrieval.citations import (
    CitationResolver,
    S3CitationResolver,
    SourceCitationContext,
    append_references_to_answer,
    build_airtable_record_url,
    build_batch_identifier_formula,
    build_citation_resolve_warning,
    build_identifier_formula,
    build_markdown_references_section,
    build_semantic_citation_label,
    column_display_name,
    escape_airtable_formula_string,
)
from retrieval.models import Citation, SearchResponse, SearchResult, redact_metadata_for_response


def test_escape_airtable_formula_string() -> None:
    assert escape_airtable_formula_string("a'b") == "a\\'b"


def test_build_identifier_formula() -> None:
    assert build_identifier_formula("Email", "jane@dalberg.com") == (
        "LOWER({Email}) = 'jane@dalberg.com'"
    )
    assert build_identifier_formula("Email", "Jeff.Berger@dalberg.com") == (
        "LOWER({Email}) = 'jeff.berger@dalberg.com'"
    )


def test_build_batch_identifier_formula() -> None:
    formula = build_batch_identifier_formula(
        "Email",
        ["Jeff.Berger@dalberg.com", "jane@dalberg.com"],
    )
    assert formula.startswith("OR(")
    assert "LOWER({Email}) = 'jeff.berger@dalberg.com'" in formula
    assert "LOWER({Email}) = 'jane@dalberg.com'" in formula
    single = build_batch_identifier_formula("Email", ["a@b.com"])
    assert single == "LOWER({Email}) = 'a@b.com'"


def test_build_semantic_citation_label_display_name_override() -> None:
    label = build_semantic_citation_label(
        {"primary_key": "jane@dalberg.com", "column_name": "cv_attachment"},
        display_name="Jane Doe",
    )
    assert label.startswith("Jane Doe")


def test_build_citation_resolve_warning() -> None:
    assert build_citation_resolve_warning({}) is None
    warn = build_citation_resolve_warning(
        {"citations": {"dalberg_profiles": {"failed": ["a@b.com", "c@d.com"]}}}
    )
    assert warn is not None
    assert "2 profile" in warn


def test_search_response_citation_resolve_warning() -> None:
    resp = SearchResponse(
        ok=True,
        diagnostics={"citations": {"dalberg_profiles": {"failed": ["x@y.com"]}}},
    )
    d = resp.to_dict()
    assert "citation_resolve_warning" in d
    assert "1 profile" in d["citation_resolve_warning"]


def test_citation_resolver_batch_lookup_mixed_case() -> None:
    airtable = MagicMock()
    airtable._fetch_rows.return_value = [
        {
            "id": "recABC123",
            "fields": {
                "Email": "Jeff.Berger@dalberg.com",
                "Display Name": "Jeff Berger",
            },
        }
    ]
    ctx = SourceCitationContext(
        source_name="dalberg_profiles",
        base_id="appX",
        table_id="tblY",
        identifier_field="Email",
        airtable=airtable,
    )
    resolver = CitationResolver()
    hit = SearchResult(
        source="dalberg_profiles",
        source_type="semantic",
        score=0.9,
        text="experience at World Bank",
        metadata={
            "primary_key": "jeff.berger@dalberg.com",
            "column_name": "cv_attachment",
            "section_canonical": "experience",
        },
    )
    diag = asyncio.run(resolver.resolve_semantic_hits([hit], ctx))
    assert diag["failed"] == []
    assert hit.citation_url == "https://airtable.com/appX/tblY/recABC123"
    assert hit.citations[0].label.startswith("Jeff Berger")
    formula = airtable._fetch_rows.call_args[0][0]["formula"]
    assert "LOWER({Email})" in formula


def test_build_airtable_record_url() -> None:
    url = build_airtable_record_url(
        base_id="appXXX",
        table_id="tblYYY",
        record_id="recZZZ",
    )
    assert url == "https://airtable.com/appXXX/tblYYY/recZZZ"


def test_column_display_name_slug_map() -> None:
    assert column_display_name("cv_attachment") == "CV Attachment"
    assert column_display_name("bio_attachment") == "Bio Attachment"


def test_build_semantic_citation_label() -> None:
    label = build_semantic_citation_label(
        {
            "primary_key": "jane@dalberg.com",
            "column_name": "cv_attachment",
            "section_canonical": "experience",
        }
    )
    assert "jane@dalberg.com" in label or "CV Attachment" in label
    assert "experience" in label.lower() or "Experience" in label


def test_redact_metadata_strips_s3() -> None:
    meta = {
        "primary_key": "a@b.com",
        "s3_key": "raw/x",
        "source_url": "s3://bucket/key",
        "section_canonical": "bio",
    }
    redacted = redact_metadata_for_response(meta)
    assert "s3_key" not in redacted
    assert "source_url" not in redacted
    assert redacted["primary_key"] == "a@b.com"


def test_redact_metadata_strips_real_semantic_hit_shape() -> None:
    """Cover nested chunker metadata from live semantic_search samples."""
    meta = {
        "primary_key": "esha.rao@dalberg.com",
        "column_name": "cv_attachment",
        "section_canonical": "unknown",
        "chunk_index": 0,
        "section_level": 0,
        "is_list_section": False,
        "s3_path": "raw/dalberg_profiles/esha.rao@dalberg.com/cv_attachment/file.txt",
        "document_id": "esha.rao@dalberg.com",
        "child_index_in_parent": 0,
        "section_path": [],
        "strategy": "resume",
    }
    redacted = redact_metadata_for_response(meta)
    assert redacted == {"primary_key": "esha.rao@dalberg.com", "section_canonical": "unknown"}


def test_citation_to_dict_slims_locator() -> None:
    cite = Citation(
        cite_id="1",
        kind="airtable_record",
        label="Jane — CV",
        url="https://airtable.com/app/tbl/rec",
        locator={
            "primary_key": "jane@dalberg.com",
            "record_id": "recABC",
            "attachment_column": "CV Attachment",
            "section_canonical": "experience",
            "chunk_id": "abc:child:0:0",
            "column_name": "cv_attachment",
        },
    )
    d = cite.to_dict()
    assert d["locator"] == {
        "attachment_column": "CV Attachment",
        "section_canonical": "experience",
    }
    assert "chunk_id" not in d["locator"]
    assert "record_id" not in d["locator"]


def test_build_markdown_references_section() -> None:
    md = build_markdown_references_section(
        [
            {
                "cite_id": "1",
                "label": "Jane — CV",
                "url": "https://airtable.com/app/tbl/recABC",
            }
        ]
    )
    assert "## References" in md
    assert "airtable.com" in md
    assert "[1]" in md


def test_append_references_to_answer() -> None:
    refs = [{"cite_id": "1", "label": "Jane", "url": "https://airtable.com/x"}]
    out = append_references_to_answer("Summary text.", refs)
    assert out is not None
    assert "Summary text." in out
    assert "## References" in out


# ---------------------------------------------------------------------------
# GUARDRAIL: derived artifacts (summary chunks, .txt files) are NEVER cited.
# ---------------------------------------------------------------------------


def _semantic_hit(meta: dict) -> SearchResult:
    return SearchResult(
        source="d_quals", source_type="semantic", score=0.9, text="t", metadata=meta
    )


def _s3_resolver() -> S3CitationResolver:
    # Short-link mode: token minting is pure HMAC — hermetic, no AWS calls.
    return S3CitationResolver(
        signing_secret="test-secret", public_base_url="https://mcp.example.com"
    )


def test_summary_chunks_are_never_cited() -> None:
    hits = [
        _semantic_hit({
            "doc_role": "record_summary",
            "s3_key": "raw/d.quals/85/__record_summary.txt",
            "s3_bucket": "b",
        }),
        _semantic_hit({
            "doc_role": "deck_summary",
            "s3_key": "raw/d.quals/85/att/x__normalized.txt",
            "s3_bucket": "b",
        }),
    ]
    diag = _s3_resolver().resolve_semantic_hits(hits)
    assert diag["skipped_derived"] == 2
    assert diag["resolved"] == 0
    for h in hits:
        assert h.citation_url is None
        assert h.citations == []


def test_txt_without_sibling_original_gets_no_citation(monkeypatch) -> None:
    import pipeline.common.aws as aws

    # Directory holds only derived artifacts — no original document.
    monkeypatch.setattr(
        aws, "list_s3_keys",
        lambda prefix, *, bucket, region_name="eu-west-1", **kw: [
            "raw/d.quals/85/att/x__normalized.txt",
            "raw/d.quals/85/att/.airtable_meta.json",
        ],
    )
    hit = _semantic_hit({
        "s3_key": "raw/d.quals/85/att/x__normalized.txt", "s3_bucket": "b",
    })
    diag = _s3_resolver().resolve_semantic_hits([hit])
    assert diag["skipped_derived"] == 1
    assert hit.citation_url is None  # NO .txt citation, ever
    assert hit.citations == []


def test_txt_with_sibling_original_cites_the_original(monkeypatch) -> None:
    import pipeline.common.aws as aws

    monkeypatch.setattr(
        aws, "list_s3_keys",
        lambda prefix, *, bucket, region_name="eu-west-1", **kw: [
            "raw/d.quals/85/att/x__normalized.txt",
            "raw/d.quals/85/att/deck.pptx",
        ],
    )
    hit = _semantic_hit({
        "s3_key": "raw/d.quals/85/att/x__normalized.txt",
        "s3_bucket": "b",
        "primary_key": "85",
    })
    diag = _s3_resolver().resolve_semantic_hits([hit])
    assert diag["resolved"] == 1
    assert hit.citation_url is not None and "/cite/" in hit.citation_url
    assert hit.metadata["source_s3_key"].endswith("deck.pptx")


def test_stored_source_key_pointing_at_txt_is_re_resolved(monkeypatch) -> None:
    import pipeline.common.aws as aws

    # Older data: sidecar's source_s3_key wrongly points at the normalized .txt.
    monkeypatch.setattr(
        aws, "list_s3_keys",
        lambda prefix, *, bucket, region_name="eu-west-1", **kw: [
            "raw/d.quals/85/att/x__normalized.txt",
            "raw/d.quals/85/att/report.pdf",
        ],
    )
    hit = _semantic_hit({
        "s3_key": "raw/d.quals/85/att/x__normalized.txt",
        "s3_bucket": "b",
        "source_s3_key": "raw/d.quals/85/att/x__normalized.txt",
    })
    diag = _s3_resolver().resolve_semantic_hits([hit])
    assert diag["resolved"] == 1
    assert hit.metadata["source_s3_key"].endswith("report.pdf")


def test_stored_original_source_key_used_directly() -> None:
    hit = _semantic_hit({
        "s3_key": "raw/d.quals/85/att/x__normalized.txt",
        "s3_bucket": "b",
        "source_s3_key": "raw/d.quals/85/att/deck.pdf",
    })
    diag = _s3_resolver().resolve_semantic_hits([hit])
    assert diag["resolved"] == 1
    assert hit.citation_url is not None and "/cite/" in hit.citation_url


def test_search_result_to_dict_redacts_s3() -> None:
    hit = SearchResult(
        source="dalberg_profiles",
        source_type="semantic",
        score=0.9,
        text="sample",
        metadata={
            "primary_key": "x@y.com",
            "s3_key": "raw/k",
            "s3_path": "raw/dalberg_profiles/x@y.com/cv.txt",
            "source_url": "s3://b/k",
            "chunk_index": 0,
            "strategy": "resume",
        },
        citations=[
            Citation(
                cite_id="1",
                kind="airtable_record",
                label="Test",
                url="https://airtable.com/app/tbl/rec",
                locator={
                    "primary_key": "x@y.com",
                    "record_id": "recXYZ",
                    "attachment_column": "CV Attachment",
                    "section_canonical": "bio",
                    "chunk_id": "deadbeef:child:0:0",
                    "column_name": "cv_attachment",
                },
            )
        ],
        citation_url="https://airtable.com/app/tbl/rec",
    )
    d = hit.to_dict()
    assert "s3_key" not in d["metadata"]
    assert "s3_path" not in d["metadata"]
    assert "chunk_index" not in d["metadata"]
    assert d["metadata"]["primary_key"] == "x@y.com"
    assert d["citation_url"].startswith("https://airtable.com")
    assert d["citations"][0]["locator"] == {
        "attachment_column": "CV Attachment",
        "section_canonical": "bio",
    }
