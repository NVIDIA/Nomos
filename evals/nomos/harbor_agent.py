# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Harbor 0.23 agent: run the fixed proposal-only council worker in its environment.

The environment image must contain this repository and the Nomos dependencies.
/experiment/trial.json is an operator-installed input, never an instruction parsed
from task prose. Provide NOMOS_EVAL_API_KEY through the environment's secret setup.
"""

from harbor.agents.nop import NopAgent


class NomosCouncilAgent(NopAgent):
    @staticmethod
    def name():
        return "nomos-council"

    async def run(self, instruction, environment, context):
        result = await environment.exec(
            command="python -m evals.nomos.harbor_worker",
            cwd="/opt/nomos",
            timeout_sec=300,
        )
        if result.return_code != 0:
            raise RuntimeError("isolated Nomos council worker failed")
