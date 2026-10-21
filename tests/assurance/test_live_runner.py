# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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


def test_sequential_cases_wait_for_default_five_minute_analysis_interval(tmp_path, monkeypatch):
    """A caught-up first case defers the second run; virtual time avoids a real wait."""
    from types import SimpleNamespace

    from scripts import assurance_smoke

    clock = {"now": 0.0, "head": 0, "analyzed": 0, "due": 0.0, "scenario": None}
    monkeypatch.setattr(runner, "STATE", tmp_path)
    (tmp_path / "access.env").write_text(
        "FIXTURE_TOKEN=operator\n"  # pragma: allowlist secret
        "DASHBOARD_USER=reviewer\n"
        "DASHBOARD_PASSWORD=test\n"  # pragma: allowlist secret
    )
    monkeypatch.setattr(runner, "forward", lambda *args: nullcontext("http://fixture.test"))
    monkeypatch.setattr(
        runner,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock["now"],
            sleep=lambda duration: clock.update(now=clock["now"] + duration),
        ),
    )
    epoch = 1700000000
    monkeypatch.setattr(assurance_smoke, "time", SimpleNamespace(time=lambda: epoch + clock["now"]))

    def transport(request):
        if request.url.path == "/fixtures/run":
            scenario = json.loads(request.content)["scenario"]
            clock.update(scenario=scenario, head=2 if scenario == "benign" else 5)
            return httpx.Response(
                200,
                json={
                    "agent_id": f"live-{scenario}-test-default",
                    "appended": 2 if scenario == "benign" else 3,
                    "sequences": [1, 2] if scenario == "benign" else [3, 4, 5],
                },
            )
        if request.url.path == "/api/status":
            assert request.headers["Authorization"].startswith("Basic ")
            if clock["now"] >= clock["due"]:
                clock.update(analyzed=clock["head"], due=clock["now"] + 300)
            return httpx.Response(
                200,
                json={
                    "chain_id": "demo",
                    "checkpoint": {"sequence": clock["head"]},
                    "analysis_sequence": clock["analyzed"],
                    "worker": {"state": "ok"},
                    "analysis_seconds": 300,
                    "poll_seconds": 2,
                    "streams": {"tool-issues": {"next_due": epoch + clock["due"]}},
                },
            )
        assert request.url.path == "/api/findings"
        findings = (
            []
            if clock["scenario"] == "benign"
            else [
                {
                    "id": "denial",
                    "kind": "repeated_unchanged_denial",
                    "agent_id": "live-denial-test-default",
                    "synthetic": True,
                    "evidence": [{"chain_id": "demo", "sequence": n} for n in [3, 4, 5]],
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
    report = runner.run("kind", ["benign", "denial"], "test-default")
    assert report["passed"] is True
    assert clock["now"] == 300
    assert len(report["cases"]) == 2
    assert all(case["passed"] for case in report["cases"])


def test_analysis_wait_uses_authenticated_interval_and_schedule(monkeypatch):
    from types import SimpleNamespace

    from scripts import assurance_smoke

    monkeypatch.setattr(assurance_smoke, "time", SimpleNamespace(time=lambda: 1000))
    assert assurance_smoke.analysis_wait_seconds({}) == 435
    assert (
        assurance_smoke.analysis_wait_seconds(
            {
                "analysis_seconds": 5,
                "poll_seconds": 2,
                "stream_timeout": 10,
            }
        )
        == 60
    )
    assert (
        assurance_smoke.analysis_wait_seconds(
            {
                "analysis_seconds": 300,
                "streams": {"tool-issues": {"next_due": 1500}},
            }
        )
        == 635
    )
    # A corrupt/older status must never create a NaN or infinite deadline.
    assert assurance_smoke.analysis_wait_seconds({"analysis_seconds": "nan"}) == 435
