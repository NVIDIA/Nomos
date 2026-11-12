#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Submit fresh synthetic cases to Kind and verify the observer's advisory findings."""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

import httpx

from assurance.fixtures import LIVE_SCENARIOS
from scripts.assurance_smoke import NAMESPACE, STATE, analysis_wait_seconds, forward


def run(cluster: str, names: list[str], run_id: str) -> dict:
    values = dict(
        line.split("=", 1)
        for line in (STATE / "access.env").read_text().splitlines()
        if "=" in line
    )
    context = f"kind-{cluster}"
    report = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "context": context,
        "namespace": NAMESPACE,
        "synthetic": True,
        "cases": [],
        "passed": False,
    }
    destination = STATE / f"live-tests-{run_id}.json"
    print(f"Run: {run_id} | {context}/{NAMESPACE}", flush=True)
    print("Synthetic observations only: no commands executed; no automatic sanctions.", flush=True)
    try:
        with (
            httpx.Client(timeout=15, trust_env=False) as client,
            forward(context, "ledger-source") as source,
            forward(context, "observer") as observer,
        ):
            auth = (values["DASHBOARD_USER"], values["DASHBOARD_PASSWORD"])
            for name in names:
                print(f"\n{name}: {LIVE_SCENARIOS[name]['description']}", flush=True)
                response = client.post(
                    f"{source}/fixtures/run",
                    json={"scenario": name, "run_id": run_id},
                    headers={"Authorization": f"Bearer {values['FIXTURE_TOKEN']}"},
                )
                response.raise_for_status()
                case = response.json()
                report["cases"].append(case)
                print(
                    f"  Agent: {case['agent_id']} | added {case['appended']} records | "
                    f"evidence sequences {case['sequences']}",
                    flush=True,
                )
                deadline = None
                wait_seconds = None
                while True:
                    response = client.get(f"{observer}/api/status", auth=auth)
                    response.raise_for_status()
                    status = response.json()
                    if deadline is None:
                        wait_seconds = analysis_wait_seconds(status)
                        deadline = time.monotonic() + wait_seconds
                    if status["worker"]["state"] in {"integrity_error", "worker_error"}:
                        raise RuntimeError(f"Observer is blocked: {status['worker']['state']}")
                    target = max(case["sequences"])
                    if (
                        status["worker"]["state"] == "ok"
                        and status["checkpoint"]["sequence"] >= target
                        and status["analysis_sequence"] >= target
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Observer did not analyze sequence {target} within {wait_seconds:g}s"
                        )
                    time.sleep(0.5)
                response = client.get(f"{observer}/api/findings", auth=auth)
                response.raise_for_status()
                findings = [
                    f for f in response.json()["findings"] if f["agent_id"] == case["agent_id"]
                ]
                counts = dict(Counter(f["kind"] for f in findings))
                expected = LIVE_SCENARIOS[name]["expected"]
                case.update(
                    observed_findings=counts,
                    finding_ids=[f["id"] for f in findings],
                    analysis_sequence=status["analysis_sequence"],
                    passed=False,
                )
                if counts != expected:
                    raise RuntimeError(f"{name}: expected {expected}, observed {counts}")
                if not all(
                    f["synthetic"] is True
                    and f["evidence"]
                    and all(
                        ref["sequence"] in case["sequences"]
                        and ref["chain_id"] == status["chain_id"]
                        for ref in f["evidence"]
                    )
                    for f in findings
                ):
                    raise RuntimeError(f"{name}: unexpected evidence or missing synthetic label")
                case["passed"] = True
                print(f"  PASS: {counts or 'no findings (benign control)'}", flush=True)
            report["passed"] = True
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nReport: {destination}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="kind")
    parser.add_argument("--scenario", choices=["all", *LIVE_SCENARIOS], default="all")
    parser.add_argument(
        "--run-id", help="Reuse an ID to verify a prior run without duplicating it."
    )
    args = parser.parse_args()
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", run_id):
        parser.error(
            "--run-id must be 1-40 lowercase letters, digits or hyphens, starting alphanumeric"
        )
    names = list(LIVE_SCENARIOS) if args.scenario == "all" else [args.scenario]
    run(args.cluster, names, run_id)


if __name__ == "__main__":
    main()
