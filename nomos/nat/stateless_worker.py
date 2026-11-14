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

import logging
import os
import re
from typing import TYPE_CHECKING

import nat.plugins.mcp.server.front_end_plugin_worker as _nat_mcp_worker
from mcp.server.fastmcp import FastMCP
from nat.plugins.mcp.server.front_end_plugin_worker import MCPFrontEndPluginWorker
from nat.runtime.session import SessionManager

if TYPE_CHECKING:
    from nomos.nat.delivery_trace import DeliverySink

logger = logging.getLogger(__name__)

WORKFLOW_MAX_CONCURRENCY_ENV = "COUNCIL_WORKFLOW_MAX_CONCURRENCY"
_NAT_DEFAULT_MAX_CONCURRENCY = 8


def workflow_max_concurrency() -> int:
    """Concurrent executions NAT allows per MCP tool, per process.

    NAT gives every MCP tool its own SessionManager with ``max_concurrency=8``
    and exposes no setting for it. Under load that default, not the LLM
    limiter, is what queues requests: 8 reviews in progress per replica however
    many arrive. ``COUNCIL_WORKFLOW_MAX_CONCURRENCY`` sizes it; 0 removes the
    cap and leaves admission to the LLM limiter and its overload shedding. An
    unset, empty or malformed value keeps NAT's default.
    """
    raw = os.environ.get(WORKFLOW_MAX_CONCURRENCY_ENV, "").strip()
    if not raw:
        return _NAT_DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using %d",
            WORKFLOW_MAX_CONCURRENCY_ENV,
            raw,
            _NAT_DEFAULT_MAX_CONCURRENCY,
        )
        return _NAT_DEFAULT_MAX_CONCURRENCY
    return max(0, value)


_CREDENTIAL_FIELDS = re.compile(
    r"\b(auth_token|token|password|secret|api_key)=(?:'[^']*'|\"[^\"]*\"|\S+)", re.IGNORECASE
)


class RedactCredentials(logging.Filter):
    """Mask credential fields in log messages.

    When a tool call fails, NAT logs the function input verbatim, which for
    council tools includes the caller's ``auth_token``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _CREDENTIAL_FIELDS.sub(lambda m: f"{m.group(1)}='[redacted]'", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def install_credential_redaction() -> None:
    """Attach the filter to the NAT loggers that echo tool input on failure."""
    for name in ("nat.builder.function", "nat.runtime.runner"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactCredentials) for f in target.filters):
            target.addFilter(RedactCredentials())


class _SizedSessionManager(SessionManager):
    """SessionManager whose per-tool concurrency comes from the environment."""

    @classmethod
    async def create(cls, *args, **kwargs):
        kwargs.setdefault("max_concurrency", workflow_max_concurrency())
        return await super().create(*args, **kwargs)


class StatelessMCPWorker(MCPFrontEndPluginWorker):
    """Default NAT MCP worker, but serving streamable-HTTP statelessly."""

    def _close_delivery_sink(self) -> None:
        sink = getattr(self, "_delivery_sink", None)
        self._delivery_sink: DeliverySink | None = None
        if sink is not None:
            try:
                # Startup failure only: close joins at most two seconds and
                # never queues behind the application's default executor.
                sink.close()
            except BaseException:
                logger.error("delivery_trace_startup_cleanup_failed")

    async def add_routes(self, mcp: FastMCP, builder) -> None:
        try:
            # NAT builds one SessionManager per tool through this module seam.
            install_credential_redaction()
            _nat_mcp_worker.SessionManager = _SizedSessionManager
            logger.info("workflow max_concurrency per tool: %d", workflow_max_concurrency())
            await super().add_routes(mcp, builder)
            if getattr(self, "_delivery_sink", None) is not None:
                from nomos.nat.delivery_trace import install_handler_observers

                install_handler_observers(mcp)
        except BaseException:
            self._close_delivery_sink()
            raise

    async def create_mcp_server(self) -> FastMCP:
        mcp: FastMCP = await super().create_mcp_server()
        # Read by FastMCP.streamable_http_app() when the transport is built
        # (after create_mcp_server + add_routes), so mutating it here takes
        # effect. json_response is left False so responses stay SSE and the
        # monitor's stream parsing is unaffected.
        mcp.settings.stateless_http = True
        from nomos.nat.delivery_trace import DeliveryASGI, configured_sink

        self._delivery_sink = configured_sink()
        if self._delivery_sink is not None:
            app_factory = mcp.streamable_http_app

            def traced_app():
                try:
                    app = app_factory()
                    app.add_middleware(DeliveryASGI, sink=self._delivery_sink)
                    return app
                except BaseException:
                    self._close_delivery_sink()
                    raise

            mcp.streamable_http_app = traced_app
        return mcp
