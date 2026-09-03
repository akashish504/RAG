"""Process-level settings for the retrieval module.

Per-source connection details live in ``config/retrieval_sources.yaml``.
This file holds the *cross-source* secrets and tunables read from env:
Airtable PAT, Voyage key, OpenSearch endpoint/credentials, AWS region.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class RetrievalRuntimeSettings:
    """Runtime secrets shared by all source adapters."""

    airtable_pat_token: str
    voyage_api_key: str | None
    voyage_model: str
    voyage_dims: int
    opensearch_endpoint: str
    opensearch_username: str | None
    opensearch_password: str | None
    aws_region: str
    citation_expiry_seconds: int
    # Short-link citation endpoint. When both secret + base_url are set, the
    # citation resolver emits "<base_url>/cite/<token>" instead of a raw
    # presigned URL. citation_link_ttl is how long that /cite link stays valid
    # (the underlying presigned URL it redirects to uses citation_expiry).
    citation_signing_secret: str | None
    citation_public_base_url: str | None
    citation_link_ttl_seconds: int
    citation_proxy_enabled: bool
    # When False (default), Airtable citations are suppressed at the output layer —
    # search results cite ONLY S3 sources, and airtable_lookup returns no citations.
    # All Airtable citation CODE stays intact; flip AIRTABLE_CITATIONS_ENABLED=1 to
    # restore them with no code change.
    airtable_citations_enabled: bool


def _require(name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value:
            return value
    msg = f"Missing required env var: {name}"
    if aliases:
        msg += f" (aliases: {', '.join(aliases)})"
    raise RuntimeError(msg)


def _optional(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@lru_cache
def get_runtime_settings() -> RetrievalRuntimeSettings:
    """Build settings from process env, cached for the lifetime of the worker."""

    return RetrievalRuntimeSettings(
        airtable_pat_token=_require("AIRTABLE_PAT_TOKEN", "PAT_TOKEN", "AIRTABLE_API_KEY"),
        voyage_api_key=_optional("VOYAGE_API_KEY"),
        voyage_model=os.environ.get("VOYAGE_MODEL", "voyage-4"),
        voyage_dims=int(os.environ.get("VOYAGE_EMBED_DIMS", "1024")),
        opensearch_endpoint=os.environ.get("OPENSEARCH_ENDPOINT", ""),
        opensearch_username=_optional("OPENSEARCH_USERNAME"),
        opensearch_password=_optional("OPENSEARCH_PASSWORD"),
        aws_region=os.environ.get("AWS_REGION", "eu-west-1"),
        citation_expiry_seconds=int(os.environ.get("S3_CITATION_EXPIRY_SECONDS", "3600")),
        citation_signing_secret=_optional("CITATION_SIGNING_SECRET"),
        citation_public_base_url=_optional("CITATION_PUBLIC_BASE_URL"),
        citation_link_ttl_seconds=int(os.environ.get("CITATION_LINK_TTL_SECONDS", "86400")),
        citation_proxy_enabled=os.environ.get("CITATION_PROXY_ENABLED", "1").strip()
        not in ("0", "false", "False", ""),
        airtable_citations_enabled=os.environ.get("AIRTABLE_CITATIONS_ENABLED", "0").strip()
        in ("1", "true", "True", "yes"),
    )
