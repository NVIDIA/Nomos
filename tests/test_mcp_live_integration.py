# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in live integration tests for a deployed MCP council.

These tests intentionally exercise the running NAT/MCP service instead of
mocking the council. They are skipped unless NOMOS_MCP_LIVE_URL and NOMOS_AUTH_TOKEN
are set because they call live LLM-backed council members.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from demo.server.scenarios import scenarios_by_id

LIVE_MCP_URL = os.environ.get("NOMOS_MCP_LIVE_URL", "")
AGENT_ID = os.environ.get("NOMOS_AGENT_ID", "agent-admin")
AUTH_TOKEN = os.environ.get("NOMOS_AUTH_TOKEN") or os.environ.get("NOMOS_TOKEN", "")
WORKSPACE = os.environ.get("NOMOS_LIVE_WORKSPACE", "/workspace")
TIMEOUT_SECONDS = float(os.environ.get("NOMOS_MCP_LIVE_TIMEOUT_SECONDS", "180"))
PROBABILISTIC_DEMO_ENABLED = os.environ.get("NOMOS_MCP_LIVE_PROBABILISTIC_DEMO", "").lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = [
    pytest.mark.live_mcp,
    pytest.mark.skipif(
        not LIVE_MCP_URL or not AUTH_TOKEN,
        reason="set NOMOS_MCP_LIVE_URL and NOMOS_AUTH_TOKEN to run live MCP council tests",
    ),
]

EXPECTED_TOOLS = {
    "council_authorize",
    "council_consume_writ",
    "council_check_status",
    "council_check_writ_status",
    "council_get_public_key",
    "council_pending_approvals",
    "council_propose_intent",
}


def _tool_payload(result: Any) -> dict[str, Any]:
    """Parse the JSON payload returned by a NAT/MCP tool call."""
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"tool returned no JSON text content: {result!r}")


async def _call(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await session.call_tool(tool_name, arguments=arguments)
    assert not getattr(result, "isError", False), f"{tool_name} returned MCP error: {result!r}"
    return _tool_payload(result)


async def _authorize(session: ClientSession, intent: dict[str, Any]) -> dict[str, Any]:
    return await _call(
        session,
        "council_authorize",
        {
            "auth_token": AUTH_TOKEN,
            "agent_id": AGENT_ID,
            "working_directory": WORKSPACE,
            **intent,
        },
    )


def _proxy_headers() -> dict[str, str]:
    """Bearer header for the monitor's /mcp proxy, when one is configured.

    The deploy scripts generate MONITOR_MCP_PROXY_TOKEN and the monitor rejects
    unauthenticated requests once it is set, so a live suite that never sends the
    header cannot reach its own deployment -- it fails with 401 before any product
    behaviour is exercised. Empty when unset, which keeps an unauthenticated dev
    proxy working unchanged.
    """
    token = os.environ.get("MONITOR_MCP_PROXY_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


@asynccontextmanager
async def live_session():
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=_proxy_headers()) as http_client:
        async with streamable_http_client(
            LIVE_MCP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
async def test_live_council_exposes_expected_tools_and_key():
    async with live_session() as session:
        tools = await session.list_tools()
        key_info = await _call(session, "council_get_public_key", {})

    tool_names = {tool.name for tool in tools.tools}
    assert EXPECTED_TOOLS <= tool_names

    assert len(key_info["public_key_hex"]) == 64
    assert len(key_info["fingerprint"]) == 64
    assert key_info["key_id"]
    assert key_info["error"] == ""


@pytest.mark.asyncio
async def test_live_council_fails_closed_for_invalid_auth():
    async with live_session() as session:
        response = await _call(
            session,
            "council_authorize",
            {
                "auth_token": "invalid-live-integration-token",
                "agent_id": AGENT_ID,
                "command": "/bin/echo should-not-authorize",
                "working_directory": WORKSPACE,
                "description": "Attempt a harmless command with an intentionally invalid token.",
                "target_resource": "stdout",
                "justification": "Verify the deployed council fails closed when auth is invalid.",
                "blast_radius": "No intended persistent change; should be rejected before review.",
            },
        )

    assert response["status"] in {"denied", "error"}
    assert response.get("writ") in {None, ""}
    assert "Authentication failed" in response.get("error", "")


@pytest.mark.asyncio
async def test_live_council_mints_and_indexes_low_risk_writ():
    async with live_session() as session:
        response = await _authorize(
            session,
            {
                "command": "/bin/echo live-mcp-integration",
                "description": (
                    "Print the literal string live-mcp-integration to stdout as a low-risk "
                    "live integration check."
                ),
                "target_resource": "stdout in executor workspace",
                "justification": (
                    "This verifies the deployed council authorization path, vote collection, "
                    "writ minting, and writ status index with a stdout-only command."
                ),
                "blast_radius": (
                    "No filesystem, network, service, or infrastructure change; stdout only."
                ),
                "context": "Live MCP integration test for deployed council health.",
            },
        )

        assert response["status"] == "approved", response
        assert response["writ"]
        assert response["writ_id"]
        assert response["decision_id"]
        assert response["chairman"]["threshold"] == "unanimous"
        assert all(member["decision"] == "approve" for member in response["council_members"])

        decision = await _call(
            session,
            "council_check_status",
            {"intent_id": response["decision_id"]},
        )
        assert decision["found"] is True
        assert decision["state"] == "approved"

        writ_status = await _call(
            session,
            "council_check_writ_status",
            {"auth_token": AUTH_TOKEN, "writ_id": response["writ_id"]},
        )
        assert writ_status["status"] == "valid"
        assert writ_status["command"] == "/bin/echo live-mcp-integration"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PROBABILISTIC_DEMO_ENABLED,
    reason="set NOMOS_MCP_LIVE_PROBABILISTIC_DEMO=1 to run this probabilistic demo verdict",
)
async def test_live_council_reviews_packaged_file_attachment():
    scenario = scenarios_by_id()["s8-packaged-script"]
    intent = scenario.to_intent()

    async with live_session() as session:
        response = await _authorize(session, intent)

        assert response["status"] == "approved", response
        assert response["writ"]
        assert response["writ_id"]
        assert response["decision_id"]
        assert response["code_review"] is not None

        code_review_reasoning = response["code_review"]["reasoning"]
        assert "scripts/packaged_code_review.py" in code_review_reasoning
        assert intent["packaged_files"][0]["sha256"] in code_review_reasoning
        assert all(member["decision"] == "approve" for member in response["council_members"])

        writ_status = await _call(
            session,
            "council_check_writ_status",
            {"auth_token": AUTH_TOKEN, "writ_id": response["writ_id"]},
        )
        assert writ_status["status"] == "valid"
        assert writ_status["command"] == scenario.command


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PROBABILISTIC_DEMO_ENABLED,
    reason="set NOMOS_MCP_LIVE_PROBABILISTIC_DEMO=1 to run this probabilistic demo verdict",
)
async def test_live_council_db_pool_bump_requires_certified_evidence():
    """Mirror the demo's checkout DB pool first turn: prose-only claims are denied."""
    scenario = scenarios_by_id()["s7-iterate"]
    first_turn = scenario.to_intent()

    async with live_session() as session:
        first_response = await _authorize(session, first_turn)

        if first_response.get("missing_permissions"):
            pytest.fail(
                "DB pool live scenario requires an admin/operator token with infra permissions; "
                f"missing {first_response['missing_permissions']}"
            )

        assert first_response["status"] == "denied", first_response
        assert not first_response.get("writ")
        assert first_response.get("guidance") or any(
            member.get("guidance") for member in first_response["council_members"]
        )
        assert any(member["decision"] == "deny" for member in first_response["council_members"])

        assert scenario.followup is not None
        assert scenario.followup.requires_certified_evidence is True
        assert scenario.followup.evidence_probe is not None
