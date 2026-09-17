# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import shlex
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


def test_council_image_installs_backends_the_default_config_needs() -> None:
    """The council image must carry the LLM backend its shipped config selects.

    configs/council.yaml now ships `provider: nim`, and nomos/llm/nim_backend.py
    imports `openai`. This test was added after deploy/Dockerfile omitted
    `--extra openai` while `nv_inference` was the default; every council tool
    call failed and executors never started.

    This is the third dependency-drift defect of the same shape (psycopg,
    nvidia-nat-mcp, openai), so the check is written against the *config*
    rather than pinning one extra: whatever providers the default config
    selects, the image must install the matching extras.
    """
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    council_config = yaml.safe_load((ROOT / "configs" / "council.yaml").read_text(encoding="utf-8"))

    provider_extra = {
        "nv_inference": "openai",  # nv_inference speaks the OpenAI-compatible API
        "openai": "openai",
        "anthropic": "anthropic",
        "nim": "openai",
    }

    backends = council_config.get("llm_backends") or {}
    assert backends, "configs/council.yaml must declare llm_backends"

    required = {
        provider_extra[b["provider"]]
        for b in backends.values()
        if b.get("provider") in provider_extra
    }
    assert required, "no known providers found in configs/council.yaml"

    install_lines = [ln for ln in dockerfile.splitlines() if "--extra" in ln]
    assert install_lines, "deploy/Dockerfile must install extras via uv sync"

    # Assert the union across all install lines, not that every line carries every
    # extra: splitting the install into two RUN steps is a legitimate refactor that
    # the per-line form would fail, while a genuinely missing extra would slip
    # through if any single line happened to list it.
    installed = {
        extra
        for line in install_lines
        for extra in re.findall(r"--extra\s+([A-Za-z0-9._-]+)", line)
    }
    missing = sorted(required - installed)
    assert not missing, (
        f"configs/council.yaml selects providers needing {missing}, which "
        f"deploy/Dockerfile never installs (it installs: {sorted(installed)})"
    )


def test_grpc_proxy_receives_the_monitor_mcp_bearer_token() -> None:
    """The gRPC proxy must be able to authenticate to the monitor's /mcp proxy.

    COUNCIL_MCP_URL points the gRPC proxy at the monitor, whose /mcp route
    requires a Bearer token once MONITOR_MCP_PROXY_TOKEN is set (which the chart
    does). Without COUNCIL_MCP_PROXY_TOKEN the proxy 401s on every call and gRPC
    is entirely non-functional:

        httpx.HTTPStatusError: Client error '401 Unauthorized'
          for url 'http://nomos-monitor:9901/mcp'

    docker-compose.yml already wires it; the Helm chart did not.
    """
    grpc_template = (
        ROOT / "deploy" / "kubernetes" / "nomos" / "templates" / "council-grpc.yaml"
    ).read_text(encoding="utf-8")

    assert "COUNCIL_MCP_URL" in grpc_template
    assert "COUNCIL_MCP_PROXY_TOKEN" in grpc_template, (
        "council-grpc must receive the monitor /mcp bearer token or every gRPC call 401s"
    )
    assert "monitorMcpProxyToken" in grpc_template, (
        "the token must come from the same secret key the monitor uses"
    )

    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "COUNCIL_MCP_PROXY_TOKEN" in compose, "compose parity check"


def test_demo_receives_the_monitor_mcp_bearer_token() -> None:
    """The demo must authenticate when it routes MCP calls through the monitor."""
    demo_environment = _compose()["services"]["demo"]["environment"]

    assert "MONITOR_MCP_PROXY_TOKEN=${MONITOR_MCP_PROXY_TOKEN:-}" in demo_environment


def _duration_seconds(value: str) -> int:
    """Parse the compose duration strings used in these healthchecks ('10s', '1m30s')."""
    total = 0
    number = ""
    for char in str(value):
        if char.isdigit():
            number += char
            continue
        if char == "s":
            total += int(number)
        elif char == "m":
            total += int(number) * 60
        elif char == "h":
            total += int(number) * 3600
        number = ""
    return total


def _timeout_seconds(line: str) -> int:
    """Extract the duration from a `timeout` invocation.

    Accepts the forms coreutils accepts -- `timeout 5`, `timeout 5s`, `timeout -k 1
    5`, `timeout --signal=TERM 5` -- because parsing only a bare integer raised
    ValueError on perfectly valid commands instead of checking the budget.
    """
    args = line.split("timeout", 1)[1].split()
    # -k/--kill-after and --signal take a value; consuming it as the duration is
    # how `timeout -k 1 5` was read as a 1-second budget.
    takes_value = {"-k", "--kill-after", "-s", "--signal"}
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            skip_next = arg in takes_value
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", arg)
        if not match:
            continue
        value = float(match.group(1))
        return int(value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)])
    raise AssertionError(f"no timeout duration found in: {line.strip()}")


def test_redis_node_cold_start_fits_inside_its_healthcheck_budget() -> None:
    """A cold Compose start must not be killed by its own healthcheck.

    Each redis node probes all three sentinels before exec'ing redis-server. On a
    cold start those sentinels cannot be up -- they depend on these very nodes
    reporting healthy -- so every hostname fails to resolve. Unbounded, each
    lookup cost ~20s, so redis-server started 60s after the container did, while
    the healthcheck gave up at start_period + retries * interval (~55s). Compose
    marked all three nodes unhealthy and aborted the deploy with exit 1, every
    time, on any machine with a fresh network and volumes.

    Guard the invariant rather than the wording: every probe in the startup
    script is bounded, and the resulting worst case still fits the budget.
    """
    compose = _compose_sentinel()
    command = compose["services"]["redis"]["command"]
    script = command[-1] if isinstance(command, list) else command

    probe_lines = [
        line.strip()
        for line in script.splitlines()
        if "redis-cli" in line and "sentinel_host" in line
    ]
    assert probe_lines, "expected the startup script to probe sentinels"

    worst_case_probe_seconds = 0
    for line in probe_lines:
        assert line.startswith("if timeout ") or " timeout " in line, (
            f"unbounded sentinel probe would hang ~20s on a cold start: {line}"
        )
        worst_case_probe_seconds += _timeout_seconds(line)

    # Three sentinel hostnames are probed in sequence.
    worst_case_startup = worst_case_probe_seconds * 3

    healthcheck = compose["services"]["redis"]["healthcheck"]
    budget = _duration_seconds(healthcheck["start_period"]) + healthcheck[
        "retries"
    ] * _duration_seconds(healthcheck["interval"])

    assert worst_case_startup < budget, (
        f"worst-case cold start {worst_case_startup}s exceeds the healthcheck "
        f"budget {budget}s; compose will mark redis unhealthy and abort the deploy"
    )


def _dc_invocation(env: dict[str, str]) -> list[str]:
    """Source dev-lib.sh with `docker` stubbed, and return the argv `dc` builds."""
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "docker"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"set -euo pipefail; source {ROOT / 'deploy/scripts/dev-lib.sh'}; dc config-noop",
            ],
            cwd=ROOT,
            env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", **env},
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    return result.stdout.split()


def test_dc_addresses_the_stack_deploy_stack_actually_created() -> None:
    """The compose-* helpers must target the deployment, not a different one.

    `dc` hard-coded docker-compose.service.yml (which flips the council to
    PostgreSQL and adds a postgres service) and project "nomos". A default
    `DEPLOY_TARGET=compose ./scripts/deploy_stack.sh` uses the Redis/sentinel
    topology under project "deploy", so every compose-* helper addressed a
    backend that was not running and a project that did not exist:
    compose-register-agent.sh died with "failed to resolve host 'postgres'".
    """
    argv = _dc_invocation(env={})

    # Project name must match scripts/deploy_stack.sh:compose_project_name,
    # which is COMPOSE_PROJECT_NAME or the compose file's directory ("deploy").
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "deploy"

    files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
    assert any(f.endswith("deploy/docker-compose.yml") for f in files)
    assert any(f.endswith("docker-compose.redis-sentinel.yml") for f in files), (
        "default topology is sentinel; dc must overlay it like deploy_stack.sh does"
    )
    assert not any(f.endswith("docker-compose.service.yml") for f in files), (
        "service.yml switches storage to postgres and is not part of a default deploy"
    )


def test_dc_honors_the_same_topology_switch_as_deploy_stack() -> None:
    standalone = _dc_invocation(env={"COMPOSE_REDIS_TOPOLOGY": "standalone"})
    files = [standalone[i + 1] for i, a in enumerate(standalone) if a == "-f"]
    assert not any(f.endswith("docker-compose.redis-sentinel.yml") for f in files)

    named = _dc_invocation(env={"COMPOSE_PROJECT_NAME": "custom-project"})
    assert named[named.index("-p") + 1] == "custom-project"


def test_compose_register_agent_surfaces_failures_and_aborts_without_a_token() -> None:
    """A registration failure must reach the operator, and no token must abort.

    Grepping for `2>/dev/null` and `die ` asserted the shape of the source, not
    the behaviour: a refactor that kept both strings while still swallowing output
    would pass. Run the script against a stubbed `dc` instead.
    """
    import tempfile

    def run(cli_stdout: str, cli_stderr: str, cli_rc: int) -> subprocess.CompletedProcess[str]:
        """Stub `docker` on PATH, so the script's own dc() wrapper still runs.

        Stubbing dc() directly does not work: compose-register-agent.sh sources
        dev-lib.sh, which redefines dc() over any stub -- so the real wrapper (and
        its overlay/project selection) is part of what this exercises.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "docker"
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s' {shlex.quote(cli_stdout)}\n"
                f"printf '%s' {shlex.quote(cli_stderr)} >&2\n"
                f"exit {cli_rc}\n"
            )
            stub.chmod(0o755)
            env_file = Path(tmp) / ".env"
            env_file.write_text("")
            return subprocess.run(
                [str(ROOT / "deploy/scripts/compose-register-agent.sh"), "agent-x", "council-prod"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{tmp}:{os.environ['PATH']}",
                    "ENV_FILE": str(env_file),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

    # 1. The CLI fails: its diagnostics must reach the operator, not /dev/null.
    failed = run("", "cannot connect to PostgreSQL: host 'postgres' not found\n", 1)
    assert failed.returncode != 0
    assert "host 'postgres' not found" in failed.stderr, (
        "the failure reason was swallowed; every distinct failure looks identical"
    )

    # 2. The CLI succeeds but returns no token: that must abort, not print empty.
    no_token = run('{\n  "agent_id": "agent-x"\n}\n', "", 0)
    assert no_token.returncode != 0
    assert "AGENT_TOKEN=" not in no_token.stdout

    # 3. Diagnostics on stderr must not be parsed as the token stream.
    good = run(
        '{\n  "agent_id": "agent-x",\n  "token": "' + "a" * 64 + '"\n}\n',
        "warning: no account integrity key is configured\n",
        0,
    )
    assert good.returncode == 0, good.stderr
    assert f"AGENT_TOKEN={'a' * 64}" in good.stdout


def _run_register_script(args: list[str], env: dict[str, str], *, token: str = "") -> dict:
    """Run kind-register-agent.sh against a recording kubectl stub.

    The stub records every invocation and the manifest piped to `apply`, so tests
    can assert what the script *did* rather than that it merely exited non-zero.
    That distinction matters here: with a stub that always succeeds, a rejection
    test passes even when validation never fires, because the empty log yields no
    token and the script dies anyway -- the right outcome for the wrong reason.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        calls = tmpdir / "calls.log"
        manifests = tmpdir / "manifests.yaml"
        stub = tmpdir / "kubectl"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$KUBECTL_CALLS"\n'
            'if [[ " $* " == *" apply "* ]]; then cat >> "$KUBECTL_MANIFESTS"; fi\n'
            'if [[ " $* " == *" logs "* && -n "${FAKE_TOKEN:-}" ]]; then\n'
            '  printf \'{\\n  "agent_id": "x",\\n  "token": "%s"\\n}\\n\' "$FAKE_TOKEN"\n'
            "fi\n"
            "exit 0\n"
        )
        stub.chmod(0o755)
        (tmpdir / "helm").write_text("#!/usr/bin/env bash\nexit 0\n")
        (tmpdir / "helm").chmod(0o755)

        result = subprocess.run(
            [str(ROOT / "deploy/kubernetes/scripts/kind-register-agent.sh"), *args],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{tmpdir}:{os.environ['PATH']}",
                "KUBECTL_CALLS": str(calls),
                "KUBECTL_MANIFESTS": str(manifests),
                "FAKE_TOKEN": token,
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "calls": calls.read_text() if calls.exists() else "",
            "manifests": manifests.read_text() if manifests.exists() else "",
        }


def test_register_script_rejects_manifest_injecting_values() -> None:
    """Hostile values must be refused *before* anything reaches kubectl.

    The heredoc is unquoted and expands agent id, council id and role directly
    into a JSON flow sequence inside the Job manifest, so a value carrying a
    quote, comma or bracket escapes the sequence and rewrites the container
    `args`. Asserting on the exit code alone is not enough -- the script exits
    non-zero for many reasons. The load-bearing assertion is that `apply` is
    never invoked.
    """
    hostile = '","--role","admin","x'

    result = _run_register_script([hostile], env={})

    assert result["returncode"] != 0
    assert "not allowed" in (result["stderr"] + result["stdout"])
    assert "apply" not in result["calls"], (
        "hostile input reached kubectl apply; validation did not fire first"
    )
    assert result["manifests"] == "", "a manifest was submitted for a rejected id"


def test_register_script_validates_council_id_and_role() -> None:
    for args, env in (
        (["good-agent", 'evil","x'], {}),
        (["good-agent", "council-prod"], {"AGENT_ROLE": 'admin","y'}),
    ):
        result = _run_register_script(args, env=env)
        assert result["returncode"] != 0, f"expected refusal for {args} {env}"
        assert "not allowed" in (result["stderr"] + result["stdout"])
        assert "apply" not in result["calls"], f"{args} {env} reached kubectl apply"


def test_register_script_deletes_the_job_holding_the_token() -> None:
    """On success the Job must be deleted, not merely left to its TTL.

    The raw token is retrieved by reading the Job's logs, so it necessarily
    exists there; what must not happen is the Job outliving the read.
    """
    result = _run_register_script(["good-agent"], env={}, token="f" * 64)

    assert result["returncode"] == 0, result["stderr"]
    assert "AGENT_TOKEN=" + "f" * 64 in result["stdout"]

    job_names = {
        line.split()[-2] for line in result["calls"].splitlines() if " delete job " in f" {line} "
    }
    assert job_names, "the Job was never deleted"
    applied = re.search(r"name:\s*(nomos-register-\S+)", result["manifests"])
    assert applied, "no Job manifest was applied"
    assert applied.group(1) in job_names, "the deleted Job is not the one that was created"


def test_register_job_name_is_kubernetes_safe() -> None:
    """AGENT_ID and metadata.name have different contracts.

    Uppercase, underscores, trailing hyphens and unbounded length are all legal
    in an agent id and all rejected by the API server, so reusing the id as the
    Job name fails at apply time rather than anywhere actionable.
    """
    rfc1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    for agent_id in ("Agent_1", "agent-", "A" * 200, "___"):
        result = _run_register_script([agent_id], env={}, token="a" * 64)
        assert result["returncode"] == 0, f"{agent_id!r}: {result['stderr']}"
        name = re.search(r"name:\s*(\S+)", result["manifests"])
        assert name, f"{agent_id!r} produced no manifest"
        job = name.group(1)
        assert rfc1123.fullmatch(job), f"{agent_id!r} produced invalid Job name {job!r}"
        assert len(job) <= 63, f"{agent_id!r} produced an over-long Job name ({len(job)})"

    # Ids that slugify identically must not collide on one Job.
    a = _run_register_script(["Agent_1"], env={}, token="a" * 64)["manifests"]
    b = _run_register_script(["agent-1"], env={}, token="a" * 64)["manifests"]
    assert re.search(r"name:\s*(\S+)", a).group(1) != re.search(r"name:\s*(\S+)", b).group(1)


def test_register_job_does_not_retry_a_non_idempotent_create() -> None:
    """A retry cannot recover the token but can orphan the account.

    `create` runs without --force. If a pod commits the account and then dies
    before its JSON reaches stdout, the retry fails with "already exists" and the
    raw token is gone, leaving a durable enabled account nobody holds a
    credential for. Failing the registration outright is recoverable; that is not.
    """
    result = _run_register_script(["good-agent"], env={}, token="b" * 64)

    backoff = re.search(r"backoffLimit:\s*(\d+)", result["manifests"])
    assert backoff and int(backoff.group(1)) == 0, "a non-idempotent create must not be retried"
    ttl = re.search(r"ttlSecondsAfterFinished:\s*(\d+)", result["manifests"])
    assert ttl and int(ttl.group(1)) <= 60, (
        "a long TTL leaves a live agent token readable in-cluster"
    )


def test_register_manifest_heredoc_has_no_command_substitution() -> None:
    """The Job manifest heredoc is unquoted, so backticks in it are executed.

    An unquoted heredoc performs command substitution on its body. A backtick in
    a YAML comment inside it is therefore run as a command while the manifest is
    being built, and the substituted text is stripped from the manifest. This is
    the same defect class as the interpolation issue this script already guards:
    content evaluated in a context that was assumed to be inert.

    It bit us for real -- a prose comment reading `create` executed `create`, and
    the resulting "command not found" was briefly mistaken for evidence that an
    injection test was working.
    """
    script = (ROOT / "deploy/kubernetes/scripts/kind-register-agent.sh").read_text(encoding="utf-8")

    body = script.split("cat <<EOF2", 1)[1].split("\nEOF2", 1)[0]

    assert "`" not in body, (
        "backticks inside the unquoted heredoc are command substitution, not quoting"
    )
    assert "$(" not in body.replace("$(POSTGRES_PASSWORD)", ""), (
        "command substitution inside the manifest heredoc runs at build time"
    )


def test_register_manifest_is_built_without_running_stray_commands() -> None:
    """Building the manifest must not emit shell errors from its own comments."""
    result = _run_register_script(["good-agent"], env={}, token="c" * 64)

    assert "command not found" not in result["stderr"], result["stderr"]
    assert "No retries" in result["manifests"], (
        "the comment text was mangled, which means something in it was substituted"
    )


def test_grpc_and_monitor_read_the_proxy_token_from_one_secret() -> None:
    """Both pods must resolve the MCP proxy token to the same Secret object.

    `nomos.componentSecretName` returns a per-component Secret when
    `secrets.componentSecrets.enabled` is set and that component has an entry.
    Reading this key under the `council-grpc` component therefore let a
    monitor-only override point the two pods at different Secrets, giving them
    different tokens and reintroducing the 401-on-every-gRPC-call failure that
    wiring the token was meant to fix. The token belongs to the monitor, so both
    consumers resolve it under the monitor component.
    """
    templates = ROOT / "deploy/kubernetes/nomos/templates"
    grpc = (templates / "council-grpc.yaml").read_text(encoding="utf-8")
    monitor = (templates / "monitor.yaml").read_text(encoding="utf-8")
    gateway = (templates / "council-gateway.yaml").read_text(encoding="utf-8")

    def proxy_token_component(template: str) -> str | None:
        for line in template.splitlines():
            if "monitorMcpProxyToken" not in line or "secretKeyRef" not in line:
                continue
            match = re.search(r'"component"\s+"([^"]+)"', line)
            return match.group(1) if match else None
        return None

    grpc_component = proxy_token_component(grpc)
    assert grpc_component is not None, "council-grpc no longer wires the proxy token"
    assert grpc_component == proxy_token_component(monitor), (
        "council-grpc resolves the MCP proxy token under a different component than "
        f"the monitor ({grpc_component!r}); a componentSecrets override would give "
        "them different tokens and every gRPC call would 401"
    )
    assert grpc_component == proxy_token_component(gateway), (
        "council-gateway and council-grpc disagree on the proxy token's component"
    )


def _dc_files_for(script: str, env: dict[str, str], args: tuple[str, ...] = ()) -> list[str]:
    """Source a compose-* script's prelude and return the -f list dc would build.

    `args` are the positional arguments the script requires before it reaches the
    overlay block: compose-register-agent.sh guards `<agent-id>` with
    `${1:?usage}`, so sourcing its prelude with no argv aborts before dc() runs.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "docker"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        prelude = (ROOT / "deploy/scripts" / script).read_text(encoding="utf-8")
        prelude = prelude.split("export COMPOSE_EXTRA_FILES", 1)[0] + "export COMPOSE_EXTRA_FILES\n"
        # Drop the shebang/flags line so we can source the prelude in isolation.
        # Drop the shebang, `set -e` flags, and the script's own SCRIPT_DIR
        # assignment: under `bash -c` BASH_SOURCE is empty, so recomputing it
        # would point dev-lib.sh at the wrong directory.
        prelude = "\n".join(
            line
            for line in prelude.splitlines()
            if not line.startswith(("#!", "set -", "SCRIPT_DIR="))
        )
        bash_script = f'SCRIPT_DIR="{ROOT / "deploy/scripts"}"\n{prelude}\ndc config-noop'
        command = ["bash", "-c", bash_script]
        # `bash -c <string> [name [args...]]`: the first trailing word becomes $0
        # and the rest $1, $2, ..., which is how the prelude's positional guards
        # (`${1:?usage}`) see them.
        if args:
            command += [script, *args]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", **env},
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    argv = result.stdout.split()
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]


@pytest.mark.parametrize("script", ["compose-up.sh", "compose-down.sh"])
def test_service_mode_scripts_declare_the_service_overlay(script: str) -> None:
    """postgres and vault exist only in the service overlay.

    dc() selects overlays from the environment so it mirrors deploy_stack.sh
    rather than hard-coding one topology. That means the scripts which actually
    deploy the service topology have to declare it: without this, the first
    backend command (`dc up -d postgres vault`) cannot resolve either service.
    """
    files = _dc_files_for(script, env={})

    assert any(f.endswith("docker-compose.service.yml") for f in files), (
        f"{script} runs in service mode but never adds the service overlay"
    )
    assert files[0].endswith("deploy/docker-compose.yml"), "base compose file must come first"
    assert (
        files.index(next(f for f in files if f.endswith("docker-compose.service.yml")))
        == len(files) - 1
    ), "the service overlay must be applied last so its overrides win"


def test_explicit_extra_files_are_preserved() -> None:
    """An operator-supplied overlay must not be replaced by the service default."""
    files = _dc_files_for("compose-up.sh", env={"COMPOSE_EXTRA_FILES": "/tmp/custom.yml"})

    assert "/tmp/custom.yml" in files
    assert any(f.endswith("docker-compose.service.yml") for f in files)


def test_compose_register_agent_declares_the_service_overlay() -> None:
    """A standalone registration must target the store the service council reads.

    compose-up.sh exports COMPOSE_EXTRA_FILES only to the processes it spawns, so
    a fresh `compose-register-agent.sh` run does not inherit it. Without the
    script declaring the overlay itself, dc() sees only the base file: the account
    is created in the dev store while the service-mode council authenticates
    against postgres, so the printed token is one that council never honours --
    a silent failure, not the "host 'postgres' not found" that up/down would hit.
    """
    files = _dc_files_for("compose-register-agent.sh", env={}, args=("agent-x",))

    assert any(f.endswith("docker-compose.service.yml") for f in files), (
        "compose-register-agent.sh registers into the service store but never adds "
        "the service overlay, so it would write to the dev store instead"
    )
    assert files[0].endswith("deploy/docker-compose.yml"), "base compose file must come first"
    assert (
        files.index(next(f for f in files if f.endswith("docker-compose.service.yml")))
        == len(files) - 1
    ), "the service overlay must be applied last so its overrides win"


def test_compose_register_agent_preserves_explicit_extra_files() -> None:
    """A caller-supplied overlay survives alongside the service default.

    The script prepends the overlay rather than overwriting COMPOSE_EXTRA_FILES,
    so an operator overriding the topology keeps their file and still reaches the
    service store.
    """
    files = _dc_files_for(
        "compose-register-agent.sh",
        env={"COMPOSE_EXTRA_FILES": "/tmp/custom.yml"},
        args=("agent-x",),
    )

    assert "/tmp/custom.yml" in files
    assert any(f.endswith("docker-compose.service.yml") for f in files)


def test_compose_register_agent_does_not_duplicate_the_service_overlay() -> None:
    """The overlay is added once even when it is already present.

    dc() is called for real work with these `-f` files; the guard skips the
    prepend when COMPOSE_EXTRA_FILES already names the overlay, so an inherited
    value does not compound into a duplicated `-f`.
    """
    overlay = str(ROOT / "deploy/docker-compose.service.yml")
    files = _dc_files_for(
        "compose-register-agent.sh",
        env={"COMPOSE_EXTRA_FILES": overlay},
        args=("agent-x",),
    )

    assert files.count(overlay) == 1


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("if timeout 5 redis-cli ...", 5),
        ("if timeout 5s redis-cli ...", 5),
        ("if timeout -k 1 5 redis-cli ...", 5),
        ("if timeout --signal=TERM 30 redis-cli ...", 30),
        ("if timeout 2m redis-cli ...", 120),
    ],
    ids=["bare", "suffix-s", "kill-after", "long-option", "minutes"],
)
def test_timeout_parsing_accepts_the_forms_coreutils_accepts(line: str, expected: int) -> None:
    """Parsing only a bare integer raised ValueError on valid commands."""
    assert _timeout_seconds(line) == expected
