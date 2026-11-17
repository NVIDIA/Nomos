# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The system one client keeps its connections open and never blocks the event loop.

One call per authorize goes to the backend. Opening a connection for each, from
a worker thread, made that call the slowest stage of a fast-path authorize.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from nomos.config.settings import SystemOneConfig
from nomos.system_one.client import SystemOneClient, SystemOneEvaluationError
from tests.test_system_one_attestation import CATALOG_PATH, _make_intent, _synthetic_response


class _Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # lets a client keep its connection open
    delay = 0.0
    status = 200
    raw: bytes | None = None
    connections: ClassVar[set[tuple[str, int]]] = set()

    def do_POST(self):
        type(self).connections.add(self.client_address)
        length = int(self.headers.get("Content-Length", 0))
        request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.delay:
            time.sleep(self.delay)
        body = (
            self.raw
            if self.raw is not None
            else json.dumps(_synthetic_response(request_body)).encode()
        )
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _Server(ThreadingHTTPServer):
    # The default listen queue of 5 drops connections when a test opens many at once.
    request_queue_size = 128
    daemon_threads = True


@pytest.fixture
def backend():
    handler = type("Handler", (_Backend,), {"connections": set()})
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _client(base_url: str, timeout_ms: int = 3000) -> SystemOneClient:
    return SystemOneClient(
        SystemOneConfig(
            enabled=True,
            provider="nemotron",
            base_url=base_url,
            api_key="test-key",  # pragma: allowlist secret
            catalog_path=str(CATALOG_PATH),
            timeout_ms=timeout_ms,
        )
    )


@pytest.mark.asyncio
async def test_one_connection_serves_consecutive_calls(backend):
    handler, url = backend
    client = _client(url)
    try:
        for _ in range(5):
            vector = await client.evaluate(_make_intent())
            assert len(vector.attestations) == len(client.catalog.questions)
        assert len(handler.connections) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_slow_backend_calls_overlap_without_holding_the_event_loop(backend):
    handler, url = backend
    handler.delay = 0.3
    client = _client(url)
    gaps: list[float] = []

    async def ticker(stop: asyncio.Event) -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    stop = asyncio.Event()
    watching = asyncio.create_task(ticker(stop))
    started = time.perf_counter()
    try:
        vectors = await asyncio.gather(*(client.evaluate(_make_intent()) for _ in range(20)))
    finally:
        stop.set()
        await watching
        await client.aclose()
    elapsed = time.perf_counter() - started

    assert len(vectors) == 20
    # Twenty 0.3 s calls one after another would take 6 s, and a blocked loop
    # would stop the ticker for at least one whole call.
    assert elapsed < 3.0
    assert max(gaps) < 0.29


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "raw", "delay", "message"),
    [
        (500, b'{"error":"backend down"}', 0.0, "system one returned HTTP 500"),
        (429, b"slow down", 0.0, "system one returned HTTP 429"),
        (200, b"not json", 0.0, "invalid JSON"),
        (200, b"[1, 2]", 0.0, "must be a JSON object"),
        (200, None, 0.6, "system one request failed"),
    ],
)
async def test_backend_failures_raise_the_evaluation_error(backend, status, raw, delay, message):
    handler, url = backend
    handler.status, handler.raw, handler.delay = status, raw, delay
    client = _client(url, timeout_ms=200)
    try:
        with pytest.raises(SystemOneEvaluationError, match=message):
            await client.evaluate(_make_intent())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_backend_raises_the_evaluation_error():
    client = _client("http://127.0.0.1:9", timeout_ms=500)
    try:
        with pytest.raises(SystemOneEvaluationError, match="system one request failed"):
            await client.evaluate(_make_intent())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_closed_client_reconnects_on_the_next_call(backend):
    handler, url = backend
    client = _client(url)
    await client.evaluate(_make_intent())
    await client.aclose()
    await client.aclose()  # closing twice is harmless
    try:
        await client.evaluate(_make_intent())
        assert len(handler.connections) == 2
    finally:
        await client.aclose()


class _Trickle(BaseHTTPRequestHandler):
    """Answers a byte at a time, each well inside a per-read timeout."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.dumps(_synthetic_response(json.loads(self.rfile.read(length)))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for index in range(0, len(body), 64):
                self.wfile.write(body[index : index + 64])
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass  # the client gave up, as it should

    def log_message(self, *_args):
        pass


@pytest.mark.asyncio
async def test_the_timeout_bounds_the_whole_call_not_each_read():
    server = _Server(("127.0.0.1", 0), _Trickle)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = _client(
        f"http://{server.server_address[0]}:{server.server_address[1]}", timeout_ms=300
    )
    started = time.perf_counter()
    try:
        with pytest.raises(SystemOneEvaluationError, match="no answer within 300 ms"):
            await client.evaluate(_make_intent())
        assert time.perf_counter() - started < 1.0
    finally:
        await client.aclose()
        server.shutdown()
        thread.join()
        server.server_close()
