# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
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
    "MONITOR_MCP_PROXY_TOKEN",
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


def _source_and_echo(varnames: str, env: dict[str, str]) -> dict[str, str]:
    """Source deploy_stack.sh and read back resolved variable values.

    Asserting resolved values rather than the assignment text means a refactor
    that preserves behaviour does not fail the test.
    """
    names = varnames.split()
    echoes = "; ".join(f'printf "%s=%s\\n" {n} "${n}"' for n in names)
    # Start from a minimal environment, not os.environ. These tests assert the
    # script's *defaults*; inheriting an exported DEPLOY_TARGET or
    # COMPOSE_REDIS_TOPOLOGY from the developer's shell or a CI job makes them
    # assert whatever that machine happens to set, and they pass either way.
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{ROOT / "scripts/deploy_stack.sh"}"; {echoes}'],
        cwd=ROOT,
        env={**base_env, **{k: v for k, v in env.items()}},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k in names:
                out[k] = v
    return out


def test_deploy_stack_defaults_to_kind_target() -> None:
    """Assert the resolved values, not the text that produces them."""
    result = _source_and_echo("DEPLOY_TARGET KIND_CLUSTER_NAME", env={})

    assert result["DEPLOY_TARGET"] == "kind"
    assert result["KIND_CLUSTER_NAME"] == "nomos"


def test_deploy_stack_target_and_cluster_are_overridable() -> None:
    result = _source_and_echo(
        "DEPLOY_TARGET KIND_CLUSTER_NAME",
        env={"DEPLOY_TARGET": "compose", "KIND_CLUSTER_NAME": "other"},
    )

    assert result["DEPLOY_TARGET"] == "compose"
    assert result["KIND_CLUSTER_NAME"] == "other"


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


@pytest.mark.parametrize(
    ("env_contents", "allow_missing", "expected"),
    [
        ("", "", "required"),
        ("NVIDIA_API_KEY=test\n", "", "optional"),
        ("", "1", "optional"),
        ("", "true", "optional"),
        ("", "false", "required"),
    ],
)
def test_needs_nvidia_api_key(
    tmp_path: Path,
    env_contents: str,
    allow_missing: str,
    expected: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(env_contents)
    command = f"""
      source {ROOT / "deploy/scripts/dev-lib.sh"}
      if needs_nvidia_api_key; then
        printf required
      else
        printf optional
      fi
    """
    env = {
        **os.environ,
        "ALLOW_MISSING_NVIDIA_API_KEY": allow_missing,
        "ENV_FILE": str(env_file),
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout == expected


@pytest.mark.parametrize("env_contents", ["", "POSTGRES_PASSWORD=\n"])
def test_compose_down_works_without_postgres_password(
    tmp_path: Path,
    env_contents: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(env_contents)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    encoded_password_probe = 'printf "%s\\n" "${POSTGRES_PASSWORD_URLENCODED-}" >> "$DOCKER_ENV_FILE"\n'  # pragma: allowlist secret  # noqa: E501
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "${POSTGRES_PASSWORD-}" > "$DOCKER_ENV_FILE"\n'  # pragma: allowlist secret
        + encoded_password_probe
        + 'printf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n',
    )
    docker.chmod(0o755)

    docker_env_file = tmp_path / "docker.env"
    docker_args_file = tmp_path / "docker.args"
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_ENV_FILE": str(docker_env_file),
        "DOCKER_ARGS_FILE": str(docker_args_file),
    }
    env.pop("POSTGRES_PASSWORD", None)  # pragma: allowlist secret

    subprocess.run(
        ["./deploy/scripts/compose-down.sh", "--keep-volumes"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert docker_env_file.read_text().splitlines() == [
        "unused-for-compose-down",
        "unused-for-compose-down",
    ]
    assert docker_args_file.read_text().splitlines()[-2:] == [
        "down",
        "--remove-orphans",
    ]


def test_compose_percent_encodes_postgres_password_for_application_urls(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=pa/ss?word#fragment\n")  # pragma: allowlist secret
    command = f"""
      set -euo pipefail
      source {ROOT / "deploy/scripts/dev-lib.sh"}
      sync_postgres_url_password
    """
    env = {**os.environ, "ENV_FILE": str(env_file)}

    subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert env_file.read_text().splitlines() == [
        "POSTGRES_PASSWORD=pa/ss?word#fragment",  # pragma: allowlist secret
        "POSTGRES_PASSWORD_URLENCODED=pa%2Fss%3Fword%23fragment",  # pragma: allowlist secret
    ]


@pytest.mark.parametrize(
    ("env_contents", "regenerate_secrets"),
    [
        ("POSTGRES_PASSWORD=retained-volume-password\n", "1"),  # pragma: allowlist secret
        ("", "0"),
    ],
    ids=["regenerated-password", "missing-password"],
)
def test_compose_up_syncs_password_to_retained_postgres_role(
    tmp_path: Path,
    env_contents: str,
    regenerate_secrets: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(env_contents)
    sql_file = tmp_path / "postgres.sql"
    args_file = tmp_path / "postgres.args"
    command = f"""
      set -euo pipefail
      source {ROOT / "deploy/scripts/dev-lib.sh"}
      rand_secret() {{ printf '%s' "rotated-postgres'password"; }}
      dc() {{
        printf '%s\n' "$@" > "$ARGS_FILE"
        cat > "$SQL_FILE"
      }}
      REGENERATE_SECRETS={regenerate_secrets}
      gen_or_keep POSTGRES_PASSWORD >/dev/null
      sync_postgres_role_password
    """
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "SQL_FILE": str(sql_file),
        "ARGS_FILE": str(args_file),
    }

    subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    expected_env = "POSTGRES_PASSWORD=rotated-postgres'password\n"  # pragma: allowlist secret
    assert env_file.read_text() == expected_env
    assert (
        sql_file.read_text()
        # pragma: allowlist secret
        == "ALTER ROLE nomos WITH PASSWORD E'rotated-postgres\\'password';\n"
    )
    assert args_file.read_text().splitlines() == [
        "exec",
        "-T",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "nomos",
        "-d",
        "nomos",
    ]

    compose_up = (ROOT / "deploy/scripts/compose-up.sh").read_text()
    assert (
        compose_up.index("wait_healthy postgres")
        < compose_up.index(
            "sync_postgres_role_password",
        )
        < compose_up.index("dc up -d redis redis-1 redis-2")
    )


def test_sync_postgres_role_password_propagates_dc_failure(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=test-postgres-password\n")  # pragma: allowlist secret
    command = f"""
      set -euo pipefail
      source {ROOT / "deploy/scripts/dev-lib.sh"}
      dc() {{ cat >/dev/null; return 42; }}
      sync_postgres_role_password
    """
    env = {**os.environ, "ENV_FILE": str(env_file)}

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Postgres role password synchronized" not in result.stdout


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


def test_build_kind_images_defaults_to_nomos_cluster(tmp_path: Path) -> None:
    """Run the script against stub binaries and observe which cluster it loads into."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kind").write_text('#!/usr/bin/env bash\necho "kind $*" >>"$STUB_LOG"\n')
    (bindir / "docker").write_text('#!/usr/bin/env bash\necho "docker $*" >>"$STUB_LOG"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    stub_log = tmp_path / "calls.log"

    subprocess.run(
        ["bash", str(ROOT / "deploy/kubernetes/scripts/build-kind-images.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_LOG": str(stub_log),
            "KIND_CLUSTER_NAME": "",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )

    calls = stub_log.read_text() if stub_log.exists() else ""
    assert "--name nomos" in calls, f"expected loads into the 'nomos' cluster; got: {calls[:400]}"


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
        # The demo spec is only emitted when the demo was requested; the Kind
        # chart has no demo template otherwise. Default-off behaviour is covered
        # by test_kind_port_forward_demo_defaults_off_when_demo_not_requested.
        "WITH_DEMO": "1",
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


def test_kind_port_forward_detaches_kubectl_binary(tmp_path: Path) -> None:
    """The forward must be detached and must exec the kubectl *binary*.

    Routing it through the kubectl_kind shell function would not survive the
    script exiting. Observed by stubbing setsid/nohup/kubectl and reading back
    what was actually launched.
    """
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      KIND_PORT_FORWARD_DIR={tmp_path}/pf
      KIND_CONTEXT=kind-nomos
      KIND_NAMESPACE=nomos
      KIND_PORT_FORWARD_ADDRESS=127.0.0.1
      log() {{ :; }}
      stop_kind_port_forward() {{ :; }}
      # The stubs must let the readiness loop actually succeed. Previously setsid
      # exited immediately and kind_port_is_open never returned 0, so the loop
      # failed on `kill -0` and the script exited non-zero -- invisible because
      # the runner used check=False. The port must read CLOSED for the pre-launch
      # short-circuit (else nothing is ever launched) and OPEN once the forward
      # is up, so a non-zero exit now means a real regression.
      kind_port_is_open() {{
        if [ -e {tmp_path}/probed ]; then return 0; fi
        : >{tmp_path}/probed
        return 1
      }}
      have_cmd() {{ [ "$1" = setsid ]; }}
      setsid() {{ printf 'LAUNCH:%s\\n' "$*" >>{tmp_path}/launch.log; }}
      kubectl_kind() {{ printf 'VIA_FUNCTION\\n' >>{tmp_path}/launch.log; }}
      start_kind_port_forward monitor nomos-monitor 9901 9901 http://127.0.0.1:9901/
      wait 2>/dev/null || true
    """
    subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    launched = (tmp_path / "launch.log").read_text() if (tmp_path / "launch.log").exists() else ""

    assert launched.startswith("LAUNCH:"), f"forward must be detached via setsid; got: {launched!r}"
    assert "VIA_FUNCTION" not in launched, "must exec the kubectl binary, not the kubectl_kind shim"
    assert "port-forward" in launched
    assert "svc/nomos-monitor" in launched
    assert "9901:9901" in launched
    assert "--address 127.0.0.1" in launched


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


def test_kind_deploy_passes_alpha_values_to_helm(tmp_path: Path) -> None:
    """Capture the helm argv the alpha profile actually produces."""
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      NOMOS_DEPLOY_PROFILE=alpha
      KIND_NAMESPACE=nomos
      HELM_RELEASE=nomos
      ensure_kind_prereqs() {{ :; }}
      ensure_env() {{ :; }}
      ensure_kind_cluster() {{ :; }}
      build_kind_images() {{ :; }}
      sync_kind_existing_secret_from_env() {{ printf 'SYNC_SECRET\\n'; }}
      kind_secret_values_file() {{ printf '' ; }}
      helm_kind() {{ printf 'HELM:%s\\n' "$*"; }}
      kubectl_kind() {{ :; }}
      wait_kind_rollouts() {{ :; }}
      create_kind_account() {{ :; }}
      start_kind_port_forwards() {{ :; }}
      print_kind_urls() {{ :; }}
      main_kind 2>/dev/null || true
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ, "NOMOS_DEPLOY_PROFILE": "alpha"},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )

    helm = "\n".join(line for line in result.stdout.splitlines() if line.startswith("HELM:"))
    assert helm, f"expected a helm invocation; got: {result.stdout[:400]}"
    assert "deploymentProfile=alpha" in helm
    assert "council.allowUncertifiedEvidence=true" in helm
    assert "council.allowPublicBind=true" in helm
    assert "networkPolicy.enabled=true" in helm


def _run_kind_deploy(
    ingressclass_name: str = "",
    ingressclass_rc: int = 0,
    ingress_ready_node: str = "",
    ingress_node_rc: int = 0,
    host_ports_present: bool = False,
    docker_rc: int = 0,
    host_port_prefix: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run main_kind with confirmed absence distinct from discovery errors."""
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      KIND_NAMESPACE=nomos
      HELM_RELEASE=nomos
      ensure_kind_prereqs() {{ :; }}
      ensure_env() {{ :; }}
      ensure_kind_cluster() {{ :; }}
      build_kind_images() {{ :; }}
      kind_secret_values_file() {{ printf '' ; }}
      helm_kind() {{ printf 'HELM:%s\\n' "$*"; }}
      kubectl_kind() {{
        if [[ "$*" == "get ingressclass nginx --ignore-not-found -o name" ]]; then
          (( {ingressclass_rc} == 0 )) && printf '%s' '{ingressclass_name}'
          return {ingressclass_rc}
        fi
        if [[ "$*" == "get nodes -l ingress-ready=true -o name" ]]; then
          (( {ingress_node_rc} == 0 )) && printf '%s' '{ingress_ready_node}'
          return {ingress_node_rc}
        fi
        :
      }}
      docker() {{
        if [[ "$1" == inspect ]]; then
          if (( {docker_rc} != 0 )); then return {docker_rc}; fi
          if (( {int(host_ports_present)} == 1 )); then
            case "$*" in
              *80/tcp*) printf '%s80\\n' '{host_port_prefix}' ;;
              *443/tcp*) printf '%s443\\n' '{host_port_prefix}' ;;
            esac
          fi
          return 0
        fi
        :
      }}
      wait_kind_rollouts() {{ :; }}
      create_kind_account() {{ :; }}
      start_kind_port_forwards() {{ :; }}
      print_kind_urls() {{ :; }}
      main_kind
    """
    return subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ, "SKIP_BUILD": "1", "KIND_USE_EXISTING_SECRET_ONLY": "0"},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def _kind_helm_argv(**kwargs) -> str:
    result = _run_kind_deploy(**kwargs)
    assert result.returncode == 0, result.stderr
    helm = "\n".join(line for line in result.stdout.splitlines() if line.startswith("HELM:"))
    assert helm, f"expected a helm invocation; got: {result.stdout[:400]}"
    return helm


def test_kind_deploy_disables_ingress_without_nginx_ingress_class() -> None:
    """A cluster this script created has no ingress-nginx; the chart must not render Ingress."""
    assert "ingress.enabled=false" in _kind_helm_argv()


def test_kind_deploy_keeps_ingress_on_a_kind_up_cluster() -> None:
    helm = _kind_helm_argv(
        ingressclass_name="ingressclass.networking.k8s.io/nginx",
        ingress_ready_node="node/nomos-control-plane",
        host_ports_present=True,
    )
    assert "ingress.enabled" not in helm


def test_kind_deploy_disables_ingress_when_nginx_is_installed_on_an_unmapped_cluster() -> None:
    helm = _kind_helm_argv(ingressclass_name="ingressclass.networking.k8s.io/nginx")
    assert "ingress.enabled=false" in helm


def test_kind_deploy_disables_ingress_when_host_ports_are_remapped() -> None:
    helm = _kind_helm_argv(
        ingressclass_name="ingressclass.networking.k8s.io/nginx",
        ingress_ready_node="node/nomos-control-plane",
        host_ports_present=True,
        host_port_prefix="1",
    )
    assert "ingress.enabled=false" in helm


def test_kind_deploy_disables_ingress_when_the_node_is_labelled_but_ports_are_unmapped() -> None:
    helm = _kind_helm_argv(
        ingressclass_name="ingressclass.networking.k8s.io/nginx",
        ingress_ready_node="node/nomos-control-plane",
    )
    assert "ingress.enabled=false" in helm


def test_kind_deploy_fails_when_kubectl_ingress_discovery_errors() -> None:
    result = _run_kind_deploy(ingressclass_rc=2)
    assert result.returncode != 0
    assert "Unable to determine ingress availability" in result.stderr
    assert "HELM:" not in result.stdout


def test_kind_deploy_fails_when_docker_ingress_discovery_errors() -> None:
    result = _run_kind_deploy(
        ingressclass_name="ingressclass.networking.k8s.io/nginx",
        ingress_ready_node="node/nomos-control-plane",
        docker_rc=2,
    )
    assert result.returncode != 0
    assert "Unable to determine ingress availability" in result.stderr
    assert "HELM:" not in result.stdout


def _run_kind_verify(
    tmp_path: Path,
    ingressclass_result: str,
    ingress_node_result: str = "absent",
    docker_result: str = "absent",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env_file = tmp_path / "deployment.env"
    env_file.write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        """#!/bin/sh
case "$*" in
  *"get ingressclass nginx --ignore-not-found -o name"*)
    case "$INGRESSCLASS_RESULT" in
      present) printf 'ingressclass.networking.k8s.io/nginx' ;;
      absent) exit 0 ;;
      error) exit 2 ;;
    esac
    ;;
  *"get nodes -l ingress-ready=true -o name"*)
    case "$INGRESS_NODE_RESULT" in
      present) printf 'node/nomos-control-plane' ;;
      absent) exit 0 ;;
      error) exit 2 ;;
    esac
    ;;
  *"jsonpath={.status.readyReplicas}"*) printf '1' ;;
  *"debug/tools/list"*) printf 'council_authorize' ;;
  *"PostgresStore"*) printf '1\\n' ;;
  *"get pod -l app.kubernetes.io/component=council"*) printf 'nomos-council-0' ;;
  *"python /tmp/nomos_e2e.py"*)
    printf 'AUTHORIZE_OK members=3 risk=low\\nEXECUTE_OK exit=0 stdout=nomos-verify evidence=yes\\n'
    ;;
esac
exit 0
"""
    )
    kubectl.chmod(0o755)
    docker = bindir / "docker"
    docker.write_text(
        """#!/bin/sh
case "$DOCKER_RESULT" in
  absent) exit 0 ;;
  error) exit 2 ;;
  exact)
    case "$*" in
      *80/tcp*) printf '80\n' ;;
      *443/tcp*) printf '443\n' ;;
    esac
    ;;
  remapped)
    case "$*" in
      *80/tcp*) printf '180\n' ;;
      *443/tcp*) printf '1443\n' ;;
    esac
    ;;
esac
"""
    )
    docker.chmod(0o755)
    curl_log = tmp_path / "curl.log"
    curl = bindir / "curl"
    curl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$CURL_LOG"
case "$*" in
  *"-H Host: mcp.nomos.local http://localhost/healthz"*) printf '200' ;;
  *"-X POST -H Host: mcp.nomos.local http://localhost/mcp"*) printf '401' ;;
  *"-H Host: monitor.nomos.local http://localhost/"*) printf '200' ;;
  *"-H Host: signup.nomos.local https://localhost/healthz"*) printf '200' ;;
  *) printf '000' ;;
esac
"""
    )
    curl.chmod(0o755)

    result = subprocess.run(
        ["bash", "deploy/kubernetes/scripts/kind-verify.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AGENT_TOKEN": "test-agent-token",  # pragma: allowlist secret
            "MONITOR_MCP_PROXY_TOKEN": "test-proxy-token",  # pragma: allowlist secret
            "CURL_LOG": str(curl_log),
            "ENV_FILE": str(env_file),
            "INGRESSCLASS_RESULT": ingressclass_result,
            "INGRESS_NODE_RESULT": ingress_node_result,
            "DOCKER_RESULT": docker_result,
        },
        check=False,
        text=True,
        capture_output=True,
    )
    return result, curl_log


def test_kind_verify_skips_ingress_checks_when_ingress_is_unavailable(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(tmp_path, ingressclass_result="absent")
    assert result.returncode == 0, result.stderr
    assert "ingress checks skipped:" in result.stderr
    assert "deploy/kubernetes/scripts/kind-up.sh's path" in result.stderr
    assert "VERIFY PASSED" in result.stdout
    assert not curl_log.exists(), "ingress probes must not run when ingress is unavailable"


def test_kind_verify_fails_when_kubectl_ingress_discovery_errors(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(tmp_path, ingressclass_result="error")
    assert result.returncode != 0
    assert "Unable to determine ingress availability" in result.stderr
    assert "VERIFY PASSED" not in result.stdout
    assert not curl_log.exists(), "ingress probes must not run after discovery fails"


def test_kind_verify_skips_ingress_checks_without_an_ingress_ready_node(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(
        tmp_path,
        ingressclass_result="present",
        ingress_node_result="absent",
    )

    assert result.returncode == 0, result.stderr
    assert "ingress checks skipped:" in result.stderr
    assert not curl_log.exists()


def test_kind_verify_skips_ingress_checks_when_host_ports_are_remapped(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(
        tmp_path,
        ingressclass_result="present",
        ingress_node_result="present",
        docker_result="remapped",
    )

    assert result.returncode == 0, result.stderr
    assert "ingress checks skipped:" in result.stderr
    assert not curl_log.exists()


def test_kind_verify_fails_when_docker_ingress_discovery_errors(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(
        tmp_path,
        ingressclass_result="present",
        ingress_node_result="present",
        docker_result="error",
    )

    assert result.returncode != 0
    assert "Unable to determine ingress availability" in result.stderr
    assert "VERIFY PASSED" not in result.stdout
    assert not curl_log.exists()


def test_kind_verify_runs_hostname_probes_when_ingress_is_usable(tmp_path: Path) -> None:
    result, curl_log = _run_kind_verify(
        tmp_path,
        ingressclass_result="present",
        ingress_node_result="present",
        docker_result="exact",
    )

    assert result.returncode == 0, result.stderr
    assert "ingress checks skipped:" not in result.stderr
    assert "VERIFY PASSED" in result.stdout
    calls = curl_log.read_text().splitlines()
    assert len(calls) == 4
    assert any("-H Host: mcp.nomos.local http://localhost/healthz" in call for call in calls)
    assert any("-X POST -H Host: mcp.nomos.local http://localhost/mcp" in call for call in calls)
    assert any("-H Host: monitor.nomos.local http://localhost/" in call for call in calls)
    assert any("-H Host: signup.nomos.local https://localhost/healthz" in call for call in calls)


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


def test_kind_deploy_creates_account_before_port_forwards(tmp_path: Path) -> None:
    """Observe call order rather than the source layout.

    The previous version indexed a literal `--timeout="${WAIT_TIMEOUT_SECONDS}s"`
    string and broke when the rollout-wait was rewritten — a refactor, not a
    defect. Ordering is the property that matters: credentials must exist before
    anything is exposed.
    """
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      ensure_kind_prereqs() {{ :; }}
      ensure_env() {{ :; }}
      ensure_kind_cluster() {{ :; }}
      build_kind_images() {{ :; }}
      helm_kind() {{ :; }}
      kubectl_kind() {{ :; }}
      kind_secret_values_file() {{ printf '' ; }}
      wait_kind_rollouts() {{ printf 'ROLLOUTS\\n'; }}
      create_kind_account() {{ printf 'ACCOUNT\\n'; }}
      start_kind_port_forwards() {{ printf 'FORWARDS\\n'; }}
      print_kind_urls() {{ :; }}
      main_kind 2>/dev/null || true
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )

    order = [
        line for line in result.stdout.splitlines() if line in {"ROLLOUTS", "ACCOUNT", "FORWARDS"}
    ]
    assert order == ["ROLLOUTS", "ACCOUNT", "FORWARDS"], (
        f"expected rollouts -> account -> port-forwards; got {order}"
    )


def test_kind_account_bootstrap_uses_council_pod_cli(tmp_path: Path) -> None:
    """Capture how the council pod is actually invoked.

    This test has broken twice on refactors while asserting literal source
    strings. What matters is observable: the exec forwards stdin (-i), targets
    the council container, and drives the accounts CLI.
    """
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      AGENT_ID=agent-developer
      AGENT_ROLE=developer
      AGENT_COUNCILS=council-prod
      AGENT_DISPLAY_NAME=agent-developer
      KIND_NAMESPACE=nomos
      SKIP_ACCOUNT=0
      log() {{ :; }}
      kind_council_deployment() {{ printf 'nomos-council\\n'; }}
      store_kind_agent_token() {{ printf 'STORED\\n'; }}
      # Record the exec argv, then emit a JSON account so the token path runs.
      # The status line is what create_kind_account switches on; without it the
      # state is indeterminate and the function refuses rather than guessing.
      kubectl_kind() {{
        printf 'EXEC:%s\\n' "$*" >>{tmp_path}/exec.log
        printf 'NOMOS_ACCOUNT_STATUS=created\\n'
        printf '{{\\n  "agent_id": "agent-developer",\\n  "token": "%s"\\n}}\\n' \\
          "$(printf 'a%.0s' $(seq 64))"
      }}
      create_kind_account
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    execs = (tmp_path / "exec.log").read_text() if (tmp_path / "exec.log").exists() else ""
    assert execs, f"expected an exec into the council pod; stdout={result.stdout[:300]}"
    assert " -i " in execs, "kubectl exec must forward stdin or the heredoc never runs"
    assert "-c council" in execs
    assert "exec" in execs
    # The token was parsed out of the CLI's JSON and handed to the store step.
    # sync_kind_agent_secret is store_kind_agent_token's business, not this unit's.
    assert "STORED" in result.stdout


def _run_compose_account_bootstrap(
    env_file: Path,
    account_output: str,
    **env_overrides: str,
) -> subprocess.CompletedProcess[str]:
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      compose() {{
        printf '%s\\n' "$*" >>"$COMPOSE_ARGS_FILE"
        case "$*" in
          *" verify-token "*) return "${{VERIFY_RETURN_CODE:-0}}" ;;
          *) printf '%s\\n' "$ACCOUNT_OUTPUT" ;;
        esac
      }}
      create_account
      if [[ "${{RUN_AUTHENTICATED_VALIDATION:-0}}" == "1" ]]; then
        printf 'AUTHENTICATED_VALIDATION_RAN\\n'
      fi
    """
    env = {
        **os.environ,
        "ACCOUNT_OUTPUT": account_output,
        "COMPOSE_ARGS_FILE": f"{env_file}.compose-args",
        "DEPLOY_TARGET": "compose",
        "ENV_FILE": str(env_file),
        "AGENT_ID": "agent-test",
        "NOMOS_AGENT_ID": "",
        "NOMOS_AUTH_TOKEN": "",
        "NOMOS_VALIDATION_AGENT_ID": "",
        "NOMOS_VALIDATION_AUTH_TOKEN": "",
        **env_overrides,
    }
    return subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_account_clean_bootstrap_persists_token_without_printing_it(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    raw_token = "new-bootstrap-token"  # pragma: allowlist secret

    result = _run_compose_account_bootstrap(
        env_file,
        f"NOMOS_ACCOUNT_STATUS=created\nNOMOS_TOKEN={raw_token}",
    )

    assert result.returncode == 0, result.stderr
    assert "Account state: absent; provisioned agent-test" in result.stdout
    assert raw_token not in result.stdout
    assert raw_token not in result.stderr
    assert f"NOMOS_AUTH_TOKEN={raw_token}" in env_file.read_text()
    assert "NOMOS_AGENT_ID=agent-test" in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_account_reuses_protected_token_without_printing_it(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    raw_token = "stored-reuse-token"  # pragma: allowlist secret
    original = f"NOMOS_AGENT_ID=agent-test\nNOMOS_AUTH_TOKEN={raw_token}\n"
    env_file.write_text(original)
    env_file.chmod(0o600)

    result = _run_compose_account_bootstrap(
        env_file,
        "NOMOS_ACCOUNT_STATUS=exists",
    )

    assert result.returncode == 0, result.stderr
    assert "protected token verified; reusing agent-test without mutation" in result.stdout
    assert raw_token not in result.stdout
    assert raw_token not in result.stderr
    assert env_file.read_text() == original


def test_existing_account_without_protected_token_fails_with_safe_choices(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NOMOS_AGENT_ID=agent-test\n")

    result = _run_compose_account_bootstrap(
        env_file,
        "NOMOS_ACCOUNT_STATUS=exists",
    )

    assert result.returncode != 0
    assert (
        "Account state: present; protected token missing or unusable for agent-test"
        in result.stderr
    )
    assert "raw token cannot be recovered" in result.stderr
    assert "restore NOMOS_AGENT_ID and NOMOS_AUTH_TOKEN" in result.stderr
    assert "VALIDATION_IDENTITY=1" in result.stderr
    assert "AGENT_FORCE_ROTATE=1" in result.stderr


def test_existing_account_with_wrong_nonempty_token_fails_before_authenticated_validation(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    raw_token = "stale-protected-token"  # pragma: allowlist secret
    env_file.write_text(f"NOMOS_AGENT_ID=agent-test\nNOMOS_AUTH_TOKEN={raw_token}\n")
    env_file.chmod(0o600)

    result = _run_compose_account_bootstrap(
        env_file,
        "NOMOS_ACCOUNT_STATUS=exists",
        VERIFY_RETURN_CODE="1",
        RUN_AUTHENTICATED_VALIDATION="1",
    )

    assert result.returncode != 0
    assert "AUTHENTICATED_VALIDATION_RAN" not in result.stdout
    assert "protected token missing or unusable for agent-test" in result.stderr
    assert "restore NOMOS_AGENT_ID and NOMOS_AUTH_TOKEN" in result.stderr
    assert "VALIDATION_IDENTITY=1" in result.stderr
    assert "AGENT_FORCE_ROTATE=1" in result.stderr
    assert raw_token not in result.stdout + result.stderr
    assert env_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("topology", ["standalone", "sentinel"])
def test_compose_verification_keeps_the_redis_password_out_of_argv(
    tmp_path: Path,
    topology: str,
) -> None:
    """Compose arguments land in the deploying host's process list.

    The URL is assembled inside the container from its own COUNCIL_REDIS_*
    environment, so the password must never appear in what is handed to
    `docker compose run` — in either topology.
    """
    env_file = tmp_path / ".env"
    raw_token = "protected-existing-token"  # pragma: allowlist secret
    redis_password = "super-secret-redis-password"  # pragma: allowlist secret
    env_file.write_text(
        f"NOMOS_AGENT_ID=agent-test\n"
        f"NOMOS_AUTH_TOKEN={raw_token}\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_file.chmod(0o600)

    result = _run_compose_account_bootstrap(
        env_file,
        "NOMOS_ACCOUNT_STATUS=exists",
        COMPOSE_REDIS_TOPOLOGY=topology,
    )

    assert result.returncode == 0, result.stderr
    compose_args = Path(f"{env_file}.compose-args").read_text()
    verify_calls = [line for line in compose_args.splitlines() if "verify-token" in line]
    assert verify_calls, compose_args
    for line in verify_calls:
        assert redis_password not in line
        assert "NOMOS_REDIS_URL=redis://:" not in line
    assert redis_password not in result.stdout + result.stderr


def _run_compose_verify(
    tmp_path: Path, *, postgres_cid: str, postgres_state: str = ""
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run compose-verify.sh with docker/curl stubbed; return (result, recorded docker argv).

    ``postgres_cid`` is what any postgres container lookup returns; ``postgres_state``
    is what ``docker inspect`` reports, and the running-only ``docker ps -q`` lists the
    container only when it is ``running``.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("")  # no NOMOS_AUTH_TOKEN: the functional flow is skipped
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_ARGS_FILE"\n'
        'case "$*" in\n'
        '  "ps -aq "*"label=com.docker.compose.project=$COMPOSE_PROJECT"*'
        '"label=com.docker.compose.service=postgres"*) printf "%s\\n" "$STUB_POSTGRES_CID" ;;\n'
        '  "ps -q "*"label=com.docker.compose.project=$COMPOSE_PROJECT"*'
        '"label=com.docker.compose.service=postgres"*) '
        '[[ "$STUB_POSTGRES_STATE" == running ]] && printf "%s\\n" "$STUB_POSTGRES_CID" ;;\n'
        '  compose*" ps -q postgres") printf "%s\\n" "$STUB_POSTGRES_CID" ;;\n'
        '  "inspect "*) printf "%s\\n" "$STUB_POSTGRES_STATE" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nprintf 000\n")
    curl.chmod(0o755)
    args_file = tmp_path / "docker.args"
    args_file.touch()
    env = {
        **os.environ,
        "ENV_FILE": str(env_file),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_ARGS_FILE": str(args_file),
        "STUB_POSTGRES_CID": postgres_cid,
        "STUB_POSTGRES_STATE": postgres_state,
        # One project name shared by the verifier (dev-lib.sh honours it) and the stub.
        "COMPOSE_PROJECT": "nomos-verify-test",
    }
    env.pop("COMPOSE_EXTRA_FILES", None)
    result = subprocess.run(
        ["./deploy/scripts/compose-verify.sh"],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return result, args_file.read_text()


def test_compose_verify_names_the_missing_service_topology(tmp_path: Path) -> None:
    """Against a redis-topology (deploy_stack.sh) stack, say which topology is required."""
    result, docker_args = _run_compose_verify(tmp_path, postgres_cid="")

    assert result.returncode == 1
    assert "requires the service topology" in result.stderr
    assert "compose-up.sh" in result.stderr
    assert "deploy_stack.sh" in result.stderr
    # The guard runs before any check: nothing was asked of docker compose.
    assert not [line for line in docker_args.splitlines() if line.startswith("compose ")]


def test_compose_verify_reports_a_stopped_postgres_as_unhealthy_not_wrong_topology(
    tmp_path: Path,
) -> None:
    """An exited postgres still identifies the service topology; its state is a health failure."""
    result, _ = _run_compose_verify(tmp_path, postgres_cid="deadbeef", postgres_state="exited")

    assert "requires the service topology" not in result.stderr
    assert "\u2717 postgres healthy" in result.stderr  # the health check names it instead
    assert result.returncode != 0  # and the verdict fails on it


def test_compose_verify_declares_the_service_overlay(tmp_path: Path) -> None:
    """Standalone runs must address the same services compose-up.sh deployed."""
    result, docker_args = _run_compose_verify(tmp_path, postgres_cid="deadbeef")

    assert "requires the service topology" not in result.stderr
    compose_calls = [line for line in docker_args.splitlines() if line.startswith("compose ")]
    assert compose_calls, docker_args
    for line in compose_calls:
        assert "deploy/docker-compose.service.yml" in line


def test_unreachable_account_store_does_not_advise_rotation(tmp_path: Path) -> None:
    """A store outage is not an unusable credential.

    Telling an operator to rotate because Redis blipped destroys a working token,
    so exit status 3 from verify-token must produce a retry message and never the
    restore/provision/rotate guidance.
    """
    env_file = tmp_path / ".env"
    raw_token = "valid-but-unverifiable-token"  # pragma: allowlist secret
    env_file.write_text(f"NOMOS_AGENT_ID=agent-test\nNOMOS_AUTH_TOKEN={raw_token}\n")
    env_file.chmod(0o600)

    result = _run_compose_account_bootstrap(
        env_file,
        "NOMOS_ACCOUNT_STATUS=exists",
        VERIFY_RETURN_CODE="3",
        RUN_AUTHENTICATED_VALIDATION="1",
    )

    assert result.returncode != 0
    assert "AUTHENTICATED_VALIDATION_RAN" not in result.stdout
    assert "Could not reach the account store" in result.stderr
    assert "AGENT_FORCE_ROTATE=1" not in result.stderr
    assert "protected token missing or unusable" not in result.stderr
    assert raw_token not in result.stdout + result.stderr
    # The credential is left exactly as it was found.
    assert f"NOMOS_AUTH_TOKEN={raw_token}" in env_file.read_text()


@pytest.mark.parametrize(
    "account_output",
    ["", "NOMOS_ACCOUNT_STATUS=\n", "totally-unexpected-output\n"],
    ids=["empty", "blank-status", "malformed"],
)
def test_unrecognised_account_state_fails_without_writing_credentials(
    tmp_path: Path,
    account_output: str,
) -> None:
    env_file = tmp_path / ".env"

    result = _run_compose_account_bootstrap(env_file, account_output)

    assert result.returncode != 0
    assert "Could not determine account state for agent-test" in result.stderr
    assert not env_file.exists() or "NOMOS_AUTH_TOKEN=" not in env_file.read_text()


def test_account_explicit_rotation_replaces_store_without_printing_token(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    old_token = "old-rotation-token"  # pragma: allowlist secret
    new_token = "new-rotation-token"  # pragma: allowlist secret
    env_file.write_text(f"NOMOS_AGENT_ID=agent-test\nNOMOS_AUTH_TOKEN={old_token}\n")

    result = _run_compose_account_bootstrap(
        env_file,
        f"NOMOS_ACCOUNT_STATUS=rotated\nNOMOS_TOKEN={new_token}",
        AGENT_FORCE_ROTATE="1",
    )

    assert result.returncode == 0, result.stderr
    assert "Account state: explicit rotation requested" in result.stdout
    assert old_token not in result.stdout + result.stderr
    assert new_token not in result.stdout + result.stderr
    assert f"NOMOS_AUTH_TOKEN={new_token}" in env_file.read_text()
    assert old_token not in env_file.read_text()
    assert "AGENT_FORCE_ROTATE=1" in Path(f"{env_file}.compose-args").read_text()


def test_kind_clean_checkout_reuses_credentials_from_persistent_secret(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    raw_token = "persistent-kind-token"  # pragma: allowlist secret
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_council_deployment() {{ printf 'nomos-council\\n'; }}
      kubectl_kind() {{
        case "$*" in
          *" verify-token "*) return 0 ;;
          *" exec "*) printf 'NOMOS_ACCOUNT_STATUS=exists\\n' ;;
          *"jsonpath={{.data.NOMOS_AGENT_ID}}"*) printf '%s' 'agent-test' | base64 ;;
          *"jsonpath={{.data.NOMOS_AUTH_TOKEN}}"*) printf '%s' "$PERSISTED_TOKEN" | base64 ;;
          *" get secret "*) return 0 ;;
        esac
      }}
      create_kind_account
      [[ ! -e "$ENV_FILE" ]]
    """
    env = {
        **os.environ,
        "AGENT_ID": "agent-test",
        "DEPLOY_TARGET": "kind",
        "ENV_FILE": str(env_file),
        "KIND_USE_EXISTING_SECRET_ONLY": "1",
        "NOMOS_AGENT_ID": "",
        "NOMOS_AUTH_TOKEN": "",
        "PERSISTED_TOKEN": raw_token,
    }

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "protected token verified; reusing agent-test without mutation" in result.stdout
    assert raw_token not in result.stdout + result.stderr
    assert not env_file.exists()


def test_validation_identity_uses_separate_dedicated_credentials(
    tmp_path: Path,
) -> None:
    """Separate store keys, and the `developer` role the documented E2E paths need.

    The scenarios create files under /workspace, so `readonly` cannot run them.
    Isolation comes from the identity being dedicated and freely rotatable rather
    than from withholding file:write.
    """
    env_file = tmp_path / ".env"
    shared_token = "shared-account-token"  # pragma: allowlist secret
    validation_token = "validation-account-token"  # pragma: allowlist secret
    env_file.write_text(
        f"NOMOS_AGENT_ID=shared-agent\nNOMOS_AUTH_TOKEN={shared_token}\n",
    )

    result = _run_compose_account_bootstrap(
        env_file,
        f"NOMOS_ACCOUNT_STATUS=created\nNOMOS_TOKEN={validation_token}",
        AGENT_ID="",
        VALIDATION_IDENTITY="1",
    )

    stored = env_file.read_text()
    assert result.returncode == 0, result.stderr
    assert "provisioned nomos-validation" in result.stdout
    assert "NOMOS_AGENT_ID=shared-agent" in stored
    assert f"NOMOS_AUTH_TOKEN={shared_token}" in stored
    assert "NOMOS_VALIDATION_AGENT_ID=nomos-validation" in stored
    assert f"NOMOS_VALIDATION_AUTH_TOKEN={validation_token}" in stored
    assert validation_token not in result.stdout + result.stderr
    compose_args = Path(f"{env_file}.compose-args").read_text()
    assert "AGENT_ID=nomos-validation" in compose_args
    assert "AGENT_ROLE=developer" in compose_args
    assert "AGENT_COUNCILS=council-prod" in compose_args


def test_store_kind_agent_token_existing_secret_only_does_not_write_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_secret_exists() {{
        return 0
      }}
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


def test_store_kind_agent_token_fails_when_the_existing_secret_is_absent(
    tmp_path: Path,
) -> None:
    """An absent Secret must fail, not silently drop the token.

    sync_kind_agent_secret treats a missing Secret as a no-op success, so without
    an explicit guard the existing-secret-only path logs that it stored the token
    while storing it nowhere — and there is no env file to fall back to.
    """
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kind_secret_exists() {{
        return 1
      }}
      sync_kind_agent_secret() {{
        printf 'sync:%s\\n' "$1"
      }}
      store_kind_agent_token token-test
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
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert "sync:token-test" not in result.stdout
    assert "Stored Kind" not in result.stdout
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
      kind_cluster_has_usable_ingress() {{ return 1; }}
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
      kind_cluster_has_usable_ingress() {{ return 1; }}
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


def test_kind_rollout_restart_excludes_vault_and_upstream_components(tmp_path: Path) -> None:
    """The post-deploy restart must not recycle vault.

    Vault runs `server -dev` (in-memory). The Helm post-install hook that enables
    its Kubernetes auth method has already run and deleted itself, so a restarted
    vault comes back with no auth method and the council's wait-for-backends init
    container blocks forever on HTTP 403. Only deployments running locally-built
    nomos/* images need the restart.

    Asserts on the selector kubectl actually receives, not on the script text.
    """
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      kubectl_kind() {{ printf 'kubectl:%s\\n' "$*"; }}
      log() {{ :; }}
      is_true() {{ [ "$1" = "1" ]; }}
      KIND_NAMESPACE=nomos
      HELM_RELEASE=nomos
      KIND_ROLLOUT_RESTART=1
      # Invoke the real sweep. Rebuilding the selector here and asserting against
      # that construction is how this test used to pass no matter what
      # deploy_stack.sh actually sent.
      kind_rollout_restart_sweep
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ, "PATH": os.environ["PATH"]},
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    selector = result.stdout.strip()
    assert "rollout restart deployment" in selector
    assert "app.kubernetes.io/instance=nomos" in selector
    for component in ("vault", "opa", "redis-sentinel", "council-gateway"):
        assert component in selector, f"{component} must be excluded from the restart sweep"
    assert "notin" in selector, "exclusion must use a notin selector, not a bare instance match"


def test_kind_port_forward_demo_defaults_off_when_demo_not_requested() -> None:
    """Kind must not port-forward a demo it never deploys.

    The Kind chart has no demo template — the demo is Compose-only. Defaulting
    KIND_PORT_FORWARD_DEMO=1 made every Kind deploy exit 1 at the port-forward
    step ("Port-forward for demo exited before listening on 127.0.0.1:8000")
    even when all ten deployments were Ready.
    """
    for with_demo, expected in (("0", "0"), ("1", "1")):
        command = f"""
          set -euo pipefail
          WITH_DEMO={with_demo}
          source {ROOT / "scripts/deploy_stack.sh"} >/dev/null 2>&1 || true
          printf 'demo=%s with_demo=%s\\n' "$KIND_PORT_FORWARD_DEMO" "$WITH_DEMO"
        """
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=ROOT,
            env={**os.environ, "WITH_DEMO": with_demo},
            check=True,
            text=True,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert f"demo={expected}" in result.stdout, (
            f"WITH_DEMO={with_demo} should yield KIND_PORT_FORWARD_DEMO={expected}, "
            f"got: {result.stdout.strip()}"
        )


def test_kind_account_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    """A deploy with no usable credentials must fail, not exit 0.

    scripts/agent_accounts.py refuses every account operation under
    COUNCIL_STORAGE_MODE=postgres (the Kind chart default), so create/show/rotate
    all fail. create_kind_account previously reported "account already existed"
    for any tokenless output, so the deploy exited 0 with an empty accounts table
    and no way to call the council.
    """
    command = f"""
      set -euo pipefail
      source {ROOT / "scripts/deploy_stack.sh"}
      SKIP_ACCOUNT=0
      AGENT_ID=agent-developer
      AGENT_ROLE=developer
      KIND_NAMESPACE=nomos
      kind_council_deployment() {{ printf 'nomos-council\\n'; }}
      # Simulate the postgres guard: the CLI errors, so no JSON is produced.
      guard='agent_accounts.py account commands do not support postgres yet'
      kubectl_kind() {{ printf '%s\\n' "$guard"; }}
      store_kind_agent_token() {{ :; }}
      log() {{ printf '[deploy] %s\\n' "$*"; }}
      die() {{ printf 'DIED: %s\\n' "$*"; exit 3; }}
      create_kind_account
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode != 0, "credential-less deploy must not succeed"
    assert "DIED:" in result.stdout, f"expected die(), got: {result.stdout}"
    assert "already existed" not in result.stdout, (
        "must not misreport a missing account as existing"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("with'quote", "with\\'quote"),
        ("with\\backslash", "with\\\\backslash"),
        ("trailing\\", "trailing\\\\"),
        ("both\\'mixed", "both\\\\\\'mixed"),
    ],
    ids=["plain", "apostrophe", "backslash", "trailing-backslash", "combined"],
)
def test_postgres_role_password_is_escaped_for_an_e_string(
    tmp_path: Path, raw: str, expected: str
) -> None:
    """Both backslash and apostrophe must be escaped, backslash first.

    Doubling apostrophes alone is not enough: with standard_conforming_strings=off
    a backslash escapes the following character, so a password ending in a
    backslash consumes the closing quote and the remainder of the value runs as
    SQL. An E'' literal with both escaped closes that, and escaping backslashes
    before apostrophes is what keeps a combined value from re-opening it.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("")
    sql_file = tmp_path / "postgres.sql"
    command = f"""
      set -euo pipefail
      source "{ROOT / "deploy/scripts/dev-lib.sh"}"
      dc() {{ cat > "$SQL_FILE"; }}
      env_set POSTGRES_PASSWORD "$RAW_PASSWORD"
      sync_postgres_role_password
    """
    subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={
            **os.environ,
            "ENV_FILE": str(env_file),
            "SQL_FILE": str(sql_file),
            "RAW_PASSWORD": raw,
        },
        check=True,
        text=True,
        capture_output=True,
    )

    assert sql_file.read_text() == f"ALTER ROLE nomos WITH PASSWORD E'{expected}';\n"


def test_rollout_restart_sweep_is_skipped_when_disabled() -> None:
    """KIND_ROLLOUT_RESTART=0 must issue no restart at all."""
    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      kubectl_kind() {{ printf 'kubectl:%s\\n' "$*"; }}
      log() {{ :; }}
      KIND_NAMESPACE=nomos
      HELM_RELEASE=nomos
      KIND_ROLLOUT_RESTART=0
      kind_rollout_restart_sweep
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    assert result.stdout.strip() == "", "no restart may be issued when the sweep is disabled"


def test_kind_account_bootstrap_forwards_force_rotate(tmp_path: Path) -> None:
    """AGENT_FORCE_ROTATE must reach the in-pod script, and drive the rotate path.

    The Kind path used to ignore the flag entirely, reporting an existing token as
    unrecoverable and leaving no way to obtain credentials for a redeployed
    cluster. Asserting only the exec shape would not have caught that.
    """
    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      AGENT_ID=agent-developer
      AGENT_ROLE=developer
      AGENT_COUNCILS=council-prod
      AGENT_DISPLAY_NAME=agent-developer
      AGENT_FORCE_ROTATE=1
      KIND_NAMESPACE=nomos
      SKIP_ACCOUNT=0
      log() {{ :; }}
      kind_council_deployment() {{ printf 'nomos-council\\n'; }}
      store_kind_agent_token() {{ printf 'STORED:%s\\n' "$1"; }}
      kubectl_kind() {{
        printf 'EXEC:%s\\n' "$*" >>{tmp_path}/exec.log
        # Echo back what the caller passes through, so the test can see whether
        # AGENT_FORCE_ROTATE crossed the boundary into the pod.
        cat >/dev/null 2>&1 || true
        printf 'NOMOS_ACCOUNT_STATUS=rotated\\n'
        printf '{{\\n  "agent_id": "agent-developer",\\n  "token": "%s"\\n}}\\n' \\
          "$(printf 'b%.0s' $(seq 64))"
      }}
      create_kind_account
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={**os.environ},
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    execs = (tmp_path / "exec.log").read_text()
    assert "AGENT_FORCE_ROTATE=1" in execs, (
        "the flag never reached the pod, so the in-pod script cannot rotate"
    )
    # status=rotated must store the replacement credential, not report "exists".
    assert "STORED:" + "b" * 64 in result.stdout


def test_in_pod_script_attempts_rotation_before_show() -> None:
    """Order matters: a forced rotation must not be pre-empted by the show path.

    If `show` ran first the account would report as existing and the rotation
    would never be attempted, which is the behaviour AGENT_FORCE_ROTATE exists to
    override.
    """
    script = (ROOT / "scripts/deploy_stack.sh").read_text(encoding="utf-8")
    body = script.split("sh -s <<'SH'", 1)[1].split("\nSH", 1)[0]

    rotate_at = body.index("rotate-token")
    show_at = body.index("agent_accounts.py show")
    assert rotate_at < show_at, "show runs before the forced rotation is attempted"
    # ...and a failed rotation must not fall through to the show path.
    assert "rotate_failed" in body, "a failed forced rotation must not report success"


def test_kind_secret_values_include_the_mcp_proxy_token() -> None:
    """The proxy token must be generated, not inherited from the chart default.

    values.yaml ships `monitorMcpProxyToken: kind-monitor-mcp-proxy-change-me`.
    When deploy_stack.sh did not write this key, that committed placeholder became
    the live credential for the monitor's /mcp proxy -- a token every reader of the
    repository already knows, while the proxy still returned 401 for anything else,
    so it looked authenticated.
    """
    script = (ROOT / "scripts/deploy_stack.sh").read_text(encoding="utf-8")

    # Every path that can populate the cluster secret must carry the key, or a
    # deploy that takes a different branch silently reinstates the placeholder.
    for writer in (
        "write_kind_secret_value monitorMcpProxyToken MONITOR_MCP_PROXY_TOKEN",
        "write_kind_secret_patch_value MONITOR_MCP_PROXY_TOKEN MONITOR_MCP_PROXY_TOKEN",
        "write_kind_secret_value_from_secret monitorMcpProxyToken MONITOR_MCP_PROXY_TOKEN",
    ):
        assert writer in script, f"missing secret writer: {writer}"

    assert "Generated MONITOR_MCP_PROXY_TOKEN in deploy/.env" in script


def test_kind_secret_writers_render_the_mcp_proxy_token() -> None:
    """Execute each writer and assert its OUTPUT carries the key.

    The test above searches the script's source text, which proves the call site
    is spelled correctly and nothing about what the function renders. A broken
    helper -- one that returns early, or emits the wrong yaml key -- passes a
    source-text search and ships the chart placeholder as the live credential,
    which is the exact defect this PR exists to fix.

    `write_kind_secret_value_from_secret` reads through kubectl, so it is driven
    by stubbing kubectl_kind with the base64 a real cluster would return.
    """
    token = "generated-proxy-token-abc123456789"  # pragma: allowlist secret
    encoded = base64.b64encode(token.encode()).decode()

    cases = (
        (
            "write_kind_secret_value monitorMcpProxyToken MONITOR_MCP_PROXY_TOKEN",
            f'MONITOR_MCP_PROXY_TOKEN="{token}"',
            "monitorMcpProxyToken",
        ),
        (
            "write_kind_secret_patch_value MONITOR_MCP_PROXY_TOKEN MONITOR_MCP_PROXY_TOKEN",
            f'MONITOR_MCP_PROXY_TOKEN="{token}"',
            "MONITOR_MCP_PROXY_TOKEN",
        ),
        (
            "write_kind_secret_value_from_secret monitorMcpProxyToken MONITOR_MCP_PROXY_TOKEN",
            f'kubectl_kind() {{ printf "%s" "{encoded}"; }}',
            "monitorMcpProxyToken",
        ),
    )

    for call, setup, expected_key in cases:
        command = f"""
          set -uo pipefail
          source "{ROOT / "scripts/deploy_stack.sh"}"
          log() {{ :; }}
          KIND_SECRET_PATCH_FIRST=1
          {setup}
          {call}
        """
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=ROOT,
            env={**os.environ, "MONITOR_MCP_PROXY_TOKEN": ""},
            text=True,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        rendered = result.stdout
        assert expected_key in rendered, (
            f"{call!r} rendered no {expected_key}: {rendered!r} stderr={result.stderr[-300:]!r}"
        )
        assert token in rendered or encoded in rendered, (
            f"{call!r} rendered the key without the token value: {rendered!r}"
        )


def test_mcp_proxy_token_is_generated_into_the_env_file(tmp_path: Path) -> None:
    """A fresh deploy must leave a real token in deploy/.env, not the placeholder.

    README's end-to-end example tells the reader their credentials are in
    deploy/.env. Before this, MONITOR_MCP_PROXY_TOKEN was the one credential the
    example needed that never landed there, so the documented snippet 401'd.
    """
    env_file = tmp_path / ".env"
    command = f"""
      set -euo pipefail
      ENV_FILE={env_file}
      # ensure_env refuses to run without a configured backend key. This test is
      # about secret generation, not the key gate, so satisfy it with a dummy.
      NVIDIA_API_KEY=dummy-key-for-test
      source "{ROOT / "scripts/deploy_stack.sh"}"
      log() {{ :; }}
      ensure_env
      grep -E '^MONITOR_MCP_PROXY_TOKEN=' "{env_file}"
    """
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env={
            **os.environ,
            # Cleared explicitly. The subprocess inherits os.environ, and
            # ensure_env PERSISTS an already-set value rather than generating
            # one -- so with this exported in the parent shell the test passed
            # while testing nothing.
            "MONITOR_MCP_PROXY_TOKEN": "",
            "ENV_FILE": str(env_file),
            "NVIDIA_API_KEY": "dummy-key-for-test",  # pragma: allowlist secret
        },
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    line = result.stdout.strip()
    value = line.split("=", 1)[1]
    assert value, "MONITOR_MCP_PROXY_TOKEN was written empty"
    assert value != "kind-monitor-mcp-proxy-change-me", (
        "the chart's committed placeholder was written as the live token"
    )
    assert len(value) >= 24, f"token looks too short to be generated: {len(value)} chars"


@pytest.mark.parametrize(
    "function",
    ["ensure_dynamic_council_config", "ensure_runtime_read_permissions"],
)
def test_dynamic_config_files_owned_by_another_user_do_not_abort_the_deploy(
    tmp_path: Path, function: str
) -> None:
    """A council-owned catalog must be skipped, not fatal.

    The council container rewrites the catalog and profile files by atomic
    replace, so after a live council mutation they belong to its UID (1000) and
    chmod refuses to change files the deploying user does not own. Files owned
    by another user cannot be created without root, so the deploying user's UID
    is stubbed instead: with ``id -u`` reporting a UID that owns nothing here,
    every file is foreign and must be left alone while the step still succeeds.
    """
    dynamic_dir = tmp_path / "dynamic-config"
    (dynamic_dir / "councils").mkdir(parents=True)
    catalog = dynamic_dir / "local_council_catalog.yaml"
    catalog.write_text("default_council_id: council-prod\ncouncils: {}\n")
    catalog.chmod(0o600)
    profile = dynamic_dir / "councils" / "live-mcp-smoke-council.yaml"
    profile.write_text("council_id: live-mcp-smoke-council\n")
    profile.chmod(0o600)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub_id = stub_bin / "id"
    stub_id.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-u" ]]; then echo 65534; else exec /usr/bin/id "$@"; fi\n'
    )
    stub_id.chmod(0o755)
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()

    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      ROOT_DIR="{empty_root}"
      ENV_FILE="{empty_root / ".env"}"
      {function}
    """
    env = {
        **os.environ,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "DYNAMIC_CONFIG_DIR": str(dynamic_dir),
    }
    result = subprocess.run(
        ["bash", "-c", command], cwd=ROOT, env=env, text=True, capture_output=True
    )

    assert result.returncode == 0, result.stderr
    assert catalog.stat().st_mode & 0o777 == 0o600
    assert profile.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "function",
    ["ensure_dynamic_council_config", "ensure_runtime_read_permissions"],
)
def test_dynamic_config_files_the_deployer_owns_are_still_relaxed(
    tmp_path: Path, function: str
) -> None:
    dynamic_dir = tmp_path / "dynamic-config"
    councils = dynamic_dir / "councils"
    councils.mkdir(parents=True)
    catalog = dynamic_dir / "local_council_catalog.yaml"
    catalog.write_text("default_council_id: council-prod\ncouncils: {}\n")
    catalog.chmod(0o600)
    councils.chmod(0o700)
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()

    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      ROOT_DIR="{empty_root}"
      ENV_FILE="{empty_root / ".env"}"
      {function}
    """
    env = {**os.environ, "DYNAMIC_CONFIG_DIR": str(dynamic_dir)}
    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True, capture_output=True)

    assert catalog.stat().st_mode & 0o777 == 0o666
    assert councils.stat().st_mode & 0o777 == 0o777


def test_dynamic_config_symlinks_never_relax_their_targets(tmp_path: Path) -> None:
    """A symlink planted in the council-writable directory must not be followed.

    ``chmod -R`` ignores symlinks it meets while traversing, but chmod follows a
    symlink named on its command line -- which is how ``find -exec chmod`` hands
    paths over. With the deploying user at UID 1000 (the council's UID) a link the
    council created is "owned" by the deployer, so it must be excluded by type.
    """
    dynamic_dir = tmp_path / "dynamic-config"
    (dynamic_dir / "councils").mkdir(parents=True)
    (dynamic_dir / "local_council_catalog.yaml").write_text(
        "default_council_id: council-prod\ncouncils: {}\n"
    )
    outside = tmp_path / "outside-secret"
    outside.write_text("secret\n")
    outside.chmod(0o600)
    (dynamic_dir / "councils" / "planted.yaml").symlink_to(outside)
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()

    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      ROOT_DIR="{empty_root}"
      ENV_FILE="{empty_root / ".env"}"
      relax_dynamic_config_permissions
    """
    env = {**os.environ, "DYNAMIC_CONFIG_DIR": str(dynamic_dir)}
    subprocess.run(["bash", "-c", command], cwd=ROOT, env=env, check=True, capture_output=True)

    assert outside.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "function",
    ["ensure_dynamic_council_config", "ensure_runtime_read_permissions"],
)
def test_dynamic_config_chmod_failures_still_abort_the_deploy(
    tmp_path: Path, function: str
) -> None:
    """Skipping foreign-owned files must not turn into swallowing real chmod errors."""
    dynamic_dir = tmp_path / "dynamic-config"
    (dynamic_dir / "councils").mkdir(parents=True)
    (dynamic_dir / "local_council_catalog.yaml").write_text(
        "default_council_id: council-prod\ncouncils: {}\n"
    )
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub_chmod = stub_bin / "chmod"
    stub_chmod.write_text('#!/usr/bin/env bash\necho "chmod: stubbed failure" >&2\nexit 1\n')
    stub_chmod.chmod(0o755)
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()

    command = f"""
      set -euo pipefail
      source "{ROOT / "scripts/deploy_stack.sh"}"
      ROOT_DIR="{empty_root}"
      ENV_FILE="{empty_root / ".env"}"
      {function}
    """
    env = {
        **os.environ,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "DYNAMIC_CONFIG_DIR": str(dynamic_dir),
    }
    result = subprocess.run(
        ["bash", "-c", command], cwd=ROOT, env=env, text=True, capture_output=True
    )

    assert result.returncode != 0
    assert "stubbed failure" in result.stderr


_COMPOSE_UP_DOCKER_STUB = """#!/usr/bin/env bash
# Enough of `docker` for compose-up.sh to reach step 9: every service is
# healthy at once, Vault bootstrap returns AppRole ids, and the accounts CLI
# answers from the test's SHOW_RC / VERIFY_RC / ROTATE_* / REG_* knobs. The
# demo recreate snapshots the token deploy/.env holds at that moment, so a
# test can prove the credential was persisted before the demo restarted.
printf '%s\\n' "$*" >> "$DOCKER_ARGS_FILE"
case " $* " in
  *" inspect "*) echo healthy ;;
  *" ps -q "*) echo cid0 ;;
  *" vault sh -s "*) printf 'VAULT_ROLE_ID=role\\nVAULT_SECRET_ID=secret\\n' ;;
  *"/agent_accounts.py show "*) exit "${SHOW_RC:-1}" ;;
  *"/agent_accounts.py verify-token "*) cat > "$VERIFIED_TOKEN_FILE"; exit "${VERIFY_RC:-1}" ;;
  *"/agent_accounts.py rotate-token "*) printf '%s' "${ROTATE_STDOUT-}"; exit "${ROTATE_RC:-0}" ;;
  *"/agent_accounts.py create "*)
    printf '%s' "${REG_STDOUT-}"; printf '%s' "${REG_STDERR-}" >&2; exit "${REG_RC:-0}" ;;
  *" up -d "*" demo "*) grep '^NOMOS_AUTH_TOKEN=' "$ENV_FILE" > "$DEMO_ENV_FILE" || : ;;
esac
cat >/dev/null
exit 0
"""

_COMPOSE_UP_KNOBS = (
    "AGENT_ID",
    "AGENT_ROLE",
    "AGENT_FORCE_ROTATE",
    "COMPOSE_EXTRA_FILES",
    "COMPOSE_PROJECT",
    "SHOW_RC",
    "VERIFY_RC",
    "ROTATE_STDOUT",
    "ROTATE_RC",
    "REG_STDOUT",
    "REG_STDERR",
    "REG_RC",
)

_FRESH_TOKEN_JSON = '{\n  "agent_id": "dev-agent",\n  "token": "deadbeef"\n}\n'
_STORED_CREDENTIALS = (
    "NOMOS_AGENT_ID=dev-agent\nNOMOS_AUTH_TOKEN=cafef00d\n"  # pragma: allowlist secret
)


def _run_compose_up(
    tmp_path: Path, *, env_file_text: str = "", **knobs: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    """Run the real compose-up.sh -y end to end with only `docker` stubbed.

    compose-up.sh resolves its helpers next to itself, so the copy under test
    sits in a temp dir beside symlinks to the real dev-lib.sh,
    compose-register-agent.sh and vault-bootstrap-compose.sh, and a stub
    compose-verify.sh that only records the AGENT_TOKEN it was handed. That
    keeps the register wrapper and dev-lib's dc() in the loop rather than
    asserting on the shape of the source.

    ``knobs`` are the stub's SHOW_RC / VERIFY_RC / ROTATE_* / REG_* answers plus
    any AGENT_* setting the scenario needs; everything else in that set is
    scrubbed from the inherited environment. The returned paths are the
    deploy/.env under test, the docker argv log, the token compose-verify.sh
    received, the token piped to verify-token, and the NOMOS_AUTH_TOKEN line
    deploy/.env held when the demo was recreated.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "deploy/scripts/compose-up.sh", scripts / "compose-up.sh")
    for helper in ("dev-lib.sh", "compose-register-agent.sh", "vault-bootstrap-compose.sh"):
        (scripts / helper).symlink_to(ROOT / "deploy/scripts" / helper)
    verify_stub = scripts / "compose-verify.sh"
    verify_stub.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "${AGENT_TOKEN-}" > "$VERIFY_FILE"\n'
    )
    verify_stub.chmod(0o755)

    root = tmp_path / "root"
    (root / "configs").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "configs/local_council_catalog.yaml").write_text("")
    paths = {
        "env": tmp_path / ".env",
        "args": tmp_path / "docker.args",
        "verify": tmp_path / "verify.token",
        "verified": tmp_path / "verified.token",
        "demo_env": tmp_path / "demo.env",
    }
    paths["env"].write_text("NVIDIA_API_KEY=nvapi-test\n" + env_file_text)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_COMPOSE_UP_DOCKER_STUB)
    docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "ROOT_DIR": str(root),
        "ENV_FILE": str(paths["env"]),
        "DOCKER_ARGS_FILE": str(paths["args"]),
        "VERIFY_FILE": str(paths["verify"]),
        "VERIFIED_TOKEN_FILE": str(paths["verified"]),
        "DEMO_ENV_FILE": str(paths["demo_env"]),
    }
    for key in _COMPOSE_UP_KNOBS:
        env.pop(key, None)
    env.update(knobs)
    proc = subprocess.run(
        [str(scripts / "compose-up.sh"), "-y"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return proc, paths


def _cli_calls(args_file: Path, subcommand: str) -> list[list[str]]:
    """argv of every accounts-CLI invocation, from the subcommand onwards."""
    calls = []
    for line in args_file.read_text().splitlines():
        argv = line.split()
        if "scripts/agent_accounts.py" in argv and subcommand in argv:
            calls.append(argv[argv.index(subcommand) :])
    return calls


def _demo_recreates(args_file: Path) -> list[list[str]]:
    """argv of every `up` that targets the demo service (step 7's `up -d` does not)."""
    return [
        argv
        for argv in (line.split() for line in args_file.read_text().splitlines())
        if "up" in argv and argv[-1] == "demo"
    ]


def _stored_credentials(env_file: Path) -> list[str]:
    """The NOMOS_AGENT_ID / NOMOS_AUTH_TOKEN lines of deploy/.env, in file order.

    compose-up.sh rewrites deploy/.env on every run (generated secrets, the
    Vault AppRole ids), so "unchanged" is asserted on these two lines only.
    """
    return [
        line
        for line in env_file.read_text().splitlines()
        if line.startswith(("NOMOS_AGENT_ID=", "NOMOS_AUTH_TOKEN="))
    ]


@pytest.mark.parametrize(
    ("reg_stdout", "reg_stderr", "reg_rc"),
    [
        ("", "ERROR: cannot connect to PostgreSQL: host 'postgres' not found\n", 1),
        ('{\n  "agent_id": "dev-agent"\n}\n', "", 0),
    ],
    ids=["cli-fails", "cli-returns-no-token"],
)
def test_compose_up_registration_failure_is_visible_and_fatal(
    tmp_path: Path, reg_stdout: str, reg_stderr: str, reg_rc: int
) -> None:
    """Step 9 must show why registration failed and stop there.

    It used to run the wrapper with `2>/dev/null … || true` and only warn, so
    every failure read as "did not return a token" and verification then ran
    against whatever stale NOMOS_AUTH_TOKEN deploy/.env still held, blaming
    Postgres and the functional flow instead of the registration.
    """
    proc, paths = _run_compose_up(
        tmp_path, SHOW_RC="1", REG_STDOUT=reg_stdout, REG_STDERR=reg_stderr, REG_RC=str(reg_rc)
    )

    assert proc.returncode != 0, proc.stdout
    assert "created agent" not in proc.stdout
    if reg_stderr:
        assert "host 'postgres' not found" in proc.stderr, (
            "the wrapper's diagnostic was swallowed; every failure looks identical"
        )
    assert "agent registration" in proc.stderr
    assert not paths["verify"].exists(), "verification ran without a freshly minted token"
    assert "NOMOS_AUTH_TOKEN=" not in paths["env"].read_text()
    assert _demo_recreates(paths["args"]) == [], "demo recreated without a new credential"


def test_compose_up_creates_a_missing_account_then_recreates_the_demo(tmp_path: Path) -> None:
    """First run: create (never --force), persist, recreate the demo, verify.

    The demo reads NOMOS_AUTH_TOKEN once at startup and step 7 started it from
    a deploy/.env that had no token yet, so the new credential has to be
    persisted first and the demo recreated after — the snapshot the stub takes
    at the recreate proves the order.
    """
    proc, paths = _run_compose_up(tmp_path, SHOW_RC="1", REG_STDOUT=_FRESH_TOKEN_JSON)

    assert proc.returncode == 0, proc.stderr
    assert "created agent 'dev-agent'" in proc.stdout
    env_text = paths["env"].read_text()
    assert "NOMOS_AGENT_ID=dev-agent\n" in env_text
    assert "NOMOS_AUTH_TOKEN=deadbeef\n" in env_text
    assert paths["verify"].read_text() == "deadbeef\n"

    assert _cli_calls(paths["args"], "create") == [
        ["create", "--json", "--role", "developer", "--council", "council-prod", "dev-agent"]
    ]
    assert _cli_calls(paths["args"], "rotate-token") == []
    (recreate,) = _demo_recreates(paths["args"])
    assert "--force-recreate" in recreate and "--no-deps" in recreate
    assert paths["demo_env"].read_text() == "NOMOS_AUTH_TOKEN=deadbeef\n", (
        "the demo was recreated before the new token reached deploy/.env"
    )


def test_compose_up_rerun_reuses_the_verified_token_without_rotating(tmp_path: Path) -> None:
    """Re-run against a retained store: verify the stored token and keep it.

    Re-creating with --force on every run rotated the demo agent's token each
    time, invalidating any other client holding it; reuse must go through the
    store's own verify-token so a stale deploy/.env cannot pass as valid.
    """
    proc, paths = _run_compose_up(
        tmp_path, env_file_text=_STORED_CREDENTIALS, SHOW_RC="0", VERIFY_RC="0"
    )

    assert proc.returncode == 0, proc.stderr
    assert "reusing it" in proc.stdout
    assert _cli_calls(paths["args"], "verify-token") == [
        ["verify-token", "--token-stdin", "--expect-council", "council-prod", "dev-agent"]
    ]
    assert paths["verified"].read_text() == "cafef00d", "a different token was verified"
    assert _cli_calls(paths["args"], "create") == []
    assert _cli_calls(paths["args"], "rotate-token") == []
    assert _stored_credentials(paths["env"]) == _STORED_CREDENTIALS.splitlines()
    assert paths["verify"].read_text() == "cafef00d\n"
    assert _demo_recreates(paths["args"]) == [], "demo recreated although its token did not change"


@pytest.mark.parametrize(
    ("env_file_text", "verify_rc"),
    [("", "0"), (_STORED_CREDENTIALS, "1")],
    ids=["no-token-in-env", "token-does-not-verify"],
)
def test_compose_up_rerun_without_a_usable_token_refuses_to_rotate(
    tmp_path: Path, env_file_text: str, verify_rc: str
) -> None:
    """An existing account with no verifiable token stops with recovery choices.

    The store keeps only the hash, so the token cannot be recovered; rotating
    silently would cut off every other client of the identity. Mirrors the
    NOMOS_ACCOUNT_STATUS=exists handling in scripts/deploy_stack.sh.
    """
    proc, paths = _run_compose_up(
        tmp_path, env_file_text=env_file_text, SHOW_RC="0", VERIFY_RC=verify_rc
    )

    assert proc.returncode != 0, proc.stdout
    assert "AGENT_FORCE_ROTATE=1" in proc.stderr
    assert "--agent-id" in proc.stderr
    assert _cli_calls(paths["args"], "create") == []
    assert _cli_calls(paths["args"], "rotate-token") == []
    assert len(_cli_calls(paths["args"], "verify-token")) == (1 if env_file_text else 0)
    assert not paths["verify"].exists(), "verification ran with an unverified token"
    assert _stored_credentials(paths["env"]) == env_file_text.splitlines()
    assert _demo_recreates(paths["args"]) == []


@pytest.mark.parametrize(
    ("show_rc", "verify_rc"), [("3", "0"), ("0", "3")], ids=["lookup", "verify"]
)
def test_compose_up_unreachable_store_does_not_advise_rotation(
    tmp_path: Path, show_rc: str, verify_rc: str
) -> None:
    """CLI exit 3 means "unknown", not "invalid": stop, but never suggest re-keying."""
    proc, paths = _run_compose_up(
        tmp_path, env_file_text=_STORED_CREDENTIALS, SHOW_RC=show_rc, VERIFY_RC=verify_rc
    )

    assert proc.returncode != 0, proc.stdout
    assert "could not reach the account store" in proc.stderr
    assert "AGENT_FORCE_ROTATE" not in proc.stderr
    assert _cli_calls(paths["args"], "create") == []
    assert _cli_calls(paths["args"], "rotate-token") == []
    assert not paths["verify"].exists()
    assert _stored_credentials(paths["env"]) == _STORED_CREDENTIALS.splitlines()
    assert _demo_recreates(paths["args"]) == []


def test_compose_up_force_rotate_rekeys_persists_then_recreates_the_demo(tmp_path: Path) -> None:
    """AGENT_FORCE_ROTATE=1 is the explicit way to mint a new token for an existing agent."""
    proc, paths = _run_compose_up(
        tmp_path,
        env_file_text=_STORED_CREDENTIALS,
        SHOW_RC="0",
        AGENT_FORCE_ROTATE="1",
        ROTATE_STDOUT='{\n  "agent_id": "dev-agent",\n  "token": "feedface"\n}\n',
    )

    assert proc.returncode == 0, proc.stderr
    assert "rotated agent 'dev-agent'" in proc.stdout
    assert _cli_calls(paths["args"], "rotate-token") == [["rotate-token", "--json", "dev-agent"]]
    assert _cli_calls(paths["args"], "create") == []
    assert _cli_calls(paths["args"], "verify-token") == [], (
        "explicit rotation must not depend on the old token"
    )
    env_text = paths["env"].read_text()
    assert "NOMOS_AUTH_TOKEN=feedface\n" in env_text
    assert "cafef00d" not in env_text
    assert paths["verify"].read_text() == "feedface\n"
    (recreate,) = _demo_recreates(paths["args"])
    assert "--force-recreate" in recreate and "--no-deps" in recreate
    assert paths["demo_env"].read_text() == "NOMOS_AUTH_TOKEN=feedface\n"
