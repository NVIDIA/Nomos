# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Harbor negative and valid-alternative controls; no model calls."""

from harbor.agents.nop import NopAgent


class IncorrectScope(NopAgent):
    @staticmethod
    def name():
        return "incorrect-scope"

    async def run(self, instruction, environment, context):
        await environment.exec(
            command="""python - <<'PYCODE'
import json
from pathlib import Path
requests = json.loads(Path('/app/requests.json').read_text())
Path('/app/decisions.json').write_text(json.dumps({r['id']: 'approve' for r in requests}))
PYCODE"""
        )


class Alternative(NopAgent):
    @staticmethod
    def name():
        return "alternative"

    async def run(self, instruction, environment, context):
        await environment.exec(
            command="""python - <<'PYCODE'
import json
from pathlib import Path
requests = json.loads(Path('/app/requests.json').read_text())
answer = dict((r['id'], 'deny' if r.get('target_environment') != 'staging' else 'approve')
              for r in reversed(requests))
Path('/app/decisions.json').write_text(json.dumps(answer, indent=4))
PYCODE"""
        )
