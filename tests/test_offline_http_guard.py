# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import sys
from pathlib import Path

import httpcore
import httpx
import pytest


@pytest.fixture
def sent_hosts(monkeypatch):
    """Preserve HTTPX's real transport while preventing any socket traffic."""
    hosts = []

    def send(self, request):
        hosts.append(request.url.host)
        return httpcore.Response(200, content=b"offline")

    async def send_async(self, request):
        async def content():
            yield b"offline"

        hosts.append(request.url.host)
        return httpcore.Response(200, content=content())

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", send)
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", send_async)
    return hosts


def test_standard_sync_transport_blocks_external_requests(monkeypatch, sent_hosts):
    monkeypatch.setenv("NOMOS_MCP_LIVE_URL", "https://council.invalid/mcp")
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(RuntimeError, match="Offline tests block external HTTPX"):
            client.get("https://council.invalid/mcp")
        assert client.get("http://localhost:8181/fixture").text == "offline"
    assert sent_hosts == [b"localhost"]


@pytest.mark.asyncio
async def test_standard_async_transport_blocks_external_requests(sent_hosts):
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(RuntimeError, match="Offline tests block external HTTPX"):
            await client.get("https://provider.invalid")
        assert (await client.get("http://[::1]:8181/fixture")).text == "offline"
    assert sent_hosts == [b"::1"]


def test_mock_transport_accepts_external_fixture_urls():
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://provider.invalid").status_code == 200


def test_live_origin_is_scoped_to_marked_item(tmp_path):
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    (tmp_path / "conftest.py").write_text(conftest.read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = live_mcp: deployed MCP test\n")
    (tmp_path / "test_probe.py").write_text(
        """from concurrent.futures import Future
from contextvars import copy_context
from threading import Event, Thread

import httpcore
import httpx
import pytest
import pytest_asyncio

def stub_send(self, request):
    assert request.url.scheme == b"https"
    assert request.url.host == b"council.invalid"
    assert request.url.port == 9443
    return httpcore.Response(200, content=b"offline")

async def stub_send_async(self, request):
    async def content():
        yield b"offline"
    stub_send(self, request)
    return httpcore.Response(200, content=content())

httpcore.ConnectionPool.handle_request = stub_send
httpcore.AsyncConnectionPool.handle_async_request = stub_send_async
origin = "https://council.invalid:9443"
start_worker = Event()
worker_result = Future()
live_contexts = []

def assert_blocked(url):
    with pytest.raises(RuntimeError, match="Offline tests block external HTTPX"):
        httpx.get(url, trust_env=False)

# A live opt-in must not apply during collection.
assert_blocked(origin + "/mcp")

def test_unmarked_starts_worker():
    def worker():
        try:
            assert start_worker.wait(5), "marked test did not start"
            assert_blocked(origin + "/unmarked-worker")
        except BaseException as error:
            worker_result.set_exception(error)
        else:
            worker_result.set_result(None)
    Thread(target=copy_context().run, args=(worker,), daemon=True).start()

@pytest.fixture
def live_fixture():
    assert httpx.get(origin + "/setup", trust_env=False).status_code == 200
    yield
    assert httpx.get(origin + "/teardown", trust_env=False).status_code == 200

@pytest.mark.live_mcp
def test_live(live_fixture):
    assert httpx.get(origin + "/mcp", trust_env=False).status_code == 200
    live_contexts.append(copy_context())
    start_worker.set()
    worker_result.result(timeout=5)
    for other in ("http://council.invalid:9443", "https://council.invalid:9444",
                  "https://other.invalid:9443"):
        assert_blocked(other)

@pytest_asyncio.fixture
async def live_async_fixture():
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.get(origin + "/async-setup")).status_code == 200
        yield client
        assert (await client.get(origin + "/async-teardown")).status_code == 200

@pytest.mark.live_mcp
@pytest.mark.asyncio
async def test_live_async(live_async_fixture):
    assert (await live_async_fixture.get(origin + "/async-call")).status_code == 200

@pytest.mark.live_mcp
def test_live_without_origin(monkeypatch):
    monkeypatch.delenv("NOMOS_MCP_LIVE_URL")
    assert_blocked(origin + "/mcp")

def test_after_live():
    assert_blocked(origin + "/mcp")
    assert len(live_contexts) == 1
    live_contexts[0].run(assert_blocked, origin + "/stale-context")
"""
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", "-q", "test_probe.py"],
        cwd=tmp_path,
        env={
            **os.environ,
            "NOMOS_MCP_LIVE_URL": "https://council.invalid:9443/mcp",
            "PYTEST_ADDOPTS": "",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
