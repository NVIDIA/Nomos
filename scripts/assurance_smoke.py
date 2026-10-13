#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise the Kind PoC, restoring injected faults and temporary forwards."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import subprocess
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".assurance-poc"
NAMESPACE = "nomos-assurance-poc"


def kube(context, *args):
    return ["kubectl", "--context", context, "-n", NAMESPACE, *args]


def wait_for(operation, predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = operation()
            if predicate(result):
                return result
        except (httpx.HTTPError, KeyError):
            pass
        time.sleep(0.5)
    raise RuntimeError("smoke condition did not converge before timeout")


@contextlib.contextmanager
def forward(context, service):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (STATE / f"{service}-smoke-forward.log").open("w") as log:
        process = subprocess.Popen(
            kube(
                context, "port-forward", "--address", "127.0.0.1", f"svc/{service}", f"{port}:8080"
            ),
            stdout=log,
            stderr=log,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(timeout=5, trust_env=False) as client:
                wait_for(lambda: client.get(f"{url}/healthz"), lambda r: r.status_code == 200)
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run(cluster):
    context = f"kind-{cluster}"
    values = dict(
        line.split("=", 1)
        for line in (STATE / "access.env").read_text().splitlines()
        if "=" in line
    )
    checks = []
    operator = {"Authorization": f"Bearer {values['FIXTURE_TOKEN']}"}
    reader = {"Authorization": f"Bearer {values['SOURCE_TOKEN']}"}
    dashboard_auth = (values["DASHBOARD_USER"], values["DASHBOARD_PASSWORD"])
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "context": context,
        "namespace": NAMESPACE,
        "synthetic": True,
        "checks": checks,
    }
    with (
        httpx.Client(timeout=35, trust_env=False) as client,
        forward(context, "ledger-source") as source,
    ):

        def seed():
            response = client.post(f"{source}/fixtures/seed", headers=operator)
            response.raise_for_status()
            return response.json()

        def fault(enabled):
            response = client.post(
                f"{source}/fixtures/fault", json={"enabled": enabled}, headers=operator
            )
            response.raise_for_status()

        with forward(context, "observer") as observer:

            def status():
                response = client.get(f"{observer}/api/status", auth=dashboard_auth)
                response.raise_for_status()
                return response.json()

            def findings():
                response = client.get(f"{observer}/api/findings", auth=dashboard_auth)
                response.raise_for_status()
                return response.json()["findings"]

            assert client.get(f"{observer}/api/findings").status_code == 401
            assert (
                client.get(f"{observer}/api/findings", auth=("reviewer", "wrong")).status_code
                == 401
            )
            assert client.post(f"{source}/fixtures/seed", headers=reader).status_code == 401
            assert (
                client.post(
                    f"{source}/debug/tools/call/council_ledger_page",
                    json={"council_id": "assurance-demo"},
                ).status_code
                == 401
            )
            checks.append("dashboard/source auth and read-vs-fixture privilege separation")
            seeded = seed()
            assert seeded["appended"] in {0, 15}
            assert seed()["appended"] == 0
            settled = wait_for(
                status,
                lambda s: (
                    s["checkpoint"]["sequence"] >= seeded["head_sequence"]
                    and s["analysis_sequence"] == s["checkpoint"]["sequence"]
                    and s["worker"]["state"] == "ok"
                ),
            )
            original = findings()
            baseline = [
                f
                for f in original
                if f["agent_id"] in {"boundary-agent", "loop-agent", "replay-agent"}
            ]
            counts = Counter(f["kind"] for f in baseline)
            assert counts["explicit_tool_failure"] == 9
            assert counts["repeated_identical_failed_call"] == 3
            assert counts["repeated_unchanged_denial"] == 1
            assert counts["verification_failure"] == 1
            assert not any(f["agent_id"] == "careful-agent" for f in original)
            assert all(
                len(f["evidence"]) == 3
                for f in baseline
                if f["kind"] == "repeated_identical_failed_call"
            )
            assert settled["coverage"]["compass_problems"] >= 1
            checks.append(
                "15 signed fixtures + genesis; 14 behavioral findings; benign correction clear"
            )
            checkpoint = settled["checkpoint"]
            try:
                fault(True)
                wait_for(status, lambda s: s["worker"]["state"] == "integrity_error")
                assert status()["checkpoint"] == checkpoint
                incidents = [
                    f
                    for f in findings()
                    if f["kind"] == "ledger_integrity_failure"
                    and f["evidence"][0]["sequence"] == checkpoint["sequence"]
                ]
                assert len(incidents) == 1
                checks.append(
                    "corrupt source rejected; checkpoint frozen; deduplicated incident retained"
                )
            finally:
                fault(False)
            wait_for(status, lambda s: s["worker"]["state"] == "ok")
            assert status()["checkpoint"] == checkpoint
            before_ids = {f["id"] for f in findings()}
            export = client.get(f"{observer}/api/export", auth=dashboard_auth)
            export.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
                assert json.loads(archive.read("validation.json"))["valid"] is True
                assert json.loads(archive.read("manifest.json"))["coverage_status"] == "unmeasured"
                assert all(
                    b"/demo/unavailable" not in archive.read(name) for name in archive.namelist()
                )
            (STATE / "nomos-eval-handoff.zip").write_bytes(export.content)
            checks.append(
                "real Eval Author validator passes; redacted export labels coverage unmeasured"
            )
        subprocess.run(kube(context, "rollout", "restart", "deployment/observer"), check=True)
        subprocess.run(
            kube(context, "rollout", "status", "deployment/observer", "--timeout=90s"), check=True
        )
        with forward(context, "observer") as observer:

            def recovered():
                response = client.get(f"{observer}/api/status", auth=dashboard_auth)
                response.raise_for_status()
                return response.json()

            status_after = wait_for(recovered, lambda s: s["worker"]["state"] == "ok")
            assert status_after["checkpoint"] == checkpoint
            after = client.get(f"{observer}/api/findings", auth=dashboard_auth).json()["findings"]
            assert {f["id"] for f in after} == before_ids
            assert seed()["appended"] == 0
            checks.append(
                "observer pod restart preserves checkpoint, analysis cursor and all finding IDs"
            )
            report.update(
                {
                    "checkpoint": checkpoint,
                    "analysis_sequence": status_after["analysis_sequence"],
                    "findings_by_kind": dict(Counter(f["kind"] for f in after)),
                    "coverage": status_after["coverage"],
                    "passed": True,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
    (STATE / "smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="kind")
    run(parser.parse_args().cluster)
