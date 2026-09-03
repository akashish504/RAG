"""Unit tests for the Entra ID OAuth provider (spec 001-mcp-entra-oauth).

PKCE verification, redirect_uri allow-listing, and client-secret checks are
performed by the ``mcp`` SDK's own route handlers
(``mcp.server.auth.handlers.*``) before any ``EntraOAuthProvider`` method
runs — see ``entra_oauth.py``'s module docstring — so those are not
re-tested here. These tests cover what this module is actually responsible
for: S3-backed storage correctness, signed access-token verification, and
the Microsoft round-trip (mocked — no live network calls).
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from pipeline.api import entra_oauth as eo
from pipeline.api.settings import EntraOAuthSettings

BUCKET = "test-mcp-bucket"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
        )
        yield client


@pytest.fixture
def settings() -> EntraOAuthSettings:
    return EntraOAuthSettings(
        azure_tenant_id="test-tenant",
        azure_client_id="test-client",
        azure_client_secret="test-secret",
        mcp_oauth_signing_secret="test-signing-secret",
        mcp_public_base_url="https://mcp.test.example.com",
    )


@pytest.fixture
def provider(settings: EntraOAuthSettings, s3_bucket) -> eo.EntraOAuthProvider:
    return eo.EntraOAuthProvider(settings)


def _mcp_client(
    client_id: str = "mcp-client", redirect_uri: str = "https://claude.ai/callback"
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[redirect_uri], client_name="claude.ai"
    )


def _auth_params(
    redirect_uri: str = "https://claude.ai/callback", state: str = "client-state-123"
) -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["openid", "profile", "email"],
        code_challenge="test-challenge",
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def _extract_ms_state(authorize_url: str) -> str:
    query = parse_qs(urlparse(authorize_url).query)
    return query["state"][0]


# --------------------------------------------------------------------------
# Client registration (S3 CRUD)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_and_get_client_round_trip(provider: eo.EntraOAuthProvider) -> None:
    client = _mcp_client()
    await provider.register_client(client)
    loaded = await provider.get_client(client.client_id)
    assert loaded is not None
    assert loaded.client_id == client.client_id
    assert loaded.redirect_uris == client.redirect_uris


@pytest.mark.asyncio
async def test_get_client_unknown_returns_none(provider: eo.EntraOAuthProvider) -> None:
    assert await provider.get_client("does-not-exist") is None


# --------------------------------------------------------------------------
# authorize() — pending Microsoft round-trip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_redirects_to_microsoft_and_stores_pending(
    provider: eo.EntraOAuthProvider, settings: EntraOAuthSettings
) -> None:
    client = _mcp_client()
    params = _auth_params()

    url = await provider.authorize(client, params)

    parsed = urlparse(url)
    assert parsed.hostname == "login.microsoftonline.com"
    assert parsed.path == f"/{settings.azure_tenant_id}/oauth2/v2.0/authorize"
    query = parse_qs(parsed.query)
    assert query["client_id"] == [settings.azure_client_id]
    assert query["redirect_uri"] == [settings.oauth_callback_url]

    ms_state = _extract_ms_state(url)
    pending = eo._load_pending_login(ms_state)
    assert pending is not None
    assert pending["client_id"] == client.client_id
    assert pending["mcp_client_state"] == "client-state-123"


# --------------------------------------------------------------------------
# US1 (P1) — happy path: authorize -> Microsoft callback -> token exchange
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_signin_and_token_exchange(
    provider: eo.EntraOAuthProvider, settings: EntraOAuthSettings
) -> None:
    client = _mcp_client()
    await provider.register_client(client)
    params = _auth_params()
    authorize_url = await provider.authorize(client, params)
    ms_state = _extract_ms_state(authorize_url)

    with (
        patch.object(
            eo, "_exchange_code_with_microsoft", return_value={"id_token": "fake.jwt.token"}
        ),
        patch.object(
            eo,
            "_verify_microsoft_id_token",
            return_value={"oid": "user-oid-123", "email": "person@example.com"},
        ),
    ):
        redirect_url = provider.complete_microsoft_login(
            ms_state=ms_state, code="ms-code-abc", error=None, error_description=None
        )

    # Redirected back to the MCP client's own redirect_uri, carrying its own state.
    parsed = urlparse(redirect_url)
    assert redirect_url.startswith("https://claude.ai/callback")
    query = parse_qs(parsed.query)
    assert query["state"] == ["client-state-123"]
    mcp_code = query["code"][0]

    # The pending record is single-use.
    assert eo._load_pending_login(ms_state) is None

    # The MCP client can now exchange this code for tokens.
    auth_code = await provider.load_authorization_code(client, mcp_code)
    assert auth_code is not None
    assert auth_code.user_oid == "user-oid-123"

    tokens = await provider.exchange_authorization_code(client, auth_code)
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "Bearer"

    # Code is single-use.
    assert await provider.load_authorization_code(client, mcp_code) is None

    # The minted access token verifies and carries the signed-in user's identity.
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    assert access.user_oid == "user-oid-123"
    assert access.client_id == client.client_id


# --------------------------------------------------------------------------
# US2 (P2) — unauthorized access is blocked
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forged_or_expired_callback_state_is_rejected(
    provider: eo.EntraOAuthProvider,
) -> None:
    with pytest.raises(eo.PendingLoginNotFound):
        provider.complete_microsoft_login(
            ms_state="never-issued-state", code="whatever", error=None, error_description=None
        )


@pytest.mark.asyncio
async def test_microsoft_error_response_is_not_granted_a_code(
    provider: eo.EntraOAuthProvider,
) -> None:
    client = _mcp_client()
    authorize_url = await provider.authorize(client, _auth_params())
    ms_state = _extract_ms_state(authorize_url)

    redirect_url = provider.complete_microsoft_login(
        ms_state=ms_state, code=None, error="access_denied", error_description="user cancelled"
    )

    query = parse_qs(urlparse(redirect_url).query)
    assert query["error"] == ["access_denied"]
    assert "code" not in query


@pytest.mark.asyncio
async def test_microsoft_exchange_failure_is_not_granted_a_code(
    provider: eo.EntraOAuthProvider,
) -> None:
    client = _mcp_client()
    authorize_url = await provider.authorize(client, _auth_params())
    ms_state = _extract_ms_state(authorize_url)

    with patch.object(eo, "_exchange_code_with_microsoft", side_effect=RuntimeError("boom")):
        redirect_url = provider.complete_microsoft_login(
            ms_state=ms_state, code="ms-code", error=None, error_description=None
        )

    query = parse_qs(urlparse(redirect_url).query)
    assert query["error"] == ["access_denied"]
    assert "code" not in query


@pytest.mark.asyncio
async def test_load_access_token_rejects_missing_or_garbage(
    provider: eo.EntraOAuthProvider,
) -> None:
    assert await provider.load_access_token("") is None
    assert await provider.load_access_token("not-a-real-token") is None


@pytest.mark.asyncio
async def test_load_access_token_rejects_tampered_signature(
    provider: eo.EntraOAuthProvider, settings: EntraOAuthSettings
) -> None:
    token = eo.make_access_token(
        client_id="c1",
        user_oid="user-1",
        scopes=["openid"],
        ttl_seconds=3600,
        secret=settings.mcp_oauth_signing_secret,
    )
    payload_b64, _sig = token.split(".", 1)
    tampered = f"{payload_b64}.tampered-signature"
    assert await provider.load_access_token(tampered) is None


@pytest.mark.asyncio
async def test_load_access_token_rejects_expired(
    provider: eo.EntraOAuthProvider, settings: EntraOAuthSettings
) -> None:
    token = eo.make_access_token(
        client_id="c1",
        user_oid="user-1",
        scopes=["openid"],
        ttl_seconds=-10,  # already expired
        secret=settings.mcp_oauth_signing_secret,
    )
    assert await provider.load_access_token(token) is None


@pytest.mark.asyncio
async def test_load_access_token_rejects_wrong_signing_secret(
    provider: eo.EntraOAuthProvider,
) -> None:
    token = eo.make_access_token(
        client_id="c1", user_oid="user-1", scopes=["openid"], ttl_seconds=3600, secret="wrong-secret"
    )
    assert await provider.load_access_token(token) is None


# --------------------------------------------------------------------------
# US3 (P3) — revocation
# --------------------------------------------------------------------------


async def _issue_session(
    provider: eo.EntraOAuthProvider, client: OAuthClientInformationFull, user_oid: str
):
    """Helper: drive a full sign-in for one user, returning the OAuthToken."""
    authorize_url = await provider.authorize(client, _auth_params(state=f"state-{user_oid}"))
    ms_state = _extract_ms_state(authorize_url)
    with (
        patch.object(eo, "_exchange_code_with_microsoft", return_value={"id_token": "fake"}),
        patch.object(eo, "_verify_microsoft_id_token", return_value={"oid": user_oid}),
    ):
        redirect_url = provider.complete_microsoft_login(
            ms_state=ms_state, code="ms-code", error=None, error_description=None
        )
    mcp_code = parse_qs(urlparse(redirect_url).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, mcp_code)
    return await provider.exchange_authorization_code(client, auth_code)


@pytest.mark.asyncio
async def test_refresh_token_is_single_use_and_rotates(
    provider: eo.EntraOAuthProvider,
) -> None:
    client = _mcp_client()
    await provider.register_client(client)
    tokens = await _issue_session(provider, client, "user-A")

    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None

    new_tokens = await provider.exchange_refresh_token(client, loaded, loaded.scopes)
    assert new_tokens.refresh_token != tokens.refresh_token

    # Old refresh token was rotated out — no longer usable.
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_revoking_one_session_does_not_affect_another(
    provider: eo.EntraOAuthProvider,
) -> None:
    client = _mcp_client()
    await provider.register_client(client)

    tokens_a = await _issue_session(provider, client, "user-A")
    tokens_b = await _issue_session(provider, client, "user-B")

    refresh_a = await provider.load_refresh_token(client, tokens_a.refresh_token)
    assert refresh_a is not None
    await provider.revoke_token(refresh_a)

    # Revoked session can no longer refresh.
    assert await provider.load_refresh_token(client, tokens_a.refresh_token) is None
    # The other user's session is untouched.
    refresh_b = await provider.load_refresh_token(client, tokens_b.refresh_token)
    assert refresh_b is not None
    assert refresh_b.user_oid == "user-B"


# --------------------------------------------------------------------------
# EntraTokenVerifier (adapter used by retrieval/mcp/server.py's
# token_verifier= wiring — see research.md §7 for why not
# mcp.server.auth.provider.ProviderTokenVerifier)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_verifier_accepts_valid_and_rejects_invalid(
    provider: eo.EntraOAuthProvider, settings: EntraOAuthSettings
) -> None:
    verifier = eo.EntraTokenVerifier(provider)
    token = eo.make_access_token(
        client_id="c1",
        user_oid="user-1",
        scopes=["openid"],
        ttl_seconds=3600,
        secret=settings.mcp_oauth_signing_secret,
    )

    verified = await verifier.verify_token(token)
    assert verified is not None
    assert verified.user_oid == "user-1"

    assert await verifier.verify_token("garbage") is None
