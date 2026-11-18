# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Guard request URLs on HTTPX's standard transports during pytest."""

import os
from contextvars import ContextVar
from ipaddress import ip_address
from threading import Event

import httpx
import pytest

_live_mcp: ContextVar[Event | None] = ContextVar("live_mcp", default=None)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


def _check_httpx_origin(url: httpx.URL) -> None:
    if url.host == "localhost":
        return
    try:
        if ip_address(url.host).is_loopback:
            return
    except ValueError:
        pass

    live_mcp = _live_mcp.get()
    live_url = os.environ.get("NOMOS_MCP_LIVE_URL", "") if live_mcp and live_mcp.is_set() else ""
    if live_url:
        try:
            if _origin(url) == _origin(httpx.URL(live_url)):
                return
        except httpx.InvalidURL:
            pass

    raise RuntimeError(
        "Offline tests block external HTTPX requests. Use loopback, MockTransport, "
        "or a live_mcp test with a matching NOMOS_MCP_LIVE_URL origin."
    )


def pytest_configure(config: pytest.Config) -> None:
    patch = pytest.MonkeyPatch()
    send = httpx.HTTPTransport.handle_request
    send_async = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_send(transport, request):
        _check_httpx_origin(request.url)
        return send(transport, request)

    async def guarded_send_async(transport, request):
        _check_httpx_origin(request.url)
        return await send_async(transport, request)

    config.add_cleanup(patch.undo)
    patch.setattr(httpx.HTTPTransport, "handle_request", guarded_send)
    patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_send_async)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item):
    live_mcp = Event() if item.get_closest_marker("live_mcp") is not None else None
    if live_mcp is not None:
        live_mcp.set()
    token = _live_mcp.set(live_mcp)
    try:
        yield
    finally:
        # Async tasks can retain a copy of the context after teardown.
        if live_mcp is not None:
            live_mcp.clear()
        _live_mcp.reset(token)
