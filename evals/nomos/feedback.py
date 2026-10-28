# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit scored-trace mapping and offline development diagnosis."""

import uuid
from pathlib import Path

from evals.nomos.workflow import compare, load, read, write


def scored_trace(row: dict, events: list[dict], task, interactions=None) -> dict:
    from trace_ingest.models import Trace

    from assurance.analysis import project_traces
    from evals.nomos.grading import grade

    traces, _ = project_traces(events, row["chain_id"])
    roots = [s.model_dump(mode="json") for t in traces for s in t.root_spans]
    for index, interaction in enumerate(interactions or []):
        roots.append(
            {
                "id": f"provider:{row['trial_id']}:{index}",
                "kind": "LLM",
                "input": interaction["messages"],
                "output": interaction["response"],
                "attributes": {
                    "subject": "council-member-review",
                    "source": "recorded provider interaction; not signed ledger payload",
                },
            }
        )
    # One trial may produce multiple provenance traces; scores apply to the trial.
    trace = Trace.model_validate(
        {
            "id": row["trial_id"],
            "root_spans": roots,
            "aggregate": {"latency_ms": row["seconds"] * 1000},
            "evaluator_results": {
                "nomos.decision-v1": {
                    "score": int(row["passed"]),
                    "expected": task.expected,
                    "observed": row["decision"],
                    "status": row["status"],
                    "checks": grade(task.expected, row, task.guidance_rule),
                    "correction_rule": task.guidance_rule,
                    "observed_correction": row.get("correction"),
                }
            },
            "attributes": {
                "task_id": task.id,
                "logical_case_id": task.id,
                "arm": row["arm"],
                "trial_id": row["trial_id"],
                "split": task.split,
                "source_traces": [t.id for t in traces],
                "evidence": row["evidence"],
            },
        }
    )
    return trace.model_dump(mode="json")


async def analyze(directory: Path, split: str, *, inference=None, shared_budget=None):
    _, spec = load(directory)
    state = read(directory / "state.json")
    if split == "held-out" and state["phase"] != "final_comparison":
        raise ValueError("final evidence is available only after final comparison")
    report = compare(directory, split)
    tasks = {t.id: t for t in spec.tasks}
    from insight_agent.compass_signals.tool_issues.signal import ToolIssueCompassSignal
    from trace_ingest.models import Trace, TraceSnapshot

    traces, gaps = [], []
    for path in sorted((directory / "attempts").glob("*.json")):
        row = read(path)
        if row["split"] != split:
            continue
        if row["status"] != "complete":
            gaps.append({"trial_id": row["trial_id"], "status": row["status"]})
            continue
        events = read(directory / "trials" / row["trial_id"] / "verified-ledger.json")
        interaction_path = directory / "trials" / row["trial_id"] / "interactions.json"
        interactions = read(interaction_path) if interaction_path.exists() else []
        traces.append(scored_trace(row, events, tasks[row["task_id"]], interactions))
    # Send exactly the independently scored representations to the pinned signal.
    snapshot = TraceSnapshot([Trace.model_validate(value) for value in traces])
    signal = ToolIssueCompassSignal()
    prerequisite = signal.check_prerequisites(snapshot)
    if prerequisite is None:
        result = await signal.analyze(snapshot)
        findings = result.model_dump(mode="json")
    else:
        findings = {"status": "skipped", "reason": str(prerequisite)}
    skipped = []
    inference_results = {"status": "not_configured"}
    if inference is None:
        skipped = [
            "ETHOS: explicit inference configuration required",
            "grader-failure: explicit judge configuration required",
        ]
    else:
        import asyncio

        from insight_agent.compass_signals.grader_failure_patterns import (
            GraderFailurePatternsCompassSignal,
        )

        from assurance.ethos import _build_llm, _detect
        from evals.nomos.budget import SharedCounter

        budget = SharedCounter(shared_budget) if shared_budget else {"calls": 0}
        try:
            judge = GraderFailurePatternsCompassSignal(_build_llm(inference, budget))
            reason = judge.check_prerequisites(snapshot)
            if reason is None:
                graded = await asyncio.wait_for(
                    judge.analyze(snapshot), inference.request_timeout * 2
                )
                inference_results = {"grader_failure": graded.model_dump(mode="json")}
            else:
                inference_results = {"grader_failure": {"status": "skipped", "reason": reason}}
            ethos = await _detect(snapshot, spec.contract, inference, budget)
            inference_results.update(
                ethos=ethos.model_dump(mode="json"),
                model_requests=budget["calls"],
                configuration=inference.manifest(),
            )
        except Exception as exc:
            inference_results = {
                "status": "error",
                "error_type": type(exc).__name__,
                "model_requests": budget["calls"],
                "configuration": inference.manifest(),
            }
    output = directory / f"analysis-{split}"
    write(output / "scored-traces.json", traces)
    payload = {"tools": findings, "inference": inference_results}
    write(output / "attempts" / (uuid.uuid4().hex + ".json"), payload)
    write(output / "compass.json", payload)
    write(
        output / "coverage-review.json",
        {
            "status": "human_eval_author_review_required",
            "decision": report["decision"],
            "unique_tasks": sorted({t["attributes"]["task_id"] for t in traces}),
            "gaps": gaps,
            "skipped_signals": skipped,
            "next_experiment": [
                {
                    "task_id": t["task_id"],
                    "hypothesis": "Review decision errors against the frozen contract",
                }
                for t in report["tasks"]
                if t["candidate"] < t["eligible_per_arm"]
            ],
            "holdout_policy": "Final cases used for diagnosis must retire before the next edit",
        },
    )
