# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Manifest-runner executor that preserves native Harbor results and signed evidence."""

import asyncio
import json
import os
import shutil
from pathlib import Path

from evals.nomos.budget import RequestBudget
from evals.nomos.workflow import read, write


class HarborExecutor:
    # Worker window plus bounded native setup, verification and evidence collection.
    lifecycle_overhead_seconds = 120

    def __init__(self, *, python: Path, image: str, budget: RequestBudget):
        self.python, self.image, self.budget = python, image, budget

    async def __call__(
        self, request, instruction, spec, trial_id, directory, remaining, *, backend_factory=None
    ):
        if backend_factory or remaining < 1:
            raise ValueError("native Harbor execution requires an available provider call")
        if spec.versions.get("runtime_image") != self.image:
            raise ValueError("runtime image differs from frozen experiment")
        self.budget.reserve("council:" + trial_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        source = directory / "input.json"
        write(
            source,
            {
                "request": request,
                "instruction": instruction,
                "trial_id": trial_id,
                "settings": {
                    k: getattr(spec, k)
                    for k in ("model", "endpoint", "trial_seconds", "max_output_tokens")
                },
            },
        )
        match = next(t for t in spec.tasks if t.request == request)
        task = prepare_task(directory, match, self.image, trial_seconds=spec.trial_seconds)
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
            if k in os.environ
        }
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        env["NOMOS_EVAL_API_KEY"] = os.environ["NOMOS_EVAL_API_KEY"]
        with (directory / "harbor.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                str(self.python),
                "-m",
                "evals.nomos.harbor_driver",
                str(task),
                str(directory),
                str(source),
                stdout=log,
                stderr=log,
                env=env,
            )
            try:
                code = await asyncio.wait_for(process.wait(), timeout=spec.trial_seconds + 90)
            except BaseException:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=30)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                raise
        if code and not (directory / "collected/decision.json").exists():
            raise RuntimeError("native Harbor execution failed")
        collected = directory / "collected"
        result = read(collected / "decision.json")
        if result.get("intent_id") is None or result.get("calls") != 1:
            raise ValueError("native trial correlation or accounting invalid")
        for name in ("verified-ledger.json", "interactions.json", "response.json"):
            shutil.copyfile(collected / "trial/evidence" / name, directory / name)
        # Correlate the verified ledger to this trial's response, never command similarity.
        events = read(directory / "verified-ledger.json")
        if not any(e.get("payload", {}).get("intent_id") == result["intent_id"] for e in events):
            raise ValueError("native result lacks correlated ledger evidence")
        result["native_harbor_result"] = str(directory / "driver-result.json")
        if code:
            result["status"] = "error"
        result["runtime_image"] = self.image
        return result


def prepare_task(directory: Path, match, image: str, *, trial_seconds: int = 120) -> Path:
    task = directory / "task"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Review the operator-installed single proposal.")
    # Expected labels and grading code are injected only by the verifier.
    from evals.nomos.grading import __file__ as grading_file

    shutil.copyfile(grading_file, task / "tests/grading.py")
    write(task / "tests/expected.json", {"expected": match.expected, "rule": match.guidance_rule})
    (task / "tests/test.sh").write_text("#!/bin/sh\nset -eu\npython /tests/verify.py\n")
    (task / "tests/verify.py").write_text(
        "import json\nfrom pathlib import Path\nfrom grading import grade\n"
        'expected=json.loads(Path("/tests/expected.json").read_text())\n'
        'try: result=json.loads(Path("/app/decision.json").read_text())\n'
        "except (OSError, ValueError): result={}\n"
        'passed=grade(expected["expected"], result, expected["rule"])["passed"]\n'
        'Path("/logs/verifier/reward.txt").write_text("1" if passed else "0")\n'
    )
    (task / "task.toml").write_text(
        f'schema_version="1.4"\n[agent]\ntimeout_sec={trial_seconds + 60}\n'
        "[verifier]\ntimeout_sec=30\n[environment]\n"
        f'docker_image={json.dumps(image)}\nnetwork_mode="public"\n'
        "cpus=1\nmemory_mb=512\nstorage_mb=1024\n"
    )
    return task
