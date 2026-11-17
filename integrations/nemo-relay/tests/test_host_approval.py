# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host assertions are accepted only under explicit operator trust."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass

import pytest
from support import Next, base_config, call, turn

from nemo_relay_nomos import NomosPlugin
from nemo_relay_nomos.host_approval import same_json
from nemo_relay_nomos.settings import Settings, SettingsError, diagnostics

TOOL = "write_file"
SOURCE = "example.application"
ARGS = {"path": "/workspace/project/note.txt", "content": "hello", "workspace_root": "/workspace/project"}


@dataclass
class ContextWithExecutionContext:
    """Unit-test SDK double; real transport is exercised in the opt-in runtime tier."""

    tool_name: str
    args: dict
    tool_call_id: str | None = None
    execution_context: dict | None = None


@pytest.fixture(autouse=True)
def sdk_with_execution_context(monkeypatch):
    # The locked SDK is 0.11.1. Simulate only the new callback field here, not
    # successful wire transport or compatibility with a released SDK.
    monkeypatch.setattr("nemo_relay_nomos.plugin.ToolExecutionContext", ContextWithExecutionContext)


def config(**overrides):
    return base_config(
        guard={"include": [TOOL]},
        tools={TOOL: {"executor": "local"}},
        host_approval={"source": SOURCE, "tools": [TOOL]},
        **overrides,
    )


def proof(arguments=None, **overrides):
    return {
        "host_approval": {
            "version": 1,
            "source": SOURCE,
            "decision": "approved",
            "approval_id": "approval-test",
            "tool_name": TOOL,
            "approved_arguments": deepcopy(ARGS if arguments is None else arguments),
            **overrides,
        }
    }


def context(arguments=None, assertion=None, *, call_id="provider-call"):
    return ContextWithExecutionContext(
        TOOL,
        deepcopy(ARGS if arguments is None else arguments),
        call_id,
        execution_context=proof() if assertion is None else assertion,
    )


async def test_host_approval_needs_neither_model_intent_nor_a_native_origin(setup):
    plugin, council, executor, _ = setup(config=config())
    nxt = Next(result={"success": True, "data": "project", "metadata": {"durationMs": 0}})
    result = await plugin.h3(context(), nxt)
    assert nxt.calls == [ARGS] and not council.calls and not executor.calls
    assert result.result["status"] == "executed"
    assert result.result["execution_status"] == "executed"
    assert result.result["evidence_status"] == "unavailable"
    assert result.result["authorization_source"] == "host_human_approval"
    assert result.result["output"] == {"success": True, "data": "project", "metadata": {"durationMs": 0}}
    assert result.annotation["tool"] == {"original": True}
    assert "writ_id" not in result.result and "council" not in result.annotation["nomos"]
    assert plugin.store.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 0


async def test_host_approval_is_disabled_by_default(setup):
    raw = config()
    del raw["host_approval"]
    plugin, council, _, _ = setup(config=raw)
    nxt = Next()
    result = await plugin.h3(context(), nxt)
    assert result.result["status"] == "host_approval_untrusted"
    assert not nxt.calls and not council.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "untrusted.application"},
        {"decision": "denied"},
        {"version": True},
        {"version": 2},
        {"tool_name": "read_file"},
        {"approval_id": ""},
        {"approval_id": "x" * 257},
        {"extra": "not-a-field"},
        {"approved_arguments": {"command": "different"}},
        {"approved_arguments": {**ARGS, "workspace_root": "/different"}},
        {"approved_arguments": {**ARGS, "command": True}},
    ],
)
async def test_invalid_host_approval_never_calls_council_or_tool(setup, changes):
    plugin, council, _, _ = setup(config=config())
    nxt = Next()
    result = await plugin.h3(context(assertion=proof(**changes)), nxt)
    assert result.result["execution_status"] == "not_executed"
    assert not nxt.calls and not council.calls


@pytest.mark.parametrize(
    "assertion",
    [
        {"host_approval": None},
        {"host_approval": []},
        {"host_approval": {}},
        {**proof(), "extra": True},
        proof(approved_arguments={**ARGS, "nonfinite": float("nan")}),
        proof(approved_arguments={"command": "x" * 16_384}),
    ],
)
async def test_malformed_or_oversized_context_fails_closed(setup, assertion):
    plugin, council, _, _ = setup(config=config())
    nxt = Next()
    result = await plugin.h3(context(assertion=assertion), nxt)
    assert result.result["execution_status"] == "not_executed"
    assert not nxt.calls and not council.calls


async def test_putting_approval_in_model_arguments_does_not_grant_it(setup):
    plugin, council, _, _ = setup(config=config())
    nxt = Next()
    result = await plugin.h3(ContextWithExecutionContext(TOOL, {**ARGS, **proof()}, "call"), nxt)
    assert result.result["status"] == "missing_intent"
    assert not nxt.calls and not council.calls


async def test_approval_id_consumed_once_even_with_a_different_provider_id(setup):
    plugin, _, _, _ = setup(config=config())
    first, second = Next(), Next()
    assert (await plugin.h3(context(), first)).result["status"] == "executed"
    replay = await plugin.h3(context(call_id="different-provider-call"), second)
    assert first.calls == [ARGS] and not second.calls
    assert replay.result["status"] == "host_approval_replayed"
    assert replay.result["execution_status"] == "not_executed"
    assert replay.result["prior_execution_status"] == "executed"


async def test_reusing_an_approval_for_a_different_payload_is_a_conflict(setup):
    plugin, _, _, _ = setup(config=config())
    await plugin.h3(context(), Next())
    changed = {**ARGS, "command": "ls"}
    nxt = Next()
    result = await plugin.h3(context(changed, proof(changed)), nxt)
    assert result.result["status"] == "tool_call_id_conflict" and not nxt.calls


async def test_ordinary_denied_call_still_reaches_council(setup):
    plugin, council, _, _ = setup("denied", config=config())
    origin = turn(plugin)
    nxt = Next()
    result = await plugin.h3(call(name=TOOL, turn=origin, **ARGS), nxt)
    assert result.result["status"] == "denied"
    assert len(council.calls) == 1 and not nxt.calls


@pytest.mark.parametrize(
    "left,right,equal",
    [
        ({"value": True}, {"value": 1}, False),
        ({"value": False}, {"value": 0}, False),
        ({"value": 1}, {"value": 1.0}, True),
        ({"value": [1, None]}, {"value": [1, None]}, True),
        ({"value": [1]}, {"value": [1, 2]}, False),
        ({"value": "1"}, {"value": 1}, False),
    ],
)
def test_json_comparison_keeps_primitive_types_distinct(left, right, equal):
    assert same_json(left, right) is equal


def test_host_approval_configuration_requires_guarded_local_tools_and_subject():
    for mutation in ("remote", "excluded", "subject", "glob"):
        raw = config()
        if mutation == "remote":
            raw["tools"][TOOL] = {"executor": "nomos_http", "command_argument": "command"}
        elif mutation == "excluded":
            raw["guard"]["exclude"] = [TOOL]
        elif mutation == "subject":
            del raw["tool_call"]
        else:
            raw["host_approval"]["tools"] = ["write_*"]
        assert diagnostics(raw)
        with pytest.raises(SettingsError):
            Settings.from_config(raw)


def test_feature_activation_rejects_an_sdk_without_context_support(monkeypatch):
    from support import Context

    monkeypatch.setattr("nemo_relay_nomos.plugin.ToolExecutionContext", type("LegacyContext", (), {}))
    with pytest.raises(ValueError, match="requires a source-matched Relay SDK"):
        NomosPlugin().register(Context(), config())


async def test_cancelled_dispatch_cannot_be_retried(setup):
    plugin, _, _, _ = setup(config=config())

    class Cancel:
        async def call(self, arguments):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await plugin.h3(context(), Cancel())
    nxt = Next()
    result = await plugin.h3(context(), nxt)
    assert result.result["execution_status"] == "unknown" and not nxt.calls


@pytest.mark.parametrize("failure", ["exception", "oversized", "non_json"])
async def test_uncertain_host_dispatch_blocks_replay_and_further_approvals(setup, failure):
    plugin, council, _, _ = setup(config=config())
    dispatched = []

    class FailingContinuation:
        async def call(self, arguments):
            dispatched.append(arguments)
            if failure == "exception":
                raise RuntimeError("private callback detail")
            value = "x" * plugin.settings.limits.max_result_bytes if failure == "oversized" else object()
            return type("Result", (), {"result": value, "annotation": None})()

    result = await plugin.h3(context(), FailingContinuation())
    assert result.result["status"] == "execution_unknown"
    assert result.result["execution_status"] == "unknown"
    assert "private callback detail" not in str(result.result)
    later = Next()
    repeated = await plugin.h3(context(), later)
    different = await plugin.h3(context(assertion=proof(approval_id="another-approval")), later)
    assert repeated.result["status"] == "host_approval_replayed"
    assert repeated.result["execution_status"] == "not_executed"
    assert repeated.result["prior_execution_status"] == "unknown"
    assert different.result["status"] == "session_requires_reconciliation"
    assert different.result["execution_status"] == "unknown"
    assert dispatched == [ARGS] and not later.calls and not council.calls


async def test_expired_operator_trust_rejects_host_approval(setup):
    from dataclasses import replace

    plugin, council, _, _ = setup(config=config())
    plugin.settings = replace(plugin.settings, trust=replace(plugin.settings.trust, valid_until=0))
    nxt = Next()
    result = await plugin.h3(context(), nxt)
    assert result.result["status"] == "trust_policy_expired"
    assert not nxt.calls and not council.calls


async def test_opening_an_ordinary_turn_preserves_host_replay_protection(setup):
    plugin, _, _, _ = setup(config=config())
    first = Next()
    await plugin.h3(context(), first)
    turn(plugin)
    later = Next()
    result = await plugin.h3(context(), later)
    assert result.result["status"] == "host_approval_replayed"
    assert first.calls == [ARGS] and not later.calls
