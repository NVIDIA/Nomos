# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Path-scoped Basic-auth for the onboarding console: the signup pages and the
council-creation page are gated by two separate credentials; probes stay public."""

from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from agent_signup import server


def _auth(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def signup_auth() -> dict[str, str]:
    return _auth("admin", "test-admin-password")


def council_auth() -> dict[str, str]:
    return _auth("council-admin", "council-pass")


def _app(**overrides: Any) -> Any:
    # Postgres mode so GET / renders without redis (static signup config).
    values: dict[str, Any] = {
        "redis_url": "redis://example",
        "redis_prefix": "nomos:",
        "default_council_id": "council-prod",
        "allowed_councils": ("council-prod",),
        "default_role": "developer",
        "allowed_roles": ("readonly", "developer"),
        "council_endpoint": "https://council.example/mcp",
        "admin_user": "admin",
        "admin_password": "test-admin-password",  # pragma: allowlist secret
        "council_admin_user": "council-admin",
        "council_admin_password": "council-pass",  # pragma: allowlist secret
        "storage_mode": "postgres",
        "database_url": "postgresql://localhost/nomos",
        "account_integrity_key": "integrity-key",
    }
    values.update(overrides)
    return server.create_app(server.SignupSettings(**values))


def test_console_requires_signup_auth() -> None:
    client = TestClient(_app())

    assert client.get("/").status_code == 401
    assert client.post("/api/agents", json={"agent_id": "x"}).status_code == 401
    assert client.post("/api/skill", json={"agent_id": "x"}).status_code == 401


def test_signup_challenge_uses_signup_realm() -> None:
    response = TestClient(_app()).get("/")

    assert response.status_code == 401
    expected = 'Basic realm="Nomos Agent Signup", charset="UTF-8"'
    assert response.headers["www-authenticate"] == expected


def test_signup_creds_render_console() -> None:
    response = TestClient(_app()).get("/", headers=signup_auth())

    assert response.status_code == 200
    assert "Agent Console" in response.text
    assert 'data-tab="skill"' in response.text  # the SKILL.md tab


def test_council_page_uses_separate_password() -> None:
    client = TestClient(_app())

    # the signup credential must NOT open the council page
    assert client.get("/council", headers=signup_auth()).status_code == 401
    # the dedicated council credential does
    assert client.get("/council", headers=council_auth()).status_code == 200


def test_council_challenge_uses_council_realm() -> None:
    response = TestClient(_app()).get("/council")

    assert response.status_code == 401
    expected = 'Basic realm="Nomos Council Admin", charset="UTF-8"'
    assert response.headers["www-authenticate"] == expected


def test_council_post_is_stubbed() -> None:
    response = TestClient(_app()).post("/api/council", headers=council_auth())

    assert response.status_code == 501


def test_council_fails_closed_when_password_unset() -> None:
    client = TestClient(_app(council_admin_password=""))

    # any credential, but the council password is not configured
    assert client.get("/council", headers=council_auth()).status_code == 503


def test_healthz_is_public() -> None:
    # public probe path: reachable without auth (status is not 401)
    assert TestClient(_app()).get("/healthz").status_code != 401


def test_wrong_password_is_rejected() -> None:
    response = TestClient(_app()).get("/", headers=_auth("admin", "wrong"))

    assert response.status_code == 401
