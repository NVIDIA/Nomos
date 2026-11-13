"""Owned service resources close only after actual critical workers finish."""

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from nomos.config.settings import CouncilSettings
from nomos.nat.functions import CouncilService, CouncilTelemetryPublisher


def _settings():
    return CouncilSettings(
        council_id="close-test",
        members=[],
        llm_backends={},
        council_key_path="",
        ledger_path="",
        redis={"host": ""},
        opa={"enabled": False},
        telemetry={"enabled": False, "redis_enabled": False},
    )


@pytest.mark.asyncio
async def test_bounded_telemetry_shutdown_drops_stalled_records(caplog):
    publisher = CouncilTelemetryPublisher(_settings())
    task = asyncio.create_task(asyncio.Event().wait())
    publisher._tasks.add(task)
    task.add_done_callback(publisher._tasks.discard)
    caplog.set_level(logging.WARNING)
    await publisher.aclose(timeout_s=0.01)
    assert task.cancelled()
    assert "shutdown dropped 1 pending" in caplog.text
    publisher.publish_later({"ignored": True})
    assert not publisher._tasks


@pytest.mark.asyncio
async def test_service_close_drains_workers_and_preserves_shared_objects():
    service = CouncilService(_settings())
    events = []
    release = threading.Event()
    entered = threading.Event()

    def work():
        entered.set()
        release.wait(3)
        events.append("worker_done")

    service._ledger_executor.submit(work)
    while not entered.is_set():
        await asyncio.sleep(0.001)

    async def lag():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("lag_cancelled")

    async def close_workflows():
        events.append("workflow_closed")

    async def close_opa():
        events.append("opa_closed")

    class Store:
        async def close(self):
            events.append("store_closed")

    class Pool:
        def close(self):
            assert "worker_done" in events
            events.append("pool_closed")

    def forbidden():
        raise AssertionError("shared object closed")

    service._loop_lag_task = asyncio.create_task(lag())
    service.aclose_workflows = close_workflows
    service._opa_client = SimpleNamespace(aclose=close_opa)
    service._ledger_pool = Pool()
    service._durable_store = service._redis_store = Store()
    service._limiter_pool = SimpleNamespace(close=forbidden)
    service._signer_provider = SimpleNamespace(close=forbidden)
    await asyncio.sleep(0)
    close = asyncio.create_task(service.aclose())
    try:
        await asyncio.sleep(0.02)
        close.cancel()
        await asyncio.sleep(0)
        close.cancel()
        # Shutdown(wait=True) must not occupy the event-loop thread.
        await asyncio.sleep(0.02)
        assert not close.done() and "pool_closed" not in events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert events.index("workflow_closed") < events.index("opa_closed")
        assert events.index("worker_done") < events.index("pool_closed")
        assert "lag_cancelled" in events and events.count("store_closed") == 1
        await service.aclose()
        assert events.count("pool_closed") == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_telemetry_shutdown_is_bounded_even_if_sink_suppresses_cancel(caplog):
    publisher = CouncilTelemetryPublisher(_settings())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(resistant())
    publisher._tasks.add(task)
    task.add_done_callback(publisher._tasks.discard)
    await entered.wait()
    caplog.set_level(logging.WARNING)
    try:
        async with asyncio.timeout(0.2):
            await publisher.aclose(timeout_s=0.01)
        assert not task.done()
        assert "cancellation-resistant" in caplog.text
        publisher.publish_later({"ignored": True})
        assert len(publisher._tasks) == 1
    finally:
        release.set()
        await task
