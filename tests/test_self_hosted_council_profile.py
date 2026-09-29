# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check that generated local NIM profiles preserve policy and route every role."""

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

from nomos.config.settings import CouncilSettings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/scripts/render-self-hosted-council.py"
SPEC = importlib.util.spec_from_file_location("render_self_hosted_council", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SOURCE = (ROOT / "configs/council.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "local_roles", "expected_backends"),
    [
        ("pure", set(MODULE.ROLES), {"nim-local"}),
        (
            "mixed",
            set(MODULE.DEFAULT_MIXED_LOCAL_ROLES),
            {"nim-local", "nim-hosted"},
        ),
        (
            "mixed",
            {"environment-isolation", "blast-containment"},
            {"nim-local", "nim-hosted"},
        ),
    ],
)
def test_generated_profile_routes_roles_without_changing_policy(
    mode, local_roles, expected_backends, monkeypatch
):
    original = yaml.safe_load(SOURCE)
    rendered = yaml.safe_load(
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode=mode,
            local_roles=local_roles,
        )
    )

    if mode == "mixed":
        monkeypatch.setenv("NVIDIA_API_KEY", "test-hosted-key")
    else:
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis")
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "test-redis-password")
    monkeypatch.setenv("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "test-integrity-key")
    CouncilSettings.model_validate(rendered)

    assert set(rendered["llm_backends"]) == expected_backends
    assert rendered["llm_backends"]["nim-local"]["model"] == "local/model-id"
    local_backend = rendered["llm_backends"]["nim-local"]
    assert local_backend["api_key"] == "local-nim"  # pragma: allowlist secret
    if mode == "mixed":
        assert rendered["llm_backends"]["nim-hosted"]["api_key"] == "${NVIDIA_API_KEY}"

    for member in rendered["members"]:
        expected = "nim-local" if member["id"] in local_roles else "nim-hosted"
        assert member["backend"] == expected
    for role in ("code_review", "chairman"):
        expected = "nim-local" if role in local_roles else "nim-hosted"
        assert rendered[role]["backend"] == expected

    for role in ("members", "code_review", "chairman"):
        original.pop(role)
        rendered.pop(role)
    original.pop("llm_backends")
    rendered.pop("llm_backends")
    assert rendered == original


def test_mixed_profile_rejects_all_local_roles():
    with pytest.raises(ValueError, match="at least one local and one hosted"):
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="mixed",
            local_roles=set(MODULE.ROLES),
        )


@pytest.mark.parametrize("port", ["not-a-port", "65536"])
def test_generated_profile_rejects_invalid_port(port):
    with pytest.raises(ValueError, match="local URL must be an http\\(s\\) /v1 base URL"):
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url=f"http://nim-llm:{port}/v1",
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )


@pytest.mark.parametrize("roles_arg", ["--local-roles=", "--local-roles"])
def test_launcher_rejects_empty_local_roles_before_starting_docker(roles_arg):
    args = [
        "bash",
        str(ROOT / "deploy/scripts/self-hosted-up.sh"),
        "--target",
        "compose",
        "--mode",
        "mixed",
        roles_arg,
    ]
    if roles_arg == "--local-roles":
        args.append("")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "missing roles" in result.stderr


@pytest.mark.parametrize("agent_arg", ["--agent-id=", "--agent-id"])
def test_launcher_rejects_empty_agent_id_before_starting_docker(agent_arg):
    args = [
        "bash",
        str(ROOT / "deploy/scripts/self-hosted-up.sh"),
        "--target",
        "kind",
        "--mode",
        "mixed",
        agent_arg,
    ]
    if agent_arg == "--agent-id":
        args.append("")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "missing agent ID" in result.stderr
