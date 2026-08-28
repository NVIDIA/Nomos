# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify source-attribution headers are passed to the MCP client.

Both the gRPC proxy and the demo's CouncilClient should pass
`X-Council-Source` upstream so the monitor proxy can attribute traffic.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest


class _FakeClientSession:
    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def initialize(self):
        return None

    async def call_tool(self, tool_name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')])


def _capture_streamable_client(captured: list[dict]):
    @contextlib.asynccontextmanager
    async def fake_streamablehttp_client(url, *, headers=None, **kwargs):
        captured.append({"url": url, "headers": dict(headers or {})})
        yield object(), object(), None

    return fake_streamablehttp_client


@pytest.mark.asyncio
async def test_grpc_proxy_client_sends_grpc_source_header(monkeypatch):
    captured: list[dict] = []

    import nomos.grpc.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "streamablehttp_client", _capture_streamable_client(captured))
    monkeypatch.setattr(mcp_client, "ClientSession", _FakeClientSession)

    client = mcp_client.StreamableHttpMCPClient(
        "http://monitor:9901/mcp",
        headers={"X-Council-Source": "grpc"},
    )
    await client.call_tool("anything", {})

    assert captured
    assert captured[0]["headers"] == {"X-Council-Source": "grpc"}


@pytest.mark.asyncio
async def test_demo_council_client_sends_demo_source_header(monkeypatch):
    captured: list[dict] = []

    import demo.server.council as council

    monkeypatch.setattr(council, "streamablehttp_client", _capture_streamable_client(captured))
    monkeypatch.setattr(council, "ClientSession", _FakeClientSession)

    client = council.CouncilClient("http://monitor:9901/mcp")
    await client.get_public_key()

    assert captured
    assert captured[0]["headers"] == {"X-Council-Source": "demo"}


@pytest.mark.asyncio
async def test_demo_council_client_authenticates_to_monitor_proxy(monkeypatch):
    captured: list[dict] = []

    import demo.server.council as council

    monkeypatch.setattr(council, "streamablehttp_client", _capture_streamable_client(captured))
    monkeypatch.setattr(council, "ClientSession", _FakeClientSession)

    client = council.CouncilClient(
        "http://monitor:9901/mcp",
        proxy_token="generated-proxy-token",  # pragma: allowlist secret
    )
    await client.get_public_key()

    assert captured
    assert captured[0]["headers"] == {
        "X-Council-Source": "demo",
        "Authorization": "Bearer generated-proxy-token",  # pragma: allowlist secret
    }


def test_demo_council_client_summarizes_exception_groups():
    import demo.server.council as council

    exc = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ConnectionError("connection refused"), TimeoutError("timed out")],
    )

    assert council._summarize_exception(exc) == (
        "unhandled errors in a TaskGroup (2 sub-exception(s)): "
        "ConnectionError: connection refused | TimeoutError: timed out"
    )
