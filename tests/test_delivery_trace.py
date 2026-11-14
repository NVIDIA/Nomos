"""Production sink boundaries and in-process MCP correlation; no live services."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from nomos.nat.delivery_trace import (
    DeliveryASGI,
    DeliverySink,
    configured_sink,
    current_trace,
    install_handler_observers,
    observe_authorize_output,
)


class MemorySink:
    def __init__(self):
        self.events = []
        self.closed = False

    def emit(self, event):
        self.events.append(event)
        return True

    def close(self):
        self.closed = True


def test_disabled_has_no_file_or_writer(tmp_path, monkeypatch):
    monkeypatch.delenv("NOMOS_DELIVERY_TRACE", raising=False)
    monkeypatch.setenv("NOMOS_DELIVERY_TRACE_DIRECTORY", str(tmp_path))
    before = set(threading.enumerate())
    assert configured_sink() is None
    assert not list(tmp_path.iterdir())
    assert set(threading.enumerate()) == before


@pytest.mark.parametrize("capacity", [0, -1, 8193, True, 1.0])
def test_sink_rejects_unbounded_or_invalid_capacity(tmp_path, capacity):
    with pytest.raises(ValueError):
        DeliverySink(str(tmp_path), capacity=capacity)
    assert not list(tmp_path.iterdir())


def test_production_sink_overflow_is_bounded_and_reported(tmp_path):
    gate = threading.Event()

    class PausedSink(DeliverySink):
        def _write(self):
            assert gate.wait(3)
            super()._write()

    sink = PausedSink(str(tmp_path), capacity=2)
    try:
        accepted = [sink.emit({"event": "probe", "sequence": i}) for i in range(100)]
        assert sum(accepted) == 1  # sink_started already occupies one entry
        assert sink.queue.qsize() == 2
        counts = sink.counters()
        assert {
            k: counts[k]
            for k in ("offered", "written", "dropped", "write_failures", "abandoned", "pending")
        } == dict(offered=101, written=0, dropped=99, write_failures=0, abandoned=0, pending=2)
    finally:
        gate.set()
        final = sink.close()
    assert not final["writer_alive"]
    assert sink.path.stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert records[-1]["event"] == "sink_final"
    assert records[-1]["written"] == 2
    assert records[-1]["dropped"] == 99


def test_writer_failure_is_explicit_and_never_exposes_error_text(tmp_path, caplog):
    gate = threading.Event()

    class PausedSink(DeliverySink):
        def _write(self):
            assert gate.wait(3)
            super()._write()

    sink = PausedSink(str(tmp_path), capacity=2)
    original = sink.file

    class FailingFile:
        def write(self, data):
            raise OSError("SECRET-CANARY")

        def flush(self):
            pass

        def close(self):
            original.close()

    sink.file = FailingFile()
    gate.set()
    sink.thread.join(3)
    assert sink.counters()["write_failures"] == 1
    assert sink.emit({"event": "after_failure"}) is False
    assert sink.close()["writer_alive"] is False
    assert "SECRET-CANARY" not in caplog.text
    assert "delivery_trace_writer_failed" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_close_failure_does_not_replace_app_exception():
    expected = RuntimeError("private-application-error")

    class BrokenClose(MemorySink):
        def close(self):
            raise OSError("private-close-error")

    async def app(scope, receive, send):
        raise expected

    with pytest.raises(RuntimeError) as caught:
        await DeliveryASGI(app, BrokenClose())({"type": "lifespan"}, None, None)
    assert caught.value is expected


@pytest.mark.asyncio
async def test_real_mcp_scope_binding_and_receipts_across_tasks(monkeypatch):
    monkeypatch.setenv("NOMOS_DELIVERY_TRACE", "1")
    sink = MemorySink()
    mcp = FastMCP("delivery-fixture", stateless_http=True)
    seen = []

    @mcp.tool()
    async def approved_fixture() -> str:
        trace = current_trace()
        assert trace is not None
        # This tool runs in an SDK handler task, not the outer HTTP scope task.
        seen.append((trace.http_id, id(asyncio.current_task())))
        observe_authorize_output(
            SimpleNamespace(status="approved", decision_id="intent-fixture", writ_id="writ-fixture")
        )
        return "fixed-result"

    install_handler_observers(mcp)
    app = DeliveryASGI(mcp.streamable_http_app(), sink)
    outer_tasks = []

    async def one():
        outer_tasks.append(id(asyncio.current_task()))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={
                "X-Nomos-Stress-Request-ID": "abcdef012345:1",
                "Authorization": "Bearer SECRET-CANARY",
            },
        )
        async with client:
            async with streamable_http_client("http://localhost:8000/mcp", http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("approved_fixture", {})
                    assert result.content[0].text == "fixed-result"

    async with mcp.session_manager.run():
        await asyncio.wait_for(asyncio.gather(one(), one()), timeout=5)
    assert len(seen) == 2
    assert len({http_id for http_id, _ in seen}) == 2
    assert all(task not in outer_tasks for _, task in seen)
    ready = [e for e in sink.events if e["event"] == "authorize_output_ready"]
    assert {e["http_trace_id"] for e in ready} == {http_id for http_id, _ in seen}
    assert all(e["logical_id"] == "abcdef012345:1" for e in ready)
    calls = [
        e for e in sink.events if e["event"] == "mcp_handler_return" and e["method"] == "tools/call"
    ]
    assert len(calls) == 2
    assert len({e["rpc_id"] for e in calls}) == 1  # repeated RPC ids, separate HTTP traces
    assert {e["http_trace_id"] for e in calls} == {e["http_trace_id"] for e in ready}
    assert (
        len(
            [
                e
                for e in sink.events
                if e["event"] == "mcp_handler_return" and e["method"] == "tools/list"
            ]
        )
        == 2
    )
    text = json.dumps(sink.events)
    assert "SECRET-CANARY" not in text
    assert "fixed-result" not in text
    assert not any(
        e["event"] == "mcp_handler_enter" and e["method"] == "initialize" for e in sink.events
    )


def test_file_limit_preserves_summary_and_conservation(tmp_path):
    sink = DeliverySink(str(tmp_path), max_bytes=8192)
    for i in range(100):
        sink.emit({"event": "bounded_probe", "sequence": i})
    result = sink.close()
    assert result["limit_reached"]
    assert result["pending"] == 0
    assert result["offered"] == result["written"] + result["dropped"] + result["abandoned"]
    assert result["abandoned"] > 0
    assert sink.path.stat().st_size == result["bytes_written"] <= 8192
    records = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert records[-1]["event"] == "sink_final"
    assert records[-1]["limit_reached"]
    assert records[-1]["abandoned"] == result["abandoned"]
