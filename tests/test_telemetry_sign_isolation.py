"""Independent telemetry and signing admission/lifetime contracts."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from nomos.auth.signing_executor import SigningCapacityError, WritSigningExecutor
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import CouncilSettings
from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.crypto import KeyPair
from nomos.level_0.types import CouncilMember, Intent
from nomos.llm.council_agent import IntentEvaluation
from nomos.nat.functions import AuthorizeInput, CouncilService, CouncilTelemetryPublisher


def settings(**telemetry):
    return CouncilSettings(
        council_id="isolation",
        require_auth=False,
        require_council_membership=False,
        members=[],
        llm_backends={},
        council_key_path="",
        ledger_path="",
        redis={"host": ""},
        opa={"enabled": False},
        chairman={"enabled": False},
        code_review={"enabled": False},
        telemetry={
            "enabled": True,
            "url": "http://monitor.invalid/records",
            "token": "test-token",
            "http_capacity": 2,
            "redis_capacity": 2,
            "http_concurrency": 1,
            "redis_concurrency": 1,
            **telemetry,
        },
    )


async def tick_until(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", ["http", "redis"])
async def test_slow_sink_does_not_consume_other_lane_capacity(slow):
    publisher = CouncilTelemetryPublisher(settings())
    release = asyncio.Event()
    http_seen, redis_seen = [], []

    async def http(request):
        http_seen.append(json.loads(request.content))
        if slow == "http":
            await release.wait()
        return httpx.Response(200)

    class Redis:
        async def xadd(self, stream, fields, **kwargs):
            redis_seen.append(json.loads(fields["data"]))
            if slow == "redis":
                await release.wait()

    publisher._http_client = httpx.AsyncClient(transport=httpx.MockTransport(http))
    publisher.attach_redis(Redis())
    try:
        publisher.publish_later({"record": 1})
        await tick_until(lambda: http_seen and redis_seen)
        fast = "redis" if slow == "http" else "http"
        await tick_until(lambda: not publisher._lane_tasks[fast])
        publisher.publish_later({"record": 2})
        await tick_until(lambda: not publisher._lane_tasks[fast])
        publisher.publish_later({"record": 3})
        await tick_until(lambda: not publisher._lane_tasks[fast])
        assert publisher.dropped_by_sink[slow] == 1
        assert publisher.dropped_by_sink[fast] == 0
        assert len(publisher._lane_tasks[slow]) == 2
        assert len(redis_seen if fast == "redis" else http_seen) == 3
    finally:
        release.set()
        await publisher.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [503, 307, "timeout"])
async def test_http_failure_does_not_prevent_redis_or_retry(outcome, caplog):
    publisher = CouncilTelemetryPublisher(settings())
    calls, redis_calls = [], []

    async def http(request):
        calls.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("sensitive endpoint")
        return httpx.Response(
            outcome, text="sensitive body", headers={"Location": "http://elsewhere"}
        )

    class Redis:
        async def xadd(self, *args, **kwargs):
            redis_calls.append(True)

    publisher._http_client = httpx.AsyncClient(transport=httpx.MockTransport(http))
    publisher.attach_redis(Redis())
    publisher.publish_later({"payload": "unchanged"})
    await publisher.aclose()
    assert len(calls) == 1 and len(redis_calls) == 1
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_default_executor_blockage_does_not_block_http_or_service_signing():
    service = CouncilService(settings(redis_enabled=False))
    seen = []
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    blocker_release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(executor)
    blocker = executor.submit(blocker_release.wait, 3)

    async def http(request):
        seen.append(True)
        return httpx.Response(200)

    service._telemetry._http_client = httpx.AsyncClient(transport=httpx.MockTransport(http))
    try:
        async with asyncio.timeout(1):
            signature = await service._writ_signing.sign(signer, b"exact bytes")
            service._telemetry.publish_later({"same": "payload"})
            await tick_until(lambda: seen)
        assert signature.signature_bytes == signer.sign(b"exact bytes").signature_bytes
    finally:
        blocker_release.set()
        blocker.result(timeout=3)
        await service.aclose()


@pytest.mark.asyncio
async def test_signing_capacity_and_cancel_waiting_and_running_hold_actual_workers():
    lane = WritSigningExecutor(workers=1, capacity=2)
    entered = threading.Event()
    release = threading.Event()
    completed = []

    class Signer:
        def sign(self, payload):
            entered.set()
            release.wait(3)
            completed.append(payload)
            return payload

    signer = Signer()
    first = asyncio.create_task(lane.sign(signer, b"one"))
    await tick_until(entered.is_set)
    waiter = asyncio.create_task(lane.sign(signer, b"two"))
    await tick_until(lambda: lane._admitted == 2)
    with pytest.raises(SigningCapacityError, match="temporarily at capacity"):
        await lane.sign(signer, b"excess")
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0.01)
    assert lane._admitted == 1 and not first.done()
    closing = asyncio.create_task(lane.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert completed == [b"one"] and lane._admitted == 0
    with pytest.raises(SigningCapacityError, match="closed"):
        await lane.sign(signer, b"late")


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_cancelled_factory_drains_sign_and_never_persists_writ(managed):
    key = KeyPair.generate(key_id="test")
    wrapped = InMemorySigner(key)
    entered, release = threading.Event(), threading.Event()
    stored = []
    lane = WritSigningExecutor(workers=1, capacity=2)

    class Signer:
        def sign(self, payload):
            entered.set()
            release.wait(3)
            return wrapped.sign(payload)

    class Store:
        async def store_writ(self, *args):
            stored.append(True)

    factory = WritFactory(signer=Signer(), store=Store(), sign_async=lane.sign if managed else None)
    intent = Intent.create(
        agent_id="test",
        action_type="system.info",
        description="Test signature cancellation",
        target_resource="/workspace",
        justification="Test",
        command="pwd",
    )
    task = asyncio.create_task(factory.mint_writ(intent, "test", "quorum", [], "low"))
    await tick_until(entered.is_set)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stored == []
    await lane.aclose()


@pytest.mark.asyncio
async def test_service_signing_overflow_returns_error_without_writ_after_audit():
    service = CouncilService(settings(enabled=False, redis_enabled=False))
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    service._signer_provider = signer
    service.council.ledger._signer = signer

    class Member:
        member_id = display_name = "member"
        provider_name = "offline"

        async def evaluate_intent(self, intent):
            return IntentEvaluation(decision="approve", reasoning="offline")

    service._agents = [Member()]
    service.council.register_council_member(CouncilMember("member", "", "Member"))

    async def full(signer, payload):
        raise SigningCapacityError("Writ signing temporarily at capacity; no writ issued")

    original = service._writ_signing.sign
    service._writ_signing.sign = full
    try:
        result = await service.authorize(
            AuthorizeInput(
                council_id="isolation",
                agent_id="test",
                command="pwd",
                working_directory="/workspace",
                description="offline review",
                target_resource="/workspace",
                justification="test",
            )
        )
        assert result.status == "error" and not result.writ
        assert "temporarily at capacity" in result.error
        assert service.council.ledger.get_review_by_intent(result.decision_id) is not None
    finally:
        service._writ_signing.sign = original
        await service.aclose()


@pytest.mark.asyncio
async def test_http_reuses_local_connection_and_preserves_payload_headers():
    received = []
    connections = []
    handlers = set()

    async def handler(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connections.append(True)
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:-2])
                body = await reader.readexactly(int(headers[b"Content-Length"]))
                received.append((headers, json.loads(body)))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    publisher = CouncilTelemetryPublisher(
        settings(url=f"http://127.0.0.1:{port}/records", redis_enabled=False)
    )
    try:
        for i in range(3):
            publisher.publish_later({"record": i, "stages": {"total": 12.5}})
            await tick_until(lambda: not publisher._tasks)
        assert len(connections) == 1
        assert [payload["record"] for _, payload in received] == [0, 1, 2]
        assert all(headers[b"X-Council-Internal-Token"] == b"test-token" for headers, _ in received)
        assert all(payload["stages"] == {"total": 12.5} for _, payload in received)
    finally:
        await publisher.aclose()
        if handlers:
            await asyncio.gather(*handlers)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_service_shutdown_drains_signing_before_stores_and_never_closes_signer():
    service = CouncilService(settings(enabled=False, redis_enabled=False))
    entered, release = threading.Event(), threading.Event()
    events = []

    class Signer:
        def sign(self, payload):
            entered.set()
            release.wait(3)
            events.append("signed")
            return payload

        def close(self):
            raise AssertionError("shared signer must not close")

    class Store:
        async def close(self):
            events.append("store")

    signer = Signer()
    service._signer_provider = signer
    service._durable_store = Store()
    signing = asyncio.create_task(service._writ_signing.sign(signer, b"test"))
    await tick_until(entered.is_set)
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0.01)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0.01)
    assert not closing.done() and events == []
    release.set()
    assert await signing == b"test"
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert events == ["signed", "store"]


@pytest.mark.asyncio
async def test_failed_http_close_is_owned_and_retryable(caplog):
    publisher = CouncilTelemetryPublisher(settings(redis_enabled=False))
    attempts = []

    class Client:
        async def aclose(self):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("close failed")

    publisher._http_client = Client()
    await publisher.aclose(timeout_s=0.01)
    assert "transport close failed" in caplog.text
    assert publisher._http_close_task.done() and not publisher._http_closed
    await publisher.aclose(timeout_s=0.01)
    assert len(attempts) == 2 and publisher._http_closed
