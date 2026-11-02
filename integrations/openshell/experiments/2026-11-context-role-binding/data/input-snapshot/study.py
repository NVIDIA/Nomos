# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prepare 108 logical comparisons, then execute 96 distinct model requests once.

Shared HTTP-only inputs have one measurement, including where conditional labels
differ. No salting, repeated submissions, retries or fabricated independence.
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
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PILOT = ROOT.parent / "2026-11-context-format-pilot"
CONDITIONS = ("baseline", "role_guided", "http_only")
_spec = importlib.util.spec_from_file_location("role_study_helpers", PILOT / "run_pilot.py")
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def source_hashes() -> dict:
    """Identify the exact plan, fixture, catalog, capture and transport sources."""
    return {
        str(path.relative_to(ROOT.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            ROOT / "PLAN.md",
            ROOT / "cases.json",
            ROOT / "questions.json",
            ROOT / "build_cases.py",
            ROOT / "study.py",
            ROOT / "capture_context.py",
            PILOT / "run_pilot.py",
        ]
    }


def prepare(output: Path, captured: Path) -> None:
    """Freeze logical cells and actual-call ordering, deduplicating identical inputs."""
    if output.exists():
        raise ValueError("preparation output must be a new directory")
    cases = json.loads((ROOT / "cases.json").read_text())["cases"]
    catalogs = json.loads((ROOT / "questions.json").read_text())["catalogs"]
    states = json.loads(captured.read_text())["states"]
    assert len(cases) == 36 and set(states) == {c["id"] for c in cases}
    rng = random.Random(2026110203)  # noqa: S311  # nosec B311 - study ordering, not security.
    cells, requests, case_cells = [], {}, {}
    for case in cases:
        enriched = helpers.make_state(case, "compact_json", states[case["id"]])
        assert isinstance(enriched.get("conversation_context"), dict)
        plain = helpers.make_state(case, "http_only", states[case["id"]])
        case_cells[case["id"]] = {}
        for policy in CONDITIONS:
            request = {
                "model": "jev-1.14.0",
                "state": plain if policy == "http_only" else enriched,
                "questions": helpers.question_map(
                    catalogs["baseline" if policy == "http_only" else policy]
                ),
            }
            sha = helpers.digest(request)
            requests.setdefault(sha, request)
            cell = {
                "case_id": case["id"],
                "pair_id": case["pair_id"],
                "cohort": case["cohort"],
                "category": case["category"],
                "variant": case["variant"],
                "policy": policy,
                "expected": case["expected"],
                "expected_rules": case["expected_rules"],
                "request_sha256": sha,
            }
            cells.append(cell)
            case_cells[case["id"]][policy] = cell
    roles = defaultdict(list)
    controls = []
    for case in cases:
        if case["cohort"] == "role":
            roles[case["pair_id"]].append(case)
        else:
            controls.append(case)
    assert len(roles) == 6 and len(controls) == 12
    variants = ("credential_role", "ordinary_role", "different_value", "exemption")
    pair_orders = {}
    for variant in variants:
        sequence = [False] * 3 + [True] * 3
        rng.shuffle(sequence)
        pair_orders[variant] = sequence
    blocks = []
    for index, (family, members) in enumerate(sorted(roles.items())):
        assert set(c["variant"] for c in members) == set(variants)
        items = []
        for case in members:
            order = (
                ("role_guided", "baseline")
                if pair_orders[case["variant"]][index]
                else ("baseline", "role_guided")
            )
            items.append([case_cells[case["id"]][policy] for policy in order])
        singletons = {
            case_cells[c["id"]]["http_only"]["request_sha256"]: case_cells[c["id"]]["http_only"]
            for c in members
        }
        assert len(singletons) == 2
        items.extend([[cell] for cell in singletons.values()])
        rng.shuffle(items)
        blocks.append((family, [cell for item in items for cell in item]))
    permutations = list(itertools.permutations(CONDITIONS))
    for expected in ("allow", "deny"):
        subset = [c for c in controls if c["expected"] == expected]
        assert len(subset) == 6
        rng.shuffle(subset)
        orders = list(permutations)
        rng.shuffle(orders)
        for case, order in zip(subset, orders, strict=True):
            blocks.append((case["id"], [case_cells[case["id"]][p] for p in order]))
    rng.shuffle(blocks)
    represented = defaultdict(list)
    for cell in cells:
        represented[cell["request_sha256"]].append(cell["case_id"])
    call_order = [
        {
            "request_sha256": cell["request_sha256"],
            "policy": cell["policy"],
            "block_id": block_id,
            "represented_case_ids": represented[cell["request_sha256"]],
        }
        for block_id, block_cells in blocks
        for cell in block_cells
    ]
    counts = Counter(call["policy"] for call in call_order)
    assert counts == {"baseline": 36, "role_guided": 36, "http_only": 24}
    assert len(requests) == len(call_order) == 96
    assert {c["request_sha256"] for c in call_order} == set(requests)
    output.mkdir(parents=True)
    for name in ("requests", "responses"):
        (output / name).mkdir()
    for sha, request in requests.items():
        helpers.write_json(output / "requests" / (sha + ".json"), request)
    manifest = {
        "schema_version": 1,
        "study_kind": "conditional credential-role interpretation",
        "model": "jev-1.14.0",
        "endpoint": helpers.ENDPOINT,
        "logical_cases": 36,
        "logical_cells": 108,
        "planned_calls": 96,
        "unique_calls_per_condition": dict(counts),
        "seed": 2026110203,
        "concurrency": 1,
        "timeout_seconds": 8,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_hashes": source_hashes(),
        "captured_states_sha256": hashlib.sha256(captured.read_bytes()).hexdigest(),
        "cache_status": "unknown; exact duplicates share one actual call; no nonces or retries",
        "order_design": (
            "18 randomized blocks: six role families with adjacent R/U pairs "
            "and HTTP singletons; twelve control triads"
        ),
    }
    helpers.write_json(output / "manifest.json", manifest)
    helpers.write_json(output / "matrix.json", cells)
    helpers.write_json(output / "call_order.json", call_order)
    print(
        json.dumps(
            {"cases": len(cases), "comparison_cells": len(cells), "actual_calls": len(call_order)}
        )
    )


def execute(output: Path) -> None:
    """Submit each unique frozen request once, preserving uncertain/failed attempts."""
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_hashes"] == source_hashes(), "source changed after preparation"
    log_path, attempts_path = output / "measurements.jsonl", output / "attempts.jsonl"
    if log_path.exists() or attempts_path.exists():
        raise ValueError("this directory has attempted calls; never silently rerun")
    key = os.environ.get("JEV_API_KEY", "")
    if not key:
        raise ValueError("JEV_API_KEY must be supplied privately")
    call_order = json.loads((output / "call_order.json").read_text())
    measurements = {}
    with log_path.open("x") as log, attempts_path.open("x") as attempts:
        for index, call in enumerate(call_order, 1):
            sha = call["request_sha256"]
            request = json.loads((output / "requests" / (sha + ".json")).read_text())
            assert helpers.digest(request) == sha
            attempts.write(json.dumps({"event": "started", **call}) + "\n")
            attempts.flush()
            os.fsync(attempts.fileno())
            measurement, response = helpers.assess(request, key, manifest["timeout_seconds"])
            measurement["measurement_id"] = f"measurement-{index:03d}"
            measurement["policy"] = call["policy"]
            measurements[sha] = measurement
            helpers.write_json(output / "responses" / (sha + ".json"), response)
            log.write(json.dumps(measurement, allow_nan=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            attempts.write(
                json.dumps(
                    {"event": "finished", "request_sha256": sha, "status": measurement["status"]}
                )
                + "\n"
            )
            attempts.flush()
            os.fsync(attempts.fileno())
            print(index, "/", len(call_order), measurement["status"], flush=True)
            if measurement["status"] != "ok":
                break
    cells = json.loads((output / "matrix.json").read_text())
    for cell in cells:
        measurement = measurements.get(cell["request_sha256"])
        cell["measurement"] = measurement
        if measurement and measurement["status"] == "ok":
            cell["decision"] = (
                "deny" if any(v >= 0.7 for v in measurement["scores"].values()) else "allow"
            )
            cell["matches_expected"] = cell["decision"] == cell["expected"]
    helpers.write_json(
        output / "results.json",
        {"manifest": manifest, "cells": cells, "unique_measurements": list(measurements.values())},
    )


def main() -> None:
    """Keep preparation offline; execute only the explicitly supplied frozen directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
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
