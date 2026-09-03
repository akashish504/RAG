"""get_document_link: mint a durable link from an S3 document path.

Scope guard mirrors the /cite endpoint (raw/ only, no traversal); the derived-
artifact guardrail refuses .txt keys — only original source documents are
citable. Short-link mode is pure HMAC (hermetic, no AWS).
"""

from __future__ import annotations

import json

import pytest

from retrieval.citation_token import verify_citation_token
from retrieval.citations import build_document_link

_SECRET = "test-secret"
_BASE = "https://mcp.example.com"


def _link(key: str, **overrides):
    kwargs = {
        "bucket": "claude-mcp-object-store",
        "signing_secret": _SECRET,
        "public_base_url": _BASE,
        "link_ttl_seconds": 3600,
    }
    kwargs.update(overrides)
    return build_document_link(key, **kwargs)


def test_valid_key_returns_verifiable_short_link() -> None:
    payload = _link("raw/d.quals/85/att/deck.pptx")
    assert payload["mode"] == "short_link"
    assert payload["url"].startswith(f"{_BASE}/cite/")
    token = payload["url"].rsplit("/cite/", 1)[1]
    verified = verify_citation_token(token, secret=_SECRET)
    assert verified == ("claude-mcp-object-store", "raw/d.quals/85/att/deck.pptx")


def test_full_s3_url_is_tolerated() -> None:
    payload = _link("s3://claude-mcp-object-store/raw/d.quals/85/att/deck.pdf")
    assert payload["s3_key"] == "raw/d.quals/85/att/deck.pdf"
    assert payload["mode"] == "short_link"


def test_key_outside_raw_is_rejected() -> None:
    with pytest.raises(ValueError, match="raw/"):
        _link("metadata/airtable_schema/x.json")


def test_traversal_is_rejected() -> None:
    with pytest.raises(ValueError, match="raw/"):
        _link("raw/../secrets/creds")


def test_derived_txt_is_rejected() -> None:
    # Same guardrail as the citation resolver: .txt files are pipeline artifacts.
    for key in (
        "raw/d.quals/85/__record_summary.txt",
        "raw/d.quals/85/att/x__normalized.txt",
    ):
        with pytest.raises(ValueError, match="derived artifact"):
            _link(key)


def test_presigned_fallback_carries_fragility_warning(monkeypatch) -> None:
    import pipeline.common.aws as aws

    monkeypatch.setattr(
        aws, "generate_presigned_url",
        lambda key, *, bucket, expiry_seconds, region_name, **kw:
            f"https://{bucket}.s3.amazonaws.com/{key}?sig",
    )
    payload = _link("raw/d.quals/85/att/deck.pptx", signing_secret=None)
    assert payload["mode"] == "presigned"
    assert "warning" in payload and "rotate" in payload["warning"]


def test_impl_rejects_empty_key() -> None:
    from retrieval.mcp.tools import get_document_link_impl

    out = json.loads(get_document_link_impl(""))
    assert out["ok"] is False
    assert "s3_key" in out["error"]
