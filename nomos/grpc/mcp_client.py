# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP client used by the gRPC council proxy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@dataclass(frozen=True)
class MCPToolResult:
    """Parsed MCP tool response plus the exact JSON text returned by MCP."""

    data: dict[str, Any]
    raw_json: str


class MCPToolClient(Protocol):
    """Minimal client interface needed by the gRPC proxy."""

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> MCPToolResult:
        """Call an MCP tool and return its JSON object result."""


class MCPResponseError(RuntimeError):
    """Raised when the MCP service returns a response the proxy cannot expose."""


class StreamableHttpMCPClient:
    """Thin MCP Streamable HTTP client for council tool calls.

    `headers` are sent on every request and used by the council monitor's
    proxy to attribute traffic origin (`X-Council-Source`). Pass `None`
    (the default) for direct callers; the gRPC proxy passes
    `{"X-Council-Source": "grpc"}`.
    """

    def __init__(self, mcp_url: str, *, headers: Mapping[str, str] | None = None):
        self._mcp_url = mcp_url
        self._headers: dict[str, str] | None = dict(headers) if headers else None

    @asynccontextmanager
    async def _session(self):
        async with streamablehttp_client(self._mcp_url, headers=self._headers) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> MCPToolResult:
        async with self._session() as session:
            result = await session.call_tool(tool_name, arguments=dict(arguments))
        return parse_mcp_tool_result(result)


def parse_mcp_tool_result(result: Any) -> MCPToolResult:
    """Parse the JSON text body used by NAT/MCP council tools."""
    try:
        text = result.content[0].text  # type: ignore[attr-defined]
    except (AttributeError, IndexError) as exc:
        raise MCPResponseError(f"unexpected MCP tool result shape: {result!r}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPResponseError("MCP tool returned non-JSON text") from exc
    if not isinstance(data, dict):
        raise MCPResponseError("MCP tool returned JSON that is not an object")
    return MCPToolResult(data=data, raw_json=text)
