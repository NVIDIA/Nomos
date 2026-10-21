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
from copy import deepcopy
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


@pytest.fixture(params=["shipped", "multiple"])
def profile_source(request):
    if request.param == "shipped":
        return SOURCE
    config = yaml.safe_load(SOURCE)
    template = next(iter(config["llm_backends"].values()))
    config["llm_backends"] = {
        f"hosted-{index}": {
            **template,
            "model": f"example/model-{index}",
            "base_url": f"https://inference-{index}.example/v1",
        }
        for index in range(3)
    }
    for index, member in enumerate(config["members"]):
        member["backend"] = f"hosted-{index}"
    config["code_review"]["backend"] = "hosted-1"
    config["chairman"]["backend"] = "hosted-2"
    return yaml.safe_dump(config, sort_keys=False)


@pytest.mark.parametrize(
    ("mode", "local_roles"),
    [
        ("pure", set(MODULE.ROLES)),
        (
            "mixed",
            set(MODULE.DEFAULT_MIXED_LOCAL_ROLES),
        ),
        (
            "mixed",
            {"environment-isolation", "blast-containment"},
        ),
    ],
)
def test_generated_profile_routes_roles_without_changing_policy(
    mode, local_roles, profile_source, monkeypatch
):
    original = yaml.safe_load(profile_source)
    expected = deepcopy(original)
    for member in expected["members"]:
        if member["id"] in local_roles:
            member["backend"] = "nim-local"
    for role in ("code_review", "chairman"):
        if role in local_roles:
            expected[role]["backend"] = "nim-local"
    hosted_names = {member["backend"] for member in expected["members"]} | {
        expected[role]["backend"] for role in ("code_review", "chairman")
    }
    hosted_names.discard("nim-local")
    rendered = yaml.safe_load(
        MODULE.render(
            profile_source,
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

    assert set(rendered["llm_backends"]) == hosted_names | {"nim-local"}
    assert rendered["llm_backends"]["nim-local"]["model"] == "local/model-id"
    local_backend = rendered["llm_backends"]["nim-local"]
    assert local_backend["provider"] == "nim"
    assert local_backend["base_url"] == "http://nim-llm:8000/v1"
    assert local_backend["api_key"] == "local-nim"  # pragma: allowlist secret
    for name in hosted_names:
        assert rendered["llm_backends"][name] == original["llm_backends"][name]

    assert rendered["members"] == expected["members"]
    for role in ("code_review", "chairman"):
        assert rendered[role] == expected[role]

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
    with pytest.raises(ValueError, match=f"duplicate YAML key: {field}"):
        MODULE.render(
            source,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )


@pytest.mark.parametrize("mode", ["pure", "mixed"])
def test_local_backend_uses_nim_protocol_without_hosted_credentials(mode, monkeypatch):
    config = yaml.safe_load(SOURCE)
    backend = config["llm_backends"][config["members"][0]["backend"]]
    backend.update(provider="anthropic", api_key="${HOSTED_TEST_KEY}")
    config["llm_backends"]["unused"] = {**backend, "api_key": "${UNUSED_TEST_KEY}"}
    monkeypatch.delenv("UNUSED_TEST_KEY", raising=False)
    monkeypatch.delenv("HOSTED_TEST_KEY", raising=False)
    roles = set(MODULE.ROLES) if mode == "pure" else set(MODULE.DEFAULT_MIXED_LOCAL_ROLES)
    result = yaml.safe_load(
        MODULE.render(
            yaml.safe_dump(config, sort_keys=False),
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode=mode,
            local_roles=roles,
        )
    )
    assert result["llm_backends"]["nim-local"] == {
        **backend,
        "provider": "nim",
        "model": "local/model-id",
        "api_key": "local-nim",  # pragma: allowlist secret
        "base_url": "http://nim-llm:8000/v1",
    }
    assert "unused" not in result["llm_backends"]
    if mode == "pure":
        assert "HOSTED_TEST_KEY" not in yaml.safe_dump(result)
    else:
        name = config["members"][1]["backend"]
        assert result["llm_backends"][name] == config["llm_backends"][name]


@pytest.mark.parametrize("problem", ["undefined-backend", "duplicate-role", "reserved-name"])
def test_generated_profile_rejects_ambiguous_routing(problem):
    config = yaml.safe_load(SOURCE)
    if problem == "undefined-backend":
        config["chairman"]["backend"] = "missing"
        message = "defined backend assignment for chairman"
    elif problem == "duplicate-role":
        config["members"].append(deepcopy(config["members"][0]))
        message = "duplicate council role"
    else:
        config["llm_backends"]["nim-local"] = next(iter(config["llm_backends"].values()))
        message = "nim-local is reserved"
    with pytest.raises(ValueError, match=message):
        MODULE.render(
            yaml.safe_dump(config, sort_keys=False),
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="pure",
            local_roles=set(MODULE.ROLES),
        )


def test_generated_profile_preserves_policy_comments_and_text():
    source = SOURCE.replace("require_justification: true", "require_justification: true # policy")
    rendered = MODULE.render(
        source,
        local_model="local/model-id",
        local_url="http://nim-llm:8000/v1",
        mode="mixed",
        local_roles={"chairman"},
    )
    assert "require_justification: true # policy" in rendered
    policy = source[source.index("members:\n") :]
    chairman_backend = yaml.safe_load(source)["chairman"]["backend"]
    policy = policy.replace(
        f"chairman:\n  enabled: true\n  backend: {chairman_backend}",
        "chairman:\n  enabled: true\n  backend: nim-local",
    )
    assert rendered.endswith(policy)


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


def test_launcher_keeps_nim_after_council_recreation_if_bootstrap_fails(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "deploy/scripts/self-hosted-up.sh", scripts / "self-hosted-up.sh")
    for name in ("dev-lib.sh", "prepare-dynamic-config.sh", "render-self-hosted-council.py"):
        (scripts / name).symlink_to(ROOT / "deploy/scripts" / name)
    compose_up = scripts / "compose-up.sh"
    compose_up.write_text(
        '#!/bin/sh\nprintf "bootstrap failed after council recreation\\n" >&2\nexit 42\n'
    )
    compose_up.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$NIM_TEST_LOG"\n'
        'case "$*" in *"ps -q council"*) printf "existing-council\\n";; esac\n'
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf \'{"data":[{"id":"local/model-id"}]}\\n\'\n')
    curl.chmod(0o755)

    docker_log = tmp_path / "docker.log"
    result = subprocess.run(
        [str(scripts / "self-hosted-up.sh"), "--target", "compose", "--mode", "pure"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROOT_DIR": str(tmp_path),
            "ENV_FILE": str(tmp_path / ".env"),
            "COUNCIL_CONFIG_GID": str(os.getgid()),
            "NOMOS_LOCAL_MODEL_ID": "local/model-id",
            "NIM_TEST_LOG": str(docker_log),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 42, result.stderr
    assert "keeping NIM running because a council may be using its local profile" in result.stderr
    profile = yaml.safe_load((tmp_path / "deploy/dynamic-config/council.yaml").read_text())
    assert profile["code_review"]["backend"] == "nim-local"
    commands = docker_log.read_text()
    assert "up -d nim-llm" in commands
    assert "up -d --no-deps --force-recreate council" in commands
    assert "stop nim-llm" not in commands


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
    assert set(active_before.llm_backends) == set(yaml.safe_load(SOURCE)["llm_backends"])

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
    assert active_after == active_before


def test_launcher_rejects_catalog_symlink_that_changes_profile_directory(tmp_path, monkeypatch):
    from nomos.config.settings import load_council_catalog

    monkeypatch.setenv("NVIDIA_API_KEY", "test-hosted-key")  # pragma: allowlist secret
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis")
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "test-redis-password")
    monkeypatch.setenv("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "test-integrity-key")
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "dev")
    dynamic = tmp_path / "deploy/dynamic-config"
    councils = dynamic / "councils"
    councils.mkdir(parents=True)
    shutil.copy(
        ROOT / "configs/local_council_catalog.yaml", councils / "local_council_catalog.yaml"
    )
    shutil.copy(ROOT / "configs/council.yaml", councils / "council.yaml")
    catalog = dynamic / "local_council_catalog.yaml"
    catalog.symlink_to("councils/local_council_catalog.yaml")
    active = load_council_catalog(str(catalog)).get("council-prod")
    for name, backend in yaml.safe_load(SOURCE)["llm_backends"].items():
        assert active.llm_backends[name].base_url == backend["base_url"]

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
    assert "resolves council-prod to" in result.stderr
    assert "not the generated profile" in result.stderr
    assert "up -d nim-llm" not in docker_log.read_text()
    assert not (dynamic / "council.yaml").exists()


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
    hosted_backend = next(iter(yaml.safe_load(SOURCE)["llm_backends"].values()))
    entry["llm_backends"] = {"nim-hosted": hosted_backend}
    entry["code_review"] = {"backend": "nim-hosted"}
    catalog.write_text(yaml.safe_dump(catalog_raw))
    active = load_council_catalog(str(catalog)).get("council-prod")
    assert active.code_review.backend == "nim-hosted"
    assert active.llm_backends["nim-hosted"].base_url == hosted_backend["base_url"]

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
@pytest.mark.parametrize("profile_already_exists", [False, True])
def test_launcher_only_stops_unused_nim_it_started_on_failure(
    tmp_path, already_running, invalid_timeout, profile_already_exists
):
    if profile_already_exists:
        profile = tmp_path / "deploy/dynamic-config/council.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(MODULE.GENERATED_MARKER)
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
        assert ("stop nim-llm" in commands) is (not already_running and not profile_already_exists)


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
