# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the monitor MCP proxy.

Uses an in-process httpx ASGI transport instead of a real upstream so the
proxy is exercised end-to-end without network or threading concerns.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from monitor.server.metrics import MetricsRegistry
from monitor.server.proxy import (
    HOP_BY_HOP_HEADERS,
    SOURCE_HEADER,
    extract_decision_summary,
    extract_method_and_tool,
    extract_request_summary,
    forward_mcp,
)


def _upstream_app(handler) -> FastAPI:
    """Build a tiny upstream that the proxy can target via httpx ASGI transport."""
    app = FastAPI()

    @app.api_route("/mcp", methods=["POST", "GET", "DELETE", "OPTIONS"])
    async def mcp(request: Request):
        return await handler(request)

    return app


def _mcp_tool_response(payload: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }


def _mcp_sse_tool_response(payload: dict) -> bytes:
    return (f"event: message\ndata: {json.dumps(_mcp_tool_response(payload))}\n\n").encode()


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


@pytest.mark.asyncio
async def test_proxy_forwards_body_and_returns_status(registry):
    captured: dict = {}

    async def handler(request: Request):
        captured["body"] = await request.body()
        captured["headers"] = dict(request.headers)
        return JSONResponse({"echo": True}, status_code=200)

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
        ).encode()
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json", "x-council-source": "demo"},
            request_body=body,
            registry=registry,
        )
    assert resp.status_code == 200
    assert json.loads(captured["body"]) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "x"},
    }
    # Source header is forwarded so council can log origin if it wants
    assert captured["headers"].get(SOURCE_HEADER.lower()) == "demo"


@pytest.mark.asyncio
async def test_proxy_records_completion_with_tool_and_source(registry):
    async def handler(request: Request):
        return JSONResponse({"ok": True}, status_code=200)

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "council_authorize", "arguments": {}},
        }
    ).encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json", "x-council-source": "grpc"},
            request_body=body,
            registry=registry,
        )
        # consume body so the streaming response finalizes
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    snap = await registry.snapshot()
    assert snap.completed_in_window == 1
    assert snap.by_source == {"grpc": 1}
    assert snap.by_tool == {"council_authorize": 1}


@pytest.mark.asyncio
async def test_proxy_records_sanitized_decision_summary(registry):
    async def handler(request: Request):
        return JSONResponse(
            _mcp_tool_response(
                {
                    "status": "denied",
                    "writ": None,
                    "writ_id": "",
                    "decision_id": "decision-123",
                    "guidance": ["Provide rollback evidence."],
                    "error": "",
                    "missing_permissions": ["infra:write"],
                    "chairman": {"threshold": "unanimous", "risk_level": "high"},
                    "code_review": {"decision": "approve"},
                    "debate_rounds": 1,
                    "council_members": [
                        {"member_id": "m1", "decision": "approve"},
                        {"member_id": "m2", "decision": "deny", "changed_vote": True},
                    ],
                }
            ),
            status_code=200,
        )

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "council_authorize",
                "arguments": {
                    "agent_id": "agent-admin",
                    "auth_token": "example-test-token",
                    "command": "/usr/bin/ipa user-show nate",
                    "packaged_files": [{"path": "change.py", "contents": "ignored"}],
                    "evidence": [{"kind": "ticket"}],
                    "environment": {"API_TOKEN": "ignored"},
                },
            },
        }
    ).encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json", "x-council-source": "demo"},
            request_body=body,
            registry=registry,
        )
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    snap = await registry.snapshot()
    assert snap.decisions_in_window == 1
    assert snap.by_decision_outcome == {"denied": 1}
    assert snap.by_risk == {"high": 1}
    assert snap.by_error_class == {"permission": 1}
    assert snap.by_missing_permission == {"infra:write": 1}
    assert snap.by_code_review == {"approve": 1}

    recent = await registry.recent(limit=1)
    decision = recent[0].to_dict()["decision"]
    assert decision["outcome"] == "denied"
    assert decision["approval_votes"] == 1
    assert decision["denial_votes"] == 1
    assert decision["changed_votes"] == 1
    assert decision["request"]["agent_id"] == "agent-admin"
    assert decision["request"]["command_executable"] == "ipa"
    assert decision["request"]["packaged_file_count"] == 1
    assert "auth_token" not in json.dumps(decision)
    assert "example-test-token" not in json.dumps(decision)
    assert "/usr/bin/ipa user-show nate" not in json.dumps(decision)


@pytest.mark.asyncio
async def test_proxy_records_decision_summary_from_sse_response(registry):
    async def handler(request: Request):
        return Response(
            content=_mcp_sse_tool_response(
                {
                    "status": "approved",
                    "writ_id": "writ-123",
                    "decision_id": "decision-123",
                    "guidance": [],
                    "error": "",
                    "missing_permissions": [],
                    "chairman": {"threshold": "unanimous", "risk_level": "low"},
                    "council_members": [
                        {"member_id": "m1", "decision": "approve"},
                        {"member_id": "m2", "decision": "approve"},
                    ],
                }
            ),
            media_type="text/event-stream",
        )

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "council_authorize", "arguments": {}},
        }
    ).encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json"},
            request_body=body,
            registry=registry,
        )
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    snap = await registry.snapshot()
    assert snap.decisions_in_window == 1
    assert snap.by_decision_outcome == {"approved": 1}
    assert snap.by_risk == {"low": 1}


@pytest.mark.asyncio
async def test_proxy_default_source_is_direct(registry):
    async def handler(request: Request):
        return JSONResponse({}, status_code=200)

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json"},
            request_body=body,
            registry=registry,
        )
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    snap = await registry.snapshot()
    assert snap.by_source == {"direct": 1}
    # initialize does not contribute to by_tool
    assert snap.by_tool == {}


@pytest.mark.asyncio
async def test_proxy_records_error_when_upstream_fails(registry):
    """A connect-time failure must still produce a recorded completion (ok=False)."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("nope")))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json"},
            request_body=body,
            registry=registry,
        )
    assert resp.status_code == 502
    snap = await registry.snapshot()
    assert snap.completed_in_window == 1
    assert snap.error_count == 1
    assert snap.inflight == 0


@pytest.mark.asyncio
async def test_proxy_ignores_requests_without_json_rpc_method(registry):
    async def handler(request: Request):
        return JSONResponse({}, status_code=200)

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)

    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="GET",
            request_headers={"x-council-source": "demo"},
            request_body=b"",
            registry=registry,
        )
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    snap = await registry.snapshot()
    recent = await registry.recent(limit=10)
    assert snap.completed_in_window == 0
    assert snap.inflight == 0
    assert recent == []


@pytest.mark.asyncio
async def test_proxy_does_not_leak_client_hop_by_hop_values(registry):
    """Client-supplied hop-by-hop headers must not be propagated as-is.

    httpx will set its own framing headers (content-length, connection) for
    the upstream request — that's correct. The invariant we care about is
    that *client-supplied* hop-by-hop *values* (in particular the client's
    Host) don't leak through.
    """
    captured: dict = {}

    async def handler(request: Request):
        captured["headers"] = dict(request.headers)
        return JSONResponse({}, status_code=200)

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)

    bogus_host = "client.example"
    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={
                "content-type": "application/json",
                "connection": "close, x-filtered-hop",
                "transfer-encoding": "chunked",
                "host": bogus_host,
                "x-filtered-hop": "should-not-leak",
            },
            request_body=b"{}",
            registry=registry,
        )
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass

    # Client's bogus Host must not be propagated
    assert captured["headers"].get("host") != bogus_host
    # Connection header must be httpx's, not the client's extra header name.
    assert "x-filtered-hop" not in captured["headers"].get("connection", "")
    # The fact that we filtered HOP_BY_HOP_HEADERS by name means our filter
    # doesn't pass them through; httpx still sets its own.
    for h in ("connection", "transfer-encoding", "host", "content-length"):
        assert h in HOP_BY_HOP_HEADERS


@pytest.mark.asyncio
async def test_proxy_streams_response_body(registry):
    """The proxy must not buffer — it must yield chunks as they arrive."""
    chunks_sent: list[bytes] = [b'{"a":1}\n', b'{"b":2}\n', b'{"c":3}\n']
    chunk_emitted = asyncio.Event()

    async def handler(request: Request):
        async def gen():
            for chunk in chunks_sent:
                yield chunk
                chunk_emitted.set()

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    upstream = _upstream_app(handler)
    transport = httpx.ASGITransport(app=upstream)
    async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
        resp = await forward_mcp(
            client=client,
            upstream_url="http://upstream/mcp",
            method="POST",
            request_headers={"content-type": "application/json"},
            request_body=b"{}",
            registry=registry,
        )
        seen = b""
        async for chunk in resp.body_iterator:  # type: ignore[attr-defined]
            seen += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert seen == b"".join(chunks_sent)


def test_extract_method_and_tool_handles_object_body():
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "council_authorize"}}
    ).encode()
    assert extract_method_and_tool(body) == ("tools/call", "council_authorize")


def test_extract_method_and_tool_handles_initialize():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    assert extract_method_and_tool(body) == ("initialize", "")


def test_extract_method_and_tool_returns_blanks_on_garbage():
    assert extract_method_and_tool(b"not json") == ("", "")
    assert extract_method_and_tool(b"") == ("", "")
    assert extract_method_and_tool(b"[]") == ("", "")  # arrays = batch, treat aggregate


def test_extract_request_summary_redacts_sensitive_values():
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "council_authorize",
                "arguments": {
                    "agent_id": "agent_test",
                    "auth_token": "placeholder-token",
                    "command": "/bin/echo hello",
                    "environment": {"CONFIG_VALUE": "hidden"},
                },
            },
        }
    ).encode()

    summary = extract_request_summary(body)

    assert summary is not None
    payload = summary.to_dict()
    assert payload["agent_id"] == "agent_test"
    assert payload["command_executable"] == "echo"
    assert payload["command_hash"]
    assert payload["env_key_count"] == 1
    assert "placeholder-token" not in json.dumps(payload)
    assert "/bin/echo hello" not in json.dumps(payload)
    assert "hidden" not in json.dumps(payload)


def test_extract_decision_summary_parses_mcp_tool_payload():
    response = json.dumps(
        _mcp_tool_response(
            {
                "status": "approved",
                "writ": "opaque-writ",
                "writ_id": "writ-123",
                "decision_id": "decision-123",
                "chairman": {"threshold": "unanimous", "risk_level": "low"},
                "council_members": [
                    {"member_id": "m1", "decision": "approve"},
                    {"member_id": "m2", "decision": "approve"},
                ],
            }
        )
    ).encode()

    summary = extract_decision_summary(
        tool="council_authorize",
        request_summary=None,
        response_body=response,
    )

    assert summary is not None
    assert summary.outcome == "approved"
    assert summary.risk_level == "low"
    assert summary.approval_votes == 2
    assert summary.writ_issued is True
    assert summary.writ_id == "writ-123"


def test_extract_decision_summary_counts_versioned_propose_alias():
    response = json.dumps(
        _mcp_tool_response(
            {
                "success": True,
                "state": "approved",
                "chairman": {"threshold": "quorum", "risk_level": "low"},
                "council_members": [
                    {"member_id": "m1", "decision": "approve"},
                    {"member_id": "m2", "decision": "abstain"},
                ],
            }
        )
    ).encode()

    summary = extract_decision_summary(
        tool="council_v2_propose_intent",
        request_summary=None,
        response_body=response,
    )

    assert summary is not None
    assert summary.outcome == "approved"
    assert summary.approval_votes == 1
    assert summary.abstain_votes == 1
