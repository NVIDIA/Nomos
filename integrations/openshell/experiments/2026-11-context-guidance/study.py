# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Freeze a 96-request paired study, then execute the reviewed frozen requests once.

Preparation is offline. Execution requires JEV_API_KEY, never writes headers,
never retries, and refuses previously attempted output directories.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PILOT = ROOT.parent / "2026-11-context-format-pilot"
_spec = importlib.util.spec_from_file_location("context_pilot_helpers", PILOT / "run_pilot.py")
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def source_hashes() -> dict:
    """Identify every source controlling fixture contents or model requests."""
    return {
        str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            ROOT / "cases.json",
            ROOT / "questions.json",
            ROOT / "build_cases.py",
            ROOT / "study.py",
            ROOT / "capture_context.py",
            PILOT / "run_pilot.py",
        ]
    }


def prepare(output: Path, captured: Path) -> None:
    """Save identical states per condition and exactly balanced randomized pair order."""
    if output.exists():
        raise ValueError("preparation directory must be new")
    cases = json.loads((ROOT / "cases.json").read_text())["cases"]
    catalogs = json.loads((ROOT / "questions.json").read_text())["catalogs"]
    states = json.loads(captured.read_text())["states"]
    assert len(cases) == 48 and set(states) == {c["id"] for c in cases}
    rng = random.Random(20261102)  # noqa: S311  # nosec B311 - experiment order, not credentials.
    rng.shuffle(cases)
    orders = [False] * 24 + [True] * 24
    rng.shuffle(orders)
    cells, requests = [], {}
    for case, candidate_first in zip(cases, orders, strict=True):
        state = helpers.make_state(case, "compact_json", states[case["id"]])
        assert isinstance(state.get("conversation_context"), dict), "context missing after capture"
        for policy in (
            ["context_aware", "baseline"] if candidate_first else ["baseline", "context_aware"]
        ):
            request = {
                "model": "jev-1.14.0",
                "state": state,
                "questions": helpers.question_map(catalogs[policy]),
            }
            sha = helpers.digest(request)
            assert sha not in requests, "duplicate complete request; fix dataset before measurement"
            requests[sha] = request
            cells.append(
                {
                    "case_id": case["id"],
                    "pair_id": case["pair_id"],
                    "category": case["category"],
                    "policy": policy,
                    "format": "compact_json",
                    "expected": case["expected"],
                    "expected_rules": case["expected_rules"],
                    "request_sha256": sha,
                }
            )
    output.mkdir(parents=True)
    (output / "requests").mkdir()
    (output / "responses").mkdir()
    for sha, request in requests.items():
        helpers.write_json(output / "requests" / (sha + ".json"), request)
    manifest = {
        "schema_version": 1,
        "study_kind": "paired short-context-instruction component/model study",
        "model": "jev-1.14.0",
        "endpoint": helpers.ENDPOINT,
        "cases": 48,
        "scenario_pairs": 24,
        "planned_calls": 96,
        "control_first": 24,
        "candidate_first": 24,
        "seed": 20261102,
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
    print(json.dumps({"prepared_cases": 48, "prepared_calls": len(requests)}))


def execute(output: Path) -> None:
    """Run each frozen request once and preserve failures rather than silently retry."""
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
