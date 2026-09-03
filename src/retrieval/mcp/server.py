"""FastMCP server for the retrieval module — five source-agnostic tools."""

from __future__ import annotations

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from pipeline.api.entra_oauth import EntraOAuthProvider, EntraTokenVerifier
from pipeline.api.settings import get_entra_oauth_settings
from retrieval.mcp.routing_prompts import (
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_NAME,
)
from retrieval.mcp.tools import register_tools


def build_mcp() -> FastMCP:
    """Build the v2 retrieval MCP server.

    Mounted by :mod:`pipeline.api.main` at ``/mcp/v2/``. The server is
    deliberately source-agnostic: adding ``d_quals`` or ``proposal_library``
    to ``config/retrieval_sources.yaml`` makes them visible via
    ``list_sources()`` without any code change here.

    Sign-in: when Entra ID OAuth settings (``AZURE_*`` / ``MCP_OAUTH_*`` /
    ``MCP_PUBLIC_BASE_URL``) are fully configured, every MCP tool call
    requires a valid access token minted through Microsoft sign-in — see
    ``pipeline.api.entra_oauth``. When they are NOT configured (e.g. local
    dev without an Entra app registration), the mount falls back to no auth
    at all, exactly like before this feature existed — this is a deliberate,
    loudly-flagged (``main.py:_log_entra_oauth_mode``) development
    convenience, not a silent gap: production has these values set.

    IMPORTANT: this passes ``token_verifier=`` (verification only), NOT
    ``auth_server_provider=``. FastMCP registers /authorize, /token,
    /register, and /.well-known/* *relative to wherever the sub-app is
    mounted* — i.e. under /mcp/v2/ — but MCP/OAuth discovery (RFC 8414 /
    9728) is always root-relative to the resource server's origin, so real
    clients look for those routes at the domain root and 404 if FastMCP
    hosts them here (found in production — see
    specs/001-mcp-entra-oauth/research.md §7). Those routes are mounted at
    root instead, directly on the top-level FastAPI app — see
    main.py's ``_mount_oauth_routes``.
    """

    entra_settings = get_entra_oauth_settings()
    token_verifier: TokenVerifier | None = None
    auth_settings: AuthSettings | None = None
    if entra_settings.is_configured():
        token_verifier = EntraTokenVerifier(EntraOAuthProvider(entra_settings))
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(entra_settings.oauth_issuer_url),
            resource_server_url=AnyHttpUrl(entra_settings.oauth_resource_url),
        )

    mcp = FastMCP(
        MCP_SERVER_NAME,
        stateless_http=True,
        # MCP SDK v1.23+ enables DNS rebinding protection by default, rejecting
        # any Host header not matching "127.0.0.1:*" / "localhost:*". We run
        # behind nginx which rewrites the Host header and controls external
        # access, so the SDK-level check is redundant and safe to disable.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
        instructions=MCP_SERVER_INSTRUCTIONS,
        token_verifier=token_verifier,
        auth=auth_settings,
    )
    register_tools(mcp)
    return mcp
