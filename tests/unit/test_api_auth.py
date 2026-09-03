"""Tests for FastAPI bearer auth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.api.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("API_BEARER_TOKEN", "test-secret-token")
    from pipeline.api import settings as settings_mod

    settings_mod.get_api_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    settings_mod.get_api_settings.cache_clear()


def test_health_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/status")
    assert r.status_code == 401


def test_status_with_bearer(client: TestClient) -> None:
    r = client.get(
        "/v1/status",
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert "mcp_mount" in body
