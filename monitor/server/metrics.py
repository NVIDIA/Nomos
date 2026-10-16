# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metrics registry for the council MCP monitor.

Holds a bounded history of recently completed requests plus the current
inflight count, and serves dashboard snapshots derived from a configurable
rolling window. Designed for a single-process FastAPI app — uses asyncio
locks for concurrency safety. No dependency on FastAPI, httpx, or MCP, so
the registry is unit-testable in isolation.

A `start` returns a handle; the caller is responsible for calling
`complete` exactly once. Failure paths in the proxy must wrap completion
in try/finally so a crash never leaves inflight permanently incremented.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_HISTORY_LIMIT = 5000
# A council record carries ~10 stage and ~5 actor samples, so the latency
# histories are capped at a multiple of the request history rather than growing
# for the life of the process.
LATENCY_SAMPLES_PER_REQUEST = 16

# Source values we recognize. The proxy accepts any string but the dashboard
# colors these three. Anything else is rendered under "other".
KNOWN_SOURCES = ("direct", "grpc", "demo")
COUNCIL_TOOLS = frozenset(
    {
        "council_authorize",
        "council_propose_intent",
        "council_v1_authorize",
        "council_v1_propose_intent",
        "council_v2_authorize",
        "council_v2_propose_intent",
    }
)


@dataclass(frozen=True)
class RequestSummary:
    """Sanitized facts from the MCP tool arguments.

    This intentionally avoids raw auth tokens, command text, file contents,
    environment values, and writ material. It exists so the dashboard can
    explain traffic shape without becoming a secret-bearing log sink.
    """

    agent_id: str
    command_hash: str
    command_executable: str
    packaged_file_count: int
    evidence_count: int
    env_key_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "command_hash": self.command_hash,
            "command_executable": self.command_executable,
            "packaged_file_count": self.packaged_file_count,
            "evidence_count": self.evidence_count,
            "env_key_count": self.env_key_count,
        }


@dataclass(frozen=True)
class DecisionSummary:
    """Sanitized council decision facts parsed from an MCP tool response."""

    outcome: str
    error_class: str
    risk_level: str
    threshold: str
    approval_votes: int
    denial_votes: int
    abstain_votes: int
    debate_rounds: int
    changed_votes: int
    code_review: str
    missing_permissions: tuple[str, ...] = ()
    guidance_count: int = 0
    writ_issued: bool = False
    decision_id: str = ""
    writ_id: str = ""
    request: RequestSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "error_class": self.error_class,
            "risk_level": self.risk_level,
            "threshold": self.threshold,
            "approval_votes": self.approval_votes,
            "denial_votes": self.denial_votes,
            "abstain_votes": self.abstain_votes,
            "debate_rounds": self.debate_rounds,
            "changed_votes": self.changed_votes,
            "code_review": self.code_review,
            "missing_permissions": list(self.missing_permissions),
            "guidance_count": self.guidance_count,
            "writ_issued": self.writ_issued,
            "decision_id": self.decision_id,
            "writ_id": self.writ_id,
            "request": self.request.to_dict() if self.request else None,
        }


@dataclass(frozen=True)
class Completion:
    """One finished request, recorded for stats."""

    start_ts: float
    end_ts: float
    duration_ms: float
    method: str
    tool: str
    source: str
    status_code: int
    ok: bool
    request: RequestSummary | None = None
    decision: DecisionSummary | None = None


@dataclass(frozen=True)
class Inflight:
    """Open request not yet completed. Dashboard shows these in the live table."""

    handle: int
    start_ts: float
    method: str
    tool: str
    source: str
    request: RequestSummary | None = None


@dataclass(frozen=True)
class LatencyPercentiles:
    p50: float
    p95: float
    p99: float

    def to_dict(self) -> dict[str, float]:
        return {"p50": self.p50, "p95": self.p95, "p99": self.p99}


@dataclass(frozen=True)
class LatencyStats:
    count: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    last_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "avg_ms": self.avg_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "last_ms": self.last_ms,
        }


@dataclass(frozen=True)
class ActorLatencySample:
    ts: float
    tool: str
    role: str
    member_id: str
    provider: str
    duration_ms: float
    decision: str
    inference_ms: float | None = None
    queue_wait_ms: float | None = None
    attempts: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None


@dataclass(frozen=True)
class StageLatencySample:
    ts: float
    tool: str
    stage: str
    duration_ms: float


@dataclass(frozen=True)
class Snapshot:
    """What `/api/metrics` returns.

    Latency, throughput, success, and error values are over the rolling window.
    The `*_total` values are cumulative for the monitor process lifetime.
    """

    inflight: int
    completed_in_window: int
    council_completed_in_window: int
    throughput_per_sec: float
    throughput_per_min: float
    council_throughput_per_sec: float
    council_throughput_per_min: float
    latency_ms: LatencyPercentiles
    error_count: int
    error_rate: float
    by_source: dict[str, int]
    by_tool: dict[str, int]
    by_agent: dict[str, int]
    by_agent_tool: dict[str, dict[str, int]]
    completed_total: int
    error_total: int
    by_source_total: dict[str, int]
    by_tool_total: dict[str, int]
    by_agent_total: dict[str, int]
    by_agent_tool_total: dict[str, dict[str, int]]
    agent_activity: dict[str, dict[str, Any]]
    decisions_in_window: int
    decisions_total: int
    by_decision_outcome: dict[str, int]
    by_decision_outcome_total: dict[str, int]
    by_risk: dict[str, int]
    by_risk_total: dict[str, int]
    by_error_class: dict[str, int]
    by_error_class_total: dict[str, int]
    by_missing_permission: dict[str, int]
    by_missing_permission_total: dict[str, int]
    by_code_review: dict[str, int]
    by_code_review_total: dict[str, int]
    council_actor_latency: dict[str, dict[str, Any]]
    council_stage_latency: dict[str, dict[str, Any]]
    council_actor_latency_window: dict[str, dict[str, Any]]
    council_stage_latency_window: dict[str, dict[str, Any]]
    council_e2e_latency: dict[str, dict[str, Any]]
    council_e2e_latency_window: dict[str, dict[str, Any]]
    llm_latency: dict[str, Any]
    llm_tokens: dict[str, Any]
    limiters: list[dict[str, Any]]
    telemetry_throughput: dict[str, Any]
    timing_model: str
    stats_source: str
    window_seconds: float
    asof_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "inflight": self.inflight,
            "completed_in_window": self.completed_in_window,
            "council_completed_in_window": self.council_completed_in_window,
            "throughput_per_sec": self.throughput_per_sec,
            "throughput_per_min": self.throughput_per_min,
            "council_throughput_per_sec": self.council_throughput_per_sec,
            "council_throughput_per_min": self.council_throughput_per_min,
            "latency_ms": self.latency_ms.to_dict(),
            "error_count": self.error_count,
            "error_rate": self.error_rate,
            "by_source": dict(self.by_source),
            "by_tool": dict(self.by_tool),
            "by_agent": dict(self.by_agent),
            "by_agent_tool": {
                agent_id: dict(counts) for agent_id, counts in self.by_agent_tool.items()
            },
            "completed_total": self.completed_total,
            "error_total": self.error_total,
            "by_source_total": dict(self.by_source_total),
            "by_tool_total": dict(self.by_tool_total),
            "by_agent_total": dict(self.by_agent_total),
            "by_agent_tool_total": {
                agent_id: dict(counts) for agent_id, counts in self.by_agent_tool_total.items()
            },
            "agent_activity": {
                agent_id: dict(row) for agent_id, row in self.agent_activity.items()
            },
            "decisions_in_window": self.decisions_in_window,
            "decisions_total": self.decisions_total,
            "by_decision_outcome": dict(self.by_decision_outcome),
            "by_decision_outcome_total": dict(self.by_decision_outcome_total),
            "by_risk": dict(self.by_risk),
            "by_risk_total": dict(self.by_risk_total),
            "by_error_class": dict(self.by_error_class),
            "by_error_class_total": dict(self.by_error_class_total),
            "by_missing_permission": dict(self.by_missing_permission),
            "by_missing_permission_total": dict(self.by_missing_permission_total),
            "by_code_review": dict(self.by_code_review),
            "by_code_review_total": dict(self.by_code_review_total),
            "council_actor_latency": dict(self.council_actor_latency),
            "council_stage_latency": dict(self.council_stage_latency),
            "council_actor_latency_window": dict(self.council_actor_latency_window),
            "council_stage_latency_window": dict(self.council_stage_latency_window),
            "council_e2e_latency": dict(self.council_e2e_latency),
            "council_e2e_latency_window": dict(self.council_e2e_latency_window),
            "llm_latency": dict(self.llm_latency),
            "llm_tokens": dict(self.llm_tokens),
            "limiters": list(self.limiters),
            "telemetry_throughput": dict(self.telemetry_throughput),
            "timing_model": self.timing_model,
            "stats_source": self.stats_source,
            "window_seconds": self.window_seconds,
            "asof_ms": self.asof_ms,
        }


@dataclass(frozen=True)
class RecentEntry:
    """Live-table row: an open inflight or a completed request."""

    state: str  # "inflight" or "completed"
    method: str
    tool: str
    source: str
    duration_ms: float | None
    age_ms: float
    status_code: int | None
    ok: bool | None
    request: RequestSummary | None = None
    decision: DecisionSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "method": self.method,
            "tool": self.tool,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "age_ms": self.age_ms,
            "status_code": self.status_code,
            "ok": self.ok,
            "request": self.request.to_dict() if self.request else None,
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclass
class _OpenRequest:
    handle: int
    start_ts: float
    method: str
    tool: str
    source: str
    request: RequestSummary | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class MetricsRegistry:
    """Thread-safe-by-asyncio metrics store for the monitor."""

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        stats_source: str = "inline",
    ):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if history_limit <= 0:
            raise ValueError("history_limit must be positive")
        self._window_seconds = window_seconds
        self._stats_source = stats_source
        self._history: deque[Completion] = deque(maxlen=history_limit)
        self._inflight: dict[int, _OpenRequest] = {}
        self._completed_total = 0
        self._error_total = 0
        self._decisions_total = 0
        self._by_source_total: Counter[str] = Counter()
        self._by_tool_total: Counter[str] = Counter()
        self._by_agent_total: Counter[str] = Counter()
        self._by_agent_error_total: Counter[str] = Counter()
        self._by_agent_tool_total: dict[str, Counter[str]] = {}
        self._agent_last_seen_ts: dict[str, float] = {}
        self._by_decision_outcome_total: Counter[str] = Counter()
        self._by_risk_total: Counter[str] = Counter()
        self._by_error_class_total: Counter[str] = Counter()
        self._by_missing_permission_total: Counter[str] = Counter()
        self._by_code_review_total: Counter[str] = Counter()
        # Council latency tables aggregate the retained samples. The dashboard
        # exposes separate rolling-window views for trend charts.
        latency_limit = history_limit * LATENCY_SAMPLES_PER_REQUEST
        self._actor_latency_history: deque[ActorLatencySample] = deque(maxlen=latency_limit)
        self._stage_latency_history: deque[StageLatencySample] = deque(maxlen=latency_limit)
        self._timing_model = ""
        self._limiter_gauges: list[dict[str, Any]] = []
        self._limiter_gauge_ts: float = 0.0
        self._limiter_history: deque[tuple[float, list[dict[str, Any]]]] = deque(maxlen=600)
        self._next_handle = 1
        self._lock = asyncio.Lock()

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    async def start(
        self,
        *,
        now: float | None = None,
        method: str,
        tool: str,
        source: str,
        request: RequestSummary | None = None,
    ) -> int:
        """Mark a new inflight request. Returns a handle to pass to `complete`."""
        ts = time.time() if now is None else now
        async with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            self._inflight[handle] = _OpenRequest(
                handle=handle,
                start_ts=ts,
                method=method,
                tool=tool,
                source=source,
                request=request,
            )
            return handle

    async def complete(
        self,
        handle: int,
        *,
        now: float | None = None,
        status_code: int,
        ok: bool,
        decision: DecisionSummary | None = None,
    ) -> None:
        """Record completion of a previously-started inflight request."""
        ts = time.time() if now is None else now
        async with self._lock:
            open_req = self._inflight.pop(handle, None)
            if open_req is None:
                # Unknown handle — never raise from the metrics path; just drop.
                return
            self._record_unlocked(
                Completion(
                    start_ts=open_req.start_ts,
                    end_ts=ts,
                    duration_ms=max(0.0, (ts - open_req.start_ts) * 1000.0),
                    method=open_req.method,
                    tool=open_req.tool,
                    source=open_req.source,
                    status_code=status_code,
                    ok=ok,
                    request=open_req.request,
                    decision=decision,
                )
            )

    async def record_council_latency(self, telemetry: dict[str, Any]) -> None:
        """Record monitor-internal council stage/actor timing telemetry."""
        async with self._lock:
            self._record_latency_samples_unlocked(telemetry, time.time())

    def _record_latency_samples_unlocked(self, telemetry: dict[str, Any], ts: float) -> None:
        """Record stage/actor/limiter samples from a telemetry record at time `ts`.

        Caller must hold the lock. `ts` is injectable so the Redis stats reader can
        replay historical stream entries with their real timestamps.
        """
        tool = _clean_label(telemetry.get("tool"))
        stages = telemetry.get("stages", {})
        actors = telemetry.get("actors", [])
        if isinstance(stages, dict):
            for stage, duration in stages.items():
                duration_ms = _float_or_none(duration)
                if duration_ms is None:
                    continue
                self._stage_latency_history.append(
                    StageLatencySample(
                        ts=ts,
                        tool=tool,
                        stage=_clean_label(stage),
                        duration_ms=duration_ms,
                    )
                )
        if isinstance(actors, list):
            for actor in actors:
                if not isinstance(actor, dict):
                    continue
                duration_ms = _float_or_none(actor.get("duration_ms"))
                if duration_ms is None:
                    continue
                self._actor_latency_history.append(
                    ActorLatencySample(
                        ts=ts,
                        tool=tool,
                        role=_clean_label(actor.get("role")),
                        member_id=_clean_label(actor.get("member_id")),
                        provider=_clean_label(actor.get("provider")),
                        duration_ms=duration_ms,
                        decision=_clean_label(actor.get("decision")),
                        inference_ms=_float_or_none(actor.get("inference_ms")),
                        queue_wait_ms=_float_or_none(actor.get("queue_wait_ms")),
                        attempts=_float_or_none(actor.get("attempts")),
                        input_tokens=_float_or_none(actor.get("input_tokens")),
                        output_tokens=_float_or_none(actor.get("output_tokens")),
                    )
                )
        # Only a record that reached member review says how review was scheduled;
        # early exits (auth/OPA denials) must not reset it.
        if isinstance(stages, dict) and "review_collect" in stages:
            self._timing_model = _clean_label(telemetry.get("timing_model"))
        limiters = telemetry.get("limiters")
        if isinstance(limiters, list):
            cleaned = [g for g in limiters if isinstance(g, dict)]
            if cleaned:
                self._limiter_gauges = cleaned
                self._limiter_gauge_ts = ts
                self._limiter_history.append((ts, cleaned))

    async def ingest_council_record(self, record: dict[str, Any], *, entry_ts: float) -> None:
        """Replay one council telemetry stream record into the registry.

        The Redis stats reader replays the windowed stream into a FRESH registry
        on each request (stateless → identical across monitor replicas). Builds a
        completion (throughput / by-tool / by-outcome / latency) plus the
        stage/actor/limiter samples. `entry_ts` is the stream entry's timestamp.

        MVP fidelity: the council telemetry payload carries tool/outcome/stages/
        actors/limiters but not the request's agent_id or the full vote/risk facts
        (those were parsed inline by the proxy). So by-agent and by-risk stay empty
        here; outcome/throughput/latency are exact. See proposal 13.
        """
        tool = _clean_label(record.get("tool"))
        outcome = _clean_label(record.get("outcome"))
        stages = record.get("stages")
        total_ms = _float_or_none(stages.get("total")) if isinstance(stages, dict) else None
        duration_ms = total_ms if total_ms is not None else 0.0
        ok = outcome != "error"
        decision = DecisionSummary(
            outcome=outcome,
            error_class="",
            risk_level="",
            threshold="",
            approval_votes=0,
            denial_votes=0,
            abstain_votes=0,
            debate_rounds=0,
            changed_votes=0,
            code_review="",
        )
        async with self._lock:
            self._record_unlocked(
                Completion(
                    start_ts=entry_ts - duration_ms / 1000.0,
                    end_ts=entry_ts,
                    duration_ms=duration_ms,
                    method="tools/call",
                    tool=tool,
                    source="council",
                    status_code=200 if ok else 500,
                    ok=ok,
                    request=None,
                    decision=decision,
                )
            )
            self._record_latency_samples_unlocked(record, entry_ts)

    async def abort(self, handle: int) -> None:
        """Drop an inflight handle without recording a completion.

        Used when the proxy never produced a recordable event (e.g. shutdown
        midway). Avoids leaking inflight count.
        """
        async with self._lock:
            self._inflight.pop(handle, None)

    async def snapshot(self, *, now: float | None = None) -> Snapshot:
        ts = time.time() if now is None else now
        async with self._lock:
            in_window = [c for c in self._history if c.end_ts >= ts - self._window_seconds]
            durations = [c.duration_ms for c in in_window]
            latency = _percentiles(durations)
            errors = sum(1 for c in in_window if not c.ok)
            count = len(in_window)
            council_count = sum(1 for c in in_window if c.tool in COUNCIL_TOOLS)
            by_source: dict[str, int] = {}
            by_tool: dict[str, int] = {}
            by_agent: Counter[str] = Counter()
            by_agent_error: Counter[str] = Counter()
            by_agent_tool: dict[str, Counter[str]] = {}
            decision_count = 0
            by_decision_outcome: Counter[str] = Counter()
            by_risk: Counter[str] = Counter()
            by_error_class: Counter[str] = Counter()
            by_missing_permission: Counter[str] = Counter()
            by_code_review: Counter[str] = Counter()
            for c in in_window:
                by_source[c.source] = by_source.get(c.source, 0) + 1
                if c.method == "tools/call" and c.tool:
                    by_tool[c.tool] = by_tool.get(c.tool, 0) + 1
                agent_id = _completion_agent_id(c)
                if agent_id:
                    by_agent[agent_id] += 1
                    if not c.ok:
                        by_agent_error[agent_id] += 1
                    _agent_tool_counter(by_agent_tool, agent_id)[_completion_call_type(c)] += 1
                if c.decision is not None:
                    decision_count += 1
                    _count_decision(
                        c.decision,
                        by_decision_outcome=by_decision_outcome,
                        by_risk=by_risk,
                        by_error_class=by_error_class,
                        by_missing_permission=by_missing_permission,
                        by_code_review=by_code_review,
                    )
            return Snapshot(
                inflight=len(self._inflight),
                completed_in_window=count,
                council_completed_in_window=council_count,
                throughput_per_sec=count / self._window_seconds,
                throughput_per_min=count * (60.0 / self._window_seconds),
                council_throughput_per_sec=council_count / self._window_seconds,
                council_throughput_per_min=council_count * (60.0 / self._window_seconds),
                latency_ms=latency,
                error_count=errors,
                error_rate=(errors / count) if count else 0.0,
                by_source=by_source,
                by_tool=by_tool,
                by_agent=dict(by_agent),
                by_agent_tool=_counter_map_to_dict(by_agent_tool),
                completed_total=self._completed_total,
                error_total=self._error_total,
                by_source_total=dict(self._by_source_total),
                by_tool_total=dict(self._by_tool_total),
                by_agent_total=dict(self._by_agent_total),
                by_agent_tool_total=_counter_map_to_dict(self._by_agent_tool_total),
                agent_activity=_agent_activity_snapshot(
                    by_agent=by_agent,
                    by_agent_error=by_agent_error,
                    by_agent_tool=by_agent_tool,
                    by_agent_total=self._by_agent_total,
                    by_agent_error_total=self._by_agent_error_total,
                    by_agent_tool_total=self._by_agent_tool_total,
                    last_seen_ts=self._agent_last_seen_ts,
                ),
                decisions_in_window=decision_count,
                decisions_total=self._decisions_total,
                by_decision_outcome=dict(by_decision_outcome),
                by_decision_outcome_total=dict(self._by_decision_outcome_total),
                by_risk=dict(by_risk),
                by_risk_total=dict(self._by_risk_total),
                by_error_class=dict(by_error_class),
                by_error_class_total=dict(self._by_error_class_total),
                by_missing_permission=dict(by_missing_permission),
                by_missing_permission_total=dict(self._by_missing_permission_total),
                by_code_review=dict(by_code_review),
                by_code_review_total=dict(self._by_code_review_total),
                council_actor_latency=_actor_latency_snapshot(list(self._actor_latency_history)),
                council_stage_latency=_stage_latency_snapshot(list(self._stage_latency_history)),
                council_actor_latency_window=_actor_latency_snapshot(
                    _windowed_samples(self._actor_latency_history, ts, self._window_seconds)
                ),
                council_stage_latency_window=_stage_latency_snapshot(
                    _windowed_samples(self._stage_latency_history, ts, self._window_seconds)
                ),
                council_e2e_latency=_e2e_latency_snapshot(
                    list(self._stage_latency_history), window_seconds=None
                ),
                council_e2e_latency_window=_e2e_latency_snapshot(
                    _windowed_samples(self._stage_latency_history, ts, self._window_seconds),
                    window_seconds=self._window_seconds,
                ),
                llm_latency=_llm_rollup(
                    _windowed_samples(self._actor_latency_history, ts, self._window_seconds),
                    window_seconds=self._window_seconds,
                ),
                llm_tokens={
                    "window": _token_rollup(
                        _windowed_samples(self._actor_latency_history, ts, self._window_seconds),
                        window_seconds=self._window_seconds,
                    ),
                    "retained": _token_rollup(
                        list(self._actor_latency_history), window_seconds=None
                    ),
                },
                limiters=[
                    {**g, "asof_ms": int(self._limiter_gauge_ts * 1000)}
                    for g in self._limiter_gauges
                ],
                telemetry_throughput=_telemetry_throughput(
                    _windowed_samples(self._stage_latency_history, ts, self._window_seconds),
                    window_seconds=self._window_seconds,
                ),
                timing_model=self._timing_model,
                stats_source=self._stats_source,
                window_seconds=self._window_seconds,
                asof_ms=int(ts * 1000.0),
            )

    async def recent(self, *, now: float | None = None, limit: int = 50) -> list[RecentEntry]:
        """Inflight first (most recently started), then most-recent completions."""
        ts = time.time() if now is None else now
        async with self._lock:
            inflight_rows = [
                RecentEntry(
                    state="inflight",
                    method=req.method,
                    tool=req.tool,
                    source=req.source,
                    request=req.request,
                    duration_ms=None,
                    age_ms=max(0.0, (ts - req.start_ts) * 1000.0),
                    status_code=None,
                    ok=None,
                    decision=None,
                )
                for req in sorted(self._inflight.values(), key=lambda r: r.start_ts, reverse=True)
            ]
            completed_rows = [
                RecentEntry(
                    state="completed",
                    method=c.method,
                    tool=c.tool,
                    source=c.source,
                    request=c.request or (c.decision.request if c.decision else None),
                    duration_ms=c.duration_ms,
                    age_ms=max(0.0, (ts - c.end_ts) * 1000.0),
                    status_code=c.status_code,
                    ok=c.ok,
                    decision=c.decision,
                )
                for c in reversed(self._history)
            ]
        out = inflight_rows + completed_rows
        return out[:limit]

    # ── test helper (kept private, but used by the unit tests) ──────────
    def _record(self, completion: Completion) -> None:
        """Insert a synthetic completion. Test-only fast path; bypasses the lock.

        Safe because tests exercise it serially; production code goes through
        start/complete which always takes the lock.
        """
        self._record_unlocked(completion)

    def _record_unlocked(self, completion: Completion) -> None:
        self._history.append(completion)
        self._completed_total += 1
        if not completion.ok:
            self._error_total += 1
        self._by_source_total[completion.source] += 1
        if completion.method == "tools/call" and completion.tool:
            self._by_tool_total[completion.tool] += 1
        agent_id = _completion_agent_id(completion)
        if agent_id:
            self._by_agent_total[agent_id] += 1
            if not completion.ok:
                self._by_agent_error_total[agent_id] += 1
            _agent_tool_counter(self._by_agent_tool_total, agent_id)[
                _completion_call_type(completion)
            ] += 1
            self._agent_last_seen_ts[agent_id] = max(
                completion.end_ts,
                self._agent_last_seen_ts.get(agent_id, 0.0),
            )
        if completion.decision is not None:
            self._decisions_total += 1
            _count_decision(
                completion.decision,
                by_decision_outcome=self._by_decision_outcome_total,
                by_risk=self._by_risk_total,
                by_error_class=self._by_error_class_total,
                by_missing_permission=self._by_missing_permission_total,
                by_code_review=self._by_code_review_total,
            )


def _count_decision(
    decision: DecisionSummary,
    *,
    by_decision_outcome: Counter[str],
    by_risk: Counter[str],
    by_error_class: Counter[str],
    by_missing_permission: Counter[str],
    by_code_review: Counter[str],
) -> None:
    by_decision_outcome[decision.outcome or "unknown"] += 1
    if decision.risk_level:
        by_risk[decision.risk_level] += 1
    if decision.error_class:
        by_error_class[decision.error_class] += 1
    if decision.code_review:
        by_code_review[decision.code_review] += 1
    for permission in decision.missing_permissions:
        by_missing_permission[permission] += 1


def _completion_agent_id(completion: Completion) -> str:
    request = completion.request or (completion.decision.request if completion.decision else None)
    if request is None:
        return ""
    return _clean_label(request.agent_id)


def _completion_call_type(completion: Completion) -> str:
    if completion.method == "tools/call" and completion.tool:
        return _clean_label(completion.tool)
    return _clean_label(completion.method) or "unknown"


def _agent_tool_counter(counters: dict[str, Counter[str]], agent_id: str) -> Counter[str]:
    counter = counters.get(agent_id)
    if counter is None:
        counter = Counter()
        counters[agent_id] = counter
    return counter


def _counter_map_to_dict(counters: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {key: dict(counter) for key, counter in counters.items()}


def _agent_activity_snapshot(
    *,
    by_agent: Counter[str],
    by_agent_error: Counter[str],
    by_agent_tool: dict[str, Counter[str]],
    by_agent_total: Counter[str],
    by_agent_error_total: Counter[str],
    by_agent_tool_total: dict[str, Counter[str]],
    last_seen_ts: dict[str, float],
) -> dict[str, dict[str, Any]]:
    agents = set(by_agent_total) | set(by_agent)
    rows: dict[str, dict[str, Any]] = {}
    for agent_id in sorted(agents):
        rows[agent_id] = {
            "agent_id": agent_id,
            "completed_in_window": by_agent.get(agent_id, 0),
            "completed_total": by_agent_total.get(agent_id, 0),
            "error_in_window": by_agent_error.get(agent_id, 0),
            "error_total": by_agent_error_total.get(agent_id, 0),
            "by_tool": dict(by_agent_tool.get(agent_id, Counter())),
            "by_tool_total": dict(by_agent_tool_total.get(agent_id, Counter())),
            "last_seen_ms": int(last_seen_ts[agent_id] * 1000.0)
            if agent_id in last_seen_ts
            else None,
        }
    return rows


def _percentiles(values: list[float]) -> LatencyPercentiles:
    """Nearest-rank percentiles. Returns zeros if there are no samples.

    Nearest-rank is intentional: an ops dashboard wants percentiles that map
    to actual observed durations, not interpolated values. With small N this
    matters; with large N the difference is noise.
    """
    if not values:
        return LatencyPercentiles(0.0, 0.0, 0.0)
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def at(p: float) -> float:
        # nearest-rank: ceil(p/100 * n) - 1, clamped to [0, n-1]
        idx = max(0, min(n - 1, int(-(-p * n // 100)) - 1))
        return sorted_vals[idx]

    return LatencyPercentiles(p50=at(50.0), p95=at(95.0), p99=at(99.0))


def _stats(values: list[float]) -> LatencyStats:
    if not values:
        return LatencyStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    percentiles = _percentiles(values)
    return LatencyStats(
        count=len(values),
        avg_ms=sum(values) / len(values),
        p50_ms=percentiles.p50,
        p95_ms=percentiles.p95,
        p99_ms=percentiles.p99,
        max_ms=max(values),
        last_ms=values[-1],
    )


def _actor_latency_snapshot(samples: list[ActorLatencySample]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ActorLatencySample]] = {}
    for sample in samples:
        key = f"{sample.role}:{sample.member_id}"
        grouped.setdefault(key, []).append(sample)

    out: dict[str, dict[str, Any]] = {}
    for key, group in grouped.items():
        latest = group[-1]
        stats = _stats([sample.duration_ms for sample in group]).to_dict()
        out[key] = {
            **stats,
            "role": latest.role,
            "member_id": latest.member_id,
            "provider": latest.provider,
            "tool": latest.tool,
            "last_decision": latest.decision,
        }
    return out


def _stage_latency_snapshot(samples: list[StageLatencySample]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[StageLatencySample]] = {}
    for sample in samples:
        grouped.setdefault(sample.stage, []).append(sample)

    out: dict[str, dict[str, Any]] = {}
    for stage, group in grouped.items():
        latest = group[-1]
        stats = _stats([sample.duration_ms for sample in group]).to_dict()
        out[stage] = {
            **stats,
            "stage": stage,
            "tool": latest.tool,
        }
    return out


def _e2e_latency_snapshot(
    samples: list[StageLatencySample], *, window_seconds: float | None
) -> dict[str, dict[str, Any]]:
    """Per-tool end-to-end request latency from the council 'total' stage."""
    grouped: dict[str, list[StageLatencySample]] = {}
    for sample in samples:
        if sample.stage == "total" and sample.tool:
            grouped.setdefault(sample.tool, []).append(sample)
    out: dict[str, dict[str, Any]] = {}
    for tool, group in grouped.items():
        stats = _stats([sample.duration_ms for sample in group]).to_dict()
        entry = {**stats, "tool": tool}
        if window_seconds:
            entry["per_sec"] = round(len(group) / window_seconds, 3)
            entry["per_min"] = round(len(group) * (60.0 / window_seconds), 2)
        out[tool] = entry
    return out


def _llm_rollup(samples: list[ActorLatencySample], *, window_seconds: float) -> dict[str, Any]:
    """At-a-glance inference vs queue-wait rollup across all LLM actors."""
    inference = [s.inference_ms for s in samples if s.inference_ms is not None]
    queue_wait = [s.queue_wait_ms for s in samples if s.queue_wait_ms is not None]
    attempts = [s.attempts for s in samples if s.attempts is not None]
    durations = [s.duration_ms for s in samples]
    out: dict[str, Any] = {
        "calls_in_window": len(samples),
        "calls_per_sec": round(len(samples) / window_seconds, 3) if window_seconds else 0.0,
        "retries_in_window": int(sum(a - 1 for a in attempts)) if attempts else 0,
    }
    for name, values in (
        ("inference_ms", inference),
        ("queue_wait_ms", queue_wait),
        ("call_ms", durations),
    ):
        if values:
            pct = _percentiles(values)
            out[name] = {
                "p50": pct.p50,
                "p95": pct.p95,
                "p99": pct.p99,
                "max": round(max(values), 1),
                "avg": round(sum(values) / len(values), 1),
            }
        else:
            out[name] = {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
    return out


def _token_rollup(
    samples: list[ActorLatencySample], *, window_seconds: float | None
) -> dict[str, Any]:
    """LLM token usage, overall and per council member.

    Debate rounds report under the same `member_id` as the member's first vote,
    so a member's row is everything that member spent. Samples from a council
    that predates token telemetry carry no counts and are left out of `calls`;
    `calls` against the LLM call count is the coverage.
    """
    members: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if sample.input_tokens is None and sample.output_tokens is None:
            continue
        key = sample.member_id or sample.role
        row = members.get(key)
        if row is None:
            row = {"member_id": key, "calls": 0, "input": 0, "output": 0}
            members[key] = row
        row["calls"] += 1
        row["input"] += int(sample.input_tokens or 0)
        row["output"] += int(sample.output_tokens or 0)
        if not sample.role.startswith("council_member_debate_"):
            row["role"] = sample.role
        row["provider"] = sample.provider

    total = sum(row["input"] + row["output"] for row in members.values())
    by_member = []
    for row in members.values():
        row_total = row["input"] + row["output"]
        by_member.append(
            {
                **row,
                "role": row.get("role", "council_member"),
                "total": row_total,
                "avg_per_call": round(row_total / row["calls"], 1),
                "share": round(row_total / total, 4) if total else 0.0,
            }
        )
    by_member.sort(key=lambda row: (-row["total"], row["member_id"]))

    out: dict[str, Any] = {
        "calls": sum(row["calls"] for row in by_member),
        "input": sum(row["input"] for row in by_member),
        "output": sum(row["output"] for row in by_member),
        "total": total,
        "by_member": by_member,
    }
    if window_seconds:
        out["per_sec"] = round(total / window_seconds, 1)
    return out


def _telemetry_throughput(
    samples: list[StageLatencySample], *, window_seconds: float
) -> dict[str, Any]:
    """True request throughput from council telemetry (covers non-proxy sources)."""
    totals = [s for s in samples if s.stage == "total"]
    by_tool: dict[str, int] = {}
    for sample in totals:
        by_tool[sample.tool] = by_tool.get(sample.tool, 0) + 1
    return {
        "requests_in_window": len(totals),
        "per_sec": round(len(totals) / window_seconds, 3) if window_seconds else 0.0,
        "per_min": round(len(totals) * (60.0 / window_seconds), 2) if window_seconds else 0.0,
        "by_tool": by_tool,
    }


def _windowed_samples(samples: deque[Any], now: float, window_seconds: float) -> list[Any]:
    cutoff = now - window_seconds
    out: list[Any] = []
    for sample in reversed(samples):
        if sample.ts < cutoff:
            break
        out.append(sample)
    out.reverse()
    return out


def _float_or_none(value: Any) -> float | None:
    # Telemetry arrives as JSON, which Python parses "Infinity" from. A non-finite
    # value would make the token rollup's int() raise and fail every snapshot.
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric >= 0 else None
    return None


def _clean_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:96]
