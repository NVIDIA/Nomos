# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

import grpc
import pytest
import pytest_asyncio
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct

from nomos.grpc.mcp_client import MCPToolResult
from nomos.grpc.server import CouncilGrpcProxy, serve
from nomos.grpc.v1 import council_pb2, council_pb2_grpc


class RecordingMCPClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        responses: dict[str, dict[str, Any]] | None = None,
    ):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error
        self.responses = responses or {}

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> MCPToolResult:
        self.calls.append((tool_name, dict(arguments)))
        if self.error is not None:
            raise self.error
        response = self.responses.get(
            tool_name,
            {
                "tool": tool_name,
                "success": True,
                "arguments": dict(arguments),
            },
        )
        return MCPToolResult(
            data=response,
            raw_json=json.dumps(response, separators=(",", ":")),
        )


@pytest.mark.asyncio
async def test_serve_rejects_unknown_deployment_profile_before_transport_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "productoin")

    def unexpected_server_construction():
        raise AssertionError("gRPC server constructed before transport validation")

    monkeypatch.setattr("nomos.grpc.server.grpc.aio.server", unexpected_server_construction)

    with pytest.raises(ValueError, match="unknown deployment profile"):
        await serve(mcp_url="https://council:9901/mcp", host="127.0.0.1", port=0)


@asynccontextmanager
async def grpc_proxy_stub(client: RecordingMCPClient):
    server = grpc.aio.server()
    council_pb2_grpc.add_CouncilProxyServicer_to_server(
        CouncilGrpcProxy(client, mcp_url="http://council.test/mcp"),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield council_pb2_grpc.CouncilProxyStub(channel)
    finally:
        await channel.close()
        await server.stop(0)


@pytest_asyncio.fixture
async def mcp_client():
    return RecordingMCPClient()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc_name", "tool_name", "arguments"),
    [
        (
            "Authorize",
            "council_authorize",
            {
                "auth_token": "token",
                "agent_id": "agent-developer",
                "command": "/bin/echo approved",
                "working_directory": "/workspace",
                "description": "Authorize a simple smoke command",
                "target_resource": "/workspace",
                "justification": "Validate gRPC proxy route",
            },
        ),
        (
            "ProposeIntent",
            "council_propose_intent",
            {
                "auth_token": "token",
                "agent_id": "agent-developer",
                "command": "/bin/ls /workspace",
                "description": "Propose a read-only smoke command",
                "target_resource": "/workspace",
                "justification": "Validate gRPC propose route",
            },
        ),
        ("CheckStatus", "council_check_status", {"intent_id": "intent-123"}),
        ("PendingApprovals", "council_pending_approvals", {}),
        ("GetPublicKey", "council_get_public_key", {}),
        (
            "CheckWritStatus",
            "council_check_writ_status",
            {"auth_token": "token", "writ_id": "writ-123"},
        ),
    ],
)
async def test_named_rpc_paths_forward_to_expected_mcp_tools(
    mcp_client,
    rpc_name,
    tool_name,
    arguments,
):
    async with grpc_proxy_stub(mcp_client) as stub:
        response = await getattr(stub, rpc_name)(
            council_pb2.CouncilToolRequest(raw_json=json.dumps(arguments))
        )

    assert mcp_client.calls == [(tool_name, arguments)]
    assert response.tool_name == tool_name
    assert json.loads(response.raw_json)["tool"] == tool_name


@pytest.mark.asyncio
async def test_authorize_forwards_raw_json_to_mcp_with_packaged_files(mcp_client):
    arguments = {
        "auth_token": "token",
        "agent_id": "agent-developer",
        "command": "/usr/local/bin/python3 scripts/task.py",
        "working_directory": "/workspace",
        "packaged_files": [
            {
                "path": "scripts/task.py",
                "content_base64": "cHJpbnQoJ29rJykK",
                "sha256": "9b787807f69f08f203f50cb6450e89ae7dbe70a5340b1e15e2f5f55aa4e9a14f",
                "mode": "0600",
            }
        ],
    }

    async with grpc_proxy_stub(mcp_client) as stub:
        response = await stub.Authorize(
            council_pb2.CouncilToolRequest(raw_json=json.dumps(arguments))
        )

    assert mcp_client.calls == [("council_authorize", arguments)]
    assert response.tool_name == "council_authorize"
    assert json.loads(response.raw_json)["arguments"] == arguments
    result = MessageToDict(response.result, preserving_proto_field_name=True)
    assert result["tool"] == "council_authorize"


@pytest.mark.asyncio
async def test_struct_arguments_are_forwarded_to_mcp(mcp_client):
    struct = Struct()
    ParseDict({"intent_id": "intent-123"}, struct)

    async with grpc_proxy_stub(mcp_client) as stub:
        response = await stub.CheckStatus(council_pb2.CouncilToolRequest(arguments=struct))

    assert mcp_client.calls == [("council_check_status", {"intent_id": "intent-123"})]
    assert response.tool_name == "council_check_status"


@pytest.mark.asyncio
async def test_raw_json_takes_precedence_over_struct_arguments(mcp_client):
    struct = Struct()
    ParseDict({"intent_id": "from-struct"}, struct)

    async with grpc_proxy_stub(mcp_client) as stub:
        response = await stub.CheckStatus(
            council_pb2.CouncilToolRequest(
                arguments=struct,
                raw_json=json.dumps({"intent_id": "from-raw-json"}),
            )
        )

    assert mcp_client.calls == [("council_check_status", {"intent_id": "from-raw-json"})]
    assert response.tool_name == "council_check_status"


@pytest.mark.asyncio
async def test_generic_call_tool_forwards_allowlisted_council_tool(mcp_client):
    arguments = {"writ_id": "writ-123", "auth_token": "token"}

    async with grpc_proxy_stub(mcp_client) as stub:
        response = await stub.CallTool(
            council_pb2.CallToolRequest(
                tool_name="council_check_writ_status",
                raw_json=json.dumps(arguments),
            )
        )

    assert mcp_client.calls == [("council_check_writ_status", arguments)]
    assert response.tool_name == "council_check_writ_status"


@pytest.mark.asyncio
async def test_generic_call_tool_is_limited_to_council_tools(mcp_client):
    async with grpc_proxy_stub(mcp_client) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CallTool(council_pb2.CallToolRequest(tool_name="not_a_council_tool"))

    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert mcp_client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grpc_request",
    [
        council_pb2.CouncilToolRequest(raw_json="{not json"),
        council_pb2.CouncilToolRequest(raw_json='["not", "object"]'),
    ],
)
async def test_named_rpc_rejects_invalid_raw_json(mcp_client, grpc_request):
    async with grpc_proxy_stub(mcp_client) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.Authorize(grpc_request)

    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert mcp_client.calls == []


@pytest.mark.asyncio
async def test_generic_call_tool_rejects_non_object_raw_json(mcp_client):
    async with grpc_proxy_stub(mcp_client) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CallTool(
                council_pb2.CallToolRequest(
                    tool_name="council_get_public_key",
                    raw_json='"not an object"',
                )
            )

    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert mcp_client.calls == []


@pytest.mark.asyncio
async def test_response_preserves_raw_json_and_struct_payload():
    response = {
        "status": "approved",
        "writ_id": "writ-123",
        "nested": {"values": ["a", "b"], "ok": True},
    }
    client = RecordingMCPClient(responses={"council_authorize": response})

    async with grpc_proxy_stub(client) as stub:
        result = await stub.Authorize(
            council_pb2.CouncilToolRequest(raw_json=json.dumps({"command": "/bin/true"}))
        )

    assert result.raw_json == json.dumps(response, separators=(",", ":"))
    assert MessageToDict(result.result, preserving_proto_field_name=True) == response


@pytest.mark.asyncio
async def test_mcp_failure_maps_to_unavailable():
    client = RecordingMCPClient(error=RuntimeError("connection refused"))

    async with grpc_proxy_stub(client) as stub:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetPublicKey(council_pb2.CouncilToolRequest())

    assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_health_reports_mcp_connectivity():
    async with grpc_proxy_stub(RecordingMCPClient()) as stub:
        healthy = await stub.Health(council_pb2.HealthRequest())

    assert healthy.ok is True
    assert healthy.mcp_url == "http://council.test/mcp"

    async with grpc_proxy_stub(RecordingMCPClient(error=RuntimeError("down"))) as stub:
        unhealthy = await stub.Health(council_pb2.HealthRequest())

    assert unhealthy.ok is False
    assert "down" in unhealthy.error
