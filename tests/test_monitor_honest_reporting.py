# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The monitor must not report what it has not checked or was not told.

Each test here pins one claim: a ledger is "valid" only after every entry was
read, a failed source is not an idle one, a value the source never carried is
unknown rather than zero, and both stats modes count an error the same way.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from monitor.server import ledger_api
from monitor.server.ledger import _hash_entry, _redact, load_ledger_page
from monitor.server.ledger_api import load_ledger_page_from_api
from monitor.server.main import _latency_targets, _probe_upstream, build_app
from monitor.server.metrics import Completion, DecisionSummary, MetricsRegistry, RequestSummary
from monitor.server.proxy import _classify_error
from monitor.server.redis_stats import RedisStatsReader

NOW = 1_000_000.0


# ── helpers ──────────────────────────────────────────────────────────────────
def _decision(outcome: str, **fields: Any) -> DecisionSummary:
    base: dict[str, Any] = {
        "error_class": "",
        "risk_level": "",
        "threshold": "",
        "approval_votes": 0,
        "denial_votes": 0,
        "abstain_votes": 0,
        "debate_rounds": 0,
        "changed_votes": 0,
        "code_review": "",
    }
    return DecisionSummary(outcome=outcome, **{**base, **fields})


def _proxied(status_code: int, outcome: str | None, *, council_id: str = "") -> Completion:
    return Completion(
        start_ts=NOW - 1,
        end_ts=NOW,
        duration_ms=1000.0,
        method="tools/call",
        tool="council_authorize",
        source="direct",
        status_code=status_code,
        ok=status_code < 500,
        decision=_decision(outcome, council_id=council_id) if outcome else None,
    )


def _record(outcome: str = "approved", **fields: Any) -> dict[str, Any]:
    """A telemetry record as a current council publishes it."""
    record: dict[str, Any] = {
        "tool": "council_authorize",
        "council_id": "council-prod",
        "intent_id": "intent-1",
        "outcome": outcome,
        "error_class": "",
        "agent_id": "agent-1",
        "agent_authenticated": True,
        "stages": {"auth": 4.0, "total": 900.0},
        "actors": [],
        "limiters": [],
        "decision": {
            "risk_level": "high",
            "threshold": "unanimous",
            "approval_votes": 2,
            "denial_votes": 1,
            "abstain_votes": 0,
            "changed_votes": 0,
            "debate_rounds": 0,
            "code_review": "approve",
            "missing_permissions": [],
            "guidance_count": 0,
            "writ_id": "",
        },
    }
    return {**record, **fields}


def _chain(count: int, *, signed: bool = True) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous = ""
    for sequence in range(count):
        entry: dict[str, Any] = {
            "sequence": sequence,
            "entry_type": "genesis" if sequence == 0 else "decision_made",
            "payload": {"chain_id": "chain-a"} if sequence == 0 else {"intent_id": f"i-{sequence}"},
            "timestamp": f"2026-11-01T00:00:{sequence:02d}+00:00",
            "previous_hash": previous,
            "signatures": [{"signer_id": "council", "signature": "00"}] if signed else [],
            "metadata": {},
        }
        entry["content_hash"] = _hash_entry(entry)
        previous = entry["content_hash"]
        entries.append(entry)
    return entries


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def _council_api(entries: list[dict[str, Any]], calls: list[dict[str, Any]]) -> httpx.AsyncClient:
    """A council that serves `entries` through council_ledger_page."""
    by_sequence = {entry["sequence"]: entry for entry in entries}
    head = max(by_sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        args = json.loads(request.content)
        calls.append(args)
        cursor = head if args.get("cursor") is None else min(args["cursor"], head)
        page = [by_sequence[s] for s in range(cursor, -1, -1) if s in by_sequence][: args["limit"]]
        oldest = page[-1]["sequence"] if page else 0
        return httpx.Response(
            200,
            json={
                "chain_id": "chain-a",
                "head_sequence": head,
                "entries": page,
                "next_cursor": oldest - 1 if oldest > 0 else None,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── errors mean the same thing in both modes ─────────────────────────────────
@pytest.mark.asyncio
async def test_a_council_error_reply_counts_as_an_error_though_http_said_200():
    registry = MetricsRegistry()
    registry._record(_proxied(200, "approved"))
    registry._record(_proxied(200, "error"))
    registry._record(_proxied(200, "denied"))
    registry._record(_proxied(502, None))

    snap = (await registry.snapshot(now=NOW)).to_dict()

    assert snap["error_count"] == 2
    assert snap["council_error_count"] == 1
    assert snap["transport_error_count"] == 1
    assert snap["error_total"] == 2
    assert [row.ok for row in await registry.recent(now=NOW)] == [False, True, False, True]


@pytest.mark.asyncio
async def test_telemetry_counts_council_errors_and_says_transport_was_not_observed():
    registry = MetricsRegistry(stats_source="redis")
    await registry.ingest_council_record(_record("approved"), entry_ts=NOW)
    await registry.ingest_council_record(_record("error"), entry_ts=NOW)

    snap = (await registry.snapshot(now=NOW)).to_dict()

    assert (snap["error_count"], snap["council_error_count"]) == (1, 1)
    assert snap["transport_error_count"] is None
    assert {row.status_code for row in await registry.recent(now=NOW)} == {None}


# ── telemetry: recorded facts, and facts that were never sent ────────────────
@pytest.mark.asyncio
async def test_a_record_with_the_reply_facts_fills_the_agent_and_decision_panels():
    registry = MetricsRegistry(stats_source="redis")
    await registry.ingest_council_record(
        _record(
            "denied",
            error_class="permission",
            decision={**_record()["decision"], "missing_permissions": ["infra:destroy"]},
        ),
        entry_ts=NOW,
    )

    snap = (await registry.snapshot(now=NOW)).to_dict()
    row = (await registry.recent(now=NOW))[0].to_dict()

    assert snap["by_agent"] == {"agent-1": 1}
    assert snap["by_risk"] == {"high": 1}
    assert snap["by_error_class"] == {"permission": 1}
    assert snap["by_missing_permission"] == {"infra:destroy": 1}
    assert snap["by_code_review"] == {"approve": 1}
    assert snap["by_council"] == {"council-prod": 1}
    assert snap["decisions_without_facts_total"] == 0
    assert (row["decision"]["approval_votes"], row["decision"]["denial_votes"]) == (2, 1)
    assert row["decision"]["decision_id"] == "intent-1"
    assert row["request"]["agent_authenticated"] is True


@pytest.mark.asyncio
async def test_a_record_from_an_older_council_is_marked_as_missing_its_facts():
    legacy = {k: v for k, v in _record().items() if k not in {"decision", "agent_id"}}
    registry = MetricsRegistry(stats_source="redis")
    await registry.ingest_council_record(legacy, entry_ts=NOW)

    snap = (await registry.snapshot(now=NOW)).to_dict()
    row = (await registry.recent(now=NOW))[0].to_dict()

    assert snap["decisions_without_facts_total"] == 1
    assert row["decision"]["facts_recorded"] is False
    assert row["decision"]["outcome"] == "approved"


@pytest.mark.asyncio
async def test_a_request_refused_before_an_intent_is_counted_but_keeps_out_of_latency():
    registry = MetricsRegistry(stats_source="redis")
    await registry.ingest_council_record(_record("approved"), entry_ts=NOW)
    await registry.ingest_council_record(
        _record(
            "error",
            intent_id="",
            error_class="auth",
            agent_authenticated=False,
            stages={"auth": 1.0, "total": 1.0},
            limiters=[{"provider": "nim", "waiting": 3, "max_concurrent": 4}],
        ),
        entry_ts=NOW,
    )

    snap = (await registry.snapshot(now=NOW)).to_dict()

    assert snap["completed_in_window"] == 2
    assert snap["by_error_class"] == {"auth": 1}
    # One reviewed request: the 1 ms refusal is not a second sample of council latency.
    assert snap["council_stage_latency"]["total"]["count"] == 1
    assert snap["telemetry_throughput"]["requests_in_window"] == 1
    assert snap["limiters"][0]["waiting"] == 3


@pytest.mark.asyncio
async def test_actor_rows_keep_the_model_that_answered_and_the_attempts_that_failed():
    actor = {
        "role": "council_member",
        "member_id": "m1",
        "provider": "openai",
        "duration_ms": 9000.0,
        "backend": "fallback",
        "configured_model": "model-a",
        "returned_model": "model-b",
        "status": "ok",
        "llm_attempts": [{"outcome": "timeout"}, {"outcome": "success"}],
    }
    registry = MetricsRegistry()
    await registry.record_council_latency(_record(actors=[actor]))

    row = (await registry.snapshot()).to_dict()["council_actor_latency"]["council_member:m1"]

    assert (row["backend"], row["configured_model"], row["returned_model"]) == (
        "fallback",
        "model-a",
        "model-b",
    )
    assert row["attempt_outcomes"] == {"timeout": 1, "success": 1}


# ── councils ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_one_council_can_be_viewed_apart_from_the_others():
    registry = MetricsRegistry(stats_source="redis")
    gauge = {"provider": "nim", "waiting": 1, "max_concurrent": 4}
    await registry.ingest_council_record(_record(limiters=[gauge]), entry_ts=NOW)
    await registry.ingest_council_record(
        _record("denied", council_id="council-staging", limiters=[{**gauge, "waiting": 9}]),
        entry_ts=NOW,
    )

    everything = (await registry.snapshot(now=NOW)).to_dict()
    staging = (await (await registry.for_council("council-staging")).snapshot(now=NOW)).to_dict()

    assert everything["by_council_total"] == {"council-prod": 1, "council-staging": 1}
    # Each council's limiter gauge is kept; the newest record no longer replaces the rest.
    assert {(g["council_id"], g["waiting"]) for g in everything["limiters"]} == {
        ("council-prod", 1),
        ("council-staging", 9),
    }
    assert staging["council_id"] == "council-staging"
    assert staging["completed_in_window"] == 1
    assert staging["by_decision_outcome"] == {"denied": 1}
    assert staging["council_stage_latency"]["total"]["count"] == 1
    assert [g["waiting"] for g in staging["limiters"]] == [9]


# ── freshness ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_newest_record_time_is_the_records_not_the_clocks():
    registry = MetricsRegistry()
    assert (await registry.snapshot(now=NOW)).last_record_ms is None

    registry._record(_proxied(200, "approved"))
    later = await registry.snapshot(now=NOW + 600)

    assert later.last_record_ms == int(NOW * 1000)
    assert later.asof_ms == int((NOW + 600) * 1000)


@pytest.mark.asyncio
async def test_history_replays_retained_samples_so_a_reload_does_not_start_empty():
    registry = MetricsRegistry(window_seconds=60.0)
    registry._record(_proxied(200, "approved"))
    registry._record_latency_samples_unlocked(_record(), NOW)

    points = (await registry.history(now=NOW + 30))["points"]

    assert points[0]["ts_ms"] < points[-1]["ts_ms"] == int((NOW + 30) * 1000)
    assert points[0]["council_throughput_per_min"] == 0
    assert points[-1]["council_throughput_per_min"] == 1.0
    assert points[-1]["p95_ms"] == 1000.0
    assert points[-1]["stages"]["total"]["p95_ms"] == 900.0


# ── the telemetry source failing is not the council being idle ───────────────
class _Stream:
    def __init__(self) -> None:
        self.fail = False
        self.entries: list[tuple[str, dict[str, str]]] = []

    async def xrevrange(self, _stream: str, count: int) -> list[tuple[str, dict[str, str]]]:
        if self.fail:
            raise ConnectionError("connection refused")
        return list(reversed(self.entries))[:count]


@pytest.mark.asyncio
async def test_an_unreachable_stream_is_reported_and_the_last_good_read_is_kept(monkeypatch):
    monkeypatch.setattr("monitor.server.redis_stats.REPLAY_TTL_SECONDS", 0.0)
    stream = _Stream()
    stream.entries.append(("1000000000-0", {"data": json.dumps(_record())}))
    stream.entries.append(("1000000001-0", {"data": "not json"}))
    reader = RedisStatsReader(stream, stream="s")  # type: ignore[arg-type]

    first = await reader.registry()
    status = reader.status()
    assert (status["ok"], status["records"], status["skipped_records"]) == (True, 2, 1)

    stream.fail = True
    second = await reader.registry()
    status = reader.status()

    assert second is first
    assert status["ok"] is False
    assert "connection refused" in status["error"]
    assert status["last_success_ms"] is not None


@pytest.mark.asyncio
async def test_a_stream_that_was_never_read_says_so():
    stream = _Stream()
    stream.fail = True
    reader = RedisStatsReader(stream, stream="s")  # type: ignore[arg-type]

    await reader.registry()

    assert reader.status()["ok"] is False
    assert reader.status()["last_success_ms"] is None


# ── ledger: "valid" only after the chain was read ────────────────────────────
def test_a_fast_read_checks_one_entry_and_claims_no_more(tmp_path):
    entries = _chain(6)
    entries[3]["payload"]["intent_id"] = "rewritten"  # hash 3 no longer matches
    _write(tmp_path / "chain.jsonl", entries)

    fast = load_ledger_page(patterns=[str(tmp_path / "chain.jsonl")])
    full = load_ledger_page(patterns=[str(tmp_path / "chain.jsonl")], validate=True)

    assert fast["validation_mode"] == "fast"
    assert fast["chains"][0]["fully_validated"] is False
    assert fast["summary"]["validated_entry_count"] == 1
    # The head is signed; that says nothing about the other five entries.
    assert fast["chains"][0]["head_signed"] is True
    assert fast["summary"]["signed_entry_count"] is None

    assert full["validation_mode"] == "full"
    assert (full["chains"][0]["is_valid"], full["chains"][0]["broken_at"]) == (False, 3)
    assert full["summary"]["validated_entry_count"] == 6
    assert full["summary"]["signed_entry_count"] == 6
    assert full["summary"]["signatures_verified"] is False


@pytest.mark.asyncio
async def test_api_ledger_validation_walks_the_whole_chain(monkeypatch):
    monkeypatch.setattr(ledger_api, "COUNCIL_PAGE_LIMIT", 4)  # several pages
    entries = _chain(11)
    entries[5]["payload"]["intent_id"] = "rewritten"
    calls: list[dict[str, Any]] = []
    async with _council_api(entries, calls) as client:
        fast = await load_ledger_page_from_api(client, "http://council")
        full = await load_ledger_page_from_api(
            client, "http://council", validate=True, council_id="council-b"
        )

    assert fast["validation_mode"] == "fast"
    assert fast["chains"][0]["is_valid"] is True  # the head alone is intact
    assert full["validation_mode"] == "full"
    assert (full["chains"][0]["is_valid"], full["chains"][0]["broken_at"]) == (False, 5)
    assert full["summary"]["validated_entry_count"] == 11
    assert full["summary"]["signed_entry_count"] == 11
    assert calls[0] == {"cursor": None, "limit": 1}
    assert all(call["council_id"] == "council-b" for call in calls[2:])


@pytest.mark.asyncio
async def test_api_ledger_validation_reports_an_intact_chain_as_valid():
    async with _council_api(_chain(7, signed=False), []) as client:
        full = await load_ledger_page_from_api(client, "http://council", validate=True)

    assert full["validation_mode"] == "full"
    assert full["chains"][0] == {
        **full["chains"][0],
        "is_valid": True,
        "broken_at": None,
        "fully_validated": True,
        "validated_entries": 7,
        "signed_count": 0,
    }


@pytest.mark.asyncio
async def test_api_ledger_validation_that_stops_early_says_how_far_it_got(monkeypatch):
    monkeypatch.setattr(ledger_api, "COUNCIL_PAGE_LIMIT", 4)
    monkeypatch.setattr(ledger_api, "MAX_VALIDATED_ENTRIES", 8)
    async with _council_api(_chain(20), []) as client:
        partial = await load_ledger_page_from_api(client, "http://council", validate=True)

    assert partial["validation_mode"] == "partial"
    assert partial["chains"][0]["fully_validated"] is False
    assert partial["summary"]["validated_entry_count"] == 8
    assert partial["summary"]["signed_entry_count"] is None


@pytest.mark.asyncio
async def test_api_ledger_validation_treats_a_missing_entry_as_a_break():
    entries = [entry for entry in _chain(9) if entry["sequence"] != 4]
    async with _council_api(entries, []) as client:
        full = await load_ledger_page_from_api(client, "http://council", validate=True)

    assert (full["chains"][0]["is_valid"], full["chains"][0]["broken_at"]) == (False, 4)
    assert full["validation_mode"] == "partial"


def test_redaction_keeps_identifiers_and_still_hides_credentials():
    redacted = _redact(
        {
            "token_id": "writ-1",
            "bearer_agent_id": "agent-1",
            "token_minted_at": "2026-11-01T00:00:00Z",
            "input_tokens": 3100,
            "author": "council",
            "auth_token": "s3cret",
            "capability_token": "eyJ...",
            "environment": {"GITHUB_TOKEN": "ghp_x", "PATH": "/usr/bin"},
            "token_secret_id": "unlisted, so it fails closed",
        }
    )

    assert redacted == {
        "token_id": "writ-1",
        "bearer_agent_id": "agent-1",
        "token_minted_at": "2026-11-01T00:00:00Z",
        "input_tokens": 3100,
        "author": "council",
        "auth_token": "[redacted]",
        "capability_token": "[redacted]",
        "environment": {"GITHUB_TOKEN": "[redacted]", "PATH": "/usr/bin"},
        "token_secret_id": "[redacted]",
    }


# ── classification, configuration, health ────────────────────────────────────
@pytest.mark.parametrize(
    ("error", "outcome", "expected"),
    [
        ("Authentication failed or missing token (require_auth is enabled)", "error", "auth"),
        ("Agent 'a' is not a member of council 'c'", "error", "auth"),
        ("council member evaluation failed: token limit exceeded", "error", "upstream"),
        ("council_overloaded: average intent response time 41s", "error", "overload"),
        ("Denied by OPA policy: no remote scripts", "denied", "opa"),
        ("Certified evidence is missing signature", "error", "evidence"),
        ("Invalid intent: command is required", "error", "validation"),
        ("", "denied", "council"),
        ("", "approved", ""),
    ],
)
def test_decline_reasons_are_read_from_whole_words(error, outcome, expected):
    assert _classify_error(error, (), outcome) == expected


def test_latency_targets_are_parsed_and_bad_entries_ignored():
    assert _latency_targets("total=15000, auth=50,request=20000") == {
        "total": 15000.0,
        "auth": 50.0,
        "request": 20000.0,
    }
    assert _latency_targets("total=fast,=5,opa=0") == {}
    assert _latency_targets("") == {}


@pytest.mark.asyncio
async def test_health_probe_retries_once_when_an_idle_connection_was_closed():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("", request=request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe = await _probe_upstream(client, "http://council/health")

    assert probe == (True, {"status_code": 200})


@pytest.mark.asyncio
async def test_health_probe_names_the_failure_when_the_council_stays_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _probe_upstream(client, "http://council/health") == (
            False,
            {"error": "ReadError"},
        )


@pytest.fixture
def api_mode_app(monkeypatch):
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LATENCY_TARGETS_MS", "total=15000")
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    return build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )


@pytest.mark.asyncio
async def test_config_and_metrics_describe_the_mode_that_is_running(api_mode_app):
    transport = httpx.ASGITransport(app=api_mode_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        config = (await client.get("/api/config")).json()
        metrics = (await client.get("/api/metrics")).json()
        history = (await client.get("/api/history")).json()
        health = (await client.get("/healthz")).json()

    assert (config["stats_source"], config["ledger_source"]) == ("inline", "api")
    assert config["ledger_patterns"] == []
    assert config["ledger_api_url"] == "http://upstream"
    assert config["latency_targets_ms"] == {"total": 15000.0}
    assert metrics["source_status"] == {"source": "inline", "ok": True, "error": ""}
    assert metrics["councils"] == []
    assert len(history["points"]) == 61
    assert health["monitor"]["stats_source"]["source"] == "inline"
    assert health["monitor"]["inflight"] == 0


@pytest.mark.asyncio
async def test_requests_are_attributed_to_the_council_named_in_the_call(api_mode_app):
    api_mode_app.state.metrics._record(
        Completion(
            start_ts=NOW,
            end_ts=NOW,
            duration_ms=5.0,
            method="tools/call",
            tool="council_authorize",
            source="direct",
            status_code=200,
            ok=True,
            request=RequestSummary("agent-1", "", "", 0, 0, 0, council_id="council-b"),
        )
    )
    transport = httpx.ASGITransport(app=api_mode_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        metrics = (await client.get("/api/metrics", params={"council_id": "council-b"})).json()
        other = (await client.get("/api/metrics", params={"council_id": "council-a"})).json()

    assert metrics["councils"] == ["council-b"]
    assert metrics["completed_total"] == 1
    assert (other["completed_total"], other["councils"]) == (0, ["council-b"])
