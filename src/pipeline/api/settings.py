"""API-specific settings (environment)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_ENV = _REPO_ROOT / ".env"


class ApiSettings(BaseSettings):
    """Settings read from the environment with ``API_`` prefix."""

    model_config = SettingsConfigDict(
        env_file=(_REPO_ENV, ".env"),
        env_file_encoding="utf-8",
        env_prefix="API_",
        extra="ignore",
    )

    bearer_token: str = Field(
        ...,
        description="Long-lived secret sent as Authorization: Bearer <token>.",
    )


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()


class EntraOAuthSettings(BaseSettings):
    """Microsoft Entra ID OAuth settings for MCP sign-in.

    Unprefixed (``AZURE_*`` / ``MCP_OAUTH_*``), unlike :class:`ApiSettings`,
    to match the names already used in the Entra ID App Registration and
    communicated to devops — an ``API_`` prefix would be misleading here
    since these aren't specific to the diagnostic HTTP routes.
    """

    model_config = SettingsConfigDict(
        env_file=(_REPO_ENV, ".env"),
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    azure_tenant_id: str = Field(
        default="", description="Entra ID tenant ID for the MCP Server app registration."
    )
    azure_client_id: str = Field(default="", description="Entra ID application (client) ID.")
    azure_client_secret: str = Field(
        default="", description="Entra ID client secret (confidential client)."
    )
    mcp_oauth_signing_secret: str = Field(
        default="",
        description="HMAC signing secret for self-contained MCP access tokens.",
    )
    mcp_public_base_url: str = Field(
        default="",
        description=(
            "Public HTTPS base URL this API is reachable at (e.g. "
            "https://mcp.dev.dalberg.com, no trailing slash) — used to derive "
            "the OAuth issuer URL and the Microsoft redirect_uri."
        ),
    )

    def is_configured(self) -> bool:
        return bool(
            self.azure_tenant_id
            and self.azure_client_id
            and self.azure_client_secret
            and self.mcp_oauth_signing_secret
            and self.mcp_public_base_url
        )

    @property
    def oauth_issuer_url(self) -> str:
        """Root of this API — OAuth discovery (RFC 8414/9728) is always
        root-relative to the resource server's origin, regardless of where
        the MCP tool endpoint itself is mounted, so /authorize, /token,
        /register, and /.well-known/* all live here, not under /mcp/v2."""
        return self.mcp_public_base_url.rstrip("/")

    @property
    def oauth_resource_url(self) -> str:
        return f"{self.mcp_public_base_url.rstrip('/')}/mcp/v2/mcp"

    @property
    def oauth_callback_url(self) -> str:
        return f"{self.mcp_public_base_url.rstrip('/')}/oauth/callback"


@lru_cache
def get_entra_oauth_settings() -> EntraOAuthSettings:
    return EntraOAuthSettings()
