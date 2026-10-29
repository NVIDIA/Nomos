#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native Harbor verifier controls for every reviewed pilot task; no provider calls."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from evals.nomos.harbor_bridge import prepare_task
from evals.nomos.workflow import Experiment, read, write


async def controls(spec_path: Path, output: Path, python: Path, image: str):
    spec = Experiment.model_validate(read(spec_path))
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    verifier = Path(__file__).resolve().parents[1] / "evals/nomos/grading.py"
    verifier_sha256 = hashlib.sha256(verifier.read_bytes()).hexdigest()
    if spec.proofs["verifier_sha256"] != verifier_sha256:
        raise ValueError("verifier proof is stale")
    results = []
    for task in spec.tasks:
        for control in (
            "oracle",
            "nop",
            "targeted-failure",
            "approve-all",
            "deny-all",
            "alternative",
        ):
            directory = output / task.id / control
            directory.mkdir(parents=True)
            task_dir = prepare_task(directory, task, image)
            decision = (
                task.expected
                if control in {"oracle", "alternative"}
                else "approve"
                if control == "approve-all"
                else "deny"
                if control == "deny-all"
                else ("deny" if task.expected == "approve" else "approve")
            )
            answer = {
                "status": "complete",
                "layer": "council",
                "decision": decision,
                "guidance": "Propose a status check in the permitted staging scope.",
                "execution": "not_attempted",
                "correction": (
                    {
                        "command": task.guidance_rule["commands"][0],
                        "target_environment": task.guidance_rule["environment"],
                        "target_resource": task.guidance_rule["resource_prefix"],
                    }
                    if task.guidance_rule
                    else None
                ),
            }
            if control == "nop":
                answer = {}
            if control == "alternative" and answer.get("correction"):
                answer["correction"]["target_resource"] += "/health"
            if answer:
                answer["guidance"] = json.dumps(
                    {"explanation": "Use the permitted scope", "correction": answer["correction"]}
                )
            source = directory / "answer.json"
            write(source, answer)
            env = {
                k: os.environ[k]
                for k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
                if k in os.environ
            }
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            with (directory / "native.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    str(python),
                    "-m",
                    "evals.nomos.harbor_driver",
                    str(task_dir),
                    str(directory),
                    str(source),
                    "control",
                    stdout=log,
                    stderr=log,
                    env=env,
                )
                try:
                    code = await asyncio.wait_for(process.wait(), timeout=120)
                except (TimeoutError, asyncio.CancelledError):
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10)
                    except TimeoutError:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                    raise
            native_path = directory / "driver-result.json"
            native = read(native_path)
            reward = native.get("verifier_result", {}).get("rewards", {}).get("reward")
            expected = (
                1
                if control in {"oracle", "alternative"}
                or (control == "approve-all" and task.expected == "approve")
                or (control == "deny-all" and task.expected == "deny")
                else 0
            )
            passed = code == 0 and native.get("exception_info") is None and reward == expected
            results.append(
                {
                    "task_id": task.id,
                    "control": control,
                    "reward": reward,
                    "expected_reward": expected,
                    "passed": passed,
                    "native_result_sha256": hashlib.sha256(native_path.read_bytes()).hexdigest(),
                }
            )
            write(
                output / "controls.json",
                {"runtime_image": image, "verifier_sha256": verifier_sha256, "results": results},
            )
            if not passed:
                raise RuntimeError("native verifier control failed")
    if hashlib.sha256(verifier.read_bytes()).hexdigest() != verifier_sha256:
        raise ValueError("verifier changed during control proof")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--harbor-python", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(controls(args.spec.resolve(), args.out.resolve(), args.harbor_python, args.image))


if __name__ == "__main__":
    main()
