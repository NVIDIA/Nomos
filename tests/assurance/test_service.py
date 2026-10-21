# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in API boundary and durable per-stream publication contracts."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from assurance.app import Settings, create_app


def settings(tmp_path, **values):
    return Settings(
        enabled=True,
        source_url="http://source",
        source_token="reader",
        council_id="c",
        chain_id="c",
        username="reviewer",
        password="password",  # pragma: allowlist secret
        api_read_token="read-only",
        keys={"signer": "00" * 32},
        state_path=tmp_path / "db",
        **values,
    )


def test_disabled_needs_no_secrets_no_state_or_optional_imports(tmp_path):
    app = create_app(Settings(state_path=tmp_path / "never" / "db"))
    with TestClient(app) as client:
        for path in ["/", "/healthz", "/api/status", "/api/findings", "/api/runs"]:
            assert client.get(path).status_code == 404
    assert not (tmp_path / "never").exists()
    code = """import sys
from assurance.app import Settings,create_app
create_app(Settings())
assert not any(x in sys.modules for x in ('trace_ingest','nooa','litellm','assurance.export'))
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_explicit_flags_and_required_settings(monkeypatch):
    monkeypatch.setenv("ASSURANCE_ENABLED", "false")
    monkeypatch.delenv("ASSURANCE_ETHOS_ENABLED", raising=False)
    assert not Settings.from_env().enabled
    monkeypatch.setenv("ASSURANCE_ETHOS_ENABLED", "true")
    with pytest.raises(ValueError, match="enable assurance"):
        Settings.from_env()
    monkeypatch.setenv("ASSURANCE_ENABLED", "yes")
    with pytest.raises(ValueError, match="true or false"):
        Settings.from_env()


def test_machine_read_credential_cannot_retry_and_export_is_optional(tmp_path):
    app = create_app(settings(tmp_path), run_worker=False)
    with TestClient(app) as client:
        bearer = {"Authorization": "Bearer read-only"}
        assert client.get("/api/status", headers=bearer).status_code == 200
        assert (
            client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        assert client.post("/api/runs/unknown/retry", headers=bearer).status_code == 401
        assert client.get("/api/export", headers=bearer).status_code == 404
        assert (
            client.get("/api/findings?agent=%27%20OR%201%3D1--", headers=bearer).json()["total"]
            == 0
        )
        assert client.get("/api/findings?limit=1001", headers=bearer).status_code == 422
        assert client.get("/api/runs?since=2026-01-01T00:00:00", headers=bearer).status_code == 422
        assert (
            client.get("/api/status", headers=bearer).json()["streams"]["ethos-divergence"][
                "enabled"
            ]
            is False
        )


@pytest.mark.asyncio
async def test_paginated_runs_filtering_history_and_private_evidence(tmp_path):
    app = create_app(settings(tmp_path), run_worker=False)
    store = app.state.store
    for i in range(3):
        store.append(
            [
                {
                    "sequence": i,
                    "content_hash": str(i),
                    "entry_type": "policy_updated",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "payload": {"agent_id": "agent", "raw-secret": "private"},
                }
            ],
            store.checkpoint(),
        )
        run = store.make_run("tool-issues", "cfg", artifacts={"documents": "private-document"})
        run.update(status="complete", completed_at="2026-01-01T00:00:00+00:00", agents=["agent"])
        store.save_run(
            run,
            [
                {
                    "id": str(i),
                    "agent_id": "agent",
                    "kind": "example",
                    "disposition": "advisory",
                    "evidence": [],
                },
                {
                    "id": "recurring",
                    "agent_id": "agent",
                    "kind": "example",
                    "disposition": "advisory",
                    "evidence": [],
                },
            ],
        )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer read-only"}
        result = client.get(
            "/api/runs?limit=2&agent=agent&stream=tool-issues", headers=headers
        ).json()
        assert result["total"] == 3 and result["next_offset"] == 2
        assert len(result["items"]) == 2
        assert "private" not in json.dumps(result)
        latest = result["items"][0]["id"]
        assert client.get(f"/api/runs/{latest}", headers=headers).status_code == 200
        findings = client.get("/api/findings?limit=2&disposition=advisory", headers=headers).json()
        assert findings["total"] == 4 and findings["next_offset"] == 2
        by_run = client.get(f"/api/findings?run_id={latest}", headers=headers).json()
        assert by_run["total"] == 2
        history = client.get("/api/findings/recurring", headers=headers).json()["history"]
        assert len(history) == 3
        assert (
            client.get("/api/findings?since=2027-01-01T00:00:00Z", headers=headers).json()["total"]
            == 0
        )


def test_two_live_workers_cannot_share_sqlite_state(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config)):
        with pytest.raises(RuntimeError, match="another assurance"):
            with TestClient(create_app(config)):
                pass


def test_operator_retry_requires_csrf_header_and_same_origin(tmp_path):
    app = create_app(settings(tmp_path), run_worker=False)
    store = app.state.store
    worker = app.state.workers[0]
    store.append([{"sequence": 0, "content_hash": "hash", "payload": {}}], store.checkpoint())
    run = store.make_run(worker.stream, worker.config_digest, artifacts={})
    run.update(status="failed", attempts=3)
    store.save_run(run)
    url = f"/api/runs/{run['id']}/retry"
    with TestClient(app) as client:
        auth = ("reviewer", "password")
        for headers in (
            {},
            {"X-Assurance-Operator": "wrong"},
            {"X-Assurance-Operator": "retry", "Sec-Fetch-Site": "cross-site"},
            {"X-Assurance-Operator": "retry", "Sec-Fetch-Site": "same-site"},
        ):
            assert client.post(url, auth=auth, headers=headers).status_code == 403
            assert store.run(run["id"])["attempts"] == 3
        for site in (None, "same-origin"):
            headers = {"X-Assurance-Operator": "retry"}
            if site is not None:
                headers["Sec-Fetch-Site"] = site
            assert client.post(url, auth=auth, headers=headers).status_code == 200
            assert store.run(run["id"])["attempts"] == 0
            run.update(status="failed", attempts=3)
            store.save_run(run)


@pytest.mark.asyncio
@pytest.mark.parametrize("backlog_limited", [False, True])
async def test_production_ingestion_records_integrity_failure_and_backlog_status(
    tmp_path, monkeypatch, backlog_limited
):
    import asyncio

    from assurance.monitor import IntegrityError, Monitor
    from assurance.scheduler import StreamWorker

    app = create_app(settings(tmp_path, max_backlog=200))
    store = app.state.store
    checkpoint = 199 if backlog_limited else 0
    store.append(
        [
            {"sequence": i, "content_hash": f"hash-{i}", "payload": {}}
            for i in range(checkpoint + 1)
        ],
        store.checkpoint(),
    )
    finished = asyncio.Event()
    original_cycle = Monitor.cycle
    calls = []

    async def ingest(self):
        calls.append(True)
        raise IntegrityError("untrusted upstream detail")

    async def cycle(self, **kwargs):
        try:
            await original_cycle(self, **kwargs)
        finally:
            finished.set()

    async def idle(self):
        pass

    monkeypatch.setattr(Monitor, "ingest", ingest)
    monkeypatch.setattr(Monitor, "cycle", cycle)
    monkeypatch.setattr(StreamWorker, "step", idle)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(finished.wait(), 2)
        assert store.checkpoint() == (checkpoint, f"hash-{checkpoint}")
        status = store.setting("status")
        if backlog_limited:
            assert not calls
            assert status["error_type"] == "BacklogLimit"
            assert status["state"] == "worker_error"
            assert not store.findings()
        else:
            assert calls
            assert status["state"] == "integrity_error"
            (incident,) = store.findings()
            assert (
                incident["evidence_role"]
                == "last trusted checkpoint; rejected data is not evidence"
            )
            assert incident["evidence"][0]["sequence"] == checkpoint
            assert "untrusted upstream" not in json.dumps(status)
        assert store.analysis_sequence() == -1
