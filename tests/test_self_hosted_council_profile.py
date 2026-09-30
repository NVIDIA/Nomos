# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check that generated local NIM profiles preserve policy and route every role."""

import importlib.util
import os
import stat
import subprocess
import sys
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


@pytest.mark.parametrize(
    "url",
    ["http://nim-llm/v1", "http://nim-llm:not-a-port/v1", "http://nim-llm:65536/v1"],
)
def test_generated_profile_rejects_invalid_port(url):
    with pytest.raises(ValueError, match="local URL must be an http\\(s\\) /v1 base URL"):
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url=url,
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )


@pytest.mark.parametrize("field", ["model", "base_url"])
def test_generated_profile_rejects_duplicate_backend_field(field):
    line = next(line for line in SOURCE.splitlines() if line.startswith(f"    {field}: "))
    source = SOURCE.replace(line, f"{line}\n{line}", 1)
    with pytest.raises(ValueError, match="exactly one model and base_url"):
        MODULE.render(
            source,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )


def test_profile_write_preserves_existing_file_if_replace_fails(tmp_path, monkeypatch):
    output = tmp_path / "council.yaml"
    output.write_text("old profile", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--output",
            str(output),
            "--local-model",
            "local/model-id",
            "--local-url",
            "http://nim-llm:8000/v1",
            "--mode",
            "pure",
        ],
    )

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(MODULE.os, "replace", fail_replace)
    with pytest.raises(SystemExit):
        MODULE.main()
    assert output.read_text(encoding="utf-8") == "old profile"
    assert list(tmp_path.iterdir()) == [output]


def test_generated_profile_is_readable_by_council_user_with_restrictive_umask(tmp_path):
    output = tmp_path / "council.yaml"
    command = [
        sys.executable,
        str(SCRIPT),
        "--output",
        str(output),
        "--local-model",
        "local/model-id",
        "--local-url",
        "http://nim-llm:8000/v1",
        "--mode",
        "pure",
    ]
    subprocess.run(command, check=True, capture_output=True, preexec_fn=lambda: os.umask(0o077))
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


@pytest.mark.parametrize("already_running", [False, True])
def test_launcher_only_stops_nim_it_started_on_failure(tmp_path, already_running):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$NIM_TEST_LOG"\n'
        'if [[ "$*" == *" ps -q --status running nim-llm"* '
        '&& "$NIM_TEST_ALREADY_RUNNING" == 1 ]]; then\n'
        '  printf "existing-container\\n"\n'
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 1\n")
    curl.chmod(0o755)
    log = tmp_path / "docker.log"
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        ROOT_DIR=str(tmp_path),
        ENV_FILE=str(tmp_path / ".env"),
        NVIDIA_API_KEY="local-nim",  # pragma: allowlist secret
        NOMOS_NIM_READY_TIMEOUT="-1",
        NIM_TEST_LOG=str(log),
        NIM_TEST_ALREADY_RUNNING="1" if already_running else "0",
    )
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/scripts/self-hosted-up.sh"),
            "--target",
            "compose",
            "--mode",
            "pure",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode != 0
    assert "NOMOS_NIM_READY_TIMEOUT must be seconds" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "up -d nim-llm" in commands
    assert ("stop nim-llm" in commands) is not already_running


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
