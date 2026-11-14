"""Bounded OPA transport, timing, and cancellation regressions."""

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from nomos.config.settings import OPAConfig
from nomos.opa.client import OPAEvaluationError, OPAHttpClient


def _client(handler, **config):
    client = OPAHttpClient(OPAConfig(**config))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _events(caplog):
    return [r.opa_timing for r in caplog.records if hasattr(r, "opa_timing")]


@pytest.mark.asyncio
async def test_reuses_idle_http_connection_and_sends_exact_input():
    connections = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            connections.append(True)
            super().setup()

        def do_POST(self):
            requests.append(
                (
                    self.path,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode()),
                )
            )
            body = b'{"result":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = OPAHttpClient(OPAConfig(url=f"http://127.0.0.1:{server.server_port}"))
    try:
        for i in range(4):
            assert (await client.evaluate({"i": i})).allow
        assert len(connections) == 1
        assert len(requests) == 4
        assert requests[-1][1] == {"input": {"i": 3}}
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_parallel_and_waiting_capacity_are_finite():
    release = asyncio.Event()
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await release.wait()
            return httpx.Response(200, json={"result": True})
        finally:
            active -= 1

    client = _client(handler, max_connections=2, max_queued_requests=1)
    tasks = [asyncio.create_task(client.evaluate({})) for _ in range(3)]
    try:
        await asyncio.sleep(0.02)
        assert active == 2 and client._reservations == 3
        with pytest.raises(OPAEvaluationError, match="queue is full"):
            await client.evaluate({})
        release.set()
        assert all(result.allow for result in await asyncio.gather(*tasks))
        assert peak == 2 and client._reservations == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_admission_timeout_is_separate_and_cancelled_waiter_releases_slot(caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return httpx.Response(200, json={"result": True})

    client = _client(handler, max_connections=1, max_queued_requests=1, admission_timeout_ms=20)
    first = asyncio.create_task(client.evaluate({}))
    try:
        await asyncio.sleep(0)
        with pytest.raises(OPAEvaluationError, match="admission timed out"):
            await client.evaluate({})
        assert client._reservations == 1
        assert _events(caplog)[0]["error_origin"] == "admission"
        queued = asyncio.create_task(client.evaluate({}))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert client._reservations == 1
    finally:
        release.set()
        await first
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,phase",
    [
        (httpx.ConnectTimeout, "connect"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
        (httpx.PoolTimeout, "pool"),
    ],
)
async def test_timeout_phase_reported_without_retry_or_sensitive_text(error_type, phase, caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    calls = []

    async def handler(request):
        calls.append(request)
        raise error_type("secret endpoint", request=request)

    client = _client(handler)
    try:
        with pytest.raises(OPAEvaluationError):
            await client.evaluate({"intent": {"intent_id": "bad\nsecret"}, "token": "secret token"})
        assert len(calls) == 1
        details = _events(caplog)[0]
        assert details["network_phase"] == phase
        assert details["intent_id"] == "-"
        assert "secret" not in caplog.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,error",
    [
        (200, b'{"result":false}', None),
        (200, b'{"result":{"allow":false,"reason":"policy"}}', None),
        (200, b"not json", "invalid JSON"),
        (200, b"\xff", "invalid JSON"),
        (200, b"[]", "JSON object"),
        (200, b'{"result":{"allow":"yes"}}', "boolean"),
        (307, b"{}", "HTTP 307"),
        (302, b"{}", "HTTP 302"),
        (503, b"{}", "HTTP 503"),
    ],
)
async def test_denial_invalid_response_and_redirect_fail_closed(status, body, error):
    calls = []

    def handler(request):
        calls.append(True)
        return httpx.Response(status, content=body, headers={"Location": "https://other.invalid"})

    client = _client(handler)
    try:
        if error:
            with pytest.raises(OPAEvaluationError, match=error):
                await client.evaluate({})
        else:
            assert not (await client.evaluate({})).allow
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_running_request_and_repeated_cancel_during_close():
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = []

    async def handler(request):
        entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"result": True})
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    client = _client(handler)
    request = asyncio.create_task(client.evaluate({}))
    await entered.wait()
    closer = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    assert not closer.done()
    with pytest.raises(OPAEvaluationError, match="closed"):
        await client.evaluate({})
    closer.cancel()
    closer.cancel()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert cancelled and client._reservations == 0 and client._client.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_no_default_executor_handoff_and_network_budget_unchanged():
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(executor)
    release = threading.Event()
    blocker = executor.submit(release.wait, 3)
    seen = []

    async def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"result": True})

    client = _client(handler, timeout_ms=500)
    # Mock transport is test-owned, so give it the same configured phase budget.
    client._client.timeout = httpx.Timeout(client._timeout_seconds)
    try:
        async with asyncio.timeout(1):
            assert (await client.evaluate({})).allow
        assert seen == [{"connect": 0.5, "read": 0.5, "write": 0.5, "pool": 0.5}]
    finally:
        release.set()
        blocker.result(timeout=3)
        executor.shutdown(wait=True)
        await client.aclose()
