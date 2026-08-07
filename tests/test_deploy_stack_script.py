# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BASH_NAMEREF_CHECK = subprocess.run(
    ["bash", "-c", "declare value=ok ref; declare -n ref=value"],
    check=False,
    capture_output=True,
)
BASH_SUPPORTS_NAMEREF = _BASH_NAMEREF_CHECK.returncode == 0
_KIND_SECRET_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "COUNCIL_MANAGEMENT_TOKEN",
    "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
    "DEMO_AUTH_PASSWORD",
    "DEMO_AUTH_USER",
    "MONITOR_DASHBOARD_PASSWORD",
    "MONITOR_DASHBOARD_USER",
    "MONITOR_INTERNAL_TOKEN",
    "NOMOS_AGENT_ID",
    "NOMOS_AUTH_TOKEN",
    "NOMOS_AUTH_TOKEN_K8S_ADMIN",
    "NOMOS_AUTH_TOKEN_K8S_DEV",
    "NOMOS_AUTH_TOKEN_K8S_READONLY",
    "NOMOS_SIGNUP_ADMIN_PASSWORD",
    "NOMOS_SIGNUP_ADMIN_USER",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "REDIS_PASSWORD",
)
_SONAR_BASH_SCRIPTS = (
    ROOT / "deploy/kubernetes/scripts/kind-up.sh",
    ROOT / "deploy/kubernetes/scripts/kind-verify.sh",
    ROOT / "deploy/scripts/compose-down.sh",
    ROOT / "deploy/scripts/compose-up.sh",
    ROOT / "deploy/scripts/compose-verify.sh",
)


def _kind_secret_test_env(env_file: Path) -> dict[str, str]:
    env = {**os.environ, "ENV_FILE": str(env_file)}
    env.update(dict.fromkeys(_KIND_SECRET_ENV_KEYS, ""))
    return env


def test_deploy_stack_defaults_to_kind_target() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert 'DEPLOY_TARGET="${DEPLOY_TARGET:-kind}"' in deploy_script
    assert 'KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"' in deploy_script


def test_deploy_stack_help_prints_usage() -> None:
    result = subprocess.run(
        ["./scripts/deploy_stack.sh", "--help"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Usage:" in result.stdout
    assert "DEPLOY_TARGET=compose COMPOSE_REDIS_TOPOLOGY=standalone" in result.stdout
    assert "DEPLOY_TARGET=kind|compose" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("script", _SONAR_BASH_SCRIPTS, ids=lambda path: path.name)
def test_deployment_script_has_valid_bash_syntax(script: Path) -> None:
    subprocess.run(
        ["bash", "-n", str(script)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(not BASH_SUPPORTS_NAMEREF, reason="requires Bash nameref support")
def test_deploy_stack_compose_defaults_to_sentinel(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      resolve_compose_redis_topology
      compose_file_args args
      printf '%s\n' "$COMPOSE_REDIS_TOPOLOGY" "${{args[@]}}"
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_REDIS_TOPOLOGY": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == "sentinel"
    assert str(ROOT / "deploy/docker-compose.redis-sentinel.yml") in lines


@pytest.mark.skipif(not BASH_SUPPORTS_NAMEREF, reason="requires Bash nameref support")
def test_deploy_stack_compose_topology_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_REDIS_TOPOLOGY=standalone\n")
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      resolve_compose_redis_topology
      compose_file_args args
      printf '%s\n' "$COMPOSE_REDIS_TOPOLOGY" "${{args[@]}}"
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_REDIS_TOPOLOGY": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == "standalone"
    assert str(ROOT / "deploy/docker-compose.redis-sentinel.yml") not in lines


def test_build_kind_images_defaults_to_nomos_cluster() -> None:
    build_script = (ROOT / "deploy/kubernetes/scripts/build-kind-images.sh").read_text()

    assert 'KIND_CLUSTER_NAME="${1:-${KIND_CLUSTER_NAME:-nomos}}"' in build_script


def test_kind_port_forward_specs_use_default_services_and_env_ports(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MONITOR_PORT=19901",
                "COUNCIL_GRPC_PORT=19904",
                "AGENT_SIGNUP_PORT=19905",
                "DEMO_PORT=18000",
                "EXECUTOR_OPENSHELL_PORT=19907",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_port_forward_specs
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "MONITOR_PORT": "",
        "COUNCIL_GRPC_PORT": "",
        "AGENT_SIGNUP_PORT": "",
        "DEMO_PORT": "",
        "EXECUTOR_OPENSHELL_PORT": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    lines = [line.split("\t") for line in result.stdout.strip().splitlines()]
    assert lines == [
        [
            "monitor",
            "nomos-monitor",
            "19901",
            "9901",
            "http://127.0.0.1:19901/",
            "http://127.0.0.1:19901/healthz",
        ],
        [
            "council-grpc",
            "nomos-council-grpc",
            "19904",
            "9904",
            "grpc://127.0.0.1:19904",
            "tcp://127.0.0.1:19904",
        ],
        [
            "agent-signup",
            "nomos-agent-signup",
            "19905",
            "9905",
            "https://127.0.0.1:19905/",
            "https://127.0.0.1:19905/healthz",
        ],
        [
            "demo",
            "nomos-demo",
            "18000",
            "8000",
            "https://127.0.0.1:18000/",
            "https://127.0.0.1:18000/api/health",
        ],
        [
            "executor",
            "nomos-executor-openshell",
            "19907",
            "9907",
            "http://127.0.0.1:19907/",
            "http://127.0.0.1:19907/v1/health",
        ],
    ]


def _run_compose_summary(
    tmp_path: Path,
    *,
    with_agent_signup: bool,
    with_demo: bool,
) -> str:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NOMOS_PUBLIC_HOST=nomos.example.com",
                "MONITOR_DASHBOARD_USER=monitor-user",  # pragma: allowlist secret
                "NOMOS_SIGNUP_ADMIN_USER=signup-user",
                "AGENT_SIGNUP_PORT=19905",
                "DEMO_PORT=18000",
                "",
            ],
        ),
    )
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_prereqs() {{ :; }}
      ensure_env() {{ :; }}
      ensure_dynamic_council_config() {{ :; }}
      ensure_runtime_read_permissions() {{ :; }}
      remove_inactive_services() {{ :; }}
      preflight_ports() {{ :; }}
      compose() {{ :; }}
      wait_for_health() {{ :; }}
      create_account() {{ :; }}
      main_compose
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_REDIS_TOPOLOGY": "standalone",
        "WITH_OPA": "0",
        "WITH_AGENT_SIGNUP": "1" if with_agent_signup else "0",
        "WITH_DEMO": "1" if with_demo else "0",
        "EXECUTOR_PROFILE": "",
        "NOMOS_PUBLIC_HOST": "",
        "AGENT_SIGNUP_PORT": "",
        "DEMO_PORT": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def test_compose_summary_uses_https_for_browser_services(tmp_path: Path) -> None:
    output = _run_compose_summary(
        tmp_path,
        with_agent_signup=True,
        with_demo=True,
    )

    assert "Agent signup:    https://nomos.example.com:19905/" in output
    assert "Agent admin:     https://nomos.example.com:19905/admin" in output
    assert "Demo UI:         https://nomos.example.com:18000" in output


def test_compose_summary_omits_disabled_browser_services(tmp_path: Path) -> None:
    output = _run_compose_summary(
        tmp_path,
        with_agent_signup=False,
        with_demo=False,
    )

    assert "Agent signup:" not in output
    assert "Agent admin:" not in output
    assert "Demo UI:" not in output


def test_stop_kind_port_forward_succeeds_without_pid_file(tmp_path: Path) -> None:
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      stop_kind_port_forward monitor
      printf 'ok\\n'
    """
    env = {
        **os.environ,
        "KIND_PORT_FORWARD_DIR": str(tmp_path),
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout == "ok\n"


def test_kind_port_forward_detaches_kubectl_binary() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert (
        'setsid kubectl --context "$KIND_CONTEXT" -n "$KIND_NAMESPACE" port-forward'
        in deploy_script
    )
    assert (
        'nohup kubectl --context "$KIND_CONTEXT" -n "$KIND_NAMESPACE" port-forward' in deploy_script
    )
    assert "have_cmd setsid" in deploy_script
    assert "nohup kubectl_kind" not in deploy_script
    assert "setsid kubectl_kind" not in deploy_script


def test_kind_secret_values_file_uses_env_file_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    values_file = tmp_path / "kind-secrets.yaml"
    env_file.write_text(
        "\n".join(
            [
                "NVIDIA_API_KEY=nvapi-test",
                "REDIS_PASSWORD=redis-test",
                "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY=integrity-test",
                "COUNCIL_MANAGEMENT_TOKEN=management-test",
                "MONITOR_DASHBOARD_PASSWORD=monitor-test",
                "NOMOS_SIGNUP_ADMIN_PASSWORD=signup-test",
                "DEMO_AUTH_PASSWORD=demo-ui-test",
                "NOMOS_AUTH_TOKEN=demo-token",
                "NOMOS_AGENT_ID=demo-agent",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      write_kind_secret_values {values_file}
    """
    env = _kind_secret_test_env(env_file)

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    values = values_file.read_text()
    assert 'redisPassword: "redis-test"' in values  # pragma: allowlist secret
    assert 'nvidiaApiKey: "nvapi-test"' in values  # pragma: allowlist secret
    assert 'councilRedisAccountIntegrityKey: "integrity-test"' in values  # pragma: allowlist secret
    assert 'councilManagementToken: "management-test"' in values  # pragma: allowlist secret
    assert 'monitorDashboardUser: "admin"' in values
    assert 'monitorDashboardPassword: "monitor-test"' in values  # pragma: allowlist secret
    assert 'signupAdminUser: "admin"' in values
    assert 'signupAdminPassword: "signup-test"' in values  # pragma: allowlist secret
    assert 'demoAuthUser: "demo"' in values
    assert 'demoAuthPassword: "demo-ui-test"' in values  # pragma: allowlist secret
    assert 'demoAuthToken: "demo-token"' in values  # pragma: allowlist secret
    assert 'demoAgentId: "demo-agent"' in values
    assert "openaiApiKey" not in values


def test_kind_values_do_not_seed_fake_demo_persona_tokens() -> None:
    kind_values = (ROOT / "deploy" / "kubernetes" / "kind-values.yaml").read_text()

    assert "demoK8sAdminToken" not in kind_values
    assert "demoK8sDevToken" not in kind_values
    assert "demoK8sReadonlyToken" not in kind_values
    assert "kind-demo-k8s" not in kind_values


def test_kind_secret_patch_file_uses_kubernetes_secret_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    patch_file = tmp_path / "kind-secret-patch.json"
    env_file.write_text(
        "\n".join(
            [
                "NVIDIA_API_KEY=nvapi-test",
                "REDIS_PASSWORD=redis-test",
                "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY=integrity-test",
                "COUNCIL_MANAGEMENT_TOKEN=management-test",
                "MONITOR_INTERNAL_TOKEN=monitor-token",
                "MONITOR_DASHBOARD_PASSWORD=monitor-test",
                "NOMOS_SIGNUP_ADMIN_PASSWORD=signup-test",
                "DEMO_AUTH_PASSWORD=demo-ui-test",
                "NOMOS_AUTH_TOKEN=demo-token",
                "NOMOS_AGENT_ID=demo-agent",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      write_kind_secret_patch {patch_file}
    """
    env = _kind_secret_test_env(env_file)

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    patch = patch_file.read_text()
    assert '"REDIS_PASSWORD":"cmVkaXMtdGVzdA=="' in patch  # pragma: allowlist secret
    assert '"NVIDIA_API_KEY":"bnZhcGktdGVzdA=="' in patch  # pragma: allowlist secret
    assert '"MONITOR_INTERNAL_TOKEN":"bW9uaXRvci10b2tlbg=="' in patch  # pragma: allowlist secret
    assert '"MONITOR_DASHBOARD_USER":"YWRtaW4="' in patch
    assert '"NOMOS_SIGNUP_ADMIN_USER":"YWRtaW4="' in patch
    assert '"DEMO_AUTH_USER":"ZGVtbw=="' in patch
    assert '"DEMO_AUTH_PASSWORD":"ZGVtby11aS10ZXN0"' in patch  # pragma: allowlist secret
    assert "openaiApiKey" not in patch


def test_ensure_env_alpha_profile_sets_alpha_runtime_flags(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NVIDIA_API_KEY=nvapi-test",
                "REDIS_PASSWORD=redis-test",
                "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY=integrity-test",
                "COUNCIL_MANAGEMENT_TOKEN=management-test",
                "MONITOR_DASHBOARD_USER=admin",
                "MONITOR_DASHBOARD_PASSWORD=monitor-test",
                "NOMOS_SIGNUP_ADMIN_USER=admin",
                "NOMOS_SIGNUP_ADMIN_PASSWORD=signup-test",
                "NOMOS_PUBLIC_HOST=127.0.0.1",
                "COUNCIL_OPA_ENABLED=true",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_env
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
        "DYNAMIC_CONFIG_DIR": str(tmp_path / "dynamic-config"),
        "NOMOS_DEPLOY_PROFILE": "alpha",
    }

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    contents = env_file.read_text()
    assert "NOMOS_DEPLOY_PROFILE=alpha\n" in contents
    assert "COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE=true\n" in contents
    assert "NOMOS_ALPHA_ALLOW_PUBLIC_BIND=true\n" in contents
    assert "MONITOR_INTERNAL_TOKEN=\n" not in contents
    assert "MONITOR_INTERNAL_TOKEN=" in contents


@pytest.mark.parametrize(
    "key",
    [
        "REDIS_PASSWORD",
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
        "COUNCIL_MANAGEMENT_TOKEN",
    ],
)
@pytest.mark.parametrize("initial_value", [None, "", "stale-file-value"])
@pytest.mark.parametrize(
    "override_suffix",
    ["value", r"value\nINJECTED_SETTING=true", r"value\rINJECTED_SETTING=true"],
    ids=["plain", "escaped-lf", "escaped-cr"],
)
def test_ensure_env_persists_explicit_bootstrap_secret_overrides(
    tmp_path: Path,
    key: str,
    initial_value: str | None,
    override_suffix: str,
) -> None:
    env_file = tmp_path / ".env"
    file_values = {
        "NVIDIA_API_KEY": "nvapi-test",  # pragma: allowlist secret
        "REDIS_PASSWORD": "redis-file-value",  # pragma: allowlist secret
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY": "integrity-file-value",
        "COUNCIL_MANAGEMENT_TOKEN": "management-file-value",
        "MONITOR_INTERNAL_TOKEN": "monitor-token",
        "MONITOR_DASHBOARD_USER": "admin",
        "MONITOR_DASHBOARD_PASSWORD": "monitor-password",  # pragma: allowlist secret
        "NOMOS_SIGNUP_ADMIN_USER": "admin",
        "NOMOS_SIGNUP_ADMIN_PASSWORD": "signup-password",  # pragma: allowlist secret
        "DEMO_AUTH_USER": "demo",
        "DEMO_AUTH_PASSWORD": "demo-password",  # pragma: allowlist secret
        "NOMOS_PUBLIC_HOST": "127.0.0.1",
    }
    if initial_value is None:
        del file_values[key]
    else:
        file_values[key] = initial_value
    env_file.write_text("".join(f"{name}={value}\n" for name, value in file_values.items()))

    override = f"caller-{key.lower()}-{override_suffix}"  # pragma: allowlist secret
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_env
      unset "$KEY_UNDER_TEST"
      [[ "$(current_value "$KEY_UNDER_TEST")" == "$EXPECTED_VALUE" ]]
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
        "DYNAMIC_CONFIG_DIR": str(tmp_path / "dynamic-config"),
        "REDIS_PASSWORD": "",
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY": "",
        "COUNCIL_MANAGEMENT_TOKEN": "",
        "KEY_UNDER_TEST": key,
        "EXPECTED_VALUE": override,
        key: override,
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    env_lines = env_file.read_text().splitlines()
    target_lines = [line for line in env_lines if line.startswith(f"{key}=")]
    assert target_lines == [f"{key}={override}"]
    assert "INJECTED_SETTING=true" not in env_lines
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert override not in result.stdout
    assert override not in result.stderr


@pytest.mark.parametrize(
    "key",
    [
        "REDIS_PASSWORD",
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
        "COUNCIL_MANAGEMENT_TOKEN",
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    ["first\nsecond", "first\rsecond", "first\r\nsecond"],
    ids=["lf", "cr", "crlf"],
)
@pytest.mark.parametrize("existing_file", [False, True], ids=["missing", "existing"])
def test_ensure_env_rejects_multiline_bootstrap_secrets_without_rewriting_env_file(
    tmp_path: Path,
    key: str,
    invalid_value: str,
    existing_file: bool,
) -> None:
    env_file = tmp_path / ".env"
    original_contents = b"ORIGINAL_SETTING=unchanged\n"
    if existing_file:
        env_file.write_bytes(original_contents)
        env_file.chmod(0o640)

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_env
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "REDIS_PASSWORD": "",
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY": "",
        "COUNCIL_MANAGEMENT_TOKEN": "",
        key: invalid_value,
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert key in result.stderr
    assert invalid_value not in result.stdout
    assert invalid_value not in result.stderr
    if existing_file:
        assert env_file.read_bytes() == original_contents
        assert env_file.stat().st_mode & 0o777 == 0o600
    else:
        assert not env_file.exists()


def test_kind_deploy_passes_alpha_values_to_helm() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert 'helm_args+=(--set "deploymentProfile=$deploy_profile")' in deploy_script
    assert '--set "council.allowUncertifiedEvidence=true"' in deploy_script
    assert '--set "council.allowPublicBind=true"' in deploy_script
    assert '--set "networkPolicy.enabled=true"' in deploy_script
    assert "sync_kind_existing_secret_from_env" in deploy_script


def test_helm_redis_default_renders_sentinel_topology() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--namespace",
            "nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
            "--show-only",
            "templates/redis.yaml",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    rendered = result.stdout
    assert "kind: StatefulSet" in rendered
    assert "replicas: 3" in rendered
    assert "name: nomos-redis-sentinel" in rendered
    assert "sentinel monitor nomos-master nomos-redis-0.nomos-redis-headless 6379 2" in rendered
    assert "volumeClaimTemplates:" in rendered
    assert "exec redis-sentinel /tmp/sentinel.conf" in rendered
    assert "kind: PodDisruptionBudget" in rendered
    assert "name: nomos-redis-headless" in rendered
    assert "exec redis-server /tmp/redis.conf" in rendered
    assert "maxmemory-policy noeviction" in rendered
    assert "readOnlyRootFilesystem: true" in rendered


def test_helm_redis_standalone_override_renders_one_redis_pod() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--namespace",
            "nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
            "--set",
            "redis.topology.mode=standalone",
            "--show-only",
            "templates/redis.yaml",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    rendered = result.stdout
    assert "kind: StatefulSet" in rendered
    assert "replicas: 1" in rendered
    assert "name: nomos-redis-sentinel" not in rendered
    assert "volumeClaimTemplates:" not in rendered


def test_helm_rejects_invalid_redis_topology_mode() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--namespace",
            "nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
            "--set",
            "redis.topology.mode=cluster",
            "--show-only",
            "templates/redis.yaml",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "redis.topology.mode must be 'standalone' or 'sentinel'" in result.stderr


def test_helm_consumers_receive_sentinel_redis_env() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--namespace",
            "nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
            "--set",
            "redis.topology.mode=sentinel",
            "--show-only",
            "templates/council.yaml",
            "--show-only",
            "templates/executor-openshell.yaml",
            "--show-only",
            "templates/agent-signup.yaml",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    rendered = result.stdout
    assert "COUNCIL_REDIS_MODE" in rendered
    assert "EXECUTOR_REDIS_MODE" in rendered
    assert "NOMOS_SIGNUP_REDIS_MODE" in rendered
    assert rendered.count('value: "sentinel"') >= 3
    assert rendered.count('value: "nomos-redis-sentinel:26379"') >= 3


def test_helm_network_policy_restricts_redis_ingress() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            "deploy/kubernetes/nomos",
            "--namespace",
            "nomos",
            "-f",
            "deploy/kubernetes/kind-values.yaml",
            "--set",
            "networkPolicy.enabled=true",
            "--show-only",
            "templates/networkpolicy.yaml",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    rendered = result.stdout
    assert "name: nomos-redis" in rendered
    assert "app.kubernetes.io/component: redis" in rendered
    assert "app.kubernetes.io/component: council" in rendered
    assert "app.kubernetes.io/component: executor-openshell" in rendered
    assert "app.kubernetes.io/component: agent-signup" in rendered
    assert "port: 6379" in rendered


def test_kind_deploy_creates_account_before_port_forwards() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert "create_kind_account" in deploy_script
    rollout_index = deploy_script.index(
        'wait_kind_rollouts || die "gave up waiting for some deployments'
    )
    account_index = deploy_script.index("create_kind_account\n\n  start_kind_port_forwards")
    assert account_index > rollout_index


def test_kind_account_bootstrap_uses_council_pod_cli() -> None:
    deploy_script = (ROOT / "scripts" / "deploy_stack.sh").read_text()

    assert 'kubectl_kind -n "$KIND_NAMESPACE" exec "deploy/$deployment" -c council' in deploy_script
    assert 'python scripts/agent_accounts.py show --json "$AGENT_ID"' in deploy_script
    assert (
        'set -- create --json --role "$AGENT_ROLE" --display-name "$AGENT_DISPLAY_NAME"'
        in deploy_script
    )
    assert 'store_kind_agent_token "$token"' in deploy_script
    assert 'sync_kind_agent_secret "$token"' in deploy_script
    assert "awk -F'\"' '$2 == \"token\" {print $4; exit}'" in deploy_script


def test_store_kind_agent_token_existing_secret_only_does_not_write_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      sync_kind_agent_secret() {{
        printf 'sync:%s\\n' "$1"
      }}
      set_env_var() {{
        printf 'set-env:%s\\n' "$1"
      }}
      store_kind_agent_token token-test
      [[ ! -e "$ENV_FILE" ]]
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
        "AGENT_ID": "agent-test",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "sync:token-test" in result.stdout
    assert "set-env:" not in result.stdout
    assert not env_file.exists()


def test_main_kind_existing_secret_only_uses_secret_without_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_kind_prereqs() {{ printf 'ensure_kind_prereqs\\n'; }}
      ensure_env() {{ printf 'ensure_env-called\\n'; return 99; }}
      ensure_kind_cluster() {{ printf 'ensure_kind_cluster\\n'; }}
      ensure_kind_existing_secret_only() {{ printf 'ensure_existing_secret\\n'; }}
      kind_secret_is_helm_owned() {{ return 1; }}
      helm_kind() {{ printf 'helm:%s\\n' "$*"; }}
      kubectl_kind() {{ printf 'kubectl:%s\\n' "$*"; }}
      wait_kind_rollouts() {{ printf 'wait_rollouts\\n'; }}
      create_kind_account() {{ printf 'create_account\\n'; }}
      start_kind_port_forwards() {{ printf 'port_forwards\\n'; }}
      print_kind_urls() {{ :; }}
      main_kind
      [[ ! -e "$ENV_FILE" ]]
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
        "SKIP_BUILD": "1",
        "KIND_ROLLOUT_RESTART": "0",
        "KIND_PORT_FORWARD": "0",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "ensure_env-called" not in result.stdout
    assert "ensure_existing_secret" in result.stdout
    assert "helm:upgrade --install nomos" in result.stdout
    assert "--set secrets.create=false --set secrets.existingSecret=nomos-secrets" in result.stdout
    assert "Secret source:    Kubernetes Secret nomos/nomos-secrets" in result.stdout
    assert not env_file.exists()


def test_main_kind_rollout_failure_skips_account_and_port_forwards(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_kind_prereqs() {{ :; }}
      ensure_kind_cluster() {{ :; }}
      ensure_kind_existing_secret_only() {{ :; }}
      kind_secret_is_helm_owned() {{ return 1; }}
      helm_kind() {{ :; }}
      wait_kind_rollouts() {{ printf 'wait_rollouts_failed\n'; return 23; }}
      create_kind_account() {{ printf 'create_account\n'; }}
      start_kind_port_forwards() {{ printf 'port_forwards\n'; }}
      main_kind
    """
    env = _kind_secret_test_env(env_file)
    env.update(
        {
            "KIND_USE_EXISTING_SECRET_ONLY": "1",
            "SKIP_BUILD": "1",
            "KIND_ROLLOUT_RESTART": "0",
            "KIND_PORT_FORWARD": "0",
        },
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "wait_rollouts_failed" in result.stdout
    assert "create_account" not in result.stdout
    assert "port_forwards" not in result.stdout


def test_main_kind_existing_secret_only_reuses_helm_owned_secret_data(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_kind_prereqs() {{ :; }}
      ensure_env() {{ printf 'ensure_env-called\\n'; return 99; }}
      ensure_kind_cluster() {{ :; }}
      ensure_kind_existing_secret_only() {{ :; }}
      kind_secret_is_helm_owned() {{ return 0; }}
      write_kind_secret_values_from_existing_secret() {{
        printf 'render-secret-values:%s\\n' "$1"
        printf 'secrets:\\n' >"$1"
      }}
      helm_kind() {{ printf 'helm:%s\\n' "$*"; }}
      kubectl_kind() {{ :; }}
      create_kind_account() {{ :; }}
      start_kind_port_forwards() {{ :; }}
      print_kind_urls() {{ :; }}
      main_kind
      [[ ! -e "$ENV_FILE" ]]
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
        "SKIP_BUILD": "1",
        "KIND_ROLLOUT_RESTART": "0",
        "KIND_PORT_FORWARD": "0",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "ensure_env-called" not in result.stdout
    assert "render-secret-values:" in result.stdout
    assert "Reusing Helm-managed Kubernetes Secret data: nomos/nomos-secrets" in result.stdout
    assert "--set secrets.create=false" not in result.stdout
    assert not env_file.exists()


def test_kind_secret_values_from_existing_secret_skips_missing_optional_keys(
    tmp_path: Path,
) -> None:
    values_file = tmp_path / "kind-secret-values.yaml"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kubectl_kind() {{
        case "$*" in
          *REDIS_PASSWORD*) printf 'cmVkaXMtdGVzdA==' ;;
          *NVIDIA_API_KEY*) printf 'bnZhcGktdGVzdA==' ;;
          *MONITOR_DASHBOARD_USER*) printf 'YWRtaW4=' ;;
          *DEMO_AUTH_PASSWORD*) printf 'ZGVtby11aS10ZXN0' ;;
          *) : ;;
        esac
      }}
      write_kind_secret_values_from_existing_secret {values_file}
    """
    env = {
        **os.environ,
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
    }

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    values = values_file.read_text()
    assert 'redisPassword: "redis-test"' in values  # pragma: allowlist secret
    assert 'nvidiaApiKey: "nvapi-test"' in values  # pragma: allowlist secret
    assert 'monitorDashboardUser: "admin"' in values
    assert 'demoAuthPassword: "demo-ui-test"' in values  # pragma: allowlist secret
    assert "openaiApiKey" not in values


def test_existing_kind_secret_only_generates_demo_auth_password() -> None:
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_secret_exists() {{ return 0; }}
      kind_secret_is_helm_owned() {{ return 1; }}
      kind_secret_data_present() {{
        [[ "$1" != "DEMO_AUTH_USER" && "$1" != "DEMO_AUTH_PASSWORD" ]]
      }}
      patch_kind_secret_literal() {{
        printf 'patch:%s\\n' "$1"
      }}
      random_secret() {{
        printf 'generated-demo-password'
      }}
      ensure_kind_existing_secret_only
    """
    env = {
        **os.environ,
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
        "DEMO_AUTH_USER": "",
        "DEMO_AUTH_PASSWORD": "",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "patch:DEMO_AUTH_USER" in result.stdout
    assert "patch:DEMO_AUTH_PASSWORD" in result.stdout
    assert "Generated DEMO_AUTH_PASSWORD in Kubernetes Secret nomos/nomos-secrets" in result.stdout


def test_existing_kind_secret_validation_reports_missing_keys() -> None:
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_secret_exists() {{ return 0; }}
      kind_secret_is_helm_owned() {{ return 1; }}
      kind_secret_data_present() {{ [[ "$1" != "NVIDIA_API_KEY" ]]; }}
      ensure_kind_existing_secret_only
    """
    env = {
        **os.environ,
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "NVIDIA_API_KEY" in result.stderr


def test_ensure_env_replaces_stale_hostname_with_detected_ip(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NVIDIA_API_KEY=nvapi-test",
                "REDIS_PASSWORD=redis-test",
                "COUNCIL_MANAGEMENT_TOKEN=management-test",
                "MONITOR_DASHBOARD_USER=admin",
                "MONITOR_DASHBOARD_PASSWORD=monitor-test",
                "NOMOS_SIGNUP_ADMIN_USER=admin",
                "NOMOS_SIGNUP_ADMIN_PASSWORD=signup-test",
                "NOMOS_PUBLIC_HOST=nomos.example.com",
                "COUNCIL_OPA_ENABLED=true",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      hostname() {{
        if [[ "${{1:-}}" == "-I" ]]; then
          printf '%s\\n' '203.0.113.10 127.0.0.1'
        else
          printf '%s\\n' 'nomos.example.com'
        fi
      }}
      ensure_env
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
        "DYNAMIC_CONFIG_DIR": str(tmp_path / "dynamic-config"),
        "NVIDIA_API_KEY": "",
        "NOMOS_PUBLIC_HOST": "",
        "PUBLIC_HOST": "",
    }

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    assert "NOMOS_PUBLIC_HOST=203.0.113.10\n" in env_file.read_text()


def test_ensure_env_respects_explicit_public_host_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_API_KEY=nvapi-test\nNOMOS_PUBLIC_HOST=old-host.example\n")

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      hostname() {{
        printf '%s\\n' '203.0.113.10'
      }}
      ensure_env
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
        "DYNAMIC_CONFIG_DIR": str(tmp_path / "dynamic-config"),
        "NOMOS_PUBLIC_HOST": "198.51.100.7",
        "PUBLIC_HOST": "",
    }

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    assert "NOMOS_PUBLIC_HOST=198.51.100.7\n" in env_file.read_text()


def test_preflight_ports_uses_env_file_overrides(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    docker_calls = tmp_path / "docker-calls.txt"
    env_file.write_text(
        "\n".join(
            [
                "MONITOR_PORT=19901",
                "COUNCIL_GRPC_PORT=19904",
                "REDIS_PORT=16379",
                "OPA_PORT=18181",
                "AGENT_SIGNUP_PORT=19905",
                "EXECUTOR_OPENSHELL_PORT=19907",
                "",
            ],
        ),
    )

    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      docker() {{
        printf '%s\\n' "$*" >> {docker_calls}
      }}
      preflight_ports
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
        "DYNAMIC_CONFIG_DIR": str(tmp_path / "dynamic-config"),
        "MONITOR_PORT": "",
        "COUNCIL_GRPC_PORT": "",
        "REDIS_PORT": "",
        "OPA_PORT": "",
        "AGENT_SIGNUP_PORT": "",
        "EXECUTOR_OPENSHELL_PORT": "",
    }

    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True)

    calls = docker_calls.read_text()
    for port in ("19901", "19904", "16379", "18181", "19905", "19907"):
        assert f"--filter publish={port}" in calls
    for default_port in ("9901", "9904", "6379", "8181", "9905", "9907"):
        assert f"--filter publish={default_port}" not in calls
