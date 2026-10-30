# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The only code that talks to Nomos over the network.

- Council: ``council_authorize`` (and, when the operator opts in,
  ``council_get_public_key``) over the Monitor's MCP endpoint (streamable HTTP),
  with the Monitor proxy bearer token when one is configured.
- Executor: ``POST /execute`` with the base64 writ; the response body is read
  with a byte bound.

Both are small protocols so tests can substitute fakes.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from .settings import Connection


class Council(Protocol):
    async def authorize(self, **fields: Any) -> dict[str, Any]: ...

    # Used only with trust.council_public_key_source = "council".
    async def public_key(self, council_id: str) -> dict[str, Any]: ...


class Executor(Protocol):
    async def execute(self, writ: str, agent_id: str) -> dict[str, Any]: ...


def _parse_tool_result(result: Any) -> dict[str, Any]:
    # Raw service text never reaches the model: only a structured AuthorizeOutput or a fixed error.
    if getattr(result, "isError", False):
        return {"status": "error", "error": "council tool call failed"}
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"status": "error", "error": "council returned a non-JSON response"}
            return parsed if isinstance(parsed, dict) else {"status": "error", "error": "non-object response"}
    return {"status": "error", "error": "empty council response"}


class McpCouncil:
    def __init__(self, connection: Connection) -> None:
        self.c = connection

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.c.proxy_token}"} if self.c.proxy_token else {}

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with (
            streamablehttp_client(self.c.mcp_url, headers=self._headers(), timeout=self.c.timeout_s) as (
                r,
                w,
                _,
            ),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool, arguments=arguments)
        return _parse_tool_result(result)

    async def authorize(self, **fields: Any) -> dict[str, Any]:
        return await self._call("council_authorize", fields)

    async def public_key(self, council_id: str) -> dict[str, Any]:
        return await self._call("council_get_public_key", {"council_id": council_id})


class HttpExecutor:
    def __init__(self, connection: Connection, max_response_bytes: int) -> None:
        self.c, self.max_response_bytes = connection, max_response_bytes

    async def execute(self, writ: str, agent_id: str) -> dict[str, Any]:
        async with (
            httpx.AsyncClient(timeout=self.c.timeout_s) as client,
            client.stream(
                "POST",
                f"{self.c.executor_url.rstrip('/')}/execute",
                json={"writ": writ},
                headers={"X-Agent-ID": agent_id},
            ) as resp,
        ):
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise ValueError("executor response exceeds configured limit")
                chunks.append(chunk)
        # Signed refusals arrive as HTTP 200 too; unsigned/non-JSON errors are unknown outcomes.
        result = json.loads(b"".join(chunks))
        if not isinstance(result, dict):
            raise ValueError("invalid executor response")
        return result
