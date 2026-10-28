# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Nomos-owned offline commands, not upstream Eval Author commands."""

import argparse
import asyncio
import os
from pathlib import Path

from evals.nomos.workflow import compare, freeze, freeze_candidate, intake, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("intake", "freeze"):
        sub = commands.add_parser(name)
        sub.add_argument("source", type=Path)
        sub.add_argument("destination", type=Path)
    for name in ("run", "compare", "freeze-candidate", "analyze"):
        sub = commands.add_parser(name)
        sub.add_argument("directory", type=Path)
        if name in {"run", "compare", "analyze"}:
            sub.add_argument("--split", choices=["development", "held-out"], default="development")
        if name == "analyze":
            sub.add_argument("--inference-config", type=Path)
    args = parser.parse_args()
    if args.command == "intake":
        intake(args.source, args.destination)
    elif args.command == "freeze":
        freeze(args.source, args.destination)
    elif args.command == "run":
        asyncio.run(run(args.directory, args.split))
    elif args.command == "compare":
        print(compare(args.directory, args.split)["decision"])
    elif args.command == "freeze-candidate":
        freeze_candidate(args.directory)
    elif args.command == "analyze":
        from evals.nomos.feedback import analyze

        inference = None
        if args.inference_config:
            from assurance.ethos import InferenceConfig
            from evals.nomos.workflow import read

            inference = InferenceConfig(
                **read(args.inference_config), api_key=os.environ["NOMOS_EVAL_ANALYSIS_API_KEY"]
            )
        asyncio.run(analyze(args.directory, args.split, inference=inference))


if __name__ == "__main__":
    main()
