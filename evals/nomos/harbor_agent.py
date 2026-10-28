# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pinned Harbor agent with single-trial input and private evidence collection."""

import json
import os
from pathlib import Path

from harbor.agents.nop import NopAgent


class NomosCouncilAgent(NopAgent):
    def __init__(self, *, trial_input=None, trial_output=None, **kwargs):
        super().__init__(**kwargs)
        self.trial_input = Path(trial_input) if trial_input else None
        self.trial_output = Path(trial_output) if trial_output else None

    @staticmethod
    def name():
        return "nomos-council"

    async def run(self, instruction, environment, context):
        if self.trial_input is None or self.trial_output is None:
            raise ValueError("explicit single-trial input and output required")
        await environment.upload_file(self.trial_input, "/experiment/trial.json")
        trial_seconds = json.loads(self.trial_input.read_text())["settings"]["trial_seconds"]
        result = await environment.exec(
            command="python -m evals.nomos.harbor_worker",
            cwd="/opt/nomos",
            timeout_sec=trial_seconds + 30,
            env={"NOMOS_EVAL_API_KEY": os.environ["NOMOS_EVAL_API_KEY"]},
        )
        self.trial_output.mkdir(parents=True, exist_ok=True, mode=0o700)
        await environment.download_dir("/trial", self.trial_output / "trial")
        await environment.download_file("/app/decision.json", self.trial_output / "decision.json")
        if result.return_code != 0:
            raise RuntimeError("isolated Nomos council worker failed")


class ControlAgent(NomosCouncilAgent):
    """Predetermined verifier proof only; never counts as measured council behavior."""

    @staticmethod
    def name():
        return "nomos-verifier-control"

    async def run(self, instruction, environment, context):
        await environment.upload_file(self.trial_input, "/app/decision.json")
