# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in source-matched Relay cache-order regression; no external services."""

import os

import pytest

if os.getenv("NOMOS_TEST_SOURCE_RELAY") != "1":
    pytest.skip("requires the matching source-built Relay runtime", allow_module_level=True)

from nemo_relay import ToolExecutionInterceptOutcome, ToolExecutionResult, adaptive, intercepts, plugin, tools
from test_host_approval import ARGS, TOOL, config, proof

from nemo_relay_nomos.plugin import EXECUTION_PRIORITY


async def test_real_tool_cache_cannot_skip_host_approval_validation(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("NEMO_RELAY_TEST_SKIP_IMPLICIT_CONFIG", "1")
    registry = tmp_path / "plugins.toml"
    registry.write_text("version = 1\n")
    worker, council, _, _ = setup(config=config())
    cache = adaptive.ComponentSpec(
        {
            "version": 1,
            "response_cache": {
                "ttl_seconds": 60,
                "namespace": "host-approval-regression",
                "backend": {"kind": "in_memory"},
                "tools": {"enabled": True, "classes": {"fixture": {"cacheable": True, "members": [TOOL]}}},
            },
        }
    )
    activation = await plugin.initialize(plugin.PluginConfig(components=[cache]), str(registry))
    business_calls = []

    class Continuation:
        def __init__(self, next_call):
            self.next_call = next_call

        async def call(self, arguments):
            return await self.next_call(arguments)

    async def govern(context, next_call):
        result = await worker.h3(context, Continuation(next_call))
        return ToolExecutionInterceptOutcome(result.result, annotation=result.annotation)

    async def business(arguments):
        business_calls.append(arguments)
        return ToolExecutionResult({"success": True, "data": "synthetic"})

    intercepts.register_tool_execution("host-approval-regression", EXECUTION_PRIORITY, govern)
    try:
        first = await tools.execute(TOOL, ARGS, business, execution_context=proof())
        assert first.result["status"] == "executed"
        # Schema-valid but untrusted context must reach Nomos before the cache.
        # A cached business result must not skip the component's trust checks.
        second = await tools.execute(
            TOOL, ARGS, business, execution_context=proof(source="untrusted.application")
        )
        assert second.result["status"] == "host_approval_untrusted"
        assert second.result["execution_status"] == "not_executed"
        # A removed decision key is a schema refusal before middleware, even if
        # a reusable earlier middleware cache already contains this operation.
        with pytest.raises(ValueError, match="context"):
            await tools.execute(TOOL, ARGS, business, execution_context=proof(decision="denied"))
        assert business_calls == [ARGS] and not council.calls
    finally:
        intercepts.deregister_tool_execution("host-approval-regression")
        await activation.close()
