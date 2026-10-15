# SPDX-License-Identifier: Apache-2.0
"""The live source exposes two authenticated reads over Nomos's stateless MCP."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from assurance.council_source import create_app

COUNCIL = "council-test"
AUTH = {"Authorization": "Bearer test-reader"}
PAGE_PATH = "/debug/tools/call/council_ledger_page"
GET_PATH = "/debug/tools/call/council_ledger_get"
PAGE = {
    "council_id": COUNCIL,
    "chain_id": "ledger-council-test",
    "head_sequence": 0,
    "entries": [{"sequence": 0, "payload": {"example": "unchanged"}}],
    "next_cursor": None,
    "error": "",
}


def rpc_response(result, *, request_id=2, sse=True):
    body = {"jsonrpc": "2.0", "id": request_id, "result": result}
    if sse:
        return httpx.Response(
            200,
            text=": keepalive\r\nevent: message\r\ndata: " + json.dumps(body) + "\r\n\r\n",
            headers={"Content-Type": "text/event-stream"},
        )
    return httpx.Response(200, json=body)


def tool_response(payload=PAGE, *, sse=True):
    return rpc_response(
        {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        sse=sse,
    )


@asynccontextmanager
async def gateway(handler):
    calls = []

    async def transport(request):
        assert str(request.url) == "http://council:9901/mcp"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        body = json.loads(request.content)
        calls.append(body)
        if body["method"] == "initialize":
            return rpc_response({"protocolVersion": "2025-03-26"}, request_id=1)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["method"] == "tools/call"
        return await handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as upstream:
        app = create_app(
            upstream_url="http://council:9901",
            council_id=COUNCIL,
            token="test-reader",
            client=upstream,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://bridge"
        ) as client:
            yield client, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [True, False])
@pytest.mark.parametrize("include_council", [True, False])
async def test_page_uses_real_mcp_and_binds_the_council(sse, include_council):
    async def respond(request):
        arguments = json.loads(request.content)["params"]
        assert arguments == {
            "name": "council_ledger_page",
            "arguments": {"council_id": COUNCIL, "cursor": 0, "limit": 1},
        }
        return tool_response(sse=sse)

    async with gateway(respond) as (client, calls):
        body = {"cursor": 0, "limit": 1}
        if include_council:
            body["council_id"] = COUNCIL
        response = await client.post(PAGE_PATH, json=body, headers={**AUTH, "Cookie": "secret=x"})
        assert response.status_code == 200
        assert response.json() == PAGE
        assert response.headers["cache-control"] == "no-store"
        assert [call["method"] for call in calls] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]


@pytest.mark.asyncio
async def test_entry_read_preserves_the_signed_record():
    payload = {
        "council_id": COUNCIL,
        "chain_id": PAGE["chain_id"],
        "found": True,
        "entry": PAGE["entries"][0],
    }

    async def respond(request):
        assert json.loads(request.content)["params"] == {
            "name": "council_ledger_get",
            "arguments": {"council_id": COUNCIL, "sequence": 0},
        }
        return tool_response(payload)

    async with gateway(respond) as (client, _):
        result = await client.post(GET_PATH, json={"sequence": 0}, headers=AUTH)
        assert result.status_code == 200
        assert result.json() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-reader"}]
)
async def test_authentication_precedes_any_council_access(headers):
    async with gateway(lambda _: None) as (client, calls):
        response = await client.post(PAGE_PATH, content=b"not json", headers=headers)
        assert response.status_code == 401
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body,status",
    [
        (PAGE_PATH, {"council_id": "another"}, 404),
        (PAGE_PATH, {"council_id": None}, 422),
        (PAGE_PATH, {"limit": 201}, 422),
        (PAGE_PATH, {"limit": 0}, 422),
        (PAGE_PATH, {"limit": True}, 422),
        (PAGE_PATH, {"cursor": -1}, 422),
        (PAGE_PATH, {"limit": "1"}, 422),
        (PAGE_PATH, {"token": "management-secret"}, 422),
        (GET_PATH, {"sequence": -1}, 422),
        (GET_PATH, {}, 422),
        ("/debug/tools/call/council_authorize", {}, 404),
        ("/fixtures/seed", {}, 404),
        ("/mcp", {}, 404),
    ],
)
async def test_only_bounded_ledger_reads_are_allowed(path, body, status):
    async with gateway(lambda _: None) as (client, calls):
        response = await client.post(path, json=body, headers=AUTH)
        assert response.status_code == status
        assert calls == []


@pytest.mark.asyncio
async def test_request_size_is_bounded_even_without_content_length():
    async def chunks():
        yield b" " * 3000
        yield b" " * 3000

    async with gateway(lambda _: None) as (client, calls):
        response = await client.post(PAGE_PATH, content=chunks(), headers=AUTH)
        assert response.status_code == 413
        assert calls == []
        malformed = await client.post(PAGE_PATH, content="[invalid", headers=AUTH)
        assert malformed.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="upstream-secret"),
        httpx.Response(302, headers={"Location": "http://untrusted"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)),
        httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 2, "error": {"message": "upstream-secret"}}
        ),
        rpc_response({"content": [], "isError": True}),
        rpc_response({"content": []}),
        tool_response({"error": "upstream-secret", "council_id": COUNCIL}),
        tool_response({**PAGE, "council_id": "another"}),
        rpc_response({"content": [{"type": "text", "text": "null"}]}),
    ],
)
async def test_upstream_failure_is_closed_and_does_not_leak_details(response):
    async def respond(_):
        return response

    async with gateway(respond) as (client, _):
        result = await client.post(PAGE_PATH, json={}, headers=AUTH)
        assert result.status_code == 502
        assert result.json() == {"detail": "Council ledger read failed"}


@pytest.mark.asyncio
async def test_whole_read_has_a_deadline(monkeypatch):
    monkeypatch.setattr("assurance.council_source.READ_TIMEOUT_SECONDS", 0.01)

    async def respond(_):
        await asyncio.sleep(1)
        return tool_response()

    async with gateway(respond) as (client, _):
        result = await client.post(PAGE_PATH, json={}, headers=AUTH)
        assert result.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["session", "version", "connection"])
async def test_initialization_failures_never_attempt_a_tool_call(failure):
    calls = []

    async def transport(request):
        calls.append(json.loads(request.content)["method"])
        if failure == "connection":
            raise httpx.ConnectError("private connection details", request=request)
        response = rpc_response(
            {"protocolVersion": "unsupported" if failure == "version" else "2025-03-26"},
            request_id=1,
        )
        if failure == "session":
            response.headers["Mcp-Session-Id"] = "stateful-session"
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as upstream:
        app = create_app(
            upstream_url="http://council", council_id=COUNCIL, token="test-reader", client=upstream
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://bridge"
        ) as client:
            result = await client.post(PAGE_PATH, json={}, headers=AUTH)
            assert result.status_code == 502
            assert result.json() == {"detail": "Council ledger read failed"}
            assert calls == ["initialize"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///tmp/source",
        "http://user:secret@council",  # pragma: allowlist secret -- rejected dummy URL
        "http://council?token=x",
        "http://council/path",
    ],
)
def test_configuration_cannot_add_upstream_credentials_or_arbitrary_paths(url):
    with pytest.raises(ValueError):
        create_app(upstream_url=url, council_id=COUNCIL, token="test-reader")


def test_empty_source_token_is_rejected():
    with pytest.raises(ValueError):
        create_app(upstream_url="http://council", council_id=COUNCIL, token="")
