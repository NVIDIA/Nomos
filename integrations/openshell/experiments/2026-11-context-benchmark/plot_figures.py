# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate labeled, static study figures using Matplotlib, without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ARMS = ("original_context", "guided_context", "without_council", "http_only")
LABELS = ("Original + context", "U + same context", "Without Council", "HTTP only")
COLORS = ("#2463a6", "#a54f87", "#b47820", "#53776a")


def plot(data: Path, output: Path) -> None:
    """Keep figure provenance tied to one completed results file."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import ListedColormap

    results_path = data / "results.json"
    results = json.loads(results_path.read_text())
    cells = results["cells"]
    if any(cell.get("status", "ok") != "ok" for cell in cells):
        raise ValueError("figures require complete assessments; report failures separately")
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "svg.fonttype": "none",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    tables = {(row["group"], row["arm"]): row for row in results["tables"]}
    fig, axes = plt.subplots(3, 2, figsize=(13, 10), layout="constrained")
    for row_index, group in enumerate(("role", "control", "distraction")):
        for column, (metric, denominator, title) in enumerate(
            (
                ("false_block", "expected_allow", "False blocks of expected allows"),
                ("harmful_allow", "expected_deny", "Harmful allows of expected denials"),
            )
        ):
            ax = axes[row_index, column]
            values = [tables[(group, arm)].get(metric, 0) for arm in ARMS]
            denominators = [tables[(group, arm)][denominator] for arm in ARMS]
            ax.barh(range(4), values, color=COLORS, height=0.62)
            ax.set_yticks(range(4), LABELS)
            ax.invert_yaxis()
            ax.set_xlim(0, max(denominators) * 1.14)
            ax.set_xlabel("Cases (lower is better)")
            ax.set_title(f"{group.capitalize()} · {title}", loc="left", weight="bold")
            ax.set_axisbelow(True)
            ax.grid(axis="x", alpha=0.16)
            for index, (value, count) in enumerate(zip(values, denominators, strict=True)):
                ax.text(value + max(denominators) * 0.025, index, f"{value}/{count}", va="center")
    fig.suptitle("Figure 1. Errors by cohort and condition", fontsize=17, weight="bold")
    fig.savefig(output / "outcomes.svg", bbox_inches="tight")
    fig.savefig(output / "outcomes.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    families = sorted(
        {
            c["pair_id"]
            for c in cells
            if c["cohort"] == "role" and c.get("variant") in ("credential_role", "ordinary_role")
        }
    )
    matrix = []
    for family in families:
        matrix.append(
            [
                sum(
                    bool(c["matches_expected"])
                    for c in cells
                    if c["pair_id"] == family
                    and c["arm"] == arm
                    and c.get("variant") in ("credential_role", "ordinary_role")
                )
                for arm in ARMS
            ]
        )
    fig, ax = plt.subplots(figsize=(13, 6.3), layout="constrained")
    ax.imshow(
        np.array(matrix),
        cmap=ListedColormap(["#f2b4ad", "#f4dd9a", "#b6d8c0"]),
        vmin=0,
        vmax=2,
        aspect="auto",
    )
    ax.set_xticks(range(4), LABELS)
    ax.set_yticks(range(len(families)), [f.replace("-", " ") for f in families])
    ax.set_xlabel("Condition · each tile contains two opposite-role decisions")
    ax.set_ylabel("Related role family")
    for row in range(len(families)):
        for column in range(4):
            ax.text(column, row, f"{matrix[row][column]}/2 correct", ha="center", va="center")
    ax.set_title(
        "Figure 2. Does each condition distinguish both roles?",
        loc="left",
        fontsize=16,
        weight="bold",
        pad=20,
    )
    fig.savefig(output / "role-pairs.svg", bbox_inches="tight")
    fig.savefig(output / "role-pairs.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    keyed = {(cell["case_id"], cell["arm"]): cell for cell in cells}
    contrasts = []
    for alternative, label in (
        ("guided_context", "U versus Original"),
        ("without_council", "Without Council versus Original"),
    ):
        changed = []
        for (case_id, arm), original in keyed.items():
            if arm != "original_context":
                continue
            other = keyed[(case_id, alternative)]
            if other["decision"] != original["decision"]:
                changed.append((case_id, original, other))
        contrasts.append((label, changed))
    height = max(4.4, max(len(rows) for _, rows in contrasts) * 0.37 + 2.1)
    fig, axes = plt.subplots(1, 2, figsize=(14, height), layout="constrained")
    for ax, (label, changes) in zip(axes, contrasts, strict=True):
        for index, (_case_id, original, other) in enumerate(changes):
            start, end = max(original["scores"].values()), max(other["scores"].values())
            color = "#32764a" if other["matches_expected"] else "#aa3d37"
            ax.plot([start, end], [index, index], color=color, linewidth=2)
            ax.scatter(start, index, facecolors="white", edgecolors=color, s=48, zorder=3)
            ax.scatter(end, index, color=color, marker="s", s=40, zorder=3)
        ax.axvline(
            0.70, color="#272b35", linestyle="--", linewidth=1.2, label="Fixed deny threshold 0.70"
        )
        ax.set_xlim(-0.02, 1.02)
        ax.set_yticks(range(len(changes)), [case_id for case_id, _, _ in changes])
        ax.invert_yaxis()
        ax.set_xlabel("Maximum of the two disclosure scores")
        ax.set_title(label, loc="left", weight="bold")
        ax.grid(axis="x", alpha=0.16)
        if not changes:
            ax.text(0.5, 0.5, "No decisions changed", transform=ax.transAxes, ha="center")
    fig.suptitle(
        "Figure 3. Every decision changed by U or Council removal\n"
        "Open circle = Original; square = alternative; green = fix; red = regression",
        fontsize=14,
        weight="bold",
    )
    fig.savefig(output / "threshold-crossings.svg", bbox_inches="tight")
    fig.savefig(output / "threshold-crossings.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    for name in ("outcomes.svg", "role-pairs.svg", "threshold-crossings.svg"):
        path = output / name
        path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
    proof = {
        "results_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "figures": ["outcomes.svg", "role-pairs.svg", "threshold-crossings.svg"],
    }
    (output / "provenance.json").write_text(json.dumps(proof, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data/measured-v1")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "figures")
    args = parser.parse_args()
    plot(args.data, args.output)


if __name__ == "__main__":
    main()
