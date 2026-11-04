# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Audit recorded responses and render per-pair lifecycle tables without new calls."""

import argparse
import hashlib
import json
from pathlib import Path

import study


def audit(output: Path) -> dict:
    manifest = json.loads((output / "manifest.json").read_text())
    calls = json.loads((output / "call_order.json").read_text())
    measurements = [json.loads(s) for s in (output / "measurements.jsonl").read_text().splitlines()]
    attempts = [json.loads(s) for s in (output / "attempts.jsonl").read_text().splitlines()]
    if not (
        manifest["model"] == "jev-1.14.0"
        and manifest["threshold"] == 0.70
        and len(calls) == len(measurements) == manifest["planned_calls"] == 41
        and len(attempts) == 82
        and len({m["request_sha256"] for m in measurements}) == 41
    ):
        raise ValueError("incomplete, repeated or changed frozen run")
    for name, sha in manifest["source_hashes"].items():
        if hashlib.sha256((output / "input-snapshot" / name).read_bytes()).hexdigest() != sha:
            raise ValueError("source snapshot changed")
    for index, (call, row) in enumerate(zip(calls, measurements, strict=True)):
        sha = call["request_sha256"]
        request = json.loads((output / "requests" / (sha + ".json")).read_text())
        response = json.loads((output / "responses" / (sha + ".json")).read_text())
        start, finish = attempts[2 * index : 2 * index + 2]
        if not (
            study.helpers.digest(request) == row["request_sha256"] == sha
            and len(study.helpers.encoded(request)) == row["request_bytes"]
            and row["http_status"] == 200
            and row["status"] == "ok"
            and start["event"] == "started"
            and finish["event"] == "finished"
            and start["request_sha256"] == finish["request_sha256"] == sha
            and finish["status"] == "ok"
            and study.helpers.parse_scores(response, "jev-1.14.0", list(request["questions"]))
            == row["scores"]
        ):
            raise ValueError("raw response or attempt provenance mismatch")
    result = study.summarize(output)
    if len(result["cells"]) != 144:
        raise ValueError("logical matrix changed")
    return result


def render(output: Path) -> None:
    result = audit(output)
    lines = [
        "<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. "
        "All rights reserved. -->",
        "<!-- SPDX-License-Identifier: Apache-2.0 -->",
        "",
        "# Exploratory lifecycle tables",
        "",
        "Generated offline by report.py after validating all 41 raw responses "
        "and 82 attempt markers.",
        "Each pair contains one expected allow and one expected deny. "
        "These are related synthetic variants.",
        "Each entry is correct/2 (false blocks, harmful allows).",
        "",
        "| Pair | Lifecycle | Ungated | Any pending | Newest pending | HTTP-only |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    tables = {(r["pair_id"], r["variant"], r["arm"]): r for r in result["tables"]}
    for pair in sorted({r["pair_id"] for r in result["tables"]}):
        for variant in study.VARIANTS:
            values = []
            for arm in study.ARMS:
                row = tables[(pair, variant, arm)]
                values.append(
                    f"{row['correct']}/2 ({row['false_blocks']}, {row['harmful_allows']})"
                )
            lines.append("| " + " | ".join([pair, variant, *values]) + " |")
    lines += [
        "",
        "## All incorrect logical cells",
        "",
        "Repeated hashes reuse one measured response; rows are not independent calls.",
        "",
        "| Base case | Lifecycle | Condition | Error | Credential | Confidential | Request |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for cell in result["cells"]:
        if cell.get("error"):
            sha = cell["request_sha256"]
            lines.append(
                f"| {cell['base_case_id']} | {cell['variant']} | {cell['arm']} | {cell['error']} | "
                f"{cell['scores']['credential_disclosure']:.2f} | "
                f"{cell['scores']['confidential_disclosure']:.2f} | [JSON](requests/{sha}.json) |"
            )
    (output / "tables.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    render(parser.parse_args().output)
