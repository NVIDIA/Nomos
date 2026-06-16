# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the stateless MCP front-end worker (nomos/nat/stateless_worker.py)."""

import pytest
from mcp.server.fastmcp import FastMCP

from nomos.nat.stateless_worker import MCPFrontEndPluginWorker, StatelessMCPWorker


@pytest.mark.asyncio
async def test_stateless_worker_enables_stateless_http(monkeypatch):
    """The worker flips the FastMCP transport to stateless so any council
    replica can serve any request (horizontal scale-out, no session affinity)."""

    async def fake_base_create(self):
        return FastMCP(name="test-council")

    # super().create_mcp_server() resolves to the base worker; stub it so we
    # don't need a full NAT config to build the FastMCP instance.
    monkeypatch.setattr(MCPFrontEndPluginWorker, "create_mcp_server", fake_base_create)

    worker = StatelessMCPWorker.__new__(StatelessMCPWorker)  # __init__ needs full NAT config
    mcp = await worker.create_mcp_server()

    assert mcp.settings.stateless_http is True
    # Left False so responses stay SSE and the monitor's stream parsing is unaffected.
    assert mcp.settings.json_response is False

    # The flag actually drives a stateless session manager (no per-pod session state).
    mcp.streamable_http_app()
    assert mcp.session_manager.stateless is True
