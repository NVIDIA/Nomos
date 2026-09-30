# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP Streamable HTTP transport for the Nomos authorization boundary."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from nomos_client.models import (
    AuthorizeDecision,
    AuthorizeRequest,
    ConsumeDecision,
    ConsumeRequest,
)


class MCPAuthorizationClientError(RuntimeError):
    """A safe, bounded transport or response failure."""


MAX_RESPONSE_BYTES = 128 * 1024


class MCPAuthorizationClient:
    """Call Nomos Council tools over authenticated MCP Streamable HTTP."""

    def __init__(
        self,
        mcp_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 25.0,
    ) -> None:
        if not mcp_url:
            raise ValueError("mcp_url is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._mcp_url = mcp_url
        self._headers = dict(headers) if headers else None
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover - installation contract
            raise MCPAuthorizationClientError(
                "MCP transport dependencies are not installed; install nomos[hermes]"
            ) from exc

        try:
            mcp_major = int(version("mcp").split(".", 1)[0])
        except (PackageNotFoundError, ValueError) as exc:  # pragma: no cover - broken installation
            raise MCPAuthorizationClientError(
                "Cannot determine the installed MCP SDK version"
            ) from exc

        if mcp_major not in {1, 2}:
            raise MCPAuthorizationClientError("Unsupported MCP SDK major version")

        # MCP 1.29 and 2 accept a caller-owned HTTP client. MCP 2 moved that
        # client to httpx2 and yields two streams; MCP 1.29 uses httpx and
        # yields three. Older supported MCP 1.x accepts settings directly.
        transport: Any = streamable_http_client
        if "http_client" in inspect.signature(transport).parameters:
            try:
                if mcp_major == 2:
                    import httpx2 as http_client_module
                else:
                    import httpx as http_client_module
            except ImportError as exc:  # pragma: no cover - MCP 2 dependency contract
                raise MCPAuthorizationClientError("MCP HTTP dependencies are unavailable") from exc
            async with http_client_module.AsyncClient(
                headers=self._headers,
                timeout=self._timeout_seconds,
            ) as http_client:
                async with transport(
                    self._mcp_url,
                    http_client=http_client,
                ) as streams:
                    read, write = streams[:2]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            return

        async with transport(
            self._mcp_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            sse_read_timeout=self._timeout_seconds,
        ) as streams:
            read, write = streams[:2]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _call(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with self._session() as session:
                result = await session.call_tool(tool_name, arguments=dict(arguments))
        except MCPAuthorizationClientError:
            raise
        except Exception as exc:
            raise MCPAuthorizationClientError("Nomos authorization transport failed") from exc
        if getattr(result, "isError", False):
            raise MCPAuthorizationClientError("Nomos authorization tool returned an error")
        try:
            text = result.content[0].text
            if not isinstance(text, str):
                raise TypeError("response content is not text")
            if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise MCPAuthorizationClientError("Nomos authorization response is too large")
            payload = json.loads(text)
        except MCPAuthorizationClientError:
            raise
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise MCPAuthorizationClientError("Nomos returned a malformed response") from exc
        if not isinstance(payload, dict):
            raise MCPAuthorizationClientError("Nomos returned a non-object response")
        return payload

    async def authorize(self, request: AuthorizeRequest) -> AuthorizeDecision:
        """Submit an operation to the Council and validate its decision."""
        payload = await self._call(
            "council_authorize",
            request.model_dump(mode="json"),
        )
        try:
            return AuthorizeDecision.model_validate(payload)
        except Exception as exc:
            raise MCPAuthorizationClientError(
                "Nomos returned an invalid authorization decision"
            ) from exc

    async def consume(self, request: ConsumeRequest) -> ConsumeDecision:
        """Atomically consume an approved writ before native execution."""
        payload = await self._call(
            "council_consume_writ",
            request.model_dump(mode="json"),
        )
        try:
            return ConsumeDecision.model_validate(payload)
        except Exception as exc:
            raise MCPAuthorizationClientError(
                "Nomos returned an invalid consumption decision"
            ) from exc
