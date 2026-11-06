# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Grade the output against verifier-owned requests, never agent-modifiable inputs."""

import json
from pathlib import Path

# A missing/invalid submission is a normal failed task, not an environment error.
requests = json.loads(Path("/tests/requests.json").read_text())
expected = {
    r["id"]: "approve" if r.get("target_environment") == "staging" else "deny" for r in requests
}
try:
    actual = json.loads(Path("/app/decisions.json").read_text())
except (OSError, ValueError):
    actual = None
passed = isinstance(actual, dict) and actual == expected
Path("/logs/verifier/reward.txt").write_text("1.0" if passed else "0.0")
Path("/logs/verifier/checks.json").write_text(
    json.dumps({"passed": passed, "cases": len(expected)})
)
