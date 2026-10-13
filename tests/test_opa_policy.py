# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import shutil
import subprocess

import pytest

from nomos.level_0.types import Intent
from nomos.opa.client import build_intent_filter_input


def test_default_opa_policy_rego_tests_pass():
    opa = shutil.which("opa")
    if opa is None:
        pytest.skip("opa binary is not installed")

    result = subprocess.run(
        [opa, "test", "policies", "--fail-on-empty"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_default_opa_policy_accepts_generated_v2_input_without_context():
    opa = shutil.which("opa")
    if opa is None:
        pytest.skip("opa binary is not installed")
    intent = Intent.create(
        agent_id="agent-opa-v2",
        council_id="council-opa-v2",
        action_type="system.exec",
        description="Exercise generated OPA v2 input",
        target_resource="/workspace",
        command="/bin/echo ok",
        working_directory="/workspace",
        context="PRIVATE-OPA-CONTEXT",
    )
    input_doc = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=True,
    )

    result = subprocess.run(
        [
            opa,
            "eval",
            "--data",
            "policies/intent_filter.rego",
            "--stdin-input",
            "--format",
            "json",
            "data.nomos.intent.decision",
        ],
        input=json.dumps(input_doc),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert input_doc["intent"]["context"] == "PRIVATE-OPA-CONTEXT"
    decision = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
    assert decision["allow"] is True
