# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fixed worker: receives only one request/instruction, never labels or other cases."""

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.nomos.adapter import trial
from evals.nomos.workflow import read, write


class TrialSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    endpoint: str
    trial_seconds: int = Field(ge=1, le=300)
    max_output_tokens: int = Field(ge=64, le=4096)


async def main():
    data = read(Path("/experiment/trial.json"))
    spec = TrialSettings.model_validate(data["settings"])
    result = await asyncio.wait_for(
        trial(
            data["request"],
            data["instruction"],
            spec,
            data["trial_id"],
            Path("/trial/evidence"),
            1,
        ),
        timeout=spec.trial_seconds,
    )
    write(Path("/app/decision.json"), result)
    if result["status"] != "complete":
        raise RuntimeError("ungradable council trial")


if __name__ == "__main__":
    asyncio.run(main())
