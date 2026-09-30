# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the small, harness-independent Nomos authorization client."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from nomos_client import AuthorizeRequest, ConsumeRequest
from nomos_client.transports.mcp import (
    MAX_RESPONSE_BYTES,
    MCPAuthorizationClient,
    MCPAuthorizationClientError,
)


class FakeSession:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = []

    async def call_tool(self, name, *, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=self.payload)])


def _install_session(monkeypatch, client, session):
    @asynccontextmanager
    async def fake_session():
        yield session

    monkeypatch.setattr(client, "_session", fake_session)


@pytest.mark.asyncio
async def test_mcp_client_calls_explicit_authorize_and_consume_tools(monkeypatch):
    client = MCPAuthorizationClient("https://nomos.example/mcp")
    authorize_session = FakeSession(
        '{"status":"approved","council_id":"council-1","writ":"blob","writ_id":"writ-1"}'
    )
    _install_session(monkeypatch, client, authorize_session)

    decision = await client.authorize(
        AuthorizeRequest(
            auth_token="secret",
            agent_id="agent-1",
            council_id="council-1",
            command="echo approved",
            working_directory="/workspace",
        )
    )

    assert decision.status == "approved"
    assert authorize_session.calls[0][0] == "council_authorize"
    assert authorize_session.calls[0][1]["auth_token"] == "secret"

    consume_session = FakeSession(
        '{"status":"consumed","consumed":true,"council_id":"council-1","writ_id":"writ-1"}'
    )
    _install_session(monkeypatch, client, consume_session)
    consumed = await client.consume(
        ConsumeRequest(
            council_id="council-1",
            writ_id="writ-1",
            nonce="cd" * 32,
            auth_token="secret",
        )
    )

    assert consumed.consumed is True
    assert consume_session.calls == [
        (
            "council_consume_writ",
            {
                "api_version": "v1",
                "council_id": "council-1",
                "writ_id": "writ-1",
                "nonce": "cd" * 32,
                "auth_token": "secret",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not-json", "[]", '{"status": 7}'])
async def test_mcp_client_rejects_malformed_authorization_responses(monkeypatch, payload):
    client = MCPAuthorizationClient("https://nomos.example/mcp")
    _install_session(monkeypatch, client, FakeSession(payload))

    with pytest.raises(MCPAuthorizationClientError):
        await client.authorize(
            AuthorizeRequest(
                auth_token="secret",
                agent_id="agent-1",
                council_id="council-1",
                command="echo approved",
                working_directory="/workspace",
            )
        )


@pytest.mark.asyncio
async def test_mcp_client_rejects_oversized_response_before_json_parsing(monkeypatch):
    client = MCPAuthorizationClient("https://nomos.example/mcp")
    _install_session(monkeypatch, client, FakeSession(" " * (MAX_RESPONSE_BYTES + 1)))

    with pytest.raises(MCPAuthorizationClientError, match="too large"):
        await client.authorize(
            AuthorizeRequest(
                auth_token="secret",
                agent_id="agent-1",
                council_id="council-1",
                command="echo approved",
                working_directory="/workspace",
            )
        )
