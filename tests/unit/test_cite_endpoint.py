"""Tests for the public /cite/{token} citation handler.

Calls the route coroutine directly to avoid the MCP streamable-HTTP lifespan,
which is incompatible with TestClient in this environment (see test_api_auth).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from pipeline.api.main import cite
from retrieval.citation_token import make_citation_token

_SECRET = "cite-test-secret"
_BUCKET = "claude-mcp-object-store"
_KEY = "raw/dalberg_profiles/p.pena@dalberg.com/cv_attachment/attX__pablo_cv.docx"


def _with_secret(secret: str | None = _SECRET):
    """Set the runtime settings env and clear the cache."""
    import os

    from retrieval import settings as rt_settings

    os.environ["AIRTABLE_PAT_TOKEN"] = "pat-x"
    if secret is None:
        os.environ.pop("CITATION_SIGNING_SECRET", None)
    else:
        os.environ["CITATION_SIGNING_SECRET"] = secret
    rt_settings.get_runtime_settings.cache_clear()


def test_valid_token_redirects_to_presigned() -> None:
    _with_secret()
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    with patch(
        "pipeline.common.aws.generate_presigned_url",
        return_value="https://s3.example/raw/...docx?sig",
    ):
        resp = asyncio.run(cite(token=tok, download=False))
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://s3.example/")


def test_tampered_token_404() -> None:
    _with_secret()
    tok = make_citation_token(_KEY, bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cite(token=tok + "xx", download=False))
    assert exc.value.status_code == 404


def test_key_out_of_scope_403() -> None:
    _with_secret()
    bad = make_citation_token("secrets/admin.txt", bucket=_BUCKET, ttl_seconds=3600, secret=_SECRET)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cite(token=bad, download=False))
    assert exc.value.status_code == 403


def test_disabled_when_no_secret() -> None:
    _with_secret(secret=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cite(token="anything", download=False))
    assert exc.value.status_code == 404
    _with_secret()  # restore for other tests
