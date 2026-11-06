# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""One native Harbor trial. Run with the separately pinned Harbor interpreter."""

import asyncio
import json
import signal
import sys
import uuid
from importlib.metadata import version
from pathlib import Path

from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
from harbor.trial.trial import Trial


async def main():
    if version("harbor") != "0.23.0":
        raise ValueError("Harbor 0.23.0 is required")
    task, output, source = [Path(value).resolve() for value in sys.argv[1:4]]
    output.mkdir(parents=True, exist_ok=True)
    gateway = output / "host-gateway.json"
    gateway.write_text(
        json.dumps({"services": {"main": {"extra_hosts": ["host.docker.internal:host-gateway"]}}})
    )
    config = TrialConfig(
        task=TaskConfig(path=task),
        trial_name="native-" + uuid.uuid4().hex[:16],
        trials_dir=output / "native",
        agent=AgentConfig(
            import_path=(
                "evals.nomos.harbor_agent:ControlAgent"
                if len(sys.argv) > 4
                else "evals.nomos.harbor_agent:NomosCouncilAgent"
            ),
            kwargs={"trial_input": str(source), "trial_output": str(output / "collected")},
        ),
        environment=EnvironmentConfig(delete=True, extra_docker_compose=[gateway]),
    )
    result = await (await Trial.create(config)).run()
    (output / "driver-result.json").write_text(result.model_dump_json(indent=2))
    if result.exception_info is not None:
        raise RuntimeError("native Harbor trial failed; inspect private result")


async def supervised():
    task = asyncio.create_task(main())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    await task


if __name__ == "__main__":
    asyncio.run(supervised())
