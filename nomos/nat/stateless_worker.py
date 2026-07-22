# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stateless MCP front-end worker for the Nomos council.

NAT's default MCP worker serves streamable-HTTP in *stateful* mode: every
replica keeps MCP sessions in process memory keyed by ``mcp-session-id``
(``StreamableHTTPSessionManager._server_instances``). Behind a round-robin
Service with more than one replica that breaks: a follow-up request whose
session was created on pod A round-robins to pod B, which has no record of it,
and the transport returns 404 before any council code runs.

The council's tools are fully independent per call — each review is atomic and
durable state lives in Postgres/redis, not in the MCP session — so the
transport-level session is pure overhead. This worker flips the FastMCP
transport to stateless (a fresh transport per request, ``mcp_session_id=None``,
no session pinning), so any replica can serve any request and the council scales
horizontally with no session affinity.

Wired via the MCP front-end ``runner_class`` in the NAT workflow config:

    general:
      front_end:
        _type: mcp
        runner_class: nomos.nat.stateless_worker.StatelessMCPWorker
"""

from mcp.server.fastmcp import FastMCP
from nat.plugins.mcp.server.front_end_plugin_worker import MCPFrontEndPluginWorker


class StatelessMCPWorker(MCPFrontEndPluginWorker):
    """Default NAT MCP worker, but serving streamable-HTTP statelessly."""

    async def create_mcp_server(self) -> FastMCP:
        mcp: FastMCP = await super().create_mcp_server()
        # Read by FastMCP.streamable_http_app() when the transport is built
        # (after create_mcp_server + add_routes), so mutating it here takes
        # effect. json_response is left False so responses stay SSE and the
        # monitor's stream parsing is unaffected.
        mcp.settings.stateless_http = True
        return mcp
