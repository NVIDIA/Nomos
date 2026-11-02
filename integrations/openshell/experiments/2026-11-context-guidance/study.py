# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Freeze a 288-request three-condition study, then execute the reviewed frozen requests once.

Preparation is offline. Execution requires JEV_API_KEY, never writes headers,
never retries, and refuses previously attempted output directories.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PILOT = ROOT.parent / "2026-11-context-format-pilot"
CONDITIONS = ("baseline", "context_aware", "http_only")
_spec = importlib.util.spec_from_file_location("context_pilot_helpers", PILOT / "run_pilot.py")
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def source_hashes() -> dict:
    """Identify every source controlling fixture contents or model requests."""
    return {
        str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            ROOT / "PLAN.md",
            ROOT / "cases.json",
            ROOT / "questions.json",
            ROOT / "build_cases.py",
            ROOT / "additional_cases.py",
            ROOT / "study.py",
            ROOT / "capture_context.py",
            PILOT / "run_pilot.py",
        ]
    }


def prepare(output: Path, captured: Path) -> None:
    """Freeze three matched conditions with order balanced within content strata."""
    if output.exists():
        raise ValueError("preparation directory must be new")
    cases = json.loads((ROOT / "cases.json").read_text())["cases"]
    catalogs = json.loads((ROOT / "questions.json").read_text())["catalogs"]
    states = json.loads(captured.read_text())["states"]
    assert len(cases) == 96 and set(states) == {c["id"] for c in cases}
    rng = random.Random(2026110202)  # noqa: S311  # nosec B311 - study order, not credentials.
    rng.shuffle(cases)
    permutations = list(itertools.permutations(CONDITIONS))
    orders = {}
    for cohort in ("existing48", "new48"):
        for expected in ("allow", "deny"):
            sequence = permutations * 4
            rng.shuffle(sequence)
            orders[(cohort, expected)] = sequence
    cells, requests = [], {}
    for case in cases:
        context_state = helpers.make_state(case, "compact_json", states[case["id"]])
        assert isinstance(context_state.get("conversation_context"), dict), "context missing"
        http_state = helpers.make_state(case, "http_only", states[case["id"]])
        order = orders[(case["cohort"], case["expected"])].pop()
        for policy in order:
            request = {
                "model": "jev-1.14.0",
                "state": http_state if policy == "http_only" else context_state,
                "questions": helpers.question_map(
                    catalogs["baseline" if policy == "http_only" else policy]
                ),
            }
            sha = helpers.digest(request)
            assert sha not in requests, "duplicate complete request; fix dataset before measurement"
            requests[sha] = request
            cells.append(
                {
                    "case_id": case["id"],
                    "pair_id": case["pair_id"],
                    "category": case["category"],
                    "cohort": case["cohort"],
                    "policy": policy,
                    "format": "http_only" if policy == "http_only" else "compact_json",
                    "expected": case["expected"],
                    "expected_rules": case["expected_rules"],
                    "request_sha256": sha,
                }
            )
    assert not any(orders.values()), "cohort/label sizes do not support balanced order"
    output.mkdir(parents=True)
    (output / "requests").mkdir()
    (output / "responses").mkdir()
    for sha, request in requests.items():
        helpers.write_json(output / "requests" / (sha + ".json"), request)
    manifest = {
        "schema_version": 1,
        "study_kind": "three-condition context/instruction study with expanded sampling",
        "model": "jev-1.14.0",
        "endpoint": helpers.ENDPOINT,
        "cases": 96,
        "scenario_pairs": 48,
        "planned_calls": 288,
        "conditions": list(CONDITIONS),
        "orders": [{"order": list(order), "cases": 16} for order in permutations],
        "strata": "existing/new content x expected allow/deny; each order four times per stratum",
        "seed": 2026110202,
        "timeout_seconds": 8,
        "concurrency": 1,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_hashes": source_hashes(),
        "captured_states_sha256": hashlib.sha256(captured.read_bytes()).hexdigest(),
        "cache_status": "unknown; no documented disable control; no nonces or retries",
    }
    helpers.write_json(output / "manifest.json", manifest)
    helpers.write_json(output / "matrix.json", cells)
    print(json.dumps({"prepared_cases": 96, "prepared_calls": len(requests)}))


def execute(output: Path) -> None:
    "Run each frozen request once and preserve failures rather than silently retry."
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_hashes"] == source_hashes(), "sources changed after freeze"
    log_path = output / "measurements.jsonl"
    attempts_path = output / "attempts.jsonl"
    if log_path.exists() or attempts_path.exists():
        raise ValueError("this run already has attempts; do not rerun or overwrite")
    key = os.environ.get("JEV_API_KEY", "")
    if not key:
        raise ValueError("JEV_API_KEY must be set privately")
    cells = json.loads((output / "matrix.json").read_text())
    measurements = {}
    with log_path.open("x") as log, attempts_path.open("x") as attempts:
        for index, cell in enumerate(cells, 1):
            sha = cell["request_sha256"]
            request = json.loads((output / "requests" / (sha + ".json")).read_text())
            assert helpers.digest(request) == sha, "frozen request changed"
            attempts.write(json.dumps({"event": "started", "request_sha256": sha}) + "\n")
            attempts.flush()
            os.fsync(attempts.fileno())
            result, response = helpers.assess(request, key, manifest["timeout_seconds"])
            result["measurement_id"] = f"measurement-{index:03d}"
            measurements[sha] = result
            helpers.write_json(output / "responses" / (sha + ".json"), response)
            log.write(json.dumps(result, allow_nan=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            attempts.write(
                json.dumps({"event": "finished", "request_sha256": sha, "status": result["status"]})
                + "\n"
            )
            attempts.flush()
            os.fsync(attempts.fileno())
            print(index, "/", len(cells), result["status"], flush=True)
            if result["status"] != "ok":
                break
    output_cells = []
    for cell in cells:
        m = measurements.get(cell["request_sha256"])
        item = {**cell, "measurement": m}
        if m and m["status"] == "ok":
            item["decision"] = "deny" if any(v >= 0.7 for v in m["scores"].values()) else "allow"
            item["matches_expected"] = item["decision"] == cell["expected"]
        output_cells.append(item)
    helpers.write_json(
        output / "results.json",
        {
            "manifest": manifest,
            "cells": output_cells,
            "unique_measurements": list(measurements.values()),
        },
    )


def main() -> None:
    """Choose offline preparation or explicit execution of its immutable matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--captured-states", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        execute(args.output)
    elif args.captured_states:
        prepare(args.output, args.captured_states)
    else:
        parser.error("preparation requires --captured-states")


if __name__ == "__main__":
    main()
