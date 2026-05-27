"""End-to-end tests for the monitor FastAPI app via in-process ASGI.

Validates that the proxy path, dashboard endpoints, and health probe wire
into the metrics registry correctly.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nomos_runtime import DeploymentPreflightError


@pytest.fixture
def upstream_app() -> FastAPI:
    """Tiny upstream that mimics MCP's POST /mcp shape."""
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.body()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        return JSONResponse({"echo": parsed}, status_code=200)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


@pytest.fixture
def monitor_app(upstream_app, monkeypatch):
    """Build a monitor FastAPI app whose upstream client targets `upstream_app`."""
    from monitor.server.main import build_app

    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    upstream_transport = httpx.ASGITransport(app=upstream_app)
    return build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=upstream_transport,
    )


def _ledger_entry(sequence: int, payload: dict, previous_hash: str | None) -> dict:
    timestamp = f"2026-05-01T12:00:0{sequence}+00:00"
    content = {
        "sequence": sequence,
        "entry_type": "intent_submitted" if sequence else "policy_updated",
        "payload": payload,
        "timestamp": timestamp,
        "previous_hash": previous_hash,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **content,
        "content_hash": f"sha256:{digest}",
        "signatures": [{"signer_id": "council-key", "signature_hex": "abc"}],
        "metadata": {},
    }


def _basic_auth(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_monitor_dev_profile_allows_blank_dashboard_password(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.delenv("NOMOS_DEPLOY_PROFILE", raising=False)
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)

    app = build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=httpx.ASGITransport(app=upstream_app),
    )

    assert app.state.dashboard_password == ""


def test_monitor_alpha_profile_rejects_blank_dashboard_password(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "generated-monitor-token")
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)

    with pytest.raises(DeploymentPreflightError) as exc:
        build_app(
            upstream_url="http://upstream/mcp",
            upstream_health_url="http://upstream/healthz",
            upstream_transport=httpx.ASGITransport(app=upstream_app),
        )

    assert exc.value.issues == ("MONITOR_DASHBOARD_PASSWORD is required",)


def test_monitor_alpha_profile_rejects_default_internal_token(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "nomos-dev-telemetry")
    monkeypatch.setenv("MONITOR_DASHBOARD_PASSWORD", "generated-dashboard-password")

    with pytest.raises(DeploymentPreflightError) as exc:
        build_app(
            upstream_url="http://upstream/mcp",
            upstream_health_url="http://upstream/healthz",
            upstream_transport=httpx.ASGITransport(app=upstream_app),
        )

    assert exc.value.issues == ("MONITOR_INTERNAL_TOKEN uses a known development default",)


@pytest.mark.asyncio
async def test_proxy_endpoint_forwards_to_upstream(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "council_authorize", "arguments": {}},
        }
        resp = await client.post("/mcp", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["echo"]["params"]["name"] == "council_authorize"


@pytest.mark.asyncio
async def test_metrics_endpoint_reflects_proxied_calls(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        for source in ("direct", "grpc", "demo", "demo"):
            await client.post(
                "/mcp",
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "council_authorize",
                            "arguments": {"agent_id": f"agent-{source}"},
                        },
                    }
                ).encode(),
                headers={
                    "content-type": "application/json",
                    "x-council-source": source,
                },
            )
        resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["completed_in_window"] == 4
    assert data["by_source"] == {"direct": 1, "grpc": 1, "demo": 2}
    assert data["by_tool"] == {"council_authorize": 4}
    assert data["by_agent_total"] == {"agent-direct": 1, "agent-grpc": 1, "agent-demo": 2}
    assert data["agent_activity"]["agent-demo"]["by_tool_total"] == {"council_authorize": 2}
    assert "p50" in data["latency_ms"]


@pytest.mark.asyncio
async def test_recent_endpoint_returns_completed_rows(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "council_propose_intent", "arguments": {}},
            },
            headers={"x-council-source": "demo"},
        )
        resp = await client.get("/api/recent?limit=10")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert rows[0]["source"] == "demo"
    assert rows[0]["tool"] == "council_propose_intent"


@pytest.mark.asyncio
async def test_healthz_reports_self_and_upstream(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["monitor"]["ok"] is True
    assert data["upstream"]["ok"] is True


@pytest.mark.asyncio
async def test_debug_tools_list_proxies_upstream_health(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        resp = await client.get("/debug/tools/list")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_root_serves_dashboard_html(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert b"Council Monitor" in resp.content


@pytest.mark.asyncio
async def test_dashboard_auth_protects_board_api_and_static(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("MONITOR_DASHBOARD_USER", "ops")
    monkeypatch.setenv("MONITOR_DASHBOARD_PASSWORD", "board-pass")  # pragma: allowlist secret
    upstream_transport = httpx.ASGITransport(app=upstream_app)
    app = build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=upstream_transport,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        root = await client.get("/")
        api = await client.get("/api/config")
        static = await client.get("/static/app.js")
        bad = await client.get("/api/config", headers=_basic_auth("ops", "wrong"))
        good = await client.get("/api/config", headers=_basic_auth("ops", "board-pass"))

    assert root.status_code == 401
    assert root.headers["www-authenticate"].startswith("Basic ")
    assert api.status_code == 401
    assert static.status_code == 401
    assert bad.status_code == 401
    assert good.status_code == 200
    assert good.json()["upstream_url"] == "http://upstream/mcp"


@pytest.mark.asyncio
async def test_dashboard_auth_does_not_block_mcp_health_or_internal(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.setenv("MONITOR_DASHBOARD_PASSWORD", "board-pass")  # pragma: allowlist secret
    upstream_transport = httpx.ASGITransport(app=upstream_app)
    app = build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=upstream_transport,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "council_authorize", "arguments": {}},
            },
        )
        health = await client.get("/healthz")
        debug = await client.get("/debug/tools/list")
        internal = await client.post(
            "/internal/council-latency",
            json={"tool": "council_authorize"},
            headers={"X-Council-Internal-Token": "test-monitor-token"},
        )

    assert mcp.status_code == 200
    assert health.status_code == 200
    assert debug.status_code == 200
    assert internal.status_code == 200


@pytest.mark.asyncio
async def test_internal_council_latency_requires_token(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        resp = await client.post("/internal/council-latency", json={"tool": "council_authorize"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_internal_council_latency_updates_metrics(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    telemetry = {
        "tool": "council_authorize",
        "intent_id": "intent-test",
        "outcome": "approved",
        "stages": {"chairman": 1200.0, "review_collect": 2400.0, "total": 3700.0},
        "actors": [
            {
                "role": "chairman",
                "member_id": "chairman",
                "provider": "nv_inference",
                "duration_ms": 1200.0,
                "decision": "unanimous",
            },
            {
                "role": "council_member",
                "member_id": "command-integrity",
                "provider": "nv_inference",
                "duration_ms": 2400.0,
                "decision": "approve",
            },
        ],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        resp = await client.post(
            "/internal/council-latency",
            json=telemetry,
            headers={"X-Council-Internal-Token": "test-monitor-token"},
        )
        metrics = await client.get("/api/metrics")
    assert resp.status_code == 200
    data = metrics.json()
    assert data["council_stage_latency"]["chairman"]["count"] == 1
    assert data["council_stage_latency"]["chairman"]["p95_ms"] == 1200.0
    assert data["council_actor_latency"]["chairman:chairman"]["last_decision"] == "unanimous"
    member = data["council_actor_latency"]["council_member:command-integrity"]
    assert member["p95_ms"] == 2400.0
    assert member["provider"] == "nv_inference"


@pytest.mark.asyncio
async def test_export_csv_includes_long_term_totals_and_latency(monitor_app):
    transport = httpx.ASGITransport(app=monitor_app)
    telemetry = {
        "tool": "council_authorize",
        "stages": {"chairman": 1200.0},
        "actors": [
            {
                "role": "chairman",
                "member_id": "chairman",
                "provider": "nv_inference",
                "duration_ms": 1200.0,
                "decision": "unanimous",
            }
        ],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "council_authorize",
                    "arguments": {"agent_id": "agent-demo"},
                },
            },
            headers={"x-council-source": "demo"},
        )
        await client.post(
            "/internal/council-latency",
            json=telemetry,
            headers={"X-Council-Internal-Token": "test-monitor-token"},
        )
        resp = await client.get("/api/export.csv")

    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert {
        "section": "summary",
        "name": "monitor",
        "metric": "completed_total",
        "value": "1",
    } in rows
    assert {
        "section": "source_total",
        "name": "demo",
        "metric": "count",
        "value": "1",
    } in rows
    assert {
        "section": "agent_total",
        "name": "agent-demo",
        "metric": "count",
        "value": "1",
    } in rows
    assert {
        "section": "agent_tool_total",
        "name": "agent-demo",
        "metric": "council_authorize",
        "value": "1",
    } in rows
    assert {
        "section": "stage_latency",
        "name": "chairman",
        "metric": "p95_ms",
        "value": "1200.0",
    } in rows


@pytest.mark.asyncio
async def test_ledger_api_lists_chains_and_entries(upstream_app, monkeypatch, tmp_path):
    from monitor.server.main import build_app

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    ledger = tmp_path / "council-ledger.jsonl"
    genesis = _ledger_entry(
        0,
        {"chain_id": "ledger-council-test", "creator_id": "test"},
        None,
    )
    submitted = _ledger_entry(
        1,
        {"intent_id": "intent-1", "agent_id": "agent-test", "auth_token": "secret"},
        genesis["content_hash"],
    )
    ledger.write_text(json.dumps(genesis) + "\n" + json.dumps(submitted) + "\n")
    monkeypatch.setenv("MONITOR_LEDGER_PATHS", str(ledger))
    upstream_transport = httpx.ASGITransport(app=upstream_app)
    app = build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=upstream_transport,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        listing = await client.get("/api/ledger")
        detail = await client.get("/api/ledger/ledger-council-test/1")

    assert listing.status_code == 200
    payload = listing.json()
    assert payload["summary"]["entry_count"] == 2
    assert payload["chains"][0]["is_valid"] is True
    assert payload["entries"][0]["sequence"] == 1
    assert detail.status_code == 200
    assert detail.json()["payload"]["auth_token"] == "[redacted]"
