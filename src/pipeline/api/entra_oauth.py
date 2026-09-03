"""Microsoft Entra ID OAuth authorization server for the MCP retrieval server.

Implements :class:`mcp.server.auth.provider.OAuthAuthorizationServerProvider`
by delegating identity verification to Microsoft Entra ID: this server issues
its own authorization codes and tokens to the MCP client (claude.ai), but
only after the user has completed a real Microsoft sign-in against the
organization's tenant — the same one behind Teams/Outlook SSO.

Storage split (see specs/001-mcp-entra-oauth/research.md §2-3):
  - Access tokens: self-contained, HMAC-signed (same pattern as
    ``retrieval/citation_token.py``), verified by recomputing the signature
    — no storage lookup on the hot path (every MCP tool call).
  - Authorization codes, pending Microsoft round-trips, refresh tokens, and
    dynamic client registrations: S3 under the ``oauth/`` prefix of the
    existing ``S3_BUCKET`` — low-frequency, revocable state. Postgres/RDS is
    being retired and is not used here (constitution — no new usage).

Most OAuth-spec validation (PKCE, redirect_uri matching, expiry, client
authentication) is already performed by the ``mcp`` SDK's route handlers
before any method here is called — see the docstrings below for exactly
what each method is still responsible for.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
import structlog

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pipeline.api.settings import EntraOAuthSettings
from pipeline.common.aws import s3_client

log = structlog.get_logger(__name__)

_OAUTH_PREFIX = "oauth"
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
AUTH_CODE_TTL_SECONDS = 300  # 5 minutes — MCP client's exchange window
PENDING_TTL_SECONDS = 600  # 10 minutes — time allowed to complete Microsoft login


# ---------------------------------------------------------------------------
# Record shapes (spec: specs/001-mcp-entra-oauth/data-model.md)
# ---------------------------------------------------------------------------


class EntraAuthorizationCode(AuthorizationCode):
    """Adds the verified Entra identity to the base authorization code."""

    user_oid: str
    user_email: str | None = None


class EntraRefreshToken(RefreshToken):
    """Adds the verified Entra identity to the base refresh token."""

    user_oid: str


class EntraAccessToken(AccessToken):
    """Adds the verified Entra identity to the base access token."""

    user_oid: str


class PendingLoginNotFound(Exception):
    """Raised when a Microsoft callback's ``state`` has no matching pending
    request — expired, already used, or forged (spec edge case: forged
    callback)."""


# ---------------------------------------------------------------------------
# Generic S3 JSON storage helpers (oauth/ prefix)
# ---------------------------------------------------------------------------


def _s3_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — required for OAuth state storage")
    return bucket


def _s3_region() -> str:
    return os.environ.get("AWS_REGION", "eu-west-1")


def _put_json(key: str, value: dict[str, Any]) -> None:
    s3_client(region_name=_s3_region()).put_object(
        Bucket=_s3_bucket(),
        Key=f"{_OAUTH_PREFIX}/{key}",
        Body=json.dumps(value).encode("utf-8"),
        ContentType="application/json",
    )


def _get_json(key: str) -> dict[str, Any] | None:
    try:
        obj = s3_client(region_name=_s3_region()).get_object(
            Bucket=_s3_bucket(), Key=f"{_OAUTH_PREFIX}/{key}"
        )
    except Exception:  # noqa: BLE001 — includes NoSuchKey; treat any failure as "not found"
        return None
    return json.loads(obj["Body"].read())


def _delete_json(key: str) -> None:
    try:
        s3_client(region_name=_s3_region()).delete_object(
            Bucket=_s3_bucket(), Key=f"{_OAUTH_PREFIX}/{key}"
        )
    except Exception:  # noqa: BLE001
        pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_client_id(client: OAuthClientInformationFull) -> str:
    """``OAuthClientInformationFull.client_id`` is typed ``str | None`` for
    RFC 7591 generality, but every client reaching this provider was either
    just assigned an id by ``register_client`` or looked up by a known id
    via ``get_client`` — it is always set in practice. Fail loudly instead
    of silently storing ``"None"`` as a key if that invariant is ever
    violated."""
    if client.client_id is None:
        raise ValueError("OAuth client has no client_id")
    return client.client_id


# --- AuthorizationCode CRUD -------------------------------------------------


def _store_authorization_code(code: EntraAuthorizationCode) -> None:
    _put_json(f"codes/{code.code}.json", json.loads(code.model_dump_json()))


def _load_authorization_code(code: str) -> EntraAuthorizationCode | None:
    data = _get_json(f"codes/{code}.json")
    if data is None:
        return None
    return EntraAuthorizationCode.model_validate(data)


def _delete_authorization_code(code: str) -> None:
    _delete_json(f"codes/{code}.json")


# --- ClientRegistration CRUD -------------------------------------------------


def _store_client(client_info: OAuthClientInformationFull) -> None:
    _put_json(f"clients/{client_info.client_id}.json", json.loads(client_info.model_dump_json()))


def _load_client(client_id: str) -> OAuthClientInformationFull | None:
    data = _get_json(f"clients/{client_id}.json")
    if data is None:
        return None
    return OAuthClientInformationFull.model_validate(data)


# --- RefreshToken CRUD (keyed by hash, not the raw token value) -------------


def _store_refresh_token(token_value: str, record: EntraRefreshToken) -> None:
    _put_json(f"refresh/{_hash_token(token_value)}.json", json.loads(record.model_dump_json()))


def _load_refresh_token(token_value: str) -> EntraRefreshToken | None:
    data = _get_json(f"refresh/{_hash_token(token_value)}.json")
    if data is None:
        return None
    return EntraRefreshToken.model_validate(data)


def _delete_refresh_token(token_value: str) -> None:
    _delete_json(f"refresh/{_hash_token(token_value)}.json")


# --- Pending Microsoft round-trip (our authorize() <-> our /oauth/callback) -


def _store_pending_login(ms_state: str, *, client_id: str, params: AuthorizationParams) -> None:
    _put_json(
        f"pending/{ms_state}.json",
        {
            "client_id": client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "mcp_client_state": params.state,
            "resource": params.resource,
            "expires_at": time.time() + PENDING_TTL_SECONDS,
        },
    )


def _load_pending_login(ms_state: str) -> dict[str, Any] | None:
    data = _get_json(f"pending/{ms_state}.json")
    if data is None:
        return None
    if data["expires_at"] < time.time():
        return None
    return data


def _delete_pending_login(ms_state: str) -> None:
    _delete_json(f"pending/{ms_state}.json")


# ---------------------------------------------------------------------------
# Self-contained access token signing/verification
# (same base64url-payload + HMAC-SHA256 pattern as retrieval/citation_token.py)
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def make_access_token(
    *, client_id: str, user_oid: str, scopes: list[str], ttl_seconds: int, secret: str
) -> str:
    exp = int(time.time()) + ttl_seconds
    payload = json.dumps(
        {"cid": client_id, "oid": user_oid, "scp": scopes, "exp": exp},
        separators=(",", ":"),
    )
    payload_b64 = _b64e(payload.encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_access_token(token: str, *, secret: str) -> dict[str, Any] | None:
    """Verify signature + expiry. Returns claims on success, ``None`` on ANY
    failure (missing, malformed, tampered, or expired) — deliberately
    uniform so callers can't distinguish failure reasons (spec FR-003)."""
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload_b64, secret), sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception:  # noqa: BLE001
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------------------
# Microsoft-side helpers
# ---------------------------------------------------------------------------


def _openid_config(tenant_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _exchange_code_with_microsoft(
    *, tenant_id: str, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    config = _openid_config(tenant_id)
    resp = requests.post(
        config["token_endpoint"],
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _verify_microsoft_id_token(id_token: str, *, tenant_id: str, client_id: str) -> dict[str, Any]:
    config = _openid_config(tenant_id)
    jwks_client = jwt.PyJWKClient(config["jwks_uri"])
    signing_key = jwks_client.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=config["issuer"],
    )


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class EntraOAuthProvider(
    OAuthAuthorizationServerProvider[EntraAuthorizationCode, EntraRefreshToken, EntraAccessToken]
):
    """OAuth authorization server for the MCP mount, backed by Entra ID.

    Client-facing OAuth mechanics (PKCE verification, redirect_uri
    allow-listing, client secret checks, expiry checks) are handled by the
    ``mcp`` SDK's route handlers before these methods run — see
    ``mcp.server.auth.handlers.{authorize,token,register}``. This class only
    needs to: persist/retrieve the records those handlers hand it, and drive
    the Microsoft side of the exchange.
    """

    def __init__(self, settings: EntraOAuthSettings) -> None:
        self._settings = settings

    # --- Dynamic client registration (claude.ai registers itself once) ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return _load_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        _store_client(client_info)
        log.info(
            "oauth.client_registered",
            client_id=client_info.client_id,
            client_name=client_info.client_name,
        )

    # --- Authorization (redirect the MCP client's browser to Microsoft) ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        ms_state = secrets.token_urlsafe(32)
        _store_pending_login(ms_state, client_id=_require_client_id(client), params=params)
        query = {
            "client_id": self._settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": self._settings.oauth_callback_url,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": ms_state,
        }
        return (
            f"https://login.microsoftonline.com/{self._settings.azure_tenant_id}"
            f"/oauth2/v2.0/authorize?{urlencode(query)}"
        )

    def complete_microsoft_login(
        self, *, ms_state: str, code: str | None, error: str | None, error_description: str | None
    ) -> str:
        """Called by the ``/oauth/callback`` route once Microsoft redirects
        back. Returns the URL to redirect the *original* MCP client to —
        either with our own authorization code (success) or an OAuth
        ``error`` param (failure). Raises :class:`PendingLoginNotFound` if
        ``ms_state`` doesn't match a live pending request (spec edge case:
        forged/expired callback — caller should respond 400, not redirect,
        since there's no trustworthy redirect_uri to send the user to).
        """
        pending = _load_pending_login(ms_state)
        if pending is None:
            raise PendingLoginNotFound(ms_state)
        _delete_pending_login(ms_state)  # single-use, regardless of outcome

        redirect_uri = pending["redirect_uri"]
        mcp_client_state = pending.get("mcp_client_state")

        if error is not None:
            log.warning("oauth.microsoft_login_failed", error=error, error_description=error_description)
            return _build_redirect(redirect_uri, error=error, state=mcp_client_state)

        if not code:
            log.warning("oauth.microsoft_login_missing_code")
            return _build_redirect(redirect_uri, error="server_error", state=mcp_client_state)

        try:
            tokens = _exchange_code_with_microsoft(
                tenant_id=self._settings.azure_tenant_id,
                client_id=self._settings.azure_client_id,
                client_secret=self._settings.azure_client_secret,
                code=code,
                redirect_uri=self._settings.oauth_callback_url,
            )
            claims = _verify_microsoft_id_token(
                tokens["id_token"],
                tenant_id=self._settings.azure_tenant_id,
                client_id=self._settings.azure_client_id,
            )
        except Exception as exc:  # noqa: BLE001 — any Microsoft/verification failure is a rejection
            log.warning("oauth.microsoft_exchange_failed", error=str(exc))
            return _build_redirect(redirect_uri, error="access_denied", state=mcp_client_state)

        user_oid = claims["oid"]
        user_email = claims.get("email") or claims.get("preferred_username")

        mcp_code = secrets.token_urlsafe(32)
        _store_authorization_code(
            EntraAuthorizationCode(
                code=mcp_code,
                scopes=pending["scopes"],
                expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
                client_id=pending["client_id"],
                code_challenge=pending["code_challenge"],
                redirect_uri=redirect_uri,
                redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
                resource=pending.get("resource"),
                user_oid=user_oid,
                user_email=user_email,
            )
        )
        log.info("oauth.signin_succeeded", user_oid=user_oid, user_email=user_email)
        return _build_redirect(redirect_uri, code=mcp_code, state=mcp_client_state)

    # --- Authorization code exchange (MCP client -> our /token) ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> EntraAuthorizationCode | None:
        return _load_authorization_code(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: EntraAuthorizationCode
    ) -> OAuthToken:
        _delete_authorization_code(authorization_code.code)  # single-use
        client_id = _require_client_id(client)
        access_token = make_access_token(
            client_id=client_id,
            user_oid=authorization_code.user_oid,
            scopes=authorization_code.scopes,
            ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            secret=self._settings.mcp_oauth_signing_secret,
        )
        refresh_token_value = secrets.token_urlsafe(48)
        _store_refresh_token(
            refresh_token_value,
            EntraRefreshToken(
                token=refresh_token_value,
                client_id=client_id,
                scopes=authorization_code.scopes,
                expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
                user_oid=authorization_code.user_oid,
            ),
        )
        log.info(
            "oauth.token_issued", client_id=client_id, user_oid=authorization_code.user_oid
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token_value,
            scope=" ".join(authorization_code.scopes),
        )

    # --- Refresh ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> EntraRefreshToken | None:
        return _load_refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: EntraRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        _delete_refresh_token(refresh_token.token)  # rotate on use
        client_id = _require_client_id(client)
        access_token = make_access_token(
            client_id=client_id,
            user_oid=refresh_token.user_oid,
            scopes=scopes,
            ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            secret=self._settings.mcp_oauth_signing_secret,
        )
        new_refresh_value = secrets.token_urlsafe(48)
        _store_refresh_token(
            new_refresh_value,
            EntraRefreshToken(
                token=new_refresh_value,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
                user_oid=refresh_token.user_oid,
            ),
        )
        log.info(
            "oauth.token_refreshed", client_id=client_id, user_oid=refresh_token.user_oid
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh_value,
            scope=" ".join(scopes),
        )

    # --- Access token verification (every MCP tool call — no I/O) ---

    async def load_access_token(self, token: str) -> EntraAccessToken | None:
        claims = verify_access_token(token, secret=self._settings.mcp_oauth_signing_secret)
        if claims is None:
            return None
        return EntraAccessToken(
            token=token,
            client_id=claims["cid"],
            scopes=claims["scp"],
            expires_at=claims["exp"],
            user_oid=claims["oid"],
        )

    # --- Revocation (spec Story 3 — bounded by the access-token TTL) ---

    async def revoke_token(self, token: EntraAccessToken | EntraRefreshToken) -> None:
        if isinstance(token, EntraRefreshToken):
            _delete_refresh_token(token.token)
            log.info("oauth.token_revoked", client_id=token.client_id, user_oid=token.user_oid)
        # Access tokens are self-contained and can't be individually revoked
        # before they expire (research.md §2) — revoking the refresh token
        # is what actually stops the session within the TTL bound.


class EntraTokenVerifier(TokenVerifier):
    """Adapts :meth:`EntraOAuthProvider.load_access_token` to the
    ``TokenVerifier`` protocol.

    Deliberately NOT ``mcp.server.auth.provider.ProviderTokenVerifier`` —
    that class is typed against the generic base
    ``OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken,
    AccessToken]``, which our ``Entra*`` subtypes don't satisfy under
    strict parameter variance (they narrow the parameter types on
    ``exchange_authorization_code``/``exchange_refresh_token``). This
    adapter only needs ``load_access_token``, so the mismatch doesn't
    apply here.
    """

    def __init__(self, provider: EntraOAuthProvider) -> None:
        self._provider = provider

    async def verify_token(self, token: str) -> EntraAccessToken | None:
        return await self._provider.load_access_token(token)


def _build_redirect(
    redirect_uri: str,
    *,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
) -> str:
    params: dict[str, str] = {}
    if code is not None:
        params["code"] = code
    if error is not None:
        params["error"] = error
    if state is not None:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{separator}{urlencode(params)}"
