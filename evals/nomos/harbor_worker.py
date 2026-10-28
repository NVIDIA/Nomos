# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fixed worker for a private Harbor environment, not a general command launcher."""

import asyncio
import uuid
from pathlib import Path

from evals.nomos.adapter import trial
from evals.nomos.workflow import Experiment, read, write


async def main():
    data = read(Path("/experiment/trial.json"))
    spec = Experiment.model_validate(data["experiment"])
    task = next(t for t in spec.tasks if t.id == data["task_id"])
    if data["arm"] not in {"baseline", "candidate"}:
        raise ValueError("invalid arm")
    identity = uuid.uuid4().hex
    result = await asyncio.wait_for(
        trial(
            task.request,
            getattr(spec, data["arm"]).instruction,
            spec,
            identity,
            Path("/trial") / identity,
            spec.max_calls,
        ),
        timeout=spec.trial_seconds,
    )
    write(Path("/app/decision.json"), result)
    if result["status"] != "complete":
        raise RuntimeError("ungradable council trial")


if __name__ == "__main__":
    asyncio.run(main())
