# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())


def _compose_sentinel() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.redis-sentinel.yml").read_text())


def _compose_service_text() -> str:
    return (ROOT / "deploy" / "docker-compose.service.yml").read_text()


def _run_service_compose_config(
    tmp_path: Path,
    *,
    password: str | None,
    encoded_password: str | None,
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is not available")
    if (
        subprocess.run(
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    ):
        pytest.skip("Docker Compose is not available")

    env_file = tmp_path / "compose.env"
    env_file.write_text("")
    env = os.environ.copy()
    env.pop("POSTGRES_PASSWORD", None)  # pragma: allowlist secret
    env.pop("POSTGRES_PASSWORD_URLENCODED", None)  # pragma: allowlist secret
    if password is not None:
        env["POSTGRES_PASSWORD"] = password  # pragma: allowlist secret
    if encoded_password is not None:
        env["POSTGRES_PASSWORD_URLENCODED"] = encoded_password  # pragma: allowlist secret

    return subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "deploy/docker-compose.yml"),
            "-f",
            str(ROOT / "deploy/docker-compose.service.yml"),
            "config",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_service_compose_requires_an_explicit_postgres_password() -> None:
    compose = _compose_service_text()

    assert "${POSTGRES_PASSWORD:-nomos-dev}" not in compose
    assert compose.count("${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}") == 1
    assert (
        compose.count(
            "${POSTGRES_PASSWORD_URLENCODED:?POSTGRES_PASSWORD_URLENCODED is required}",
        )
        == 2
    )


@pytest.mark.parametrize(
    ("password", "encoded_password", "expected_error"),
    [
        (None, "encoded-test-password", "POSTGRES_PASSWORD is required"),
        ("", "encoded-test-password", "POSTGRES_PASSWORD is required"),
        (
            "raw-test-password",  # pragma: allowlist secret
            None,
            "POSTGRES_PASSWORD_URLENCODED is required",
        ),
        (
            "raw-test-password",  # pragma: allowlist secret
            "",
            "POSTGRES_PASSWORD_URLENCODED is required",
        ),
    ],
    ids=["raw-unset", "raw-empty", "encoded-unset", "encoded-empty"],
)
def test_service_compose_config_denies_missing_postgres_password(
    tmp_path: Path,
    password: str | None,
    encoded_password: str | None,
    expected_error: str,
) -> None:
    result = _run_service_compose_config(
        tmp_path,
        password=password,
        encoded_password=encoded_password,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_service_compose_config_accepts_explicit_postgres_password(tmp_path: Path) -> None:
    password = "pa/ss?word#fragment"  # pragma: allowlist secret
    encoded_password = "pa%2Fss%3Fword%23fragment"  # pragma: allowlist secret
    result = _run_service_compose_config(
        tmp_path,
        password=password,
        encoded_password=encoded_password,
    )

    assert result.returncode == 0, result.stderr
    compose = yaml.safe_load(result.stdout)
    services = compose["services"]
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == password
    expected_url = f"postgresql://nomos:{encoded_password}@postgres:5432/nomos"
    assert services["council"]["environment"]["COUNCIL_DATABASE_URL"] == expected_url
    assert services["agent-signup"]["environment"]["COUNCIL_DATABASE_URL"] == expected_url


def test_service_compose_uses_encoded_password_only_in_application_urls() -> None:
    compose = yaml.safe_load(_compose_service_text())
    raw_password = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
    encoded_password = "${POSTGRES_PASSWORD_URLENCODED:?POSTGRES_PASSWORD_URLENCODED is required}"

    assert compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] == raw_password
    for service in ("council", "agent-signup"):
        database_url = compose["services"][service]["environment"]["COUNCIL_DATABASE_URL"]
        assert encoded_password in database_url
        assert raw_password not in database_url


def test_opa_management_api_is_not_on_executor_network() -> None:
    compose = _compose()

    assert compose["services"]["opa"]["networks"] == ["opa-net"]
    assert "opa-net" in compose["services"]["council"]["networks"]
    assert "nomos-net" in compose["services"]["council"]["networks"]
    assert compose["networks"]["opa-net"]["internal"] is True

    for service_name in (
        "executor-default",
        "executor-dev",
        "executor-openshell",
    ):
        networks = compose["services"][service_name].get("networks", [])
        assert "opa-net" not in networks


def test_executors_mount_public_key_volume_only() -> None:
    compose = _compose()

    for service_name in (
        "executor-default",
        "executor-dev",
        "executor-openshell",
    ):
        service = compose["services"][service_name]
        volumes = service["volumes"]

        assert "council-keys:/app/keys:ro" not in volumes
        assert "council-public-keys:/app/public-keys:ro" in volumes
        assert "EXECUTOR_PUBLIC_KEY_PATH=/app/public-keys/council.pub" in service["environment"]


def test_council_exports_public_key_to_executor_volume() -> None:
    compose = _compose()
    council = compose["services"]["council"]

    assert "council-keys:/app/keys" in council["volumes"]
    assert "council-public-keys:/app/public-keys" in council["volumes"]
    assert "COUNCIL_PUBLIC_KEY_EXPORT_PATH=/app/public-keys/council.pub" in council["environment"]
    assert "council-public-keys" in compose["volumes"]


def test_openshell_profile_builds_runtime_backend() -> None:
    compose = _compose()
    openshell = compose["services"]["openshell"]
    executor = compose["services"]["executor-openshell"]

    assert openshell["build"]["dockerfile"] == "deploy/Dockerfile.openshell"
    assert openshell["profiles"] == ["executor-openshell"]
    assert openshell.get("ports") is None
    assert openshell["expose"] == ["8080"]
    assert openshell["networks"] == ["openshell-net"]
    assert compose["networks"]["openshell-net"]["internal"] is True
    assert "executor-openshell-workspace:/workspace" in openshell["volumes"]
    assert executor["networks"] == ["nomos-net", "openshell-net"]
    assert executor["depends_on"]["openshell"]["condition"] == "service_healthy"
    assert executor["ports"] == ["${EXECUTOR_OPENSHELL_PORT:-9907}:9906"]
    assert "EXECUTOR_PORT=9906" in executor["environment"]


def test_deploy_stack_defaults_to_openshell_executor() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert 'EXECUTOR_PROFILE="${EXECUTOR_PROFILE:-executor-openshell}"' in deploy_script


def test_alpha_profile_runtime_package_is_in_service_images() -> None:
    dockerfiles = (
        ROOT / "deploy" / "Dockerfile",
        ROOT / "deploy" / "Dockerfile.monitor",
        ROOT / "deploy" / "Dockerfile.agent-signup",
        ROOT / "deploy" / "Dockerfile.demo",
    )

    for dockerfile in dockerfiles:
        contents = dockerfile.read_text()

        assert "COPY nomos_runtime/ ./nomos_runtime/" in contents
        assert "./nomos_runtime" in contents


def test_compose_sentinel_override_defines_redis_quorum() -> None:
    compose = _compose_sentinel()
    services = compose["services"]

    assert {"redis", "redis-1", "redis-2"}.issubset(services)
    assert {"redis-sentinel-1", "redis-sentinel-2", "redis-sentinel-3"}.issubset(services)
    assert services["redis-1"]["volumes"] == ["redis-data-1:/data"]
    assert services["redis-2"]["volumes"] == ["redis-data-2:/data"]
    assert "redis-data-1" in compose["volumes"]
    assert "redis-data-2" in compose["volumes"]

    command = "\n".join(services["redis-sentinel-1"]["command"])
    assert "sentinel monitor $$REDIS_SENTINEL_SERVICE_NAME redis 6379 2" in command
    assert "sentinel resolve-hostnames yes" in command
    node_command = "\n".join(services["redis"]["command"])
    assert "for sentinel_host in redis-sentinel-1 redis-sentinel-2 redis-sentinel-3" in node_command


def test_compose_sentinel_override_wires_redis_clients() -> None:
    compose = _compose_sentinel()
    services = compose["services"]

    assert services["council"]["environment"]["COUNCIL_REDIS_MODE"] == "sentinel"
    assert (
        services["council"]["environment"]["COUNCIL_REDIS_SENTINEL_NODES"]
        == "redis-sentinel-1:26379,redis-sentinel-2:26379,redis-sentinel-3:26379"
    )
    assert services["agent-signup"]["environment"]["NOMOS_SIGNUP_REDIS_MODE"] == "sentinel"
    assert services["executor-openshell"]["environment"]["EXECUTOR_REDIS_MODE"] == "sentinel"
    assert services["executor-default"]["environment"]["EXECUTOR_REDIS_MODE"] == "sentinel"

    for service_name in ("council", "agent-signup", "executor-openshell", "executor-default"):
        depends_on = services[service_name]["depends_on"]
        for dependency in (
            "redis",
            "redis-1",
            "redis-2",
            "redis-sentinel-1",
            "redis-sentinel-2",
            "redis-sentinel-3",
        ):
            assert depends_on[dependency]["condition"] == "service_healthy"


def test_deploy_stack_can_select_compose_sentinel_topology() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert "resolve_compose_redis_topology" in deploy_script
    assert "current_or_default COMPOSE_REDIS_TOPOLOGY sentinel" in deploy_script
    assert "docker-compose.redis-sentinel.yml" in deploy_script
    assert "COMPOSE_EXTRA_FILES" in deploy_script


def test_account_wrapper_can_select_compose_sentinel_topology() -> None:
    accounts_script = (ROOT / "scripts" / "agent_accounts.sh").read_text()

    assert 'COMPOSE_REDIS_TOPOLOGY="${COMPOSE_REDIS_TOPOLOGY:-sentinel}"' in accounts_script
    assert "docker-compose.redis-sentinel.yml" in accounts_script
    assert (
        "redis_services+=(redis-1 redis-2 redis-sentinel-1 redis-sentinel-2 redis-sentinel-3)"
        in accounts_script
    )
    assert "--env NOMOS_REDIS_SENTINEL_NODES" in accounts_script


def test_openshell_executor_policy_matches_limited_runtime_tools() -> None:
    config = yaml.safe_load((ROOT / "deploy" / "executor-openshell.yaml").read_text())
    allowed_tools = set(config["tools"]["allowed"])

    assert {"bash", "python3", "cat", "ls"}.issubset(allowed_tools)
    assert "git" not in allowed_tools
    assert "uv" not in allowed_tools
    assert "pytest" not in allowed_tools
