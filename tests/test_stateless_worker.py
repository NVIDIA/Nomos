# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the stateless MCP front-end worker (nomos/nat/stateless_worker.py)."""

import importlib.util

import pytest
from mcp.server.fastmcp import FastMCP

# Skip only when nvidia-nat[mcp] is absent (find_spec locates without importing),
# so a present-but-broken install still fails at the real import below rather
# than silently skipping like importorskip would.
_NAT_MCP_WORKER = "nat.plugins.mcp.server.front_end_plugin_worker"
_NAT_MCP_PARENTS = {
    "nat",
    "nat.plugins",
    "nat.plugins.mcp",
    "nat.plugins.mcp.server",
}
try:
    _has_nat_mcp = importlib.util.find_spec(_NAT_MCP_WORKER) is not None
except ModuleNotFoundError as exc:
    # find_spec imports parents; only expected path misses mean "not installed".
    if exc.name in _NAT_MCP_PARENTS or exc.name == _NAT_MCP_WORKER:
        _has_nat_mcp = False
    else:
        raise

if not _has_nat_mcp:
    pytest.skip("nvidia-nat[mcp] not installed", allow_module_level=True)

from nomos.nat.stateless_worker import MCPFrontEndPluginWorker, StatelessMCPWorker  # noqa: E402


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
