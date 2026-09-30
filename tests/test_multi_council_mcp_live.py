# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in live tests for the deployed multi-council MCP API.

These tests avoid live LLM review by exercising management tools, v2 routing
validation, and a temporary no-member/no-auth council profile.
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

LIVE_MCP_URL = os.environ.get("NOMOS_MCP_LIVE_URL", "")
MANAGEMENT_TOKEN = os.environ.get("NOMOS_COUNCIL_MANAGEMENT_TOKEN") or os.environ.get(
    "COUNCIL_MANAGEMENT_TOKEN",
    "",
)
MUTATE_ENABLED = os.environ.get("NOMOS_MCP_LIVE_MUTATE", "") in {"1", "true", "yes"}
SMOKE_COUNCIL_ID = os.environ.get("NOMOS_MCP_LIVE_SMOKE_COUNCIL_ID", "live-mcp-smoke-council")

pytestmark = [
    pytest.mark.live_mcp,
    pytest.mark.skipif(
        not LIVE_MCP_URL,
        reason="set NOMOS_MCP_LIVE_URL to run live multi-council MCP tests",
    ),
]

EXPECTED_TOOLS = {
    "council_authorize",
    "council_consume_writ",
    "council_propose_intent",
    "council_v1_authorize",
    "council_v1_propose_intent",
    "council_v2_authorize",
    "council_v2_propose_intent",
    "council_check_status",
    "council_pending_approvals",
    "council_get_public_key",
    "council_check_writ_status",
    "council_list_councils",
    "council_show_council",
    "council_create_council",
    "council_put_council_profile",
    "council_set_council_enabled",
    "council_validate_catalog",
}


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
    async with httpx.AsyncClient(timeout=120.0, headers=_proxy_headers()) as http_client:
        async with streamable_http_client(LIVE_MCP_URL, http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _tool_payload(result: Any) -> dict[str, Any]:
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


def _smoke_profile(enabled: bool) -> dict[str, Any]:
    return {
        "council_id": SMOKE_COUNCIL_ID,
        "enabled": enabled,
        "require_auth": False,
        "require_council_membership": True,
        "require_justification": True,
        "llm_backends": {},
        "members": [],
        "opa": {"enabled": False},
        "council_key_path": "",
        "ledger_path": "",
        "redis": {"host": ""},
        "metadata": {"purpose": "live multi-council MCP smoke test"},
    }


@pytest.mark.asyncio
async def test_live_multi_council_mcp_read_contract():
    async with live_session() as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert EXPECTED_TOOLS <= names

        listed = await _call(session, "council_list_councils", {})
        assert listed["success"] is True
        assert listed["default_council_id"]
        assert listed["councils"]

        shown = await _call(
            session,
            "council_show_council",
            {"council_id": listed["default_council_id"]},
        )
        assert shown["success"] is True
        assert shown["council"]["council_id"] == listed["default_council_id"]

        unknown = await _call(
            session,
            "council_show_council",
            {"council_id": "missing-live-council"},
        )
        assert unknown["success"] is False
        assert "unknown council" in unknown["error"]

        validated = await _call(session, "council_validate_catalog", {})
        assert validated["success"] is True

        missing_v2 = await _call(
            session,
            "council_v2_propose_intent",
            {
                "agent_id": "live-multi-council-smoke",
                "description": "Check that v2 requires an explicit council id.",
                "target_resource": "/workspace",
                "justification": "Validate the live multi-council API contract.",
            },
        )
        assert missing_v2["success"] is False
        assert "council_id is required" in missing_v2["error"]

        unknown_v2 = await _call(
            session,
            "council_v2_propose_intent",
            {
                "agent_id": "live-multi-council-smoke",
                "council_id": "missing-live-council",
                "description": "Check that v2 rejects an unknown council id.",
                "target_resource": "/workspace",
                "justification": "Validate the live multi-council API contract.",
            },
        )
        assert unknown_v2["success"] is False
        assert "Unknown council_id" in unknown_v2["error"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not MANAGEMENT_TOKEN or not MUTATE_ENABLED,
    reason=(
        "set NOMOS_COUNCIL_MANAGEMENT_TOKEN and NOMOS_MCP_LIVE_MUTATE=1 to run "
        "live catalog mutation tests"
    ),
)
async def test_live_multi_council_mcp_mutation_and_hot_reload():
    async with live_session() as session:
        bad_token = await _call(
            session,
            "council_set_council_enabled",
            {
                "council_id": SMOKE_COUNCIL_ID,
                "management_token": "wrong-token",
                "enabled": False,
            },
        )
        assert bad_token["success"] is False

        # Under COUNCIL_STORAGE_MODE=postgres the catalog is served static and
        # read-only: profile mutations would write replica-local YAML and diverge
        # across council replicas, so they are refused before the token is even
        # examined. That is an accepted degradation (NOMOS-4 in
        # docs/proposals/08_pg_ha_cutover_outstanding_issues.md), not a failure —
        # but it means the rest of this test cannot run against such a deployment.
        if "postgres storage mode" in bad_token["error"]:
            pytest.skip(
                "deployment runs postgres storage mode; dynamic council mutation "
                "is disabled there by design (NOMOS-4)"
            )

        assert "Invalid or missing" in bad_token["error"]

        put_disabled = await _call(
            session,
            "council_put_council_profile",
            {
                "council_id": SMOKE_COUNCIL_ID,
                "management_token": MANAGEMENT_TOKEN,
                "profile": _smoke_profile(enabled=False),
                "enabled": False,
            },
        )
        assert put_disabled["success"] is True
        assert put_disabled["council"]["enabled"] is False

        listed_after_put = await _call(session, "council_list_councils", {})
        by_id = {item["council_id"]: item for item in listed_after_put["councils"]}
        assert by_id[SMOKE_COUNCIL_ID]["enabled"] is False

        disabled_route = await _call(
            session,
            "council_v2_propose_intent",
            {
                "agent_id": "live-multi-council-smoke",
                "council_id": SMOKE_COUNCIL_ID,
                "description": "Check disabled dynamic council routing.",
                "target_resource": "/workspace",
                "justification": "Validate disabled councils reject v2 requests.",
            },
        )
        assert disabled_route["success"] is False
        assert "disabled" in disabled_route["error"]

        enabled = await _call(
            session,
            "council_set_council_enabled",
            {
                "council_id": SMOKE_COUNCIL_ID,
                "management_token": MANAGEMENT_TOKEN,
                "enabled": True,
            },
        )
        assert enabled["success"] is True
        assert enabled["council"]["enabled"] is True

        routed = await _call(
            session,
            "council_v2_propose_intent",
            {
                "agent_id": "live-multi-council-smoke",
                "council_id": SMOKE_COUNCIL_ID,
                "description": "Route to the enabled dynamic smoke council.",
                "target_resource": "/workspace",
                "justification": "Validate registry hot reload after MCP profile update.",
            },
        )
        assert routed["success"] is True
        assert routed["council_id"] == SMOKE_COUNCIL_ID

        disabled = await _call(
            session,
            "council_set_council_enabled",
            {
                "council_id": SMOKE_COUNCIL_ID,
                "management_token": MANAGEMENT_TOKEN,
                "enabled": False,
            },
        )
        assert disabled["success"] is True
        assert disabled["council"]["enabled"] is False
