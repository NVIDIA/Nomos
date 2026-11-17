# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in real OPA + core service + source-matched Relay continuation checks.

No model, live connector or shell command executes. The business callback only
records its arguments. The core service and OPA policy are the actual source
implementations, not duplicated decisions in a fake Council. Relay middleware
runs in process; this tier does not claim remote worker-protocol coverage.
"""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

if os.getenv("NOMOS_TEST_CORE_POLICY") != "1":
    pytest.skip("requires matching core policy source, OPA and source-built Relay", allow_module_level=True)

source = Path(os.environ["NOMOS_TEST_NOMOS_SOURCE"]).resolve()
sys.path.insert(0, str(source))

from nemo_relay import (  # noqa: E402
    ToolExecutionInterceptOutcome,
    ToolExecutionResult,
    intercepts,
    plugin,
    tools,
)
from nomos.config.settings import CouncilSettings, OPAConfig  # noqa: E402
from nomos.nat.functions import AuthorizeInput, CouncilService  # noqa: E402
from support import base_config, call, turn  # noqa: E402

from nemo_relay_nomos.plugin import EXECUTION_PRIORITY  # noqa: E402

TOOL = "run_shell"


@pytest.fixture
async def core_policy(tmp_path, monkeypatch):
    """Run only isolated loopback OPA and an in-memory core service."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    output = (tmp_path / "opa.log").open("wb")
    process = subprocess.Popen(
        [
            os.environ["NOMOS_TEST_OPA_BIN"],
            "run",
            "--server",
            "--addr",
            f"127.0.0.1:{port}",
            "--log-level",
            "error",
            str(source / "policies/intent_filter.rego"),
        ],
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("isolated OPA failed to start")
                await asyncio.sleep(0.05)
        else:
            pytest.fail("isolated OPA did not become ready")
        service = CouncilService(
            CouncilSettings(
                council_id="council",
                require_auth=False,
                llm_backends={},
                members=[],
                council_key_path=str(tmp_path / "keys"),
                opa=OPAConfig(
                    enabled=True,
                    policy_id="local-bash",
                    shell_tool_arguments={TOOL: "command"},
                    url=f"http://127.0.0.1:{port}",
                ),
            )
        )

        async def unexpected(*args, **kwargs):
            pytest.fail("Policy-only and non-allowing policy decisions must not run model review")

        monkeypatch.setattr(service, "_run_code_review", unexpected)
        yield service
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        output.close()


class CoreCouncil:
    """Adapt the real core service's typed API to the worker's transport protocol."""

    def __init__(self, service):
        self.service = service
        self.calls = []

    async def authorize(self, **fields):
        self.calls.append(fields)
        return (await self.service.authorize(AuthorizeInput.model_validate(fields))).model_dump(mode="json")


@pytest.mark.parametrize(
    "command,expected",
    [
        ("rm note.txt", "requires_human_approval"),
        ("rm -rf *", "denied"),
        ("sudo /bin/echo hello", "denied"),
    ],
)
async def test_real_core_policy_routes_ordinary_proposal_without_effect(
    setup, core_policy, command, expected
):
    council = CoreCouncil(core_policy)
    worker, _, _, _ = setup(
        council=council,
        config=base_config(
            guard={"include": [TOOL]},
            tools={TOOL: {"executor": "local"}},
        ),
    )
    origin = turn(worker)

    class NoEffect:
        async def call(self, arguments):
            pytest.fail("non-allowing policy must not call the continuation")

    result = await worker.h3(call(name=TOOL, turn=origin, command=command), NoEffect())
    assert result.result["status"] == expected
    assert result.result["execution_status"] == "not_executed"
    assert len(council.calls) == 1 and not council.calls[0].get("policy_only")


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/bin/echo hello", "executed"),
        ("rm note.txt", "executed"),
        ("rm -rf *", "denied"),
    ],
)
async def test_real_relay_context_and_core_policy_recheck_on_approved_replay(
    setup,
    core_policy,
    tmp_path,
    monkeypatch,
    command,
    expected,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NEMO_RELAY_TEST_SKIP_IMPLICIT_CONFIG", "1")
    registry = tmp_path / "plugins.toml"
    registry.write_text("version = 1\n")
    council = CoreCouncil(core_policy)
    worker, _, _, _ = setup(
        council=council,
        config=base_config(
            guard={"include": [TOOL]},
            tools={TOOL: {"executor": "local"}},
            host_approval={"source": "example.application", "tools": [TOOL]},
        ),
    )
    activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
    effects = []
    arguments = {"command": command, "cwd": "/workspace/project"}

    class Continuation:
        def __init__(self, next_call):
            self.next_call = next_call

        async def call(self, actual):
            return await self.next_call(actual)

    async def govern(context, next_call):
        result = await worker.h3(context, Continuation(next_call))
        return ToolExecutionInterceptOutcome(result.result, annotation=result.annotation)

    async def business(actual):
        effects.append(actual)
        return ToolExecutionResult({"success": True, "data": "synthetic effect only"})

    intercepts.register_tool_execution("core-policy-runtime", EXECUTION_PRIORITY, govern)
    try:
        result = await tools.execute(
            TOOL,
            arguments,
            business,
            execution_context={
                "host_approval": {
                    "version": 1,
                    "source": "example.application",
                    "approval_id": "test-approved-operation",
                    "tool_name": TOOL,
                    "approved_arguments": arguments,
                },
            },
        )
        assert result.result["status"] == expected
        assert effects == ([arguments] if expected == "executed" else [])
        assert len(council.calls) == 1 and council.calls[0]["policy_only"] is True
        assert "writ" not in result.result
    finally:
        intercepts.deregister_tool_execution("core-policy-runtime")
        await activation.close()
