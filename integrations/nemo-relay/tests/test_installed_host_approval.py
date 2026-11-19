# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Approval context through a freshly installed, separate Python worker process.

The Relay runtime, CLI installer and worker SDK are real. Council responses and
business effects are local fixtures; this does not establish live model quality.
"""

import os
from copy import deepcopy

import pytest

if not os.getenv("NOMOS_TEST_RELAY_BIN"):
    pytest.skip("requires the matching source-built Relay CLI", allow_module_level=True)

from nemo_relay import ToolExecutionInterceptOutcome, ToolExecutionResult, intercepts, plugin, tools
from support import base_config
from test_installed_worker import Services, install

from nemo_relay_nomos.plugin import EXECUTION_PRIORITY

pytestmark = pytest.mark.installed


async def test_installed_worker_preserves_approval_context_and_current_policy(tmp_path, monkeypatch):
    """Carry exact approval/runtime assertions without a model snapshot or intent."""
    name = "read_file"
    arguments = {"path": "/host/project/note.txt"}
    runtime = {
        "working_directory": "/host/project",
        "workspace_mappings": [
            {"logical_root": "/workspace/project", "execution_root": "/host/project"},
        ],
    }
    config = base_config(
        guard={"include": [name]},
        tools={name: {"executor": "local"}},
        host_approval={"source": "example.application", "tools": [name]},
        runtime_context={"source": "example.application", "tools": [name]},
    )
    async with Services() as services:
        config["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, config, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        calls = []

        async def downstream(context, next_call):
            calls.append("downstream")
            result = await next_call(context.args)
            return ToolExecutionInterceptOutcome(result.result, annotation=result.annotation)

        async def business(actual):
            assert actual == arguments
            calls.append("business")
            return ToolExecutionResult({"content": "synthetic file"})

        def proof(identity, actual, runtime_changes=None, **changes):
            assertion = {
                "version": 1,
                "source": "example.application",
                "approval_id": identity,
                "tool_name": name,
                "approved_arguments": deepcopy(arguments),
                **changes,
            }
            facts = {
                "version": 1,
                "source": "example.application",
                "tool_name": name,
                "tool_call_id": "call-" + identity,
                "arguments": deepcopy(actual),
                "runtime": deepcopy(runtime),
                **(runtime_changes or {}),
            }
            return {"data": {"host_approval": assertion, "runtime_context": facts}}

        async def execute(identity, *, actual=None, runtime_changes=None, **changes):
            actual = arguments if actual is None else actual
            return await tools.execute(
                name,
                actual,
                business,
                tool_call_id="call-" + identity,
                execution_context=proof(identity, actual, runtime_changes, **changes),
            )

        intercepts.register_tool_execution("after-approval", EXECUTION_PRIORITY + 1, downstream)
        try:
            allowed = await execute("approved-1")
            assert allowed.result["status"] == "executed", allowed.result
            assert calls == ["downstream", "business"]
            assert services.council.policy_calls[0]["tool_call"]["arguments"] == arguments
            assert not services.council.calls  # No ordinary Council review or invented writ.

            replay = await execute("approved-1")
            assert replay.result["status"] == "host_approval_replayed"
            for identity, changes, expected in [
                ("untrusted", {"source": "untrusted.application"}, "host_approval_untrusted"),
                ("malformed", {"version": 2}, "host_approval_invalid"),
                (
                    "changed",
                    {"actual": {"path": "/host/project/other.txt"}},
                    "host_approval_operation_mismatch",
                ),
            ]:
                refused = await execute(identity, **changes)
                assert refused.result["status"] == expected
                assert refused.result["execution_status"] == "not_executed"
            for identity, runtime_changes, expected in [
                (
                    "runtime-call",
                    {"tool_call_id": "another-call"},
                    "runtime_context_operation_mismatch",
                ),
                (
                    "runtime-arguments",
                    {"arguments": {"path": "/host/project/other.txt"}},
                    "runtime_context_operation_mismatch",
                ),
                (
                    "runtime-source",
                    {"source": "untrusted.application"},
                    "runtime_context_untrusted",
                ),
            ]:
                refused = await execute(identity, runtime_changes=runtime_changes)
                assert refused.result["status"] == expected
                assert refused.result["execution_status"] == "not_executed"
            assert calls == ["downstream", "business"]
            assert len(services.council.policy_calls) == 1

            services.council.policy_reply = {"status": "requires_human_approval", "decision_id": "policy-2"}
            satisfied = await execute("approved-2")
            assert satisfied.result["status"] == "executed"
            assert calls == ["downstream", "business"] * 2

            services.council.policy_reply = {
                "status": "denied",
                "error": "Current policy forbids this operation",
            }
            denied = await execute("approved-3")
            assert denied.result["status"] == "denied"
            assert denied.result["execution_status"] == "not_executed"
            assert calls == ["downstream", "business"] * 2
            assert len(services.council.policy_calls) == 3
            assert all(call["policy_only"] for call in services.council.policy_calls)
            for call in services.council.policy_calls:
                submitted = call["tool_call"]
                assert submitted["arguments"] == arguments
                assert submitted["runtime_context"] == {
                    "version": 1,
                    "source": "example.application",
                    "runtime": runtime,
                }
                assert "runtime_context" not in submitted["arguments"]
        finally:
            intercepts.deregister_tool_execution("after-approval")
            await activation.close()
