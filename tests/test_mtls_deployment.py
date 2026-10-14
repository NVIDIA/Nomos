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
            "--profile",
            "executor-default",
            "--profile",
            "executor-openshell",
            "--profile",
            "executor-dev",
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
    nginx_config = (ROOT / "deploy/nginx/council-mtls.conf").read_text()
    assert "proxy_pass http://127.0.0.1:9901;" in nginx_config
    assert "proxy_pass http://council:9901;" not in nginx_config
    assert "proxy_set_header Host 127.0.0.1:9901;" in nginx_config
    assert "ssl_verify_client on;" in nginx_config
    assert "client_max_body_size 0;" in nginx_config
    assert "optional_no_ca" not in nginx_config
    assert services["council"]["entrypoint"][-1] == "127.0.0.1"
    assert services["monitor"]["environment"]["MONITOR_UPSTREAM"].startswith("https://")
    assert services["monitor"]["environment"]["MONITOR_LISTENER_TLS_ENABLED"] == "true"
    assert services["agent-signup"]["environment"]["NOMOS_SIGNUP_COUNCIL_ENDPOINT"].startswith(
        "https://"
    )
    for name in ("executor-default", "executor-openshell"):
        environment = services[name]["environment"]
        assert environment["EXECUTOR_COUNCIL_URL"] == "https://council:9443"
        assert environment["EXECUTOR_COUNCIL_TLS_ENABLED"] == "true"
        assert environment["EXECUTOR_COUNCIL_TLS_CA_FILE"]
        assert environment["EXECUTOR_COUNCIL_TLS_CERT_FILE"]
        assert environment["EXECUTOR_COUNCIL_TLS_KEY_FILE"]
        assert services[name]["healthcheck"]["test"][-1].startswith("https://")
    assert services["redis"]["command"][-2] == "--requirepass"
    assert services["redis"]["command"][-1]
    assert "--tls-auth-clients" in services["redis"]["command"]
    assert services["opa"]["command"].count("--authentication=tls") == 1
    dev = services["executor-dev"]
    assert dev["environment"]["EXECUTOR_REDIS_URL"].startswith("rediss://")
    assert dev["environment"]["EXECUTOR_REDIS_TLS_ENABLED"] == "true"
    assert dev["environment"]["EXECUTOR_LISTENER_TLS_ENABLED"] == "true"
    assert any(mount["target"] == "/run/nomos/tls/tls.crt" for mount in dev["volumes"])
    assert dev["healthcheck"]["test"][-1].startswith("https://")


def test_compose_mtls_preserves_configured_demo_executor() -> None:
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
        env={**os.environ, "NOMOS_EXECUTOR_URL": "https://executor-default:9902"},
        check=True,
        capture_output=True,
        text=True,
    )
    demo = yaml.safe_load(result.stdout)["services"]["demo"]
    assert demo["environment"]["NOMOS_EXECUTOR_URL"] == "https://executor-default:9902"


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
        assert "if [ ! -s /data/sentinel/sentinel.conf ]" in command
        assert "redis-sentinel /data/sentinel/sentinel.conf" in command
        mounts = services[name]["volumes"]
        assert any(
            mount["target"] == "/data/sentinel"
            and mount["source"] == f"redis-sentinel-mtls-{name[-1]}-data"
            for mount in mounts
        )
        assert "--tls" in services[name]["healthcheck"]["test"][-1]


@pytest.mark.parametrize(
    ("compose_file", "anchor"),
    [
        ("deploy/docker-compose.redis-sentinel.yml", "x-redis-sentinel-command"),
        ("deploy/docker-compose.redis-sentinel-mtls.yml", "x-redis-sentinel-mtls-command"),
    ],
)
def test_sentinel_startup_refreshes_password_without_erasing_learned_state(
    tmp_path: Path, compose_file: str, anchor: str
) -> None:
    script = yaml.safe_load((ROOT / compose_file).read_text())[anchor][-1]
    config_dir = tmp_path / "sentinel"
    script = script.replace("$$", "$").replace("/data/sentinel", str(config_dir))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sentinel = bindir / "redis-sentinel"
    sentinel.write_text("#!/bin/sh\nexit 0\n")
    sentinel.chmod(0o755)

    def start(
        password: str,
        service: str = "nomos-master",
        down_after: str = "5000",
        failover: str = "30000",
    ) -> None:
        result = subprocess.run(
            ["sh", "-c", script],
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "REDIS_PASSWORD": password,
                "REDIS_SENTINEL_SERVICE_NAME": service,
                "REDIS_SENTINEL_DOWN_AFTER_MS": down_after,
                "REDIS_SENTINEL_FAILOVER_TIMEOUT_MS": failover,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    start("first-password")
    conf = config_dir / "sentinel.conf"
    with conf.open("a") as handle:
        handle.write("sentinel known-replica nomos-master redis-2 6379\n")
    start("second-password", down_after="7000", failover="45000")
    refreshed = conf.read_text()
    assert "requirepass second-password" in refreshed
    assert "sentinel auth-pass nomos-master second-password" in refreshed
    assert "first-password" not in refreshed
    assert "sentinel known-replica nomos-master redis-2 6379" in refreshed
    assert "sentinel down-after-milliseconds nomos-master 7000" in refreshed
    assert "sentinel failover-timeout nomos-master 45000" in refreshed

    start("second-password", "replacement-master")
    reconfigured = conf.read_text()
    assert "sentinel monitor replacement-master redis 6379 2" in reconfigured
    assert "sentinel auth-pass replacement-master second-password" in reconfigured
    assert "sentinel known-replica nomos-master" not in reconfigured


@pytest.mark.parametrize(
    ("compose_file", "anchor", "state_volume_prefix"),
    [
        (
            "deploy/docker-compose.redis-sentinel.yml",
            "x-redis-node-command",
            "redis-sentinel-{}-data",
        ),
        (
            "deploy/docker-compose.redis-sentinel-mtls.yml",
            "x-redis-node-mtls-command",
            "redis-sentinel-mtls-{}-data",
        ),
    ],
)
@pytest.mark.parametrize(
    ("records", "expected_master"),
    [
        ((("redis-1", 1), ("redis-1", 1), ("redis-1", 1)), "redis-1"),
        ((("redis-1", 1), ("redis", 2), ("redis", 2)), "redis"),
        ((("redis-1", 3), ("redis", 2), ("redis", 2)), None),
    ],
)
def test_redis_cold_start_uses_authoritative_persisted_sentinel_quorum(
    tmp_path: Path,
    compose_file: str,
    anchor: str,
    state_volume_prefix: str,
    records: tuple[tuple[str, int], ...],
    expected_master: str | None,
) -> None:
    overlay = yaml.safe_load((ROOT / compose_file).read_text())
    for node in ("redis", "redis-1", "redis-2"):
        mounts = overlay["services"][node]["volumes"]
        for index in (1, 2, 3):
            state_volume = state_volume_prefix.format(index)
            assert f"{state_volume}:/run/nomos/sentinel-state-{index}:ro" in mounts

    for index, (host, epoch) in enumerate(records, start=1):
        state_dir = tmp_path / f"sentinel-state-{index}"
        state_dir.mkdir()
        (state_dir / "sentinel.conf").write_text(
            f"sentinel monitor nomos-master {host} 6379 2\n"
            f"sentinel config-epoch nomos-master {epoch}\n"
        )
    script = (
        overlay[anchor][-1]
        .replace("$$", "$")
        .replace("/run/nomos/sentinel-state", str(tmp_path / "sentinel-state"))
        .replace("/tmp/redis.conf", str(tmp_path / "source.conf"))
        .replace("/tmp/current-master", str(tmp_path / "current-master"))
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "timeout": 'shift\nexec "$@"',
        "redis-cli": "exit 1",
        "redis-server": 'cp "$1" "$CAPTURE_FILE"',
    }.items():
        executable = bindir / name
        executable.write_text(f"#!/bin/sh\n{body}\n")
        executable.chmod(0o755)

    for node in ("redis", "redis-1", "redis-2"):
        captured = tmp_path / f"{node}.conf"
        result = subprocess.run(
            ["sh", "-c", script],
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "CAPTURE_FILE": str(captured),
                "REDIS_PASSWORD": "test-password",  # pragma: allowlist secret
                "REDIS_SENTINEL_SERVICE_NAME": "nomos-master",
                "REDIS_INITIAL_MASTER": "redis",
                "REDIS_ANNOUNCE_HOST": node,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        if expected_master is None:
            assert result.returncode != 0
            assert "Ambiguous persisted Sentinel master" in result.stderr
            assert not captured.exists()
            continue
        assert result.returncode == 0, result.stderr
        text = captured.read_text()
        if node == expected_master:
            assert "replicaof " not in text
        else:
            assert f"replicaof {expected_master} 6379" in text


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
        "councilGateway.enabled=true",
        "global.podAnnotations.nomos-test-reload=enabled",
        "monitor.service.port=10901",
        "council.mtlsResources.requests.cpu=23m",
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
    council_containers = {
        item["name"]: item for item in council["spec"]["template"]["spec"]["containers"]
    }
    council_app = council_containers["council"]
    for probe_name in ("livenessProbe", "readinessProbe"):
        command = council_app[probe_name]["exec"]["command"]
        assert "nomos-council:9443:127.0.0.1" in command
        assert "https://nomos-council:9443/debug/tools/list" in command
        assert "http://127.0.0.1:9901/debug/tools/list" not in command
    assert council_containers["council-mtls"]["resources"]["requests"]["cpu"] == "23m"
    council_service = next(
        item
        for item in documents
        if item.get("kind") == "Service" and item["metadata"]["name"] == "nomos-council"
    )
    assert council_service["spec"]["ports"][0]["targetPort"] == "mtls"
    monitor = deployments["nomos-monitor"]
    assert monitor["spec"]["template"]["spec"]["serviceAccountName"] == "nomos-monitor"
    for probe_name in ("livenessProbe", "readinessProbe"):
        command = monitor["spec"]["template"]["spec"]["containers"][0][probe_name]["exec"][
            "command"
        ]
        assert "nomos-monitor:9901:127.0.0.1" in command
        assert "https://nomos-monitor:9901/livez" in command
        assert not any("https://localhost" in item for item in command)
    executor_openshell = deployments["nomos-executor-openshell"]
    executor_container = executor_openshell["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("livenessProbe", "readinessProbe"):
        command = executor_container[probe_name]["exec"]["command"]
        assert "nomos-executor-openshell:9906:127.0.0.1" in command
        assert "https://nomos-executor-openshell:9906/v1/health" in command
    service_accounts = {
        item["metadata"]["name"] for item in documents if item.get("kind") == "ServiceAccount"
    }
    assert {"nomos-council", "nomos-monitor", "nomos-executor"} <= service_accounts
    controllers = {
        item["metadata"]["name"]: item
        for item in documents
        if item.get("kind") in {"Deployment", "StatefulSet"}
    }
    for name in (
        "nomos-agent-signup",
        "nomos-council",
        "nomos-council-gateway",
        "nomos-council-grpc",
        "nomos-executor-openshell",
        "nomos-monitor",
        "nomos-opa",
        "nomos-openshell",
        "nomos-redis",
        "nomos-redis-sentinel",
    ):
        assert (
            controllers[name]["spec"]["template"]["metadata"]["annotations"]["nomos-test-reload"]
            == "enabled"
        )
    tls_volume = next(
        volume
        for volume in council["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "nomos-mtls"
    )
    assert tls_volume["projected"]["defaultMode"] == 0o440
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
    council_mtls_config = next(
        item
        for item in documents
        if item.get("kind") == "ConfigMap" and item["metadata"]["name"] == "nomos-council-mtls"
    )
    nginx_config = council_mtls_config["data"]["nginx.conf"]
    assert "proxy_pass http://127.0.0.1:9901;" in nginx_config
    assert "proxy_set_header Host 127.0.0.1:9901;" in nginx_config
    assert "ssl_verify_client on;" in nginx_config
    assert "client_max_body_size 0;" in nginx_config
    assert "optional_no_ca" not in nginx_config
    assert "fastcgi_temp_path /tmp/fastcgi_temp;" in nginx_config
    assert "uwsgi_temp_path /tmp/uwsgi_temp;" in nginx_config
    assert "scgi_temp_path /tmp/scgi_temp;" in nginx_config


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


def test_helm_mtls_service_ledger_and_custom_redis_port_render_securely() -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "-f",
            "deploy/kubernetes/mtls-values.example.yaml",
            "--set",
            "monitor.ledgerSource=api",
            "--set",
            "redis.service.port=6380",
            "--set",
            "storage.mode=service",
            "--set",
            "signing.provider=vault",
            "--set",
            "signing.councilKeyId=nomos-council",
            "--set",
            "signing.executorKeyId=nomos-executor",
            "--set",
            "signing.vault.addr=https://vault.example",
            "--set",
            "signing.vault.auth.role=nomos-signer",
            "--set",
            "networkPolicy.egress.vault.cidrs[0]=10.0.0.0/8",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    monitor = next(
        item
        for item in documents
        if item.get("kind") == "Deployment" and item["metadata"]["name"] == "nomos-monitor"
    )
    monitor_env = {
        item["name"]: item.get("value")
        for item in monitor["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert monitor_env["MONITOR_LEDGER_API_URL"] == "https://nomos-council:9901"

    redis = next(
        item
        for item in documents
        if item.get("kind") == "StatefulSet" and item["metadata"]["name"] == "nomos-redis"
    )
    redis_container = redis["spec"]["template"]["spec"]["containers"][0]
    assert redis_container["ports"][0]["containerPort"] == 6380
    for probe_name in ("startupProbe", "livenessProbe", "readinessProbe"):
        command = redis_container[probe_name]["exec"]["command"][-1]
        assert "redis-cli -p 6380 " in command

    council = next(
        item
        for item in documents
        if item.get("kind") == "Deployment" and item["metadata"]["name"] == "nomos-council"
    )
    wait = next(
        item
        for item in council["spec"]["template"]["spec"]["initContainers"]
        if item["name"] == "wait-for-backends"
    )
    script = wait["args"][0]
    assert 'urlparse(addr).scheme.lower() != "https"' in script
    assert script.index("urlparse(addr)") < script.index("def vault_ready")


def test_helm_plaintext_redis_honors_custom_service_port() -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--set",
            "redis.service.port=6380",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    redis_config = next(
        item
        for item in yaml.safe_load_all(result.stdout)
        if item
        and item.get("kind") == "ConfigMap"
        and item["metadata"]["name"] == "nomos-redis-config"
    )
    assert "port 6380" in redis_config["data"]["redis.conf"]


def test_monitor_image_healthcheck_supports_listener_tls() -> None:
    dockerfile = (ROOT / "deploy/Dockerfile.monitor").read_text()
    assert 'tls_enabled="$(printf' in dockerfile
    assert '"$tls_enabled" = "yes"' in dockerfile
    assert '--cacert "$MONITOR_LISTENER_TLS_CLIENT_CA_FILE"' in dockerfile
    assert '--cert "$MONITOR_LISTENER_TLS_CERT_FILE"' in dockerfile
    assert '--key "$MONITOR_LISTENER_TLS_KEY_FILE"' in dockerfile
    assert "https://localhost:9901/healthz" in dockerfile


def test_helm_development_profile_allows_plaintext_vault_wait() -> None:
    """The bundled dev Vault is plaintext, so the Kind path must still render.

    Requiring https:// unconditionally left council and executor-openshell in
    Init:CrashLoopBackOff on the repository's own Kind deployment.
    """
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    manifests = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    waits = [
        container
        for doc in manifests
        if doc.get("kind") == "Deployment"
        for container in doc["spec"]["template"]["spec"].get("initContainers", [])
        if container["name"] == "wait-for-backends"
    ]
    assert waits, "the Kind values must still render the backend wait init container"
    for container in waits:
        assert 'urlparse(addr).scheme.lower() != "https"' not in container["args"][0]


@pytest.mark.parametrize("mode", [288, 256])
def test_helm_mtls_accepts_modes_without_world_access(mode: int) -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "-f",
            "deploy/kubernetes/mtls-values.example.yaml",
            "--set",
            f"mtls.secretDefaultMode={mode}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    projected = [
        volume["projected"]["defaultMode"]
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("kind") == "Deployment"
        for volume in doc["spec"]["template"]["spec"].get("volumes", [])
        if volume["name"] == "nomos-mtls"
    ]
    assert projected, "mTLS values must project certificate material"
    assert set(projected) == {mode}


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        (420, "would grant world access"),
        (511, "would grant world access"),
        ("0440", "must be a number"),
        (1000, "must be between 1 and 511"),
        (0, "must be between 1 and 511"),
    ],
)
def test_helm_mtls_rejects_insecure_secret_default_mode(mode: object, message: str) -> None:
    result = subprocess.run(
        [
            _tool("helm"),
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "-f",
            "deploy/kubernetes/mtls-values.example.yaml",
            "--set",
            f"mtls.secretDefaultMode={mode}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
