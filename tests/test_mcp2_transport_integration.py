# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in transport contract test for the MCP 2.0.0 pinned by Hermes 0.21.3."""

from contextlib import asynccontextmanager
from importlib.metadata import version

import pytest

from nomos_client.transports.mcp import MCPAuthorizationClient

pytestmark = pytest.mark.skipif(
    version("mcp") != "2.0.0",
    reason="run in an isolated MCP 2.0.0 environment",
)


@pytest.mark.asyncio
async def test_mcp2_transport_uses_httpx2_client_and_two_stream_contract(monkeypatch):
    import httpx2
    import mcp
    from mcp.client import streamable_http

    observed = {}

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            observed["http"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    @asynccontextmanager
    async def fake_transport(url, *, http_client=None, terminate_on_close=True):
        observed["transport"] = (
            url,
            {
                "http_client": http_client,
                "terminate_on_close": terminate_on_close,
            },
        )
        yield ("read", "write")

    class FakeSession:
        def __init__(self, read, write):
            observed["streams"] = (read, write)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def initialize(self):
            observed["initialized"] = True

    monkeypatch.setattr(httpx2, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_transport)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    client = MCPAuthorizationClient(
        "https://nomos.example/mcp",
        headers={"Authorization": "Bearer secret"},
        timeout_seconds=17,
    )
    async with client._session() as session:
        assert isinstance(session, FakeSession)

    assert observed["http"] == {
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 17,
    }
    assert observed["transport"][0] == "https://nomos.example/mcp"
    assert isinstance(observed["transport"][1]["http_client"], FakeHTTPClient)
    assert observed["streams"] == ("read", "write")
    assert observed["initialized"] is True
