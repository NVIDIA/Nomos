# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SKILL.md generation: mode-specific guidance + the council protocol body."""

from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from agent_signup import server


def make_settings(**overrides: Any) -> server.SignupSettings:
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
    }
    values.update(overrides)
    return server.SignupSettings(**values)


def test_build_skill_md_both_modes_and_injection() -> None:
    settings = make_settings()
    md = server.build_skill_md(
        settings,
        server.SkillRequest(agent_id="alice-agent", council_id="council-prod", mode="both"),
    )

    assert "# Routing actions through the Nomos council" in md
    assert "## Mode A — tool use" in md
    assert "## Mode B — direct MCP" in md
    assert "council_propose_intent" in md
    assert "council_authorize" in md
    assert "tools/call" in md
    assert "/mcp" in md
    # injected identity + endpoint
    assert "alice-agent" in md
    assert "council-prod" in md
    assert "https://council.example/mcp" in md
    # protocol body present
    assert "## When to revise vs. accept" in md


def test_build_skill_md_tool_use_only() -> None:
    md = server.build_skill_md(
        make_settings(), server.SkillRequest(agent_id="bob", mode="tool-use")
    )

    assert "## Mode A — tool use" in md
    assert "## Mode B — direct MCP" not in md


def test_build_skill_md_mcp_only() -> None:
    md = server.build_skill_md(make_settings(), server.SkillRequest(agent_id="bob", mode="mcp"))

    assert "## Mode B — direct MCP" in md
    assert "## Mode A — tool use" not in md


def test_build_skill_md_includes_certified_evidence_for_te() -> None:
    md = server.build_skill_md(
        make_settings(),
        server.SkillRequest(agent_id="bob", mode="both", has_trusted_executor=True),
    )

    assert "## Certified evidence (trusted executor)" in md
    assert "certified_evidence" in md


def test_build_skill_md_custom_endpoint_overrides_default() -> None:
    md = server.build_skill_md(
        make_settings(),
        server.SkillRequest(
            agent_id="bob", mode="mcp", council_endpoint="https://other.example/mcp"
        ),
    )

    assert "https://other.example/mcp" in md
    assert "https://council.example/mcp" not in md


def _signup_auth() -> dict[str, str]:
    token = base64.b64encode(b"admin:test-admin-password").decode()
    return {"Authorization": f"Basic {token}"}


def test_skill_endpoint_returns_markdown_attachment() -> None:
    # postgres mode renders without redis; the route still needs signup auth.
    app = server.create_app(
        make_settings(
            storage_mode="postgres",
            database_url="postgresql://localhost/nomos",
            account_integrity_key="integrity-key",
        )
    )

    response = TestClient(app).post(
        "/api/skill",
        json={"agent_id": "alice-agent", "mode": "both"},
        headers=_signup_auth(),
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="SKILL.md"'
    assert response.headers["content-type"].startswith("text/markdown")
    assert "alice-agent" in response.text


def test_skill_endpoint_rejects_bad_mode() -> None:
    app = server.create_app(
        make_settings(
            storage_mode="postgres",
            database_url="postgresql://localhost/nomos",
            account_integrity_key="integrity-key",
        )
    )

    response = TestClient(app).post(
        "/api/skill",
        json={"agent_id": "alice-agent", "mode": "nonsense"},
        headers=_signup_auth(),
    )

    assert response.status_code == 422
