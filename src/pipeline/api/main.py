"""FastAPI application entrypoint.

Hosts two MCP mounts plus a small set of bearer-protected diagnostic
routes. All retrieval logic lives in :mod:`retrieval`; this file only
wires HTTP endpoints to that module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, BaseModel, Field

from pipeline.api.auth import require_api_token
from pipeline.api.entra_oauth import EntraOAuthProvider, PendingLoginNotFound
from pipeline.api.settings import EntraOAuthSettings, get_api_settings, get_entra_oauth_settings
from retrieval.config import LogicalSource, get_registry
from retrieval.mcp.server import build_mcp as build_retrieval_mcp
from retrieval.mcp.tools import (
    airtable_lookup_impl,
    list_sources_impl,
    plan_retrieval_impl,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# FastMCP v2 server — source-agnostic, five tools.
# Public MCP endpoint: https://<host>/mcp/v2/mcp
# v1 legacy mount removed — no existing Claude clients depended on it.
# ---------------------------------------------------------------------------

_mcp_v2 = build_retrieval_mcp()


# ---------------------------------------------------------------------------
# Lifespan — FastMCP streamable HTTP transport requires an anyio task group
# to be running before it can handle requests; without this every request
# raises: RuntimeError: Task group is not initialized. Make sure to use run()
# ---------------------------------------------------------------------------


def _log_citation_mode() -> None:
    """Surface the active citation delivery mode at boot, so the fragile
    raw-presigned mode (breaks with InvalidToken when EC2 role creds rotate) can
    never run silently."""
    from retrieval.citations import active_citation_mode  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    try:
        rt = get_runtime_settings()
    except Exception:  # noqa: BLE001 — never block startup on settings
        return
    mode, base = active_citation_mode(
        signing_secret=rt.citation_signing_secret,
        public_base_url=rt.citation_public_base_url,
    )
    if mode == "short_link":
        log.info("citations: /cite short-link (re-signs fresh per click) ✓", base_url=base)
    else:
        log.warning(
            "citations: RAW PRESIGNED — these break with InvalidToken once EC2 role "
            "creds rotate. Set CITATION_PUBLIC_BASE_URL (real api URL) + "
            "CITATION_SIGNING_SECRET to use /cite.",
            base_url=base or "unset/placeholder",
        )


def _log_entra_oauth_mode() -> None:
    """Surface at boot whether MCP sign-in is actually enforced, so a
    misconfigured deployment (missing AZURE_*/MCP_OAUTH_*/MCP_PUBLIC_BASE_URL)
    can never silently leave /mcp/v2/ open — mirrors _log_citation_mode."""
    settings = get_entra_oauth_settings()
    if settings.is_configured():
        log.info(
            "oauth: Entra ID sign-in enforced on /mcp/v2/ ✓",
            tenant_id=settings.azure_tenant_id,
            issuer_url=settings.oauth_issuer_url,
        )
    else:
        log.warning(
            "oauth: Entra ID sign-in NOT configured — /mcp/v2/ is running WITHOUT "
            "authentication. Set AZURE_TENANT_ID, AZURE_CLIENT_ID, "
            "AZURE_CLIENT_SECRET, MCP_OAUTH_SIGNING_SECRET, and MCP_PUBLIC_BASE_URL "
            "to enforce sign-in before any production deployment.",
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _log_citation_mode()
    _log_entra_oauth_mode()
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(_mcp_v2.session_manager.run())
        yield


app = FastAPI(
    title="Dalberg MCP Pipeline API",
    version="0.3.0",
    lifespan=_lifespan,
    description=(
        "HTTP layer for pipelines and integrations.\n\n"
        "MCP server:\n"
        "  - /mcp/v2/mcp  source-agnostic 'Dalberg Retrieval' MCP — five tools: "
        "list_sources, get_schema, plan_retrieval, semantic_search, airtable_lookup."
    ),
)

# MCP v2 mount — public URL: https://<host>/mcp/v2/mcp
# Sign-in (when configured — see _log_entra_oauth_mode above) is enforced by
# bearer-auth middleware FastMCP wraps around the tool endpoint
# (token_verifier= in retrieval/mcp/server.py:build_mcp). The OAuth
# authorization-server routes themselves (/authorize, /token, /register,
# etc.) are NOT hosted here — see _mount_oauth_routes below for why.
app.mount("/mcp/v2/", _mcp_v2.streamable_http_app())


def _mount_oauth_routes(app: FastAPI, settings: EntraOAuthSettings) -> None:
    """Mount the OAuth authorization-server routes at the domain ROOT.

    MCP/OAuth discovery (RFC 8414 / RFC 9728) is always root-relative to the
    resource server's origin, regardless of where the protected resource
    itself lives. Letting FastMCP host these routes on its own sub-app
    (mounted at /mcp/v2/) put them at /mcp/v2/authorize, /mcp/v2/register,
    etc. — real clients (claude.ai's connector) request
    /.well-known/oauth-authorization-server and /register at the bare
    domain, found nothing there, and every sign-in attempt failed before
    ever reaching Microsoft's login screen (confirmed via production logs;
    see specs/001-mcp-entra-oauth/research.md §7). Bearer-token enforcement
    on the tool endpoint itself is unaffected — that comes from
    ``token_verifier=`` in retrieval/mcp/server.py:build_mcp, independent
    of where these routes live.
    """
    provider = EntraOAuthProvider(settings)
    routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(settings.oauth_issuer_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["openid", "profile", "email"],
            default_scopes=["openid", "profile", "email"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes.extend(
        create_protected_resource_routes(
            resource_url=AnyHttpUrl(settings.oauth_resource_url),
            authorization_servers=[AnyHttpUrl(settings.oauth_issuer_url)],
            scopes_supported=["openid", "profile", "email"],
        )
    )
    app.router.routes.extend(routes)


_entra_settings = get_entra_oauth_settings()
if _entra_settings.is_configured():
    _mount_oauth_routes(app, _entra_settings)


_DEFAULT_SOURCE = "dalberg_profiles"


def _resolve_source(name: str | None = None) -> LogicalSource:
    """Pick the source for diagnostic routes.

    Defaults to ``dalberg_profiles`` when not specified, matching the v1
    behaviour. Raises HTTP 503 if the source is unknown or disabled.
    """
    target = name or _DEFAULT_SOURCE
    try:
        return get_registry().get(target)
    except KeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness for load balancers and Nginx; no authentication."""

    return {"status": "ok"}


@app.get("/varnishcheck", tags=["health"], include_in_schema=False)
async def varnishcheck() -> dict[str, str]:
    """Alias for /health: the Route 53 health check still probes this legacy
    path. Keep until the check is repointed to /health, then delete."""

    return {"status": "ok"}


@app.get(
    "/health/deep",
    tags=["health"],
    dependencies=[Depends(require_api_token)],
)
async def health_deep() -> JSONResponse:
    """Functional health for the CloudWatch Synthetics canary.

    Exercises the real dependency path (OpenSearch cluster + S3) without any
    metered Anthropic/Voyage calls. Bearer-protected — unlike /health — so the
    open internet can't use it to drive AWS calls. Returns 503 when any
    dependency check fails, which is what the canary alarms on. Failure detail
    goes to the log only, never the response body.
    """
    import os  # noqa: PLC0415

    from pipeline.common.aws import s3_client  # noqa: PLC0415
    from pipeline.common.opensearch import build_opensearch_client  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    rt = get_runtime_settings()

    def _check_opensearch() -> str:
        if not rt.opensearch_endpoint:
            return "skipped (OPENSEARCH_ENDPOINT not set)"
        client = build_opensearch_client(
            rt.opensearch_endpoint,
            username=rt.opensearch_username,
            password=rt.opensearch_password,
            aws_region=rt.aws_region,
        )
        # yellow is normal for a single-node dev domain; only red is a failure.
        cluster_status = client.cluster.health().get("status", "unknown")
        return "ok" if cluster_status in ("green", "yellow") else f"error (cluster {cluster_status})"

    def _check_s3() -> str:
        bucket = (os.environ.get("S3_BUCKET") or "").strip()
        if not bucket:
            return "skipped (S3_BUCKET not set)"
        s3_client(region_name=rt.aws_region).head_bucket(Bucket=bucket)
        return "ok"

    checks: dict[str, str] = {}
    for name, fn in (("opensearch", _check_opensearch), ("s3", _check_s3)):
        try:
            checks[name] = await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001
            log.warning("health_deep_check_failed", check=name, error=str(exc))
            checks[name] = "error"

    healthy = all(not v.startswith("error") for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@app.get("/oauth/callback", tags=["oauth"], include_in_schema=False)
async def oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Microsoft's redirect target after the user completes sign-in.

    PUBLIC by necessity — same as any OAuth authorization server's callback
    endpoint. The ``state`` parameter is our own single-use, server-generated
    value (not guessable), so a request with an unknown/expired ``state`` is
    rejected outright rather than trusted (spec edge case: forged callback).
    """
    settings = get_entra_oauth_settings()
    if not settings.is_configured():
        raise HTTPException(status_code=404, detail="OAuth sign-in is not enabled")

    provider = EntraOAuthProvider(settings)
    try:
        redirect_url = provider.complete_microsoft_login(
            ms_state=state, code=code, error=error, error_description=error_description
        )
    except PendingLoginNotFound:
        return PlainTextResponse("Sign-in request expired or invalid. Please try again.", status_code=400)
    return RedirectResponse(redirect_url, status_code=302)


@app.get("/cite/{token}", tags=["citations"])
async def cite(token: str, download: bool = Query(False)):
    """Resolve an opaque citation token to the original S3 document.

    PUBLIC by design — the HMAC-signed token IS the authorization, scoped to a
    single object for a bounded time. Default: 302-redirect to a fresh
    short-lived presigned URL (S3 serves the bytes). ``?download=1`` streams the
    file through the API (proxy mode), keeping S3 fully private.
    """
    from pipeline.common.aws import generate_presigned_url, s3_client  # noqa: PLC0415
    from retrieval.citation_token import verify_citation_token  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    rt = get_runtime_settings()
    if not rt.citation_signing_secret:
        raise HTTPException(status_code=404, detail="Citation links are not enabled")

    verified = verify_citation_token(token, secret=rt.citation_signing_secret)
    if verified is None:
        raise HTTPException(status_code=404, detail="Invalid or expired citation link")
    bucket, key = verified
    # Scope guard: only objects under the raw/ ingestion prefix, no traversal.
    if not key.startswith("raw/") or ".." in key:
        raise HTTPException(status_code=403, detail="Citation key out of scope")

    if download and rt.citation_proxy_enabled:
        try:
            obj = s3_client(region_name=rt.aws_region).get_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="Source document not found") from exc
        leaf = key.rsplit("/", 1)[-1]
        nice_name = leaf.split("__", 1)[-1] if "__" in leaf else leaf  # drop att-id prefix
        return StreamingResponse(
            obj["Body"].iter_chunks(),
            media_type=obj.get("ContentType") or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{nice_name}"'},
        )

    url = generate_presigned_url(
        key,
        bucket=bucket,
        expiry_seconds=rt.citation_expiry_seconds,
        region_name=rt.aws_region,
    )
    if not url:
        raise HTTPException(status_code=404, detail="Could not sign source document")
    return RedirectResponse(url, status_code=302)


@app.get(
    "/v1/status",
    tags=["status"],
    dependencies=[Depends(require_api_token)],
)
async def status() -> dict[str, Any]:
    """Example protected route; requires ``Authorization: Bearer <API_BEARER_TOKEN>``."""

    return {"status": "ready", "mcp_mounts": ["/mcp/v2/mcp"]}


# --- Local / integration testing (Bearer required) ----------------------------


@app.get(
    "/v1/test/airtable/ping",
    tags=["airtable-test"],
    dependencies=[Depends(require_api_token)],
    summary="Verify the configured retrieval source loads its schema and Airtable returns at least one row",
)
async def airtable_ping(
    source: str | None = Query(default=None, description="Source name (defaults to dalberg_profiles)"),
) -> dict[str, Any]:
    logical = _resolve_source(source)
    if logical.airtable is None:
        raise HTTPException(status_code=503, detail=f"source {logical.name!r} has no Airtable adapter")
    try:
        schema = logical.get_schema()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})
    try:
        await logical.airtable.filter_structured(formula=None, fields=None, max_records=1)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": f"Schema ok but Airtable request failed: {exc}",
            },
        )
    return {
        "ok": True,
        "source": logical.name,
        "display_name": logical.display_name,
        "field_count": len(schema.fields),
        "long_text_fields": list(schema.long_text_fields),
    }


@app.get(
    "/v1/test/airtable/schema",
    tags=["airtable-test"],
    dependencies=[Depends(require_api_token)],
    summary="Return the merged schema descriptor for a source (Airtable + OpenSearch metadata)",
)
async def airtable_schema(
    source: str | None = Query(default=None),
) -> dict[str, Any]:
    logical = _resolve_source(source)
    return logical.get_schema().to_dict()


@app.get(
    "/v1/test/airtable/records",
    tags=["airtable-test"],
    dependencies=[Depends(require_api_token)],
    summary="Run airtable_lookup against a source",
)
async def airtable_records(
    source: str | None = Query(default=None),
    max_records: int = Query(default=10, ge=1),
    formula: str | None = Query(default=None, description="Airtable filterByFormula expression."),
) -> dict[str, Any]:
    logical = _resolve_source(source)
    payload = await asyncio.to_thread(
        airtable_lookup_impl,
        source=logical.name,
        formula=formula,
        max_records=max_records,
    )
    return json.loads(payload)


@app.get(
    "/v1/test/mcp",
    tags=["mcp-test"],
    dependencies=[Depends(require_api_token)],
    summary="Where the MCP streamable HTTP apps are mounted (for local Claude / MCP clients)",
)
async def mcp_test_mount() -> dict[str, Any]:
    return {
        "mounts": {
            "v2_retrieval": {
                "path": "/mcp/v2/mcp",
                "tools": [
                    "list_sources",
                    "get_schema",
                    "plan_retrieval",
                    "semantic_search",
                    "airtable_lookup",
                ],
                "status": "current",
            },
        },
        "note": "MCP JSON-RPC over streamable HTTP. Connect Claude to https://mcp.dev.dalberg.com/mcp/v2/mcp",
    }


@app.get(
    "/v1/test/sources",
    tags=["mcp-test"],
    dependencies=[Depends(require_api_token)],
    summary="Return the v2 list_sources payload (mirrors the MCP tool of the same name)",
)
async def list_sources_route() -> dict[str, Any]:
    return json.loads(list_sources_impl())


class NaturalLanguageQueryBody(BaseModel):
    """Body for the v2-backed plan_retrieval route."""

    question: str = Field(..., min_length=1, description="User question in natural language.")
    sources: list[str] | None = Field(default=None, description="Subset of sources to query; ['*'] for all.")


@app.post(
    "/v1/test/airtable/nl-query",
    tags=["airtable-test"],
    dependencies=[Depends(require_api_token)],
    summary="Plan NL retrieval (forwards to v2 `plan_retrieval`) — returns suggested_calls, no synthesis",
)
async def airtable_nl_query(body: NaturalLanguageQueryBody) -> dict[str, Any]:
    sources = body.sources or [_DEFAULT_SOURCE]
    try:
        payload = await asyncio.to_thread(
            plan_retrieval_impl,
            question=body.question.strip(),
            sources=sources,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return json.loads(payload)
