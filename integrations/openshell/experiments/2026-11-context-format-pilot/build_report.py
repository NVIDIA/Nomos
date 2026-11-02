#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build a standalone, evidence-backed HTML report without external dependencies.

Logical case agreement includes shared baseline assessments. Performance and
usage summaries deduplicate actual measurements within each round/condition;
rounds remain separate because the instruction revision is a deliberate change.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORMATS = ("http_only", "full_json", "compact_json", "prose")


def summarize(results: dict) -> list[dict]:
    """Keep case outcomes and unique measured-call statistics on distinct denominators."""
    summaries = []
    policies = list(dict.fromkeys(cell["policy"] for cell in results["cells"]))
    for policy in policies:
        for format_name in FORMATS:
            cells = [
                cell
                for cell in results["cells"]
                if cell["policy"] == policy and cell["format"] == format_name
            ]
            if not cells:
                continue
            measurements = {
                cell["measurement"]["measurement_id"]: cell["measurement"]
                for cell in cells
                if cell.get("measurement")
            }
            successful = [value for value in measurements.values() if value["status"] == "ok"]
            latency = [value["latency_ms"] for value in successful]
            usage = [value["usage"] for value in successful if isinstance(value.get("usage"), dict)]
            summaries.append(
                {
                    "policy": policy,
                    "format": format_name,
                    "cases": len(cells),
                    "agreement": sum(cell.get("matches_expected") is True for cell in cells),
                    "false_allows": sum(
                        cell.get("decision") == "allow" and cell["expected"] == "deny"
                        for cell in cells
                    ),
                    "false_blocks": sum(
                        cell.get("decision") == "deny" and cell["expected"] == "allow"
                        for cell in cells
                    ),
                    "assessment_failures": sum(
                        value["status"] != "ok" for value in measurements.values()
                    ),
                    "actual_calls": len(measurements),
                    "mean_latency_ms": statistics.mean(latency) if latency else None,
                    "median_latency_ms": statistics.median(latency) if latency else None,
                    "stdev_latency_ms": statistics.stdev(latency) if len(latency) > 1 else None,
                    "mean_input_tokens": statistics.mean(value["input_tokens"] for value in usage)
                    if usage
                    else None,
                    "mean_output_tokens": statistics.mean(value["output_tokens"] for value in usage)
                    if usage
                    else None,
                }
            )
    return summaries


def load_run(path: Path, label: str) -> dict:
    """Embed exact model requests, response evidence and derived summaries for one round."""
    results = json.loads(path.read_text())
    requests, responses = {}, {}
    for value in results["unique_measurements"]:
        sha = value["request_sha256"]
        requests[sha] = json.loads((path.parent / "requests" / f"{sha}.json").read_text())
        responses[sha] = json.loads((path.parent / "responses" / f"{sha}.json").read_text())
    return {
        "label": label,
        "results": results,
        "summaries": summarize(results),
        "requests": requests,
        "responses": responses,
    }


def render(data: dict) -> str:
    """Include all data locally and escape closing script tags in arbitrary fixture text."""
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    return TEMPLATE.replace("__STUDY_DATA__", encoded)


def main() -> None:
    """Keep the frozen initial run and diagnostic instruction iteration visibly separate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial", type=Path, required=True)
    parser.add_argument("--iteration", type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [load_run(args.initial, "Initial: existing and expanded instructions")]
    if args.iteration:
        runs.append(
            load_run(args.iteration, "Diagnostic iteration: revised confidentiality instruction")
        )
    data = {"cases": json.loads(args.cases.read_text())["cases"], "runs": runs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data))
    print(
        json.dumps(
            {"report": str(args.output), "runs": len(runs), "bytes": args.output.stat().st_size}
        )
    )


TEMPLATE = (ROOT / "report-template.html").read_text()


if __name__ == "__main__":
    main()
