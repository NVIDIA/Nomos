from __future__ import annotations

import base64
import importlib

import pytest
from fastapi.testclient import TestClient

from nomos_runtime.deployment import DeploymentPreflightError


def _basic_auth(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _load_demo_app(monkeypatch, *, password: str | None, user: str = "demo"):
    monkeypatch.setenv("DEMO_AUTH_USER", user)
    if password is None:
        monkeypatch.delenv("DEMO_AUTH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DEMO_AUTH_PASSWORD", password)  # pragma: allowlist secret

    import demo.server.main as demo_main

    return importlib.reload(demo_main).app


def test_demo_requires_basic_auth_for_ui_and_api(monkeypatch):
    app = _load_demo_app(monkeypatch, user="viewer", password="demo-pass")
    client = TestClient(app)

    assert client.get("/").status_code == 401
    assert client.get("/api/config").status_code == 401
    assert client.get("/static/app.js").status_code == 401

    wrong = client.get("/api/config", headers=_basic_auth("viewer", "wrong-pass"))
    assert wrong.status_code == 401

    response = client.get("/api/config", headers=_basic_auth("viewer", "demo-pass"))
    assert response.status_code == 200
    assert response.json()["workspace"] == "/workspace"


def test_demo_auth_challenge_uses_basic_realm(monkeypatch):
    app = _load_demo_app(monkeypatch, user="viewer", password="demo-pass")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Nomos Demo", charset="UTF-8"'


def test_demo_health_is_public_for_probes(monkeypatch):
    app = _load_demo_app(monkeypatch, user="viewer", password="demo-pass")
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert set(response.json()) == {"council", "executor"}


def test_demo_fails_closed_when_password_is_missing(monkeypatch):
    app = _load_demo_app(monkeypatch, password=None)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 503
    assert "password is not configured" in response.text
    assert client.get("/api/health").status_code == 200


def test_demo_alpha_profile_requires_password(monkeypatch):
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
    monkeypatch.setenv("DEMO_AUTH_USER", "demo")
    monkeypatch.delenv("DEMO_AUTH_PASSWORD", raising=False)

    import demo.server.main as demo_main

    with pytest.raises(DeploymentPreflightError) as exc:
        importlib.reload(demo_main)

    assert exc.value.issues == ("DEMO_AUTH_PASSWORD is required",)
