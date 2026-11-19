# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the stateless MCP front-end worker (nomos/nat/stateless_worker.py)."""

import asyncio
import importlib.util
from types import SimpleNamespace

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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_delivery_sink_closed_when_route_registration_fails(monkeypatch, tmp_path, failure):
    from nomos.nat.delivery_trace import DeliverySink

    expected = failure("controlled route failure")

    async def bad_routes(self, mcp, builder):
        raise expected

    monkeypatch.setattr(MCPFrontEndPluginWorker, "add_routes", bad_routes)
    worker = StatelessMCPWorker.__new__(StatelessMCPWorker)
    sink = DeliverySink(str(tmp_path))
    worker._delivery_sink = sink
    with pytest.raises(failure) as caught:
        await worker.add_routes(None, None)
    assert caught.value is expected
    assert sink.closed.is_set() and not sink.thread.is_alive() and sink.file.closed
    assert worker._delivery_sink is None


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["factory", "middleware"])
async def test_delivery_sink_closed_before_lifespan_factory_failure(monkeypatch, tmp_path, where):
    expected = RuntimeError("controlled app setup failure")

    class App:
        def add_middleware(self, *args, **kwargs):
            raise expected

    class MCP:
        settings = SimpleNamespace(stateless_http=False)

        def streamable_http_app(self):
            if where == "factory":
                raise expected
            return App()

    async def create(self):
        return MCP()

    monkeypatch.setattr(MCPFrontEndPluginWorker, "create_mcp_server", create)
    monkeypatch.setenv("NOMOS_DELIVERY_TRACE", "1")
    monkeypatch.setenv("NOMOS_DELIVERY_TRACE_DIRECTORY", str(tmp_path))
    worker = StatelessMCPWorker.__new__(StatelessMCPWorker)
    mcp = await worker.create_mcp_server()
    sink = worker._delivery_sink
    with pytest.raises(RuntimeError) as caught:
        mcp.streamable_http_app()
    assert caught.value is expected
    assert sink.closed.is_set() and not sink.thread.is_alive() and sink.file.closed
    assert worker._delivery_sink is None
