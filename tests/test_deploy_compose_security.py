# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())


def _compose_sentinel() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.redis-sentinel.yml").read_text())


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

    configs/council.yaml ships `provider: nv_inference`, and
    nomos/llm/nv_inference_backend.py imports `openai`. When deploy/Dockerfile
    omitted `--extra openai`, every council tool call failed at runtime with
    "openai package required for nv_inference backend" — the deployed council
    could not answer council_get_public_key, so executors never started.

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

    for extra in sorted(required):
        for line in install_lines:
            assert f"--extra {extra}" in line, (
                f"configs/council.yaml selects a provider needing the '{extra}' extra, "
                f"but deploy/Dockerfile install line does not include it: {line.strip()}"
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
        seconds = int(line.split("timeout", 1)[1].split()[0])
        worst_case_probe_seconds += seconds

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


def test_compose_register_agent_does_not_discard_errors() -> None:
    """A failure must reach the operator.

    The registration output was captured with `2>/dev/null` inside a command
    substitution, so a wrong backend, an unreachable database and a bad council
    id were indistinguishable: the script exited 1 having printed nothing at all.
    """
    script = (ROOT / "deploy" / "scripts" / "compose-register-agent.sh").read_text(encoding="utf-8")

    assert "2>/dev/null" not in script, "discarding stderr makes every registration failure silent"
    assert "die " in script, "the script must still fail loudly when no token is produced"


def _run_register_script(args: list[str], env: dict[str, str]) -> object:
    """Run kind-register-agent.sh with kubectl stubbed, returning the result."""
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("kubectl", "helm"):
            stub = Path(tmp) / name
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(0o755)
        return subprocess.run(
            [str(ROOT / "deploy/kubernetes/scripts/kind-register-agent.sh"), *args],
            cwd=ROOT,
            env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", **env},
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )


def test_register_script_rejects_manifest_injecting_values() -> None:
    """Values are interpolated into a YAML manifest, so they must be validated first.

    The heredoc is unquoted and expands agent id, council id and role directly
    into a JSON flow sequence inside the Job manifest. A value carrying a quote,
    comma or bracket escapes the sequence and rewrites the manifest -- including
    the container `args`. The CLI's own validation runs inside the container,
    long after `kubectl apply`, so it cannot guard this path.
    """
    hostile = '","--role","admin","x'

    result = _run_register_script([hostile], env={})

    assert result.returncode != 0, "a manifest-injecting agent id must be refused"
    assert "not allowed" in (result.stderr + result.stdout)


def test_register_script_validates_council_id_and_role() -> None:
    for args, env in (
        (["good-agent", 'evil","x'], {}),
        (["good-agent", "council-prod"], {"AGENT_ROLE": 'admin","y'}),
    ):
        result = _run_register_script(args, env=env)
        assert result.returncode != 0, f"expected refusal for {args} {env}"
        assert "not allowed" in (result.stderr + result.stdout)


def test_register_script_does_not_leave_the_token_in_job_logs() -> None:
    """The raw token is read from pod logs, so the Job must not outlive the read."""
    script = (ROOT / "deploy/kubernetes/scripts/kind-register-agent.sh").read_text(encoding="utf-8")

    assert "trap cleanup_job EXIT" in script, (
        "the Job holding the token in its logs must be deleted once read"
    )
    ttl = re.search(r"ttlSecondsAfterFinished:\s*(\d+)", script)
    assert ttl and int(ttl.group(1)) <= 60, (
        "a long TTL leaves a live agent token readable in-cluster"
    )
