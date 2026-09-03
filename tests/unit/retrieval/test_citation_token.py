"""Tests for opaque HMAC citation tokens and short-link emission."""

from __future__ import annotations

from unittest.mock import patch

from retrieval.citation_token import make_citation_token, verify_citation_token
from retrieval.citations import S3CitationResolver
from retrieval.models import SearchResult

_SECRET = "test-secret-please-rotate"
_KEY = "raw/dalberg_profiles/pablo.pena@dalberg.com/cv_attachment/attX__pablo_cv.docx"
_BUCKET = "claude-mcp-object-store"


def test_token_roundtrip() -> None:
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    out = verify_citation_token(tok, secret=_SECRET)
    assert out == (_BUCKET, _KEY)


def test_token_is_compact() -> None:
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    # The token + a base URL is far shorter than a raw STS presigned URL (~1800).
    assert len(tok) < 400


def test_tampered_token_rejected() -> None:
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    tampered = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    assert verify_citation_token(tampered, secret=_SECRET) is None


def test_wrong_secret_rejected() -> None:
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    assert verify_citation_token(tok, secret="other-secret") is None


def test_expired_token_rejected() -> None:
    # issued at now=1000 with ttl 10 → expires at 1010; verify at now=2000.
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=10, secret=_SECRET, now=1000)
    assert verify_citation_token(tok, secret=_SECRET, now=2000) is None
    # still valid before expiry
    assert verify_citation_token(tok, secret=_SECRET, now=1005) == (_BUCKET, _KEY)


def test_garbage_token_rejected() -> None:
    assert verify_citation_token("not-a-token", secret=_SECRET) is None
    assert verify_citation_token("", secret=_SECRET) is None


# ---------------------------------------------------------------------------
# Resolver emits short links when configured, presigned URLs otherwise
# ---------------------------------------------------------------------------


def _hit() -> SearchResult:
    return SearchResult(
        source="dalberg_profiles", source_type="semantic", score=0.9, text="...",
        metadata={"s3_key": _KEY, "s3_bucket": _BUCKET, "source_s3_key": _KEY,
                  "primary_key": "pablo.pena@dalberg.com"},
    )


def test_resolver_emits_short_link_when_configured() -> None:
    h = _hit()
    resolver = S3CitationResolver(
        signing_secret=_SECRET,
        public_base_url="https://mcp.example.com/",
        link_ttl_seconds=86400,
    )
    # No S3 presigning needed in short-link mode.
    with patch("pipeline.common.aws.generate_presigned_url") as mp:
        diag = resolver.resolve_semantic_hits([h])
    assert mp.call_count == 0
    assert diag["mode"] == "short_link"
    assert h.citation_url.startswith("https://mcp.example.com/cite/")
    assert len(h.citation_url) < 450
    # token round-trips back to the original document key
    tok = h.citation_url.rsplit("/cite/", 1)[1]
    assert verify_citation_token(tok, secret=_SECRET) == (_BUCKET, _KEY)


def test_resolver_falls_back_to_presigned_when_unconfigured() -> None:
    h = _hit()
    resolver = S3CitationResolver()  # no secret/base_url
    with patch("pipeline.common.aws.generate_presigned_url",
               side_effect=lambda k, **kw: f"https://s3/{k}?sig") as mp:
        diag = resolver.resolve_semantic_hits([h])
    assert mp.call_count == 1
    assert diag["mode"] == "presigned"
    assert h.citation_url == f"https://s3/{_KEY}?sig"
