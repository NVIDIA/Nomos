# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Async gRPC proxy for the Nomos council MCP service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct

from nomos.grpc.mcp_client import MCPToolClient, StreamableHttpMCPClient
from nomos.grpc.v1 import council_pb2, council_pb2_grpc

logger = logging.getLogger(__name__)

RPC_TO_MCP_TOOL = {
    "Authorize": "council_authorize",
    "ProposeIntent": "council_propose_intent",
    "CheckStatus": "council_check_status",
    "PendingApprovals": "council_pending_approvals",
    "GetPublicKey": "council_get_public_key",
    "CheckWritStatus": "council_check_writ_status",
}
ALLOWED_TOOLS = frozenset(RPC_TO_MCP_TOOL.values())


class CouncilGrpcProxy(council_pb2_grpc.CouncilProxyServicer):
    """gRPC servicer that forwards calls to council MCP tools."""

    def __init__(self, client: MCPToolClient, mcp_url: str):
        self._client = client
        self._mcp_url = mcp_url

    async def Authorize(self, request, context):
        return await self._call_known_tool("Authorize", request, context)

    async def ProposeIntent(self, request, context):
        return await self._call_known_tool("ProposeIntent", request, context)

    async def CheckStatus(self, request, context):
        return await self._call_known_tool("CheckStatus", request, context)

    async def PendingApprovals(self, request, context):
        return await self._call_known_tool("PendingApprovals", request, context)

    async def GetPublicKey(self, request, context):
        return await self._call_known_tool("GetPublicKey", request, context)

    async def CheckWritStatus(self, request, context):
        return await self._call_known_tool("CheckWritStatus", request, context)

    async def CallTool(self, request, context):
        tool_name = request.tool_name.strip()
        if tool_name not in ALLOWED_TOOLS:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"tool {tool_name!r} is not exposed by this gRPC proxy",
            )
            raise AssertionError("context.abort returned unexpectedly")
        arguments = await _arguments_from_request(request, context)
        return await self._call_mcp_tool(tool_name, arguments, context)

    async def Health(self, request, context):
        try:
            await self._client.call_tool("council_get_public_key", {})
        except Exception as exc:
            return council_pb2.HealthResponse(
                ok=False,
                mcp_url=self._mcp_url,
                error=f"MCP health check failed: {exc}",
            )
        return council_pb2.HealthResponse(ok=True, mcp_url=self._mcp_url)

    async def _call_known_tool(self, rpc_name: str, request, context):
        arguments = await _arguments_from_request(request, context)
        return await self._call_mcp_tool(RPC_TO_MCP_TOOL[rpc_name], arguments, context)

    async def _call_mcp_tool(self, tool_name: str, arguments: Mapping[str, Any], context):
        try:
            result = await self._client.call_tool(tool_name, arguments)
        except Exception as exc:
            logger.exception("MCP tool %s failed", tool_name)
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                f"MCP tool {tool_name!r} failed: {exc}",
            )
            raise AssertionError("context.abort returned unexpectedly") from None

        try:
            result_struct = Struct()
            ParseDict(result.data, result_struct)
            raw_json = result.raw_json or json.dumps(result.data, separators=(",", ":"))
            response_cls = council_pb2.CouncilToolResponse  # type: ignore[attr-defined]
            return response_cls(
                tool_name=tool_name,
                result=result_struct,
                raw_json=raw_json,
            )
        except Exception as exc:
            logger.exception("Failed to encode gRPC response for %s", tool_name)
            await context.abort(
                grpc.StatusCode.INTERNAL,
                f"MCP tool {tool_name!r} returned an unencodable result: {exc}",
            )
            raise AssertionError("context.abort returned unexpectedly") from None


async def _arguments_from_request(request, context) -> dict[str, Any]:
    raw_json = getattr(request, "raw_json", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"raw_json must be a JSON object: {exc}",
            )
            raise AssertionError("context.abort returned unexpectedly") from None
        if not isinstance(parsed, dict):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "raw_json must decode to a JSON object",
            )
            raise AssertionError("context.abort returned unexpectedly")
        return parsed

    data = MessageToDict(request.arguments, preserving_proto_field_name=True)
    if not isinstance(data, dict):
        await context.abort(
            grpc.StatusCode.INVALID_ARGUMENT,
            "arguments must be a JSON object",
        )
        raise AssertionError("context.abort returned unexpectedly")
    return data


async def serve(
    *,
    mcp_url: str,
    host: str,
    port: int,
) -> None:
    server = grpc.aio.server()
    # Tag outbound MCP traffic with the gRPC source so the council monitor
    # can attribute calls in the dashboard. The header is informational and
    # ignored by the council itself; only the monitor proxy reads it.
    headers = {"X-Council-Source": "grpc"}
    # When the upstream is the monitor proxy with MCP-auth enabled, forward
    # the configured Bearer token so internal gRPC traffic isn't blocked.
    proxy_token = os.environ.get("COUNCIL_MCP_PROXY_TOKEN", "").strip()
    if proxy_token:
        headers["Authorization"] = f"Bearer {proxy_token}"
    client = StreamableHttpMCPClient(mcp_url, headers=headers)
    council_pb2_grpc.add_CouncilProxyServicer_to_server(
        CouncilGrpcProxy(client, mcp_url=mcp_url),
        server,
    )
    bind_target = f"{host}:{port}"
    bound_port = server.add_insecure_port(bind_target)
    if bound_port == 0:
        raise RuntimeError(f"failed to bind gRPC council proxy on {bind_target}")

    await server.start()
    logger.info("Council gRPC proxy listening on %s; forwarding to %s", bind_target, mcp_url)
    await server.wait_for_termination()


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("COUNCIL_GRPC_LOG_LEVEL", "INFO")),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(
        serve(
            mcp_url=os.environ.get("COUNCIL_MCP_URL", "http://localhost:9901/mcp"),
            host=os.environ.get("COUNCIL_GRPC_HOST", "127.0.0.1"),
            port=_env_int("COUNCIL_GRPC_PORT", 9904),
        )
    )


if __name__ == "__main__":
    main()
