# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Redis telemetry sink (nomos/nat/telemetry_sink.py) and the
publisher fan-out (council writes stats to the Redis stream post-gateway)."""

import asyncio
import json

import pytest

from nomos.nat.telemetry_sink import RedisTelemetrySink


class FakeRedis:
    """Minimal async stand-in capturing xadd calls."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    async def xadd(self, stream, fields, *, maxlen=None, approximate=None):
        if self._fail:
            raise ConnectionError("redis down")
        self.calls.append(
            {"stream": stream, "fields": fields, "maxlen": maxlen, "approximate": approximate}
        )
        return b"1-0"


PAYLOAD = {
    "tool": "council_propose_intent",
    "council_id": "council-prod",
    "intent_id": "abc123",
    "outcome": "approved",
    "stages": {"total": 1234.5, "opa": 12.0},
    "actors": [{"role": "member", "member_id": "m1", "duration_ms": 800.0}],
    "limiters": [],
}


@pytest.mark.asyncio
async def test_sink_inactive_without_client_is_noop():
    sink = RedisTelemetrySink(stream="nomos:council:telemetry", maxlen=5000)
    assert sink.active is False
    await sink.publish(PAYLOAD)  # must not raise


@pytest.mark.asyncio
async def test_sink_xadds_payload_with_cap():
    sink = RedisTelemetrySink(stream="nomos:council:telemetry", maxlen=5000)
    client = FakeRedis()
    sink.attach(client)
    assert sink.active is True

    await sink.publish(PAYLOAD)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["stream"] == "nomos:council:telemetry"
    assert call["maxlen"] == 5000 and call["approximate"] is True
    # Payload is stored losslessly as one JSON field; ts comes from the entry ID.
    assert json.loads(call["fields"]["data"]) == PAYLOAD


@pytest.mark.asyncio
async def test_sink_swallows_redis_errors():
    sink = RedisTelemetrySink(stream="s", maxlen=100)
    sink.attach(FakeRedis(fail=True))
    await sink.publish(PAYLOAD)  # best-effort: must not raise


@pytest.mark.asyncio
async def test_disabled_sink_does_not_publish():
    sink = RedisTelemetrySink(stream="s", maxlen=100, enabled=False)
    client = FakeRedis()
    sink.attach(client)
    assert sink.active is False
    await sink.publish(PAYLOAD)
    assert client.calls == []


@pytest.mark.asyncio
async def test_publisher_fans_out_to_redis_when_http_disabled():
    """publish_later fires the redis sink even when the HTTP sink is off."""
    from nomos.config.settings import CouncilSettings
    from nomos.nat.functions import CouncilTelemetryPublisher

    settings = CouncilSettings()  # telemetry.enabled defaults False (no HTTP)
    pub = CouncilTelemetryPublisher(settings)
    client = FakeRedis()
    pub.attach_redis(client)

    pub.publish_later(PAYLOAD)
    # publish_later schedules an asyncio task; let it run.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        await t

    assert len(client.calls) == 1
    assert client.calls[0]["stream"].endswith("council:telemetry")
