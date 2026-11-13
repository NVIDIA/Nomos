"""OPA timing distinguishes local queueing without changing transport behavior."""

import asyncio
import json
import logging
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from nomos.config.settings import OPAConfig
from nomos.opa.client import OPAEvaluationError, OPAHttpClient


def _events(caplog, event):
    return [
        record.opa_timing
        for record in caplog.records
        if hasattr(record, "opa_timing") and record.opa_timing["event"] == event
    ]


@pytest.mark.asyncio
async def test_executor_queue_is_distinct_from_worker_time(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(executor)
    release = threading.Event()
    occupied = executor.submit(release.wait, 3)
    client = OPAHttpClient(OPAConfig())
    monkeypatch.setattr(client, "_post", lambda _: {"result": True})
    try:
        request = asyncio.create_task(client.evaluate({"intent": {"intent_id": "intent-queue"}}))
        await asyncio.sleep(0.04)
        assert not request.done()
        release.set()
        assert (await request).allow
        worker = _events(caplog, "worker")[0]
        assert worker["queue_ms"] >= 20
        assert worker["worker_ms"] < worker["queue_ms"]
        assert worker["network_phase"] == "unknown"
        assert _events(caplog, "evaluate")[0]["outcome"] == "allow"
    finally:
        release.set()
        occupied.result(timeout=3)
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_worker_time_and_denial_are_preserved(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    client = OPAHttpClient(OPAConfig(timeout_ms=500))

    def delayed(_):
        time.sleep(0.03)
        return {"result": {"allow": False, "reason": "policy denial"}}

    monkeypatch.setattr(client, "_post", delayed)
    result = await client.evaluate({})
    assert not result.allow and result.reasons == ["policy denial"]
    assert _events(caplog, "worker")[0]["worker_ms"] >= 20
    assert _events(caplog, "evaluate")[0]["outcome"] == "deny"
    assert client._timeout_seconds == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause", "origin"),
    [
        (urllib.error.URLError(TimeoutError("secret endpoint")), "urllib_wrapped_timeout"),
        (urllib.error.URLError(OSError("secret endpoint")), "urllib_wrapped"),
        (TimeoutError("secret endpoint"), "direct_timeout"),
        (
            urllib.error.HTTPError("https://secret-endpoint", 503, "secret body", {}, None),
            "http_status",
        ),
        (json.JSONDecodeError("secret body", "", 0), "json_decode"),
    ],
)
async def test_exception_identity_and_private_data_are_preserved(
    cause, origin, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    client = OPAHttpClient(OPAConfig())
    original = OPAEvaluationError("original error text contains secret")
    original.__cause__ = cause

    def fail(_):
        raise original

    monkeypatch.setattr(client, "_post", fail)
    with pytest.raises(OPAEvaluationError) as raised:
        await client.evaluate(
            {
                "intent": {"intent_id": "bad\nsecret", "command": "secret command"},
                "token": "secret token",
            }
        )
    assert raised.value is original
    for event in ("worker", "evaluate"):
        details = _events(caplog, event)[0]
        assert details["error_origin"] == origin
        assert details["network_phase"] == "unknown"
        assert details["intent_id"] == "-"
        assert details["cause_class"] == type(cause).__name__
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_hide_running_worker_completion(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    started = threading.Event()
    release = threading.Event()
    client = OPAHttpClient(OPAConfig())

    def blocked(_):
        started.set()
        release.wait(3)
        return {"result": True}

    monkeypatch.setattr(client, "_post", blocked)
    request = asyncio.create_task(client.evaluate({"intent": {"intent_id": "intent-cancel"}}))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert _events(caplog, "evaluate")[0]["outcome"] == "cancelled"
        assert not _events(caplog, "worker")
        release.set()
        async with asyncio.timeout(2):
            while not _events(caplog, "worker"):
                await asyncio.sleep(0.001)
        assert _events(caplog, "worker")[0]["outcome"] == "completed"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_invalid_policy_shape_remains_an_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nomos.opa.client")
    client = OPAHttpClient(OPAConfig())
    monkeypatch.setattr(client, "_post", lambda _: {"result": {"allow": "yes"}})
    with pytest.raises(OPAEvaluationError, match="boolean"):
        await client.evaluate({})
    assert _events(caplog, "worker")[0]["outcome"] == "completed"
    assert _events(caplog, "evaluate")[0]["outcome"] == "error"
