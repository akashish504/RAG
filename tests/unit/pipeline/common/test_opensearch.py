"""Tests for pipeline.common.opensearch.build_opensearch_client.

Covers the fix for the production incident where a frozen credentials
snapshot (``get_frozen_credentials()`` called once at client-construction
time) went stale once the ambient IAM role's temporary credentials rotated,
breaking OpenSearch auth until the process was restarted. See
specs/002-fix-opensearch-sigv4-auth/ for the full spec/plan/research.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opensearchpy import AWSV4SignerAuth

from pipeline.common.opensearch import build_opensearch_client


class _FakeLiveCredentials:
    """Stands in for boto3's ``RefreshableCredentials``.

    The real object re-derives current values from AWS on every
    ``get_frozen_credentials()`` call instead of returning a value fixed at
    construction time. This fake models that by letting a test mutate the
    "current" access key/secret/token between calls, simulating a credential
    rotation happening in the background between two signed requests.
    """

    def __init__(self, access_key: str, secret_key: str, token: str = "tok-1") -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token
        self.frozen_call_count = 0

    def get_frozen_credentials(self) -> SimpleNamespace:
        self.frozen_call_count += 1
        return SimpleNamespace(
            access_key=self.access_key, secret_key=self.secret_key, token=self.token
        )

    def rotate(self, *, access_key: str, secret_key: str, token: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token


class _FakeStaticCredentials:
    """Stands in for boto3's plain (non-refreshable) ``Credentials`` object,
    as returned when static AWS access keys (not an IAM role) are configured.
    Always returns the same frozen values — there is nothing to rotate.
    """

    def __init__(self, access_key: str, secret_key: str, token: str | None = None) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

    def get_frozen_credentials(self) -> SimpleNamespace:
        return SimpleNamespace(
            access_key=self.access_key, secret_key=self.secret_key, token=self.token
        )


def _patch_boto3_session(monkeypatch: pytest.MonkeyPatch, credentials: object | None) -> None:
    # build_opensearch_client does `import boto3` locally inside the function
    # body, so patching the real boto3 module's Session is what takes effect.
    import boto3

    fake_session = SimpleNamespace(get_credentials=lambda: credentials)
    monkeypatch.setattr(boto3, "Session", lambda *a, **kw: fake_session)


# ---------------------------------------------------------------------------
# User Story 1 (P1) — SigV4 path must sign from live, not frozen, credentials
# ---------------------------------------------------------------------------


def test_sigv4_mode_wraps_the_live_credentials_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_creds = _FakeLiveCredentials("AKIA_OLD", "secret_old")
    _patch_boto3_session(monkeypatch, fake_creds)

    client = build_opensearch_client("https://search.example.com:443", aws_region="eu-west-1")

    connection = client.transport.connection_pool.connections[0]
    auth = connection.session.auth

    assert isinstance(auth, AWSV4SignerAuth)
    # The signer must hold the SAME live object we handed it, not a copy or
    # a frozen snapshot taken at construction time.
    assert auth.signer.credentials is fake_creds
    # Constructing the client/signer must not itself call get_frozen_credentials —
    # that must only happen lazily, per request, at sign time.
    assert fake_creds.frozen_call_count == 0


def test_sigv4_signing_reflects_credential_rotation_without_rebuilding_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_creds = _FakeLiveCredentials("AKIA_OLD", "secret_old", token="tok-1")
    _patch_boto3_session(monkeypatch, fake_creds)

    client = build_opensearch_client("https://search.example.com:443", aws_region="eu-west-1")
    connection = client.transport.connection_pool.connections[0]
    signer = connection.session.auth.signer

    first_headers = signer.sign(method="GET", url="https://search.example.com/_search", body=None)
    assert "AKIA_OLD" in first_headers["Authorization"]

    # Simulate AWS rotating the role's temporary credentials in the background —
    # no client rebuild, no restart.
    fake_creds.rotate(access_key="AKIA_NEW", secret_key="secret_new", token="tok-2")

    second_headers = signer.sign(
        method="GET", url="https://search.example.com/_search", body=None
    )
    assert "AKIA_NEW" in second_headers["Authorization"]
    assert "AKIA_OLD" not in second_headers["Authorization"]


def test_sigv4_mode_raises_when_no_credentials_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_boto3_session(monkeypatch, None)

    with pytest.raises(ValueError, match="No AWS credentials found"):
        build_opensearch_client("https://search.example.com:443", aws_region="eu-west-1")


# ---------------------------------------------------------------------------
# User Story 2 (P2) — other auth paths must be unaffected by the fix
# ---------------------------------------------------------------------------


def test_basic_auth_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("basic-auth mode must not touch boto3 at all")

    import boto3

    monkeypatch.setattr(boto3, "Session", _boom)

    client = build_opensearch_client(
        "https://search.example.com:443", username="dev", password="dev-pass"
    )

    connection = client.transport.connection_pool.connections[0]
    assert connection.session.auth == ("dev", "dev-pass")
    assert connection.use_ssl is True


def test_basic_auth_mode_disables_tls_for_localhost() -> None:
    client = build_opensearch_client("http://localhost:9200", username="dev", password="dev-pass")

    connection = client.transport.connection_pool.connections[0]
    assert connection.use_ssl is False


def test_sigv4_mode_works_with_static_non_refreshable_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static AWS access keys (not an IAM role) must keep working — the fix
    makes no assumption that credentials are refreshable, only that the
    *live* object (whatever its refresh behavior) is what gets wrapped."""
    fake_static = _FakeStaticCredentials("AKIA_STATIC", "secret_static", token=None)
    _patch_boto3_session(monkeypatch, fake_static)

    client = build_opensearch_client("https://search.example.com:443", aws_region="eu-west-1")

    connection = client.transport.connection_pool.connections[0]
    auth = connection.session.auth
    assert isinstance(auth, AWSV4SignerAuth)
    headers = auth.signer.sign(method="GET", url="https://search.example.com/_search", body=None)
    assert "AKIA_STATIC" in headers["Authorization"]
