# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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
    monkeypatch.setenv("MONITOR_MCP_PROXY_TOKEN", "generated-proxy-token")
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
    monkeypatch.setenv("MONITOR_MCP_PROXY_TOKEN", "generated-proxy-token")

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


def test_monitor_alpha_profile_rejects_blank_mcp_proxy_token(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "generated-monitor-token")
    monkeypatch.setenv("MONITOR_DASHBOARD_PASSWORD", "generated-dashboard-password")
    monkeypatch.delenv("MONITOR_MCP_PROXY_TOKEN", raising=False)

    with pytest.raises(DeploymentPreflightError) as exc:
        build_app(
            upstream_url="http://upstream/mcp",
            upstream_health_url="http://upstream/healthz",
            upstream_transport=httpx.ASGITransport(app=upstream_app),
        )

    assert exc.value.issues == ("MONITOR_MCP_PROXY_TOKEN is required",)


@pytest.mark.asyncio
async def test_mcp_proxy_rejects_anonymous_when_token_configured(upstream_app, monkeypatch):
    from monitor.server.main import build_app

    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "test-monitor-token")
    monkeypatch.setenv("MONITOR_MCP_PROXY_TOKEN", "proxy-secret")  # pragma: allowlist secret
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    app = build_app(
        upstream_url="http://upstream/mcp",
        upstream_health_url="http://upstream/healthz",
        upstream_transport=httpx.ASGITransport(app=upstream_app),
    )

    transport = httpx.ASGITransport(app=app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        anonymous = await client.post("/mcp", json=body)
        wrong = await client.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"})
        wrong_scheme = await client.post(
            "/mcp", json=body, headers={"Authorization": "Basic proxy-secret"}
        )
        good = await client.post(
            "/mcp", json=body, headers={"Authorization": "Bearer proxy-secret"}
        )

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Bearer ")
    assert wrong.status_code == 401
    assert wrong_scheme.status_code == 401
    assert good.status_code == 200
    assert good.json()["echo"]["params"]["name"] == "x"


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


def _signed_ledger_entries(private, count: int = 2) -> list[dict]:
    """Chain of real Ed25519-signed entries, oldest first."""
    entries: list[dict] = []
    previous = None
    for sequence in range(count):
        entry = _ledger_entry(sequence, {"intent_id": f"intent-{sequence}"}, previous)
        digest = bytes.fromhex(entry["content_hash"].split(":", 1)[1])
        entry["signatures"] = [
            {"signer_id": "council-key", "signature_hex": private.sign(digest).hex()}
        ]
        entries.append(entry)
        previous = entry["content_hash"]
    return entries


def _forge_payload(entry: dict) -> None:
    """Rewrite the payload and re-hash so only the signature can tell."""
    entry["payload"] = {"intent_id": "FORGED"}
    content = {
        key: entry[key]
        for key in ("sequence", "entry_type", "payload", "timestamp", "previous_hash")
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entry["content_hash"] = f"sha256:{digest}"


def _council_with_ledger(entries_oldest_first: list[dict]) -> FastAPI:
    """Upstream that answers council_ledger_page the way the NAT debug endpoint does."""
    app = FastAPI()
    newest_first = list(reversed(entries_oldest_first))
    head_sequence = newest_first[0]["sequence"]

    @app.post("/debug/tools/call/council_ledger_page")
    async def ledger_page(request: Request):
        body = json.loads(await request.body())
        cursor = body.get("cursor")
        start = head_sequence if cursor is None else min(int(cursor), head_sequence)
        page = [e for e in newest_first if e["sequence"] <= start][: int(body.get("limit", 50))]
        older = page[-1]["sequence"] - 1 if page else -1
        return {
            "result": {
                "chain_id": "ledger-council-test",
                "head_sequence": head_sequence,
                "entries": page,
                "next_cursor": older if older >= 0 else None,
            }
        }

    return app


@pytest.mark.asyncio
async def test_ledger_api_source_verifies_signatures_with_trusted_keys(monkeypatch):
    """MONITOR_LEDGER_SOURCE=api must apply MONITOR_LEDGER_PUBLIC_KEYS exactly like
    file mode: a re-hashed head that no longer verifies is a broken chain."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from monitor.server.main import build_app

    private = Ed25519PrivateKey.generate()
    entries = _signed_ledger_entries(private)
    _forge_payload(entries[1])
    council = _council_with_ledger(entries)

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LEDGER_API_URL", "http://council")
    monkeypatch.setenv(
        "MONITOR_LEDGER_PUBLIC_KEYS",
        "hex:" + private.public_key().public_bytes_raw().hex(),
    )
    app = build_app(
        upstream_url="http://council/mcp",
        upstream_health_url="http://council/healthz",
        upstream_transport=httpx.ASGITransport(app=council),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        listing = await client.get("/api/ledger")
        forged = await client.get("/api/ledger/ledger-council-test/1")
        genuine = await client.get("/api/ledger/ledger-council-test/0")
        broken_only = await client.get("/api/ledger", params={"integrity": "broken"})

    assert listing.status_code == 200
    payload = listing.json()
    assert payload["chains"][0]["is_valid"] is False
    assert payload["chains"][0]["broken_at"] == 1
    assert payload["summary"]["invalid_chain_count"] == 1
    by_sequence = {row["sequence"]: row for row in payload["entries"]}
    assert by_sequence[1]["hash_valid"] is True
    assert by_sequence[1]["signature_valid"] is False
    assert by_sequence[0]["signature_valid"] is True
    assert forged.json()["signature_valid"] is False
    assert genuine.json()["signature_valid"] is True
    assert [row["sequence"] for row in broken_only.json()["entries"]] == [1]


@pytest.mark.asyncio
async def test_ledger_api_summary_reflects_failed_older_signature(monkeypatch):
    """A verified-bad older entry must not sit under a chain reported as valid:
    the API summary is built from the head, but failures seen in the fetched
    page have to fold into is_valid / broken_at / invalid_chain_count."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from monitor.server.main import build_app

    private = Ed25519PrivateKey.generate()
    entries = _signed_ledger_entries(private)
    entries[0]["signatures"] = []  # strip only the older entry; head stays genuine
    council = _council_with_ledger(entries)

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LEDGER_API_URL", "http://council")
    monkeypatch.setenv(
        "MONITOR_LEDGER_PUBLIC_KEYS",
        "hex:" + private.public_key().public_bytes_raw().hex(),
    )
    app = build_app(
        upstream_url="http://council/mcp",
        upstream_health_url="http://council/healthz",
        upstream_transport=httpx.ASGITransport(app=council),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        fast = await client.get("/api/ledger")
        full = await client.get("/api/ledger", params={"validate": "true"})
        broken_only = await client.get(
            "/api/ledger", params={"validate": "true", "integrity": "broken"}
        )

    for response in (fast, full, broken_only):
        assert response.status_code == 200
        payload = response.json()
        assert payload["chains"][0]["is_valid"] is False
        assert payload["chains"][0]["broken_at"] == 0
        assert payload["summary"]["invalid_chain_count"] == 1
        assert payload["summary"]["valid_chain_count"] == 0
    rows = {row["sequence"]: row for row in full.json()["entries"]}
    assert rows[0]["signature_valid"] is False
    assert rows[1]["signature_valid"] is True
    assert [row["sequence"] for row in broken_only.json()["entries"]] == [0]


@pytest.mark.asyncio
async def test_ledger_api_full_validation_walks_past_the_first_page(monkeypatch):
    """validate=true must reach a failure that sits beyond the display page."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import monitor.server.ledger_api as ledger_api
    from monitor.server.main import build_app

    monkeypatch.setattr(ledger_api, "COUNCIL_PAGE_CAP", 1)
    private = Ed25519PrivateKey.generate()
    entries = _signed_ledger_entries(private, count=3)
    entries[0]["signatures"] = []  # oldest entry, two pages below the head
    council = _council_with_ledger(entries)

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LEDGER_API_URL", "http://council")
    monkeypatch.setenv(
        "MONITOR_LEDGER_PUBLIC_KEYS",
        "hex:" + private.public_key().public_bytes_raw().hex(),
    )
    app = build_app(
        upstream_url="http://council/mcp",
        upstream_health_url="http://council/healthz",
        upstream_transport=httpx.ASGITransport(app=council),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://monitor") as client:
        fast = await client.get("/api/ledger")
        full = await client.get("/api/ledger", params={"validate": "true"})

    # The fast view only sees the head page, and says so.
    fast_chain = fast.json()["chains"][0]
    assert fast_chain["is_valid"] is True
    assert fast_chain["fully_validated"] is False
    assert [row["sequence"] for row in fast.json()["entries"]] == [2]
    # Full validation walked to sequence 0 and found the stripped signature.
    full_chain = full.json()["chains"][0]
    assert full_chain["is_valid"] is False
    assert full_chain["broken_at"] == 0
    assert full_chain["fully_validated"] is True
    assert full_chain["signatures_verified"] is False
    assert full.json()["summary"]["invalid_chain_count"] == 1


def _relink(entry: dict, previous_hash: str, private) -> None:
    """Point ``entry`` at ``previous_hash`` and re-hash + re-sign it, so the
    entry is individually valid and only its link can be wrong."""
    entry["previous_hash"] = previous_hash
    content = {
        key: entry[key]
        for key in ("sequence", "entry_type", "payload", "timestamp", "previous_hash")
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entry["content_hash"] = f"sha256:{digest}"
    entry["signatures"] = [
        {"signer_id": "council-key", "signature_hex": private.sign(bytes.fromhex(digest)).hex()}
    ]


@pytest.mark.asyncio
async def test_ledger_api_full_validation_checks_links_across_pages(monkeypatch):
    """A broken link that straddles a page boundary must still break the chain."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import monitor.server.ledger_api as ledger_api
    from monitor.server.main import build_app

    monkeypatch.setattr(ledger_api, "COUNCIL_PAGE_CAP", 2)  # pages: [3,2] [1,0]
    private = Ed25519PrivateKey.generate()
    entries = _signed_ledger_entries(private, count=4)
    _relink(entries[2], "sha256:" + "0" * 64, private)  # 2 -> 1 edge is wrong
    _relink(entries[3], entries[2]["content_hash"], private)  # 3 -> 2 stays right
    council = _council_with_ledger(entries)

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LEDGER_API_URL", "http://council")
    monkeypatch.setenv(
        "MONITOR_LEDGER_PUBLIC_KEYS",
        "hex:" + private.public_key().public_bytes_raw().hex(),
    )
    app = build_app(
        upstream_url="http://council/mcp",
        upstream_health_url="http://council/healthz",
        upstream_transport=httpx.ASGITransport(app=council),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://monitor"
    ) as client:
        full = await client.get("/api/ledger", params={"validate": "true"})

    chain = full.json()["chains"][0]
    assert chain["fully_validated"] is True
    assert chain["is_valid"] is False
    assert chain["broken_at"] == 1
    assert chain["signatures_verified"] is True  # every signature is genuine


@pytest.mark.asyncio
async def test_ledger_api_full_validation_marks_a_clean_chain_verified(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import monitor.server.ledger_api as ledger_api
    from monitor.server.main import build_app

    monkeypatch.setattr(ledger_api, "COUNCIL_PAGE_CAP", 1)
    private = Ed25519PrivateKey.generate()
    council = _council_with_ledger(_signed_ledger_entries(private, count=3))
    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("MONITOR_LEDGER_SOURCE", "api")
    monkeypatch.setenv("MONITOR_LEDGER_API_URL", "http://council")
    monkeypatch.setenv(
        "MONITOR_LEDGER_PUBLIC_KEYS",
        "hex:" + private.public_key().public_bytes_raw().hex(),
    )
    app = build_app(
        upstream_url="http://council/mcp",
        upstream_health_url="http://council/healthz",
        upstream_transport=httpx.ASGITransport(app=council),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://monitor"
    ) as client:
        full = await client.get("/api/ledger", params={"validate": "true"})
    chain = full.json()["chains"][0]
    assert chain["is_valid"] is True
    assert chain["fully_validated"] is True
    assert chain["signatures_verified"] is True


def test_monitor_refuses_unusable_ledger_public_keys(monkeypatch, tmp_path):
    """A configured key list that yields no key must not silently fall back to
    hash-only checking."""
    from monitor.server.main import build_app

    monkeypatch.delenv("MONITOR_DASHBOARD_PASSWORD", raising=False)
    missing = tmp_path / "council.pub"
    monkeypatch.setenv("MONITOR_LEDGER_PUBLIC_KEYS", f"{missing},hex:not-hex")

    with pytest.raises(DeploymentPreflightError) as exc:
        build_app(
            upstream_url="http://upstream/mcp",
            upstream_health_url="http://upstream/healthz",
        )
    assert "MONITOR_LEDGER_PUBLIC_KEYS" in str(exc.value)
    assert str(missing) in str(exc.value) and "hex:not-hex" in str(exc.value)


def test_dashboard_script_treats_failed_signature_as_broken():
    """No JS runtime in the test env: pin the predicate the ledger renderers use."""
    from monitor.server.main import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "entry.signature_valid !== false" in script
    assert 'entry.signature_valid === false) return { cls: "broken"' in script
    for renderer in ("function renderLedgerRow(entry) {", "function renderLedgerDetail(entry) {"):
        body = script.split(renderer, 1)[1].split("\n}\n", 1)[0]
        assert "ledgerIntegrity(entry)" in body, renderer
        assert "ledgerSignatureLabel(entry)" in body, renderer
