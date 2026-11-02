# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate a standalone role-study report without model calls or plotting packages.

Committed SVGs are generated separately with Matplotlib; provenance prevents
accidentally using figures from a different measurement file.
"""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "role_report_helpers", ROOT.parent / "2026-11-context-guidance/build_report.py"
)
assert _spec is not None and _spec.loader is not None
view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(view)
LABELS = {"baseline": "Original + context", "role_guided": "U + context", "http_only": "HTTP only"}
VARIANTS = {
    "credential_role": "Credential A correctly denied",
    "ordinary_role": "Ordinary A correctly allowed",
    "different_value": "Ordinary B allowed despite credential A in history",
    "exemption": "Credential A denied despite exemption claim",
}
STATUS = (
    "Completed once on 2 November 2026: **96 unique API calls succeeded**, "
    "supporting 108 logical comparisons. No retries or post-result tuning."
)


def performance(measurements: list[dict]) -> dict:
    """Count resources per actual call rather than duplicated logical references."""
    successful = [m for m in measurements if m["status"] == "ok"]
    times = [m["latency_ms"] for m in successful]
    return {
        "actual_calls": len(measurements),
        "successful_calls": len(successful),
        "failures": len(measurements) - len(successful),
        "mean_ms": statistics.mean(times) if times else None,
        "median_ms": statistics.median(times) if times else None,
        "stdev_ms": statistics.stdev(times) if len(times) > 1 else None,
        "input_tokens": sum(m["usage"]["input_tokens"] for m in successful),
        "output_tokens": sum(m["usage"]["output_tokens"] for m in successful),
    }


def pair_summary(cases: list[dict], cells: list[dict]) -> dict:
    """Separate full binary pair success from the diagnostic ordering of scores."""
    families = defaultdict(dict)
    by_case = defaultdict(dict)
    for cell in cells:
        by_case[cell["case_id"]][cell["policy"]] = cell
    for case in cases:
        if case["cohort"] == "role":
            families[case["pair_id"]][case["variant"]] = case
    summary = {}
    for policy in LABELS:
        both, ranked = 0, 0
        for family in families.values():
            a = by_case[family["credential_role"]["id"]][policy]
            b = by_case[family["ordinary_role"]["id"]][policy]
            both += a["decision"] == "deny" and b["decision"] == "allow"
            ranked += (
                a["measurement"]["scores"]["credential_disclosure"]
                > b["measurement"]["scores"]["credential_disclosure"]
            )
        summary[policy] = {
            "both_core_decisions_correct": both,
            "credential_score_above_reference": ranked,
            "families": len(families),
        }
    return summary


def embedded_json(value: object) -> str:
    """Keep script-closing fixture text inert without altering downloaded JSON values."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def main() -> None:
    """Build narrative, conditional-case inspector and offline artifact downloads."""
    result_path = ROOT / "data/measured-v1/results.json"
    result = json.loads(result_path.read_text())
    cases = json.loads((ROOT / "cases.json").read_text())["cases"]
    case_map = {c["id"]: c for c in cases}
    cells = result["cells"]
    measurements = result["unique_measurements"]
    assert len(cases) == 36 and len(cells) == 108 and len(measurements) == 96
    assert all(m["status"] == "ok" for m in measurements), (
        "Inspect incomplete/failed evidence separately"
    )
    proof = json.loads((ROOT / "figures/provenance.json").read_text())
    assert proof["results_sha256"] == hashlib.sha256(result_path.read_bytes()).hexdigest(), (
        "Regenerate figures"
    )
    assert proof["cases_sha256"] == hashlib.sha256((ROOT / "cases.json").read_bytes()).hexdigest()
    by_case = {c["id"]: {v["policy"]: v for v in cells if v["case_id"] == c["id"]} for c in cases}
    pairs = pair_summary(cases, cells)
    stats = {p: performance([m for m in measurements if m["policy"] == p]) for p in LABELS}
    pair_rows = [
        [
            "Both decisions correct",
            *[f"{pairs[p]['both_core_decisions_correct']}/6" for p in LABELS],
        ],
        [
            "Credential score higher than reference score",
            *[f"{pairs[p]['credential_score_above_reference']}/6" for p in LABELS],
        ],
    ]
    pair_text = view.table(["Metric", *LABELS.values()], pair_rows)
    pair_text += (
        "\n\nBoth context conditions ordered the two role scores "
        "correctly in all six\nfamilies. U nevertheless missed two "
        "denials at 0.67, below the unchanged 0.70\nthreshold. Ranking"
        " is diagnostic; it is not a successful enforcement decision."
        "\nHTTP-only ties each pair because it shares exactly one "
        "request and response.\n\n"
    )
    family_rows = []
    families = list(dict.fromkeys(c["pair_id"] for c in cases if c["cohort"] == "role"))
    for family in families:
        members = {c["variant"]: c for c in cases if c["pair_id"] == family}
        row = [family.replace("-", " ")]
        for p in LABELS:
            a = by_case[members["credential_role"]["id"]][p]
            b = by_case[members["ordinary_role"]["id"]][p]
            a_score = a["measurement"]["scores"]["credential_disclosure"]
            b_score = b["measurement"]["scores"]["credential_disclosure"]
            row.append(f"{a_score:.2f} ({a['decision']}) / {b_score:.2f} ({b['decision']})")
        family_rows.append(row)
    pair_text += (
        "Each cell below is **credential-role score (decision) / "
        "ordinary-role score (decision)**.\n\n"
    )
    pair_text += view.table(["Role family", *LABELS.values()], family_rows)
    role_rows = [
        [
            label,
            *[
                str(
                    sum(
                        c["matches_expected"]
                        for c in cells
                        if c["variant"] == variant and c["policy"] == p
                    )
                )
                + "/6"
                for p in LABELS
            ],
        ]
        for variant, label in VARIANTS.items()
    ]
    role_rows.append(
        [
            "All role cells (logical, conditional)",
            *[
                str(
                    sum(
                        c["matches_expected"]
                        for c in cells
                        if c["cohort"] == "role" and c["policy"] == p
                    )
                )
                + "/24"
                for p in LABELS
            ],
        ]
    )
    controls = {
        p: [c for c in cells if c["cohort"] == "control" and c["policy"] == p] for p in LABELS
    }
    control_rows = []
    for expected, description in [
        ("allow", "Benign controls correctly allowed"),
        ("deny", "Harmful controls correctly denied"),
    ]:
        control_rows.append(
            [
                description,
                *[
                    str(
                        sum(c["matches_expected"] for c in controls[p] if c["expected"] == expected)
                    )
                    + "/6"
                    for p in LABELS
                ],
            ]
        )
    control_rows.append(
        [
            "All controls correct",
            *[str(sum(c["matches_expected"] for c in controls[p])) + "/12" for p in LABELS],
        ]
    )
    perf_rows = []
    for p, st in stats.items():
        perf_rows.append(
            [
                LABELS[p],
                st["actual_calls"],
                f"{st['mean_ms']:.1f} ms",
                f"{st['median_ms']:.1f} ms",
                f"{st['stdev_ms']:.1f} ms",
                f"{st['input_tokens'] / st['successful_calls']:.1f}",
                st["input_tokens"],
                st["output_tokens"],
            ]
        )
    performance_text = view.table(
        [
            "Condition",
            "Actual calls",
            "Mean",
            "Median",
            "Time SD",
            "Mean input tokens",
            "Total input",
            "Total output",
        ],
        perf_rows,
    )
    performance_text += (
        "\n\nFor a matched input mixture, the twelve controls provide "
        "twelve unique calls\nper condition. These are model-HTTP "
        "times, not added OpenShell latency:\n\n"
    )
    matched_rows = []
    for p in LABELS:
        shas = {c["request_sha256"] for c in controls[p]}
        st = performance([m for m in measurements if m["request_sha256"] in shas])
        matched_rows.append(
            [LABELS[p], st["actual_calls"], f"{st['mean_ms']:.1f} ms", f"{st['median_ms']:.1f} ms"]
        )
    performance_text += view.table(
        ["Control-only condition", "Unique calls", "Mean", "Median"], matched_rows
    )
    errors = [
        cid
        for cid, items in by_case.items()
        if not all(c["matches_expected"] for c in items.values())
    ]
    error_rows = []
    for cid in errors:
        row = [f"`{cid}`", case_map[cid]["expected"]]
        for p in LABELS:
            cell = by_case[cid][p]
            scores = cell["measurement"]["scores"]
            row.append(
                f"{cell['decision']} ({scores['credential_disclosure']:.2f} / "
                f"{scores['confidential_disclosure']:.2f})"
            )
        error_rows.append(row)
    interpretation = "\n".join(
        line
        for line in (ROOT / "interpretation.md").read_text().splitlines()
        if not line.startswith("<!-- SPDX-")
    ).strip()
    total_input = sum(v["input_tokens"] for v in stats.values())
    total_output = sum(v["output_tokens"] for v in stats.values())
    replacements = {
        "PAIR_TABLE": pair_text,
        "ROLE_TABLE": view.table(["Role test", *LABELS.values()], role_rows),
        "CONTROL_TABLE": view.table(["Control test", *LABELS.values()], control_rows),
        "PERFORMANCE_TABLE": performance_text,
        "ERROR_TABLE": view.table(["Case", "Conditional expected", *LABELS.values()], error_rows),
        "INTERPRETATION": interpretation,
        "ERROR_COUNT": str(len(errors)),
        "TOKEN_TOTALS": f"**{total_input:,} input / {total_output:,} output tokens**",
        "CALL_STATUS": STATUS,
    }
    doc = (ROOT / "report-template.md").read_text()
    for key, value in replacements.items():
        doc = doc.replace("@@" + key + "@@", value)
    assert "@@" not in doc
    (ROOT / "RESULTS.md").write_text(doc)
    figures = {p.name: p.read_text() for p in (ROOT / "figures").glob("*.svg")}
    downloads = {}
    inspector = []
    usage = Counter(c["request_sha256"] for c in cells)
    for case in cases:
        cid = case["id"]
        rows = []
        for p in LABELS:
            cell = by_case[cid][p]
            sha = cell["request_sha256"]
            m = cell["measurement"]
            scores = m["scores"]
            if sha not in downloads:
                downloads[sha] = {
                    kind: json.loads(
                        (ROOT / "data/measured-v1" / folder / (sha + ".json")).read_text()
                    )
                    for kind, folder in [("request", "requests"), ("response", "responses")]
                }
            cls = "correct" if cell["matches_expected"] else "incorrect"
            status = "matches label" if cell["matches_expected"] else "mismatch"
            buttons = "".join(
                f'<button data-sha="{sha}" data-kind="{kind}">{kind} JSON</button>'
                for kind in ["request", "response"]
            )
            shared = f" · shared by {usage[sha]} cases" if usage[sha] > 1 else ""
            rows.append(
                f"<tr>"
                f"<td>{LABELS[p]}{shared}</td>"
                f'<td class="{cls}">{cell["decision"]} — {status}</td>'
                f"<td>{scores['credential_disclosure']:.2f}</td>"
                f"<td>{scores['confidential_disclosure']:.2f}</td>"
                f"<td>{buttons}</td>"
                f"</tr>"
            )
        state = downloads[by_case[cid]["baseline"]["request_sha256"]]["request"]["state"]
        inspector.append(
            (
                f'<details data-case="{cid}" '
                f'data-cohort="{case["cohort"]}" '
                f'data-category="{case["variant"]}" '
                f'data-error="{str(cid in errors).lower()}"><summary>'
            )
            + html.escape(cid + " · " + case["title"] + " · expected " + case["expected"])
            + "</summary><p>"
            + html.escape(case["label_basis"])
            + (
                "</p><table><tr><th>Condition</th><th>Decision</th><th>Creden"
                "tial score</th><th>Confidentiality "
                "score</th><th>Artifacts</th></tr>"
            )
            + "".join(rows)
            + (
                "</table><p>Green/red compares conditional fixture labels; "
                "shared HTTP-only outcomes are one assessment, not "
                "independent role judgments.</p><h3>Shared HTTP evidence and "
                "compact context</h3>"
            )
            + view.readable(state)
            + "</details>"
        )
    options = '<option value="all">All role variants and controls</option>' + "".join(
        '<option value="' + k + '">' + html.escape(v) + "</option>"
        for k, v in [*VARIANTS.items(), ("control", "HTTP-decidable controls")]
    )
    page = (
        (
            '<!doctype html><html lang="en"><head><meta '
            'charset="utf-8"><meta name="viewport" content="width=device-'
            'width,initial-scale=1"><title>Context-needed role '
            "interpretation</title><style>"
        )
        + (ROOT / "report.css").read_text()
        + (
            '</style></head><body><main><nav><a href="#finding-and-'
            'interpretation">Findings</a> · <a href="#a-worked-family-'
            'identical-outgoing-a-different-reported-role">Example</a> · '
            '<a href="#table-1-core-role-pair-discrimination">Role '
            'results</a> · <a href="#case-inspector">All 36 '
            "cases</a></nav>"
        )
        + view.render_markdown(doc, figures)
        + (
            '<h2 id="case-inspector">Inspect all 36 logical '
            "cases</h2><p>Green = matches conditional label; red = "
            "mismatch. HTTP-only sharing reflects missing background "
            "information, not independent model errors.</p><label>Variant"
            ' <select id="category">'
        )
        + options
        + (
            '</select></label> <label>Cohort <select id="cohort"><option '
            'value="all">All 36 cases</option><option value="role">Role '
            'cohort 24</option><option value="control">Controls '
            '12</option></select></label> <label><input type="checkbox" '
            'id="only-errors">Only cases with any mismatch</label><p '
            'id="visible-count"></p>'
        )
        + "".join(inspector)
        + '<h2 id="exact-questions">Exact questions</h2>'
        + view.readable(json.loads((ROOT / "questions.json").read_text())["catalogs"])
        + '</main><script id="downloads" type="application/json">'
        + embedded_json(downloads)
        + "</script><script>"
        + (ROOT / "report.js").read_text()
        + "</script></body></html>\n"
    )
    (ROOT / "report.html").write_text("\n".join(line.rstrip() for line in page.splitlines()) + "\n")
    (ROOT / "analysis.json").write_text(
        json.dumps(
            {
                "core_pairs": pairs,
                "resources_unique_calls": stats,
                "cases_with_any_mismatch": errors,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "cases": 36,
                "logical_cells": 108,
                "unique_calls": len(measurements),
                "report_bytes": len(page.encode()),
            }
        )
    )


if __name__ == "__main__":
    main()
