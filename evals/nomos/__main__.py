# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Nomos-owned offline commands, not upstream Eval Author commands."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from evals.nomos.workflow import (
    attach_candidate,
    attempts_for,
    compare,
    freeze,
    freeze_candidate,
    intake,
    load,
    read,
    run,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("intake", "freeze"):
        sub = commands.add_parser(name)
        sub.add_argument("source", type=Path)
        sub.add_argument("destination", type=Path)
    for name in ("run", "compare", "freeze-candidate", "attach-candidate", "analyze"):
        sub = commands.add_parser(name)
        sub.add_argument("directory", type=Path)
        if name in {"run", "compare", "analyze"}:
            sub.add_argument("--split", choices=["development", "held-out"], default="development")
        if name == "run":
            sub.add_argument("--arm", choices=["baseline", "candidate", "both"], default="both")
        if name == "attach-candidate":
            sub.add_argument("instruction", type=Path)
        if name == "analyze":
            sub.add_argument("--inference-config", type=Path)
            sub.add_argument("--budget", type=Path, help="Existing shared provider request budget")
        if name == "run":
            sub.add_argument("--executor", choices=["direct", "harbor"], default="direct")
            sub.add_argument("--harbor-python", type=Path)
            sub.add_argument("--image", help="Immutable image ID from the reviewed experiment")
            sub.add_argument("--budget", type=Path, help="Existing shared provider request budget")
    budget = commands.add_parser("create-budget", help="Create a new budget; refuses overwrite")
    budget.add_argument("path", type=Path)
    budget.add_argument("--calls", type=int, required=True)
    budget.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    executor = None
    if args.command == "run":
        options = (args.harbor_python, args.image, args.budget)
        if args.executor == "direct" and any(value is not None for value in options):
            parser.error("--harbor-python, --image and --budget require --executor harbor")
        if args.executor == "harbor":
            if any(value is None for value in options):
                parser.error("--executor harbor requires --harbor-python, --image and --budget")
            if not args.harbor_python.is_file() or not os.access(args.harbor_python, os.X_OK):
                parser.error("--harbor-python must name an executable interpreter")
            if not args.budget.is_file():
                parser.error("--budget must name an existing budget; use create-budget once")
            from evals.nomos.budget import RequestBudget
            from evals.nomos.harbor_bridge import HarborExecutor

            _, spec = load(args.directory)
            if args.image != spec.versions.get("runtime_image"):
                parser.error("--image differs from the frozen experiment runtime_image")
            executor = HarborExecutor(
                # Preserve the venv symlink: resolving it selects the base interpreter.
                python=args.harbor_python.absolute(),
                image=args.image,
                budget=RequestBudget(args.budget.resolve()),
            )
    if args.command == "analyze" and args.budget:
        if not args.inference_config:
            parser.error("--budget requires --inference-config for model-backed diagnosis")
        if not args.budget.is_file():
            parser.error("--budget must name an existing shared budget")
    if args.command == "create-budget":
        from evals.nomos.budget import RequestBudget

        RequestBudget.create(args.path, calls=args.calls, seconds=args.seconds)
        print(f"Created request budget: {args.path}")
    elif args.command == "intake":
        intake(args.source, args.destination)
    elif args.command == "freeze":
        freeze(args.source, args.destination)
    elif args.command == "run":
        arms = ("baseline", "candidate") if args.arm == "both" else (args.arm,)
        asyncio.run(
            run(
                args.directory,
                args.split,
                arms=arms,
                executor=executor,
            )
        )
        manifest, spec = load(args.directory)
        rows = [
            row
            for row in attempts_for(args.directory, manifest, spec)
            if row["split"] == args.split and row["arm"] in arms
        ]
        expected = sum(t.split == args.split for t in spec.tasks) * spec.repetitions * len(arms)
        completed = sum(row["status"] == "complete" for row in rows)
        state = read(args.directory / "state.json")
        print(
            json.dumps(
                {
                    "executor": args.executor,
                    "split": args.split,
                    "arms": arms,
                    "expected": expected,
                    "completed": completed,
                    "errors_or_interrupted": len(rows) - completed,
                    "missing": expected - len(rows),
                    "stop_reason": state.get("stop_reason"),
                    "artifacts": str(args.directory),
                },
                indent=2,
            )
        )
        if completed != expected:
            raise SystemExit(1)
    elif args.command == "compare":
        print(compare(args.directory, args.split)["decision"])
    elif args.command == "attach-candidate":
        attach_candidate(args.directory, args.instruction.read_text())
    elif args.command == "freeze-candidate":
        freeze_candidate(args.directory)
    elif args.command == "analyze":
        from evals.nomos.feedback import analyze

        inference = None
        if args.inference_config:
            from assurance.ethos import InferenceConfig

            inference = InferenceConfig(
                **read(args.inference_config), api_key=os.environ["NOMOS_EVAL_ANALYSIS_API_KEY"]
            )
        from evals.nomos.budget import RequestBudget

        asyncio.run(
            analyze(
                args.directory,
                args.split,
                inference=inference,
                shared_budget=RequestBudget(args.budget) if args.budget else None,
            )
        )


if __name__ == "__main__":
    main()
