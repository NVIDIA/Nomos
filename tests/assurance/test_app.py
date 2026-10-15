# SPDX-License-Identifier: Apache-2.0
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assurance.app import Settings, create_app
from assurance.export import build_bundle
from assurance.monitor import Store


def test_dashboard_requires_auth_but_health_is_public(tmp_path):
    settings = Settings(
        source_url="http://source",
        council_id="c",
        chain_id="c",
        source_token="fixture-token",
        username="reviewer",
        password="fixture-password",  # pragma: allowlist secret
        keys={"signer": "00" * 32},
        state_path=tmp_path / "state.db",
    )
    app = create_app(settings, run_worker=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        for route in ("/", "/api/status", "/api/findings", "/api/export"):
            assert client.get(route).status_code == 401
            assert client.get(route, auth=("reviewer", "bad")).status_code == 401
        response = client.get("/api/status", auth=("reviewer", "fixture-password"))
        assert response.status_code == 200
        assert response.json()["checkpoint"]["sequence"] == -1
        assert client.get("/", auth=("reviewer", "fixture-password")).status_code == 200


def test_empty_credentials_rejected(tmp_path):
    with pytest.raises(ValueError):
        Settings(
            source_url="http://source",
            council_id="c",
            chain_id="c",
            source_token="",
            username="reviewer",
            password="",
            keys={},
            state_path=tmp_path / "state.db",
        )


def test_export_is_validated_by_real_eval_author_and_omits_raw_payloads(tmp_path, monkeypatch):
    pytest.importorskip("trace_ingest", reason="optional pinned Compass checkout required")
    root = Path(
        os.environ.get(
            "EVAL_AUTHOR_ROOT", str(Path(__file__).resolve().parents[3] / "labs-eval-author")
        )
    )
    if not root.exists():
        root = Path(__file__).resolve().parents[2] / ".assurance-poc/upstream/eval-author"
    if not root.exists():
        pytest.skip("pinned Eval Author checkout is required")
    monkeypatch.setenv("EVAL_AUTHOR_ROOT", str(root))
    store = Store(tmp_path / "state.db", "c")
    store.append(
        [
            {
                "sequence": 0,
                "content_hash": "sha256:fixture",
                "entry_type": "policy_updated",
                "payload": {"secret": "must-never-export"},  # pragma: allowlist secret
                "timestamp": "2026-10-08T00:00:00+00:00",
            }
        ],
        (-1, None),
    )
    bundle = build_bundle(store)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert json.loads(archive.read("validation.json"))["valid"] is True
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["coverage_status"] == "unmeasured"
        assert "audit.md" in archive.namelist()
        assert not any(b"must-never-export" in archive.read(name) for name in archive.namelist())


def test_dashboard_assets_are_authenticated_and_allowlisted(tmp_path):
    settings = Settings(
        source_url="http://source",
        council_id="c",
        chain_id="c",
        source_token="fixture-token",
        username="reviewer",
        password="fixture-password",  # pragma: allowlist secret
        keys={"signer": "00" * 32},
        state_path=tmp_path / "state.db",
    )
    with TestClient(create_app(settings, run_worker=False)) as client:
        auth = ("reviewer", "fixture-password")
        for name, mime in (
            ("dashboard.css", "text/css"),
            ("dashboard.mjs", "application/javascript"),
            ("dashboard-model.mjs", "application/javascript"),
        ):
            assert client.get(f"/assets/{name}").status_code == 401
            response = client.get(f"/assets/{name}", auth=auth)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(mime)
        assert client.get("/assets/app.py", auth=auth).status_code == 404
        status = client.get("/api/status", auth=auth).json()
        assert status["poll_seconds"] == 2
        policy = client.get("/", auth=auth).headers["content-security-policy"]
        assert "unsafe-inline" not in policy
