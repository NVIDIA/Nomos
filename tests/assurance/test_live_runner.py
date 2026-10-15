# SPDX-License-Identifier: Apache-2.0
"""The live runner must fail on unexpected findings or a blocked observer."""

import json
from contextlib import nullcontext

import httpx
import pytest

from scripts import assurance_live_tests as runner


@pytest.mark.parametrize("mode", ["benign", "unexpected-finding", "blocked"])
def test_runner_checks_negative_control_and_retains_failure_report(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(runner, "STATE", tmp_path)
    (tmp_path / "access.env").write_text(
        "\n".join(
            ["FIXTURE_TOKEN=operator", "DASHBOARD_USER=reviewer", "DASHBOARD_PASSWORD=test", ""]
        )
    )
    monkeypatch.setattr(runner, "forward", lambda *args: nullcontext("http://fixture.test"))
    requests = []

    def transport(request):
        requests.append(request.url.path)
        if request.url.path == "/fixtures/run":
            assert request.headers["Authorization"] == "Bearer operator"
            assert json.loads(request.content) == {"scenario": "benign", "run_id": "test-1"}
            return httpx.Response(
                200,
                json={
                    "scenario": "benign",
                    "run_id": "test-1",
                    "agent_id": "live-benign-test-1",
                    "synthetic": True,
                    "appended": 2,
                    "sequences": [1, 2],
                    "head_sequence": 2,
                },
            )
        if request.url.path == "/api/status":
            return httpx.Response(
                200,
                json={
                    "chain_id": "demo",
                    "checkpoint": {"sequence": 2},
                    "analysis_sequence": 2,
                    "worker": {"state": "integrity_error" if mode == "blocked" else "ok"},
                },
            )
        assert request.url.path == "/api/findings"
        findings = (
            []
            if mode == "benign"
            else [
                {
                    "id": "unexpected",
                    "kind": "verification_failure",
                    "agent_id": "live-benign-test-1",
                }
            ]
        )
        return httpx.Response(200, json={"findings": findings})

    client_class = httpx.Client
    monkeypatch.setattr(
        runner.httpx,
        "Client",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(transport)),
    )
    if mode == "benign":
        assert runner.run("kind", ["benign"], "test-1")["passed"] is True
    else:
        with pytest.raises(RuntimeError, match=r"expected|blocked"):
            runner.run("kind", ["benign"], "test-1")
    saved = json.loads((tmp_path / "live-tests-test-1.json").read_text())
    assert saved["passed"] is (mode == "benign")
    assert saved["cases"][0]["agent_id"] == "live-benign-test-1"
    assert requests[0] == "/fixtures/run"
