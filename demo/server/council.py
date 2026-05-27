"""Thin async wrapper around the production Council MCP surface.

This module owns the MCP client session lifecycle. It does not mock,
cache, or reinterpret council responses — it forwards the arguments to
the published MCP tools and returns whatever the council says.

Tools used (all from `nomos/nat/plugin.py`):
  - council_authorize         → full pipeline, returns a writ if approved
  - council_check_writ_status → lifecycle state of an issued writ
  - council_get_public_key    → for the banner, proves live connection
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


class CouncilClient:
    """Call the live council over its Streamable HTTP MCP endpoint.

    Per-call agent identity — the server decides which seeded agent (and
    therefore which auth token) to submit under, so the client doesn't
    bake an identity into its constructor.
    """

    # Sent on every MCP request so the monitor proxy can attribute demo
    # traffic in its dashboard. Ignored by the council itself.
    _SOURCE_HEADERS: ClassVar[dict[str, str]] = {"X-Council-Source": "demo"}

    def __init__(self, council_url: str):
        self.council_url = council_url

    @asynccontextmanager
    async def session(self):
        """Yield an initialized MCP ClientSession against the council."""
        async with streamablehttp_client(self.council_url, headers=self._SOURCE_HEADERS) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def authorize(
        self, agent_id: str, auth_token: str, intent: dict[str, Any]
    ) -> dict[str, Any]:
        """Submit an intent to `council_authorize` and return the parsed result."""
        args = {
            "auth_token": auth_token,
            "agent_id": agent_id,
            **intent,
        }
        result = await self._call_tool("council_authorize", args)
        return _parse_tool_text(result)

    async def check_writ_status(
        self, agent_id: str, auth_token: str, writ_id: str
    ) -> dict[str, Any]:
        """Call `council_check_writ_status` and return the parsed result."""
        args = {
            "auth_token": auth_token,
            "writ_id": writ_id,
        }
        result = await self._call_tool("council_check_writ_status", args)
        return _parse_tool_text(result)

    async def get_public_key(self) -> dict[str, Any]:
        """Call `council_get_public_key` — used to confirm the link is live."""
        result = await self._call_tool("council_get_public_key", {})
        return _parse_tool_text(result)

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            async with self.session() as session:
                return await session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            detail = _summarize_exception(exc)
            logger.exception("MCP tool call failed: %s: %s", tool_name, detail)
            raise RuntimeError(f"MCP {tool_name} failed: {detail}") from exc


def _parse_tool_text(result: Any) -> dict[str, Any]:
    """MCP tool results wrap a JSON-string payload in content[0].text."""
    try:
        text = result.content[0].text  # type: ignore[attr-defined]
    except (AttributeError, IndexError) as exc:
        raise RuntimeError(f"Unexpected MCP tool result shape: {result!r}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Tool returned non-JSON payload: {text!r}") from exc


def _summarize_exception(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        leaves = _exception_group_leaves(exc)
        shown = " | ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves[:4])
        extra = "" if len(leaves) <= 4 else f" | ... {len(leaves) - 4} more"
        return f"{exc.message} ({len(leaves)} sub-exception(s)): {shown}{extra}"
    return f"{type(exc).__name__}: {exc}"


def _exception_group_leaves(exc: BaseExceptionGroup) -> list[BaseException]:
    leaves: list[BaseException] = []
    for child in exc.exceptions:
        if isinstance(child, BaseExceptionGroup):
            leaves.extend(_exception_group_leaves(child))
        else:
            leaves.append(child)
    return leaves
