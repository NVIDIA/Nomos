# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not installed")
    return path


def test_compose_mtls_overlay_renders_protected_edges() -> None:
    result = subprocess.run(
        [
            _tool("docker"),
            "compose",
            "-f",
            "deploy/docker-compose.yml",
            "-f",
            "deploy/docker-compose.mtls.yml",
            "config",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    compose = yaml.safe_load(result.stdout)
    services = compose["services"]
    assert "council-mtls" in services
    assert services["council-mtls"]["network_mode"] == "service:council"
    assert services["council"]["entrypoint"][-1] == "127.0.0.1"
    assert services["monitor"]["environment"]["MONITOR_UPSTREAM"].startswith("https://")
    assert services["monitor"]["environment"]["MONITOR_LISTENER_TLS_ENABLED"] == "true"
    assert services["redis"]["command"][-2] == "--requirepass"
    assert services["redis"]["command"][-1]
    assert "--tls-auth-clients" in services["redis"]["command"]
    assert services["opa"]["command"].count("--authentication=tls") == 1


def test_compose_sentinel_mtls_overlay_protects_discovery_and_data_nodes() -> None:
    result = subprocess.run(
        [
            _tool("docker"),
            "compose",
            "-f",
            "deploy/docker-compose.yml",
            "-f",
            "deploy/docker-compose.redis-sentinel.yml",
            "-f",
            "deploy/docker-compose.mtls.yml",
            "-f",
            "deploy/docker-compose.redis-sentinel-mtls.yml",
            "config",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    services = yaml.safe_load(result.stdout)["services"]
    for name in ("redis", "redis-1", "redis-2"):
        command = "\n".join(services[name]["command"])
        assert "port 0" in command
        assert "tls-port 6379" in command
        assert "tls-replication yes" in command
        assert "--tls" in services[name]["healthcheck"]["test"][-1]
    for name in ("redis-sentinel-1", "redis-sentinel-2", "redis-sentinel-3"):
        command = "\n".join(services[name]["command"])
        assert "port 0" in command
        assert "tls-port 26379" in command
        assert "tls-auth-clients yes" in command
        assert "--tls" in services[name]["healthcheck"]["test"][-1]


def test_helm_mtls_values_render_sidecar_and_projected_identities() -> None:
    settings = [
        "mtls.enabled=true",
        "mtls.caSecretName=nomos-ca",
        "mtls.identities.council=nomos-council-tls",
        "mtls.identities.monitor=nomos-monitor-tls",
        "mtls.identities.councilGrpc=nomos-grpc-tls",
        "mtls.identities.councilGateway=nomos-gateway-tls",
        "mtls.identities.executor=nomos-executor-tls",
        "mtls.identities.openshell=nomos-openshell-tls",
        "mtls.identities.agentSignup=nomos-signup-tls",
        "mtls.identities.redis=nomos-redis-tls",
        "mtls.identities.opa=nomos-opa-tls",
    ]
    args = [_tool("helm"), "template", "nomos", "deploy/kubernetes/nomos"]
    for setting in settings:
        args.extend(("--set", setting))
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployments = {
        item["metadata"]["name"]: item for item in documents if item.get("kind") == "Deployment"
    }
    council = deployments["nomos-council"]
    assert council["spec"]["template"]["spec"]["serviceAccountName"] == "nomos-council"
    assert {item["name"] for item in council["spec"]["template"]["spec"]["containers"]} >= {
        "council",
        "council-mtls",
    }
    assert council["spec"]["template"]["spec"]["containers"][0]["command"][-1] == "127.0.0.1"
    council_service = next(
        item
        for item in documents
        if item.get("kind") == "Service" and item["metadata"]["name"] == "nomos-council"
    )
    assert council_service["spec"]["ports"][0]["targetPort"] == "mtls"
    monitor = deployments["nomos-monitor"]
    assert monitor["spec"]["template"]["spec"]["serviceAccountName"] == "nomos-monitor"
    service_accounts = {
        item["metadata"]["name"] for item in documents if item.get("kind") == "ServiceAccount"
    }
    assert {"nomos-council", "nomos-monitor", "nomos-executor"} <= service_accounts
    env = {
        item["name"]: item.get("value")
        for item in monitor["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["MONITOR_UPSTREAM_TLS_ENABLED"] == "true"
    assert env["MONITOR_LISTENER_TLS_ENABLED"] == "true"
    redis_config = next(
        item
        for item in documents
        if item.get("kind") == "ConfigMap" and item["metadata"]["name"] == "nomos-redis-config"
    )
    assert "tls-auth-clients yes" in redis_config["data"]["redis.conf"]


def test_helm_strict_profile_rejects_plaintext_transport() -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--set",
            "deploymentProfile=alpha",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires mtls.enabled=true" in result.stderr


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("postgres.dev.enabled=true", "development PostgreSQL listener is plaintext"),
        ("vault.dev.enabled=true", "development Vault listener is plaintext"),
    ],
)
def test_helm_mtls_rejects_plaintext_development_backends(setting: str, message: str) -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "-f",
            "deploy/kubernetes/mtls-values.example.yaml",
            "--set",
            setting,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
