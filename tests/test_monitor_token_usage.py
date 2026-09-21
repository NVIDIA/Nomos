# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-member LLM token usage: council call → telemetry actor entry → monitor
rollup, plus the Redis-mode dashboard paths that used to read an empty registry."""

import json
import time

import httpx
import pytest

from monitor.server.metrics import LATENCY_SAMPLES_PER_REQUEST, MetricsRegistry
from monitor.server.redis_stats import RedisStatsReader
from nomos.llm.base import LLMResponse
from nomos.llm.council_agent import _call_timing, _complete_with_retry
from nomos.nat.functions import CouncilService


def _actor(member_id, *, role="council_member", tokens=(None, None), provider="nim"):
    actor = {"role": role, "member_id": member_id, "provider": provider, "duration_ms": 900.0}
    if tokens[0] is not None:
        actor["input_tokens"], actor["output_tokens"] = tokens
    return actor


def _record(actors, *, timing_model=None, reviewed=True, tool="council_propose_intent"):
    stages = {"total": 1500.0}
    if reviewed:
        stages["review_collect"] = 1000.0
    record = {"tool": tool, "intent_id": "i", "outcome": "approved", "stages": stages}
    record["actors"] = actors
    if timing_model is not None:
        record["timing_model"] = timing_model
    return record


# ── council side ──────────────────────────────────────────────────────


class TestCouncilReportsTokens:
    @pytest.mark.parametrize(
        "usage",
        [
            {"input_tokens": 1200, "output_tokens": 80},
            {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280},
        ],
    )
    def test_call_timing_normalises_both_usage_shapes(self, usage):
        timing = _call_timing(LLMResponse(content="{}", usage=usage), attempt=0, call_ms=10.0)
        assert (timing["input_tokens"], timing["output_tokens"]) == (1200.0, 80.0)

    def test_call_timing_without_usage_reports_zero(self):
        timing = _call_timing(LLMResponse(content="{}"), attempt=0, call_ms=10.0)
        assert (timing["input_tokens"], timing["output_tokens"]) == (0.0, 0.0)

    @pytest.mark.asyncio
    async def test_tokens_from_an_unparseable_attempt_are_still_counted(self, monkeypatch):
        monkeypatch.setattr("nomos.llm.council_agent.RETRY_DELAY_SECONDS", 0.0)
        responses = [
            LLMResponse(content="not json", usage={"input_tokens": 1000, "output_tokens": 50}),
            LLMResponse(content='{"ok": 1}', usage={"input_tokens": 1100, "output_tokens": 60}),
        ]

        class Backend:
            async def complete(self, **_):
                return responses.pop(0)

            def provider_name(self):
                return "fake"

        _, response, attempt, call_ms = await _complete_with_retry(
            Backend(), [], temperature=0.0, max_tokens=64, parse=json.loads, log_ctx="test"
        )
        timing = _call_timing(response, attempt, call_ms)
        assert attempt == 1
        assert (timing["input_tokens"], timing["output_tokens"]) == (2100.0, 110.0)

    def test_actor_entry_carries_tokens(self):
        entry = CouncilService._actor_latency(
            role="council_member",
            member_id="blast",
            provider="nim",
            duration_ms=12.0,
            timing={"input_tokens": 900.0, "output_tokens": 40.0, "call_ms": 12.0},
        )
        assert (entry["input_tokens"], entry["output_tokens"]) == (900.0, 40.0)
        assert "call_ms" not in entry  # whitelist is unchanged for everything else


# ── monitor rollup ────────────────────────────────────────────────────


class TestTokenRollup:
    @pytest.mark.asyncio
    async def test_per_member_totals_with_debate_folded_into_the_member(self):
        reg = MetricsRegistry(window_seconds=60.0)
        await reg.record_council_latency(
            _record(
                [
                    _actor("integrity", tokens=(1000, 100)),
                    _actor("integrity", role="council_member_debate_1", tokens=(1500, 120)),
                    _actor("blast", tokens=(900, 80)),
                    _actor("chairman", role="chairman", tokens=(700, 30)),
                ]
            )
        )
        tokens = (await reg.snapshot()).to_dict()["llm_tokens"]["window"]

        assert (tokens["input"], tokens["output"], tokens["total"]) == (4100, 330, 4430)
        assert tokens["calls"] == 4
        assert tokens["per_sec"] == round(4430 / 60.0, 1)
        rows = {row["member_id"]: row for row in tokens["by_member"]}
        assert [row["member_id"] for row in tokens["by_member"]] == [
            "integrity",
            "blast",
            "chairman",
        ]
        assert rows["integrity"]["calls"] == 2
        assert rows["integrity"]["total"] == 2720
        assert rows["integrity"]["role"] == "council_member"  # not the debate role
        assert rows["chairman"]["role"] == "chairman"
        assert rows["blast"]["avg_per_call"] == 980.0
        assert sum(row["share"] for row in tokens["by_member"]) == pytest.approx(1.0, abs=1e-3)

    @pytest.mark.asyncio
    async def test_window_and_retained_are_separate(self):
        reg = MetricsRegistry(window_seconds=60.0)
        now = time.time()
        await reg.ingest_council_record(
            _record([_actor("blast", tokens=(500, 50))]), entry_ts=now - 600
        )
        await reg.ingest_council_record(_record([_actor("blast", tokens=(300, 30))]), entry_ts=now)
        tokens = (await reg.snapshot(now=now)).to_dict()["llm_tokens"]
        assert tokens["window"]["total"] == 330
        assert tokens["retained"]["total"] == 880
        assert "per_sec" not in tokens["retained"]

    @pytest.mark.asyncio
    async def test_council_without_token_telemetry_is_reported_as_no_coverage(self):
        reg = MetricsRegistry(window_seconds=60.0)
        await reg.record_council_latency(_record([_actor("blast")]))
        snap = (await reg.snapshot()).to_dict()
        assert snap["llm_latency"]["calls_in_window"] == 1
        assert snap["llm_tokens"]["window"] == {
            "calls": 0,
            "input": 0,
            "output": 0,
            "total": 0,
            "by_member": [],
            "per_sec": 0.0,
        }


class TestRegistryAccuracy:
    @pytest.mark.asyncio
    async def test_timing_model_follows_reviews_and_ignores_early_exits(self):
        reg = MetricsRegistry()
        assert (await reg.snapshot()).timing_model == ""
        await reg.record_council_latency(_record([], timing_model="overlapping"))
        await reg.record_council_latency(_record([], reviewed=False))  # OPA denial
        assert (await reg.snapshot()).timing_model == "overlapping"
        await reg.record_council_latency(_record([]))  # serial review
        assert (await reg.snapshot()).timing_model == ""

    @pytest.mark.asyncio
    async def test_latency_histories_are_bounded(self):
        reg = MetricsRegistry(history_limit=2)
        for _ in range(LATENCY_SAMPLES_PER_REQUEST * 3):
            await reg.record_council_latency(_record([_actor("blast", tokens=(1, 1))]))
        snap = (await reg.snapshot()).to_dict()
        cap = 2 * LATENCY_SAMPLES_PER_REQUEST
        assert snap["council_actor_latency"]["council_member:blast"]["count"] == cap
        assert snap["council_stage_latency"]["total"]["count"] <= cap


# ── Redis mode: every dashboard endpoint reads the replay ─────────────


class _FakeStream:
    def __init__(self, entries):
        self.entries = entries
        self.reads = 0

    async def xrevrange(self, stream, count=None):
        self.reads += 1
        return list(reversed(self.entries))[:count]


def _stream_entries():
    now_ms = int(time.time() * 1000)
    record = _record([_actor("blast", tokens=(900, 80))], timing_model="overlapping")
    return [(f"{now_ms}-{i}", {"data": json.dumps(record)}) for i in range(3)]


@pytest.mark.asyncio
async def test_replay_is_shared_within_the_ttl():
    stream = _FakeStream(_stream_entries())
    reader = RedisStatsReader(stream, stream="s")
    first = await reader.registry()
    assert await reader.registry() is first
    await reader.snapshot()
    assert stream.reads == 1


@pytest.mark.asyncio
async def test_redis_mode_serves_recent_and_export_from_the_stream(monkeypatch):
    """Regression: these two read the empty in-memory registry in Redis mode."""
    from monitor.server.main import build_app

    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    app = build_app(upstream_url="http://upstream/mcp", upstream_health_url="http://upstream/h")
    app.state.stats_reader = RedisStatsReader(_FakeStream(_stream_entries()), stream="s")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://monitor"
    ) as client:
        metrics = (await client.get("/api/metrics")).json()
        recent = (await client.get("/api/recent")).json()
        export = (await client.get("/api/export.csv")).text

    assert metrics["stats_source"] == "redis"
    assert metrics["completed_in_window"] == 3
    assert metrics["council_completed_in_window"] == 3
    assert metrics["timing_model"] == "overlapping"
    assert metrics["llm_tokens"]["window"]["total"] == 3 * 980
    assert len(recent["rows"]) == 3
    assert recent["rows"][0]["tool"] == "council_propose_intent"
    assert "summary,monitor,stats_source,redis" in export
    assert "llm_tokens,blast,total,2940" in export


@pytest.mark.asyncio
async def test_inline_mode_reports_its_source():
    assert (await MetricsRegistry().snapshot()).to_dict()["stats_source"] == "inline"
