# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the monitor's Redis stats path: replaying the council telemetry
stream into a fresh MetricsRegistry reproduces the snapshot (Phase 5)."""

import json

import pytest

from monitor.server.metrics import MetricsRegistry
from monitor.server.redis_stats import RedisStatsReader, _decode_record, _entry_ts_seconds


def _rec(tool, outcome, total_ms, **stages):
    return {
        "tool": tool,
        "council_id": "council-prod",
        "intent_id": "i",
        "outcome": outcome,
        "stages": {"total": total_ms, **stages},
        "actors": [{"role": "member", "member_id": "m1", "duration_ms": total_ms / 2}],
        "limiters": [],
    }


@pytest.mark.asyncio
async def test_ingest_record_builds_completion_and_outcome():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1_000_000.0
    await reg.ingest_council_record(
        _rec("council_propose_intent", "approved", 1200.0), entry_ts=now
    )
    await reg.ingest_council_record(_rec("council_propose_intent", "denied", 800.0), entry_ts=now)
    await reg.ingest_council_record(_rec("council_authorize", "error", 50.0), entry_ts=now)

    snap = (await reg.snapshot(now=now)).to_dict()
    assert snap["completed_in_window"] == 3
    assert snap["by_tool"] == {"council_propose_intent": 2, "council_authorize": 1}
    assert snap["by_decision_outcome"] == {"approved": 1, "denied": 1, "error": 1}
    # one error (outcome=="error") → error_rate 1/3
    assert snap["error_count"] == 1
    assert round(snap["error_rate"], 3) == round(1 / 3, 3)
    # latency percentiles populated from stages.total
    assert snap["latency_ms"]["p50"] > 0
    # stage + actor latency captured from the same records
    assert snap["council_stage_latency"]  # non-empty


@pytest.mark.asyncio
async def test_window_excludes_old_entries():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1_000_000.0
    await reg.ingest_council_record(
        _rec("council_propose_intent", "approved", 100.0), entry_ts=now - 120
    )
    await reg.ingest_council_record(_rec("council_propose_intent", "approved", 100.0), entry_ts=now)
    snap = (await reg.snapshot(now=now)).to_dict()
    assert snap["completed_in_window"] == 1  # the 120s-old entry is outside the 60s window


class FakeRedisStream:
    def __init__(self, entries, *, fail=False):
        self._entries = entries  # list[(id, {"data": json})]
        self._fail = fail

    async def xrevrange(self, stream, count=None):
        if self._fail:
            raise ConnectionError("redis down")
        return list(reversed(self._entries))[:count]  # newest first


def _entry(ts_ms, rec):
    return (f"{ts_ms}-0", {"data": json.dumps(rec)})


@pytest.mark.asyncio
async def test_reader_rebuilds_snapshot_from_stream():
    base = 1_700_000_000_000  # ms
    entries = [
        _entry(base + 0, _rec("council_propose_intent", "approved", 900.0)),
        _entry(base + 1000, _rec("council_propose_intent", "denied", 1100.0)),
        _entry(base + 2000, _rec("council_authorize", "approved", 700.0)),
    ]
    # window large enough to include all three (their ts ≈ base seconds, ~now)
    reader = RedisStatsReader(
        FakeRedisStream(entries), stream="s", window_seconds=10**9, history_limit=5000
    )
    snap = (await reader.snapshot()).to_dict()
    assert snap["completed_in_window"] == 3
    assert snap["by_tool"] == {"council_propose_intent": 2, "council_authorize": 1}
    assert snap["by_decision_outcome"] == {"approved": 2, "denied": 1}


@pytest.mark.asyncio
async def test_reader_degrades_to_empty_on_redis_error():
    reader = RedisStatsReader(FakeRedisStream([], fail=True), stream="s")
    snap = (await reader.snapshot()).to_dict()  # must not raise
    assert snap["completed_in_window"] == 0


def test_entry_ts_and_decode_helpers():
    assert _entry_ts_seconds("1700000000123-5") == 1700000000.123
    assert _entry_ts_seconds("garbage") == 0.0
    assert _decode_record({"data": '{"tool":"x"}'}) == {"tool": "x"}
    assert _decode_record({"data": "not json"}) is None
    assert _decode_record({"nope": "1"}) is None
