# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate standalone research figures with optional permissive Matplotlib tooling.

The HTML/Markdown builder uses committed figures and only the standard library.
This script is needed only to regenerate figures, never to make model calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parent
COLORS = {"baseline": "#22608d", "role_guided": "#bf671f", "http_only": "#65717b"}


def main() -> None:
    """Plot role scores and unique-call input usage, preserving explicit denominators."""
    path = ROOT / "data/measured-v1/results.json"
    result = json.loads(path.read_text())
    cases = json.loads((ROOT / "cases.json").read_text())["cases"]
    cells = result["cells"]
    by_case = {c["id"]: {v["policy"]: v for v in cells if v["case_id"] == c["id"]} for c in cases}
    families = list(dict.fromkeys(c["pair_id"] for c in cases if c["cohort"] == "role"))
    figures = ROOT / "figures"
    figures.mkdir(exist_ok=True)
    plt.rcParams.update(
        {
            "svg.fonttype": "none",
            "svg.hashsalt": "nomos-role-study-v1",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig, ax = plt.subplots(figsize=(11, 6))
    for index, family in enumerate(families):
        members = {c["variant"]: c for c in cases if c["pair_id"] == family}
        for policy, offset in [("baseline", -0.16), ("role_guided", 0.16)]:
            secret = by_case[members["credential_role"]["id"]][policy]["measurement"]["scores"][
                "credential_disclosure"
            ]
            ordinary = by_case[members["ordinary_role"]["id"]][policy]["measurement"]["scores"][
                "credential_disclosure"
            ]
            y = index + offset
            ax.plot([ordinary, secret], [y, y], color=COLORS[policy], alpha=0.35, lw=2)
            ax.scatter([secret], [y], color=COLORS[policy], s=48, zorder=3)
            ax.scatter(
                [ordinary], [y], facecolors="white", edgecolors=COLORS[policy], s=48, zorder=3
            )
        shared = by_case[members["credential_role"]["id"]]["http_only"]["measurement"]["scores"][
            "credential_disclosure"
        ]
        ax.scatter([shared], [index], marker="D", color=COLORS["http_only"], s=35, zorder=3)
    ax.axvline(0.7, color="#414141", ls="--", lw=1)
    ax.text(0.705, -0.65, "Denial threshold 0.70", fontsize=9)
    ax.set_yticks(
        range(6),
        [
            "Client token / inventory",
            "Runner token / artifact",
            "Mail password / shipment",
            "Webhook secret / document",
            "Login password / ticket",
            "Storage secret / batch",
        ],
    )
    ax.invert_yaxis()
    ax.set_xlim(-0.03, 1.03)
    ax.set_xlabel("Credential-disclosure score (not calibrated for this workload)")
    ax.grid(axis="x", alpha=0.15)
    fig.suptitle("Figure 1. Same outgoing value, different reported role evidence", fontsize=14)
    handles = [
        Line2D(
            [],
            [],
            marker="o",
            ls="",
            color=COLORS[p],
            markerfacecolor=COLORS[p] if role == "credential" else "white",
            label=label + ": " + role,
        )
        for p, label in [("baseline", "Original"), ("role_guided", "U")]
        for role in ["credential", "ordinary reference"]
    ]
    handles.append(
        Line2D(
            [],
            [],
            marker="D",
            ls="",
            color=COLORS["http_only"],
            label="HTTP only: identical input, shared score",
        )
    )
    fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.subplots_adjust(left=0.24, bottom=0.23, right=0.97, top=0.86)
    fig.savefig(figures / "role-scores.svg", metadata={"Date": None})
    plt.close(fig)
    policies = ["baseline", "role_guided", "http_only"]
    measurements = result["unique_measurements"]
    ns = [sum(m["policy"] == p for m in measurements) for p in policies]
    means = [
        sum(m["usage"]["input_tokens"] for m in measurements if m["policy"] == p) / n
        for p, n in zip(policies, ns, strict=True)
    ]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    rectangles = ax.bar(
        [
            "Original + context\n36 unique calls",
            "U + context\n36 unique calls",
            "HTTP only\n24 unique calls",
        ],
        means,
        color=[COLORS[p] for p in policies],
    )
    ax.bar_label(rectangles, labels=[f"{value:.1f}" for value in means], padding=5)
    ax.set_ylim(0, max(means) * 1.2)
    ax.set_ylabel("Mean provider-reported input tokens")
    ax.set_title("Figure 2. Input usage over actual unique assessments")
    ax.grid(axis="y", alpha=0.15)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(figures / "input-tokens.svg", metadata={"Date": None})
    plt.close(fig)
    # Normalize SVG formatting to the repository whitespace convention.
    for svg in figures.glob("*.svg"):
        svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    proof = {
        "results_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256((ROOT / "cases.json").read_bytes()).hexdigest(),
        "matplotlib_version": matplotlib.__version__,
        "license": "Matplotlib PSF-based permissive license; optional figure tool only",
    }
    (figures / "provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
    print("Generated two standalone SVG figures from preserved measurements.")


if __name__ == "__main__":
    main()
