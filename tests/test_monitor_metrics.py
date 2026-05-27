"""Unit tests for the monitor metrics registry.

The registry is the source of truth for every dashboard widget. Tests are
deterministic — they pass an explicit `now` to the snapshot to avoid any
reliance on wall-clock time.
"""

from __future__ import annotations

import asyncio

import pytest

from monitor.server.metrics import (
    Completion,
    DecisionSummary,
    MetricsRegistry,
    RequestSummary,
    Snapshot,
)


def _completion(
    *,
    end: float,
    duration_ms: float,
    method: str = "tools/call",
    tool: str = "council_authorize",
    source: str = "direct",
    status_code: int = 200,
    ok: bool = True,
    request: RequestSummary | None = None,
    decision: DecisionSummary | None = None,
) -> Completion:
    return Completion(
        start_ts=end - duration_ms / 1000.0,
        end_ts=end,
        duration_ms=duration_ms,
        method=method,
        tool=tool,
        source=source,
        status_code=status_code,
        ok=ok,
        request=request,
        decision=decision,
    )


def _decision(
    *,
    outcome: str = "approved",
    risk_level: str = "low",
    error_class: str = "",
    missing_permissions: tuple[str, ...] = (),
) -> DecisionSummary:
    return DecisionSummary(
        outcome=outcome,
        error_class=error_class,
        risk_level=risk_level,
        threshold="unanimous",
        approval_votes=2,
        denial_votes=0,
        abstain_votes=0,
        debate_rounds=0,
        changed_votes=0,
        code_review="approve",
        missing_permissions=missing_permissions,
        guidance_count=0,
        writ_issued=outcome == "approved",
        decision_id="decision-1",
        writ_id="writ-1" if outcome == "approved" else "",
        request=RequestSummary(
            agent_id="agent-admin",
            command_hash="abc123",
            command_executable="echo",
            packaged_file_count=1,
            evidence_count=2,
            env_key_count=0,
        ),
    )


def _request(agent_id: str, *, executable: str = "echo") -> RequestSummary:
    return RequestSummary(
        agent_id=agent_id,
        command_hash=f"hash-{agent_id}-{executable}",
        command_executable=executable,
        packaged_file_count=0,
        evidence_count=0,
        env_key_count=0,
    )


@pytest.mark.asyncio
async def test_inflight_starts_at_zero():
    reg = MetricsRegistry()
    snap = await reg.snapshot(now=1000.0)
    assert snap.inflight == 0
    assert snap.completed_in_window == 0


@pytest.mark.asyncio
async def test_start_increments_inflight_complete_decrements():
    reg = MetricsRegistry()
    h1 = await reg.start(now=1000.0, method="tools/call", tool="x", source="direct")
    h2 = await reg.start(now=1000.5, method="tools/call", tool="y", source="grpc")
    snap = await reg.snapshot(now=1001.0)
    assert snap.inflight == 2

    await reg.complete(h1, now=1001.0, status_code=200, ok=True)
    snap = await reg.snapshot(now=1001.0)
    assert snap.inflight == 1

    await reg.complete(h2, now=1002.0, status_code=200, ok=True)
    snap = await reg.snapshot(now=1002.0)
    assert snap.inflight == 0


@pytest.mark.asyncio
async def test_complete_records_duration():
    reg = MetricsRegistry()
    h = await reg.start(now=1000.0, method="tools/call", tool="t", source="direct")
    await reg.complete(h, now=1000.250, status_code=200, ok=True)
    snap = await reg.snapshot(now=1001.0)
    assert snap.completed_in_window == 1
    assert snap.latency_ms.p50 == pytest.approx(250.0, abs=1.0)


@pytest.mark.asyncio
async def test_percentiles_over_window():
    reg = MetricsRegistry(window_seconds=60.0)
    # 100 completions, durations 1..100 ms, all ending right before snapshot
    now = 1000.0
    for i in range(1, 101):
        reg._record(_completion(end=now - 0.5, duration_ms=float(i)))  # type: ignore[attr-defined]
    snap = await reg.snapshot(now=now)
    # standard nearest-rank percentile: p50 ≈ 50, p95 ≈ 95, p99 ≈ 99
    assert snap.latency_ms.p50 == pytest.approx(50.0, abs=1.0)
    assert snap.latency_ms.p95 == pytest.approx(95.0, abs=1.0)
    assert snap.latency_ms.p99 == pytest.approx(99.0, abs=1.0)
    assert snap.completed_in_window == 100


@pytest.mark.asyncio
async def test_window_drops_old_completions_from_stats():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1000.0
    # Old (outside window) — should not affect percentiles
    reg._record(_completion(end=now - 120.0, duration_ms=9999.0))  # type: ignore[attr-defined]
    # In window
    reg._record(_completion(end=now - 1.0, duration_ms=10.0))  # type: ignore[attr-defined]
    snap = await reg.snapshot(now=now)
    assert snap.completed_in_window == 1
    assert snap.latency_ms.p50 == pytest.approx(10.0, abs=1.0)
    assert snap.completed_total == 2
    assert snap.by_source_total == {"direct": 2}
    assert snap.by_tool_total == {"council_authorize": 2}


@pytest.mark.asyncio
async def test_throughput_per_minute_reflects_window():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1000.0
    for i in range(30):
        reg._record(_completion(end=now - 30.0 + i, duration_ms=10.0))  # type: ignore[attr-defined]
    snap = await reg.snapshot(now=now)
    # 30 events in a 60s window → 30 per window, 0.5/sec
    assert snap.completed_in_window == 30
    assert snap.throughput_per_sec == pytest.approx(0.5, abs=1e-6)
    assert snap.council_completed_in_window == 30
    assert snap.council_throughput_per_sec == pytest.approx(0.5, abs=1e-6)
    assert snap.council_throughput_per_min == pytest.approx(30.0, abs=1e-6)


@pytest.mark.asyncio
async def test_council_throughput_counts_versioned_aliases_only():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1000.0
    for tool in [
        "council_authorize",
        "council_propose_intent",
        "council_v1_authorize",
        "council_v1_propose_intent",
        "council_v2_authorize",
        "council_v2_propose_intent",
    ]:
        reg._record(_completion(end=now - 1.0, duration_ms=10.0, tool=tool))  # type: ignore[attr-defined]
    reg._record(_completion(end=now - 1.0, duration_ms=10.0, tool="tools/list"))  # type: ignore[attr-defined]

    snap = await reg.snapshot(now=now)

    assert snap.completed_in_window == 7
    assert snap.council_completed_in_window == 6
    assert snap.throughput_per_min == pytest.approx(7.0, abs=1e-6)
    assert snap.council_throughput_per_min == pytest.approx(6.0, abs=1e-6)


@pytest.mark.asyncio
async def test_source_breakdown():
    reg = MetricsRegistry()
    now = 1000.0
    for source, count in [("direct", 3), ("grpc", 5), ("demo", 2)]:
        for _ in range(count):
            reg._record(  # type: ignore[attr-defined]
                _completion(end=now - 1.0, duration_ms=10.0, source=source)
            )
    snap = await reg.snapshot(now=now)
    assert snap.by_source == {"direct": 3, "grpc": 5, "demo": 2}
    assert snap.by_source_total == {"direct": 3, "grpc": 5, "demo": 2}


@pytest.mark.asyncio
async def test_tool_breakdown_only_for_tools_call():
    reg = MetricsRegistry()
    now = 1000.0
    reg._record(_completion(end=now - 1.0, duration_ms=10.0, method="tools/call", tool="a"))  # type: ignore[attr-defined]
    reg._record(_completion(end=now - 1.0, duration_ms=10.0, method="tools/call", tool="a"))  # type: ignore[attr-defined]
    reg._record(_completion(end=now - 1.0, duration_ms=10.0, method="tools/call", tool="b"))  # type: ignore[attr-defined]
    # Non-tools/call entries shouldn't show up in by_tool
    reg._record(_completion(end=now - 1.0, duration_ms=10.0, method="initialize", tool=""))  # type: ignore[attr-defined]
    snap = await reg.snapshot(now=now)
    assert snap.by_tool == {"a": 2, "b": 1}
    assert snap.by_tool_total == {"a": 2, "b": 1}


@pytest.mark.asyncio
async def test_agent_account_activity_is_windowed_and_cumulative():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1000.0
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 1.0,
            duration_ms=20.0,
            tool="council_authorize",
            request=_request("agent-admin"),
        )
    )
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 2.0,
            duration_ms=30.0,
            tool="council_propose_intent",
            status_code=502,
            ok=False,
            request=_request("agent-admin", executable="git"),
        )
    )
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 120.0,
            duration_ms=10.0,
            tool="council_authorize",
            request=_request("agent-reader"),
        )
    )

    snap = await reg.snapshot(now=now)

    assert snap.by_agent == {"agent-admin": 2}
    assert snap.by_agent_total == {"agent-admin": 2, "agent-reader": 1}
    assert snap.by_agent_tool == {
        "agent-admin": {"council_authorize": 1, "council_propose_intent": 1}
    }
    assert snap.by_agent_tool_total == {
        "agent-admin": {"council_authorize": 1, "council_propose_intent": 1},
        "agent-reader": {"council_authorize": 1},
    }

    admin = snap.agent_activity["agent-admin"]
    assert admin["completed_in_window"] == 2
    assert admin["completed_total"] == 2
    assert admin["error_in_window"] == 1
    assert admin["error_total"] == 1
    assert admin["by_tool_total"] == {"council_authorize": 1, "council_propose_intent": 1}
    assert admin["last_seen_ms"] == int((now - 1.0) * 1000.0)

    reader = snap.agent_activity["agent-reader"]
    assert reader["completed_in_window"] == 0
    assert reader["completed_total"] == 1
    assert reader["by_tool_total"] == {"council_authorize": 1}


@pytest.mark.asyncio
async def test_error_rate():
    reg = MetricsRegistry()
    now = 1000.0
    for _ in range(8):
        reg._record(_completion(end=now - 1.0, duration_ms=10.0, ok=True, status_code=200))  # type: ignore[attr-defined]
    for _ in range(2):
        reg._record(_completion(end=now - 1.0, duration_ms=10.0, ok=False, status_code=502))  # type: ignore[attr-defined]
    snap = await reg.snapshot(now=now)
    assert snap.completed_in_window == 10
    assert snap.error_count == 2
    assert snap.error_rate == pytest.approx(0.2)
    assert snap.completed_total == 10
    assert snap.error_total == 2


@pytest.mark.asyncio
async def test_decision_breakdowns_are_windowed_and_cumulative():
    reg = MetricsRegistry(window_seconds=60.0)
    now = 1000.0
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 1.0,
            duration_ms=10.0,
            decision=_decision(outcome="approved", risk_level="low"),
        )
    )
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 1.0,
            duration_ms=10.0,
            decision=_decision(
                outcome="denied",
                risk_level="high",
                error_class="permission",
                missing_permissions=("infra:write",),
            ),
        )
    )
    reg._record(  # type: ignore[attr-defined]
        _completion(
            end=now - 120.0,
            duration_ms=10.0,
            decision=_decision(outcome="error", risk_level="critical", error_class="auth"),
        )
    )

    snap = await reg.snapshot(now=now)

    assert snap.decisions_in_window == 2
    assert snap.decisions_total == 3
    assert snap.by_decision_outcome == {"approved": 1, "denied": 1}
    assert snap.by_decision_outcome_total == {"approved": 1, "denied": 1, "error": 1}
    assert snap.by_risk == {"low": 1, "high": 1}
    assert snap.by_risk_total == {"low": 1, "high": 1, "critical": 1}
    assert snap.by_error_class == {"permission": 1}
    assert snap.by_error_class_total == {"permission": 1, "auth": 1}
    assert snap.by_missing_permission == {"infra:write": 1}
    assert snap.by_missing_permission_total == {"infra:write": 1}
    assert snap.by_code_review == {"approve": 2}


@pytest.mark.asyncio
async def test_recent_returns_inflight_then_completed():
    reg = MetricsRegistry()
    h1 = await reg.start(
        now=1000.0,
        method="tools/call",
        tool="t",
        source="direct",
        request=_request("agent-direct"),
    )
    await reg.complete(h1, now=1000.1, status_code=200, ok=True)
    h2 = await reg.start(
        now=1001.0,
        method="tools/call",
        tool="u",
        source="grpc",
        request=_request("agent-grpc"),
    )
    recent = await reg.recent(now=1001.5, limit=10)
    # Inflight first (still open), then completed in reverse chronological
    assert recent[0].state == "inflight"
    assert recent[0].source == "grpc"
    assert recent[0].request is not None
    assert recent[0].request.agent_id == "agent-grpc"
    assert recent[0].duration_ms is None  # ongoing
    assert recent[0].age_ms == pytest.approx(500.0, abs=2.0)
    assert recent[1].state == "completed"
    assert recent[1].source == "direct"
    assert recent[1].request is not None
    assert recent[1].request.agent_id == "agent-direct"
    assert recent[1].duration_ms == pytest.approx(100.0, abs=1.0)
    # cleanup so we don't leak inflight handle into other tests
    await reg.complete(h2, now=1002.0, status_code=200, ok=True)


@pytest.mark.asyncio
async def test_concurrent_starts_are_safe():
    reg = MetricsRegistry()

    async def hit() -> None:
        h = await reg.start(now=1000.0, method="tools/call", tool="t", source="direct")
        await reg.complete(h, now=1000.05, status_code=200, ok=True)

    await asyncio.gather(*(hit() for _ in range(50)))
    snap = await reg.snapshot(now=1001.0)
    assert snap.inflight == 0
    assert snap.completed_in_window == 50


@pytest.mark.asyncio
async def test_snapshot_is_serializable():
    """The dashboard returns Snapshot.to_dict() as JSON; verify shape."""
    reg = MetricsRegistry()
    h = await reg.start(now=1000.0, method="tools/call", tool="t", source="direct")
    await reg.complete(h, now=1000.05, status_code=200, ok=True)
    snap: Snapshot = await reg.snapshot(now=1001.0)
    payload = snap.to_dict()
    assert set(payload).issuperset(
        {
            "inflight",
            "completed_in_window",
            "council_completed_in_window",
            "throughput_per_sec",
            "throughput_per_min",
            "council_throughput_per_sec",
            "council_throughput_per_min",
            "latency_ms",
            "error_count",
            "error_rate",
            "by_source",
            "by_tool",
            "by_agent",
            "by_agent_tool",
            "completed_total",
            "error_total",
            "by_source_total",
            "by_tool_total",
            "by_agent_total",
            "by_agent_tool_total",
            "agent_activity",
            "decisions_in_window",
            "decisions_total",
            "by_decision_outcome",
            "by_decision_outcome_total",
            "by_risk",
            "by_risk_total",
            "by_error_class",
            "by_error_class_total",
            "by_missing_permission",
            "by_missing_permission_total",
            "by_code_review",
            "by_code_review_total",
            "council_actor_latency",
            "council_stage_latency",
            "council_actor_latency_window",
            "council_stage_latency_window",
            "window_seconds",
            "asof_ms",
        }
    )
    assert payload["latency_ms"]["p50"] == pytest.approx(50.0, abs=1.0)


@pytest.mark.asyncio
async def test_council_latency_telemetry_aggregates_actor_and_stage_metrics():
    reg = MetricsRegistry(window_seconds=60.0)
    await reg.record_council_latency(
        {
            "tool": "council_propose_intent",
            "stages": {"chairman": 1000.0, "review_collect": 3000.0},
            "actors": [
                {
                    "role": "chairman",
                    "member_id": "chairman",
                    "provider": "nv_inference",
                    "duration_ms": 1000.0,
                    "decision": "unanimous",
                },
                {
                    "role": "council_member",
                    "member_id": "command-integrity",
                    "provider": "nv_inference",
                    "duration_ms": 3000.0,
                    "decision": "approve",
                },
            ],
        }
    )
    await reg.record_council_latency(
        {
            "tool": "council_propose_intent",
            "stages": {"chairman": 1500.0},
            "actors": [
                {
                    "role": "chairman",
                    "member_id": "chairman",
                    "provider": "nv_inference",
                    "duration_ms": 1500.0,
                    "decision": "unanimous",
                }
            ],
        }
    )
    snap = await reg.snapshot()
    chairman = snap.council_actor_latency["chairman:chairman"]
    assert chairman["count"] == 2
    assert chairman["avg_ms"] == pytest.approx(1250.0)
    assert chairman["p95_ms"] == 1500.0
    assert chairman["last_decision"] == "unanimous"
    assert snap.council_stage_latency["chairman"]["count"] == 2
    assert snap.council_stage_latency["review_collect"]["max_ms"] == 3000.0

    future = await reg.snapshot(now=(snap.asof_ms / 1000.0) + 120.0)
    assert future.council_actor_latency["chairman:chairman"]["count"] == 2
    assert future.council_stage_latency["review_collect"]["max_ms"] == 3000.0
    assert future.council_actor_latency_window == {}
    assert future.council_stage_latency_window == {}


@pytest.mark.asyncio
async def test_ring_buffer_caps_history():
    reg = MetricsRegistry(history_limit=5)
    now = 1000.0
    for i in range(20):
        reg._record(_completion(end=now - 1.0 + i * 0.001, duration_ms=10.0))  # type: ignore[attr-defined]
    recent = await reg.recent(now=now + 1.0, limit=100)
    # Capped at 5 — only the last 5 fit
    assert len(recent) == 5
    snap = await reg.snapshot(now=now + 1.0)
    assert snap.completed_total == 20
    assert snap.by_tool_total == {"council_authorize": 20}
