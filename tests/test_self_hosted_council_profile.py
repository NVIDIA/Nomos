# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check that generated local NIM profiles preserve policy and route every role."""

import importlib.util
import json
import os
import shutil
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
    subprocess.run(command, check=True, capture_output=True, umask=0o077)
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


@pytest.mark.parametrize("existing_directory", [False, True])
def test_launcher_prepares_profile_access_before_recreating_council(
    tmp_path, existing_directory, monkeypatch
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "deploy/scripts/self-hosted-up.sh", scripts / "self-hosted-up.sh")
    for name in ("dev-lib.sh", "prepare-dynamic-config.sh", "render-self-hosted-council.py"):
        (scripts / name).symlink_to(ROOT / "deploy/scripts" / name)
    compose_up = scripts / "compose-up.sh"
    compose_up.write_text('#!/bin/sh\nprintf "compose-up\\n" >> "$NIM_TEST_LOG"\n')
    compose_up.chmod(0o755)
    dynamic = tmp_path / "deploy/dynamic-config"
    if existing_directory:
        dynamic.mkdir(parents=True, mode=0o700)
        (dynamic / "council.yaml").write_text(MODULE.GENERATED_MARKER)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "from pathlib import Path\n"
        "args = ' '.join(sys.argv[1:])\n"
        "if 'ps -q council' in args:\n"
        "    print('existing-council')\n"
        "if 'up -d --no-deps --force-recreate council' in args:\n"
        "    directory = Path(os.environ['ROOT_DIR']) / 'deploy/dynamic-config'\n"
        "    profile = directory / 'council.yaml'\n"
        "    record = {'directory_mode': stat.S_IMODE(directory.stat().st_mode),\n"
        "              'profile_mode': stat.S_IMODE(profile.stat().st_mode),\n"
        "              'gid': profile.stat().st_gid,\n"
        "              'compose_gid': os.environ['COUNCIL_CONFIG_GID']}\n"
        "    with open(os.environ['NIM_TEST_LOG'], 'a') as log:\n"
        "        log.write(json.dumps(record) + '\\n')\n"
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf \'{"data":[{"id":"local/model-id"}]}\\n\'\n')
    curl.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROOT_DIR": str(tmp_path),
        "ENV_FILE": str(tmp_path / ".env"),
        "COUNCIL_CONFIG_GID": str(os.getgid()),
        "NOMOS_LOCAL_MODEL_ID": "local/model-id",
        "NIM_TEST_LOG": str(log),
    }
    result = subprocess.run(
        [str(scripts / "self-hosted-up.sh"), "--target", "compose", "--mode", "pure"],
        env=env,
        capture_output=True,
        text=True,
        umask=0o077,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    recreate, compose_started = log.read_text().splitlines()
    assert json.loads(recreate) == {
        "directory_mode": 0o2770,
        "profile_mode": 0o660,
        "gid": os.getgid(),
        "compose_gid": str(os.getgid()),
    }
    assert compose_started == "compose-up"
    profile = yaml.safe_load((dynamic / "council.yaml").read_text())
    assert profile["llm_backends"]["nim-local"]["model"] == "local/model-id"
    # compose-up seeds this catalog when absent; resolve the profile it selects.
    from nomos.config.settings import load_council_catalog

    catalog = dynamic / "local_council_catalog.yaml"
    shutil.copy(ROOT / "configs/local_council_catalog.yaml", catalog)
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis")
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "test-redis-password")
    monkeypatch.setenv("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "test-integrity-key")
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "dev")
    active = load_council_catalog(str(catalog)).get("council-prod")
    assert set(active.llm_backends) == {"nim-local"}
    assert active.llm_backends["nim-local"].model == "local/model-id"


@pytest.mark.parametrize("mode", ["pure", "mixed"])
def test_launcher_rejects_managed_compose_catalog_before_starting_nim(tmp_path, monkeypatch, mode):
    from nomos.config.council_catalog_store import CouncilCatalogStore
    from nomos.config.settings import load_council_catalog

    for key, value in {
        "NVIDIA_API_KEY": "test-hosted-key",  # pragma: allowlist secret
        "COUNCIL_REDIS_HOST": "redis",
        "COUNCIL_REDIS_PASSWORD": "test-redis-password",  # pragma: allowlist secret
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY": "test-integrity-key",
        "NOMOS_DEPLOY_PROFILE": "dev",
    }.items():
        monkeypatch.setenv(key, value)
    dynamic = tmp_path / "deploy/dynamic-config"
    dynamic.mkdir(parents=True)
    catalog = dynamic / "local_council_catalog.yaml"
    shutil.copy(ROOT / "configs/local_council_catalog.yaml", catalog)
    store = CouncilCatalogStore(catalog, dynamic / "councils")
    store.update_council_profile("council-prod", yaml.safe_load(SOURCE))
    assert (
        yaml.safe_load(catalog.read_text())["councils"]["council-prod"]["config_file"]
        == "councils/council-prod.yaml"
    )
    # No custom top-level profile remains, as instructed by the quick start.
    assert not (dynamic / "council.yaml").exists()
    active_before = load_council_catalog(str(catalog)).get("council-prod")
    assert set(active_before.llm_backends) == {"nim-kimi"}

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NIM_TEST_LOG"\n')
    docker.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nexit 1\n")
    curl.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/scripts/self-hosted-up.sh"),
            "--target",
            "compose",
            "--mode",
            mode,
        ],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ROOT_DIR": str(tmp_path),
            "ENV_FILE": str(tmp_path / ".env"),
            "NOMOS_NIM_READY_TIMEOUT": "0",
            "NIM_TEST_LOG": str(docker_log),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "restore default_council_id: council-prod" in result.stderr
    assert "up -d nim-llm" not in docker_log.read_text()
    assert not (dynamic / "council.yaml").exists()
    active_after = load_council_catalog(str(catalog)).get("council-prod")
    assert set(active_after.llm_backends) == {"nim-kimi"}


def test_launcher_rejects_catalog_role_override_before_starting_nim(tmp_path, monkeypatch):
    from nomos.config.settings import load_council_catalog

    for key, value in {
        "NVIDIA_API_KEY": "test-hosted-key",  # pragma: allowlist secret
        "COUNCIL_REDIS_HOST": "redis",
        "COUNCIL_REDIS_PASSWORD": "test-redis-password",  # pragma: allowlist secret
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY": "test-integrity-key",
        "NOMOS_DEPLOY_PROFILE": "dev",
    }.items():
        monkeypatch.setenv(key, value)
    dynamic = tmp_path / "deploy/dynamic-config"
    dynamic.mkdir(parents=True)
    (dynamic / "council.yaml").write_text(
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )
    )
    catalog = dynamic / "local_council_catalog.yaml"
    catalog_raw = yaml.safe_load((ROOT / "configs/local_council_catalog.yaml").read_text())
    entry = catalog_raw["councils"]["council-prod"]
    entry["llm_backends"] = {"nim-hosted": yaml.safe_load(SOURCE)["llm_backends"]["nim-kimi"]}
    entry["code_review"] = {"backend": "nim-hosted"}
    catalog.write_text(yaml.safe_dump(catalog_raw))
    active = load_council_catalog(str(catalog)).get("council-prod")
    assert active.code_review.backend == "nim-hosted"
    assert active.llm_backends["nim-hosted"].base_url == "https://integrate.api.nvidia.com/v1"

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NIM_TEST_LOG"\n')
    docker.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nexit 1\n")
    curl.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/scripts/self-hosted-up.sh"),
            "--target",
            "compose",
            "--mode",
            "pure",
        ],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ROOT_DIR": str(tmp_path),
            "ENV_FILE": str(tmp_path / ".env"),
            "NOMOS_NIM_READY_TIMEOUT": "0",
            "NIM_TEST_LOG": str(docker_log),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "overrides council-prod" in result.stderr
    assert "up -d nim-llm" not in docker_log.read_text()


@pytest.mark.parametrize("invalid_timeout", [False, True])
@pytest.mark.parametrize("already_running", [False, True])
def test_launcher_only_stops_nim_it_started_on_failure(tmp_path, already_running, invalid_timeout):
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
        NOMOS_NIM_READY_TIMEOUT="-1" if invalid_timeout else "0",
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
    commands = log.read_text(encoding="utf-8")
    if invalid_timeout:
        assert "NOMOS_NIM_READY_TIMEOUT must be seconds" in result.stderr
        assert "up -d nim-llm" not in commands
        assert "stop nim-llm" not in commands
    else:
        assert "NIM did not become ready" in result.stderr
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
