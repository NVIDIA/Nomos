# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-call runtime facts never mint permission or depend on tool argument names."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from support import Next, base_config, call, turn

from nemo_relay_nomos.runtime_context import RuntimeContextError, runtime_context
from nemo_relay_nomos.settings import Settings, diagnostics

TOOL = "read_file"
ARGS = {"path": "/host/demo/note.txt"}
RUNTIME = {
    "working_directory": "/host/demo",
    "workspace_mappings": [
        {"logical_root": "/workspace/demo", "execution_root": "/host/demo"},
    ],
}


def config(**overrides):
    return base_config(
        guard={"include": [TOOL]},
        tools={TOOL: {"executor": "local"}},
        runtime_context={"source": "example.application", "tools": [TOOL]},
        **overrides,
    )


def assertion():
    return {
        "data": {
            "runtime_context": {
                "version": 1,
                "source": "example.application",
                "tool_name": TOOL,
                "tool_call_id": "call1",
                "arguments": deepcopy(ARGS),
                "runtime": deepcopy(RUNTIME),
            }
        }
    }


def ctx(envelope=None):
    return SimpleNamespace(
        tool_name=TOOL,
        tool_call_id="call1",
        args=deepcopy(ARGS),
        execution_context=assertion() if envelope is None else envelope,
    )


def test_valid_host_facts_need_no_cwd_named_argument_and_are_snapshotted():
    context = ctx()
    result = runtime_context(context, ARGS, Settings.from_config(config()))
    assert result == {"version": 1, "source": "example.application", "runtime": RUNTIME}
    context.execution_context["data"]["runtime_context"]["runtime"]["workspace_mappings"].clear()
    assert len(result["runtime"]["workspace_mappings"]) == 1
    assert "arguments" not in result and "tool_call_id" not in result


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("version", 1.0),
        ("version", 2),
        ("source", "other"),
        ("tool_name", "other"),
        ("tool_call_id", "other"),
        ("arguments", {"path": "/changed"}),
        ("arguments", None),
        ("runtime", []),
        ("runtime", {}),
        ("runtime", {"working_directory": "relative"}),
        ("runtime", {"working_directory": "/host/../other"}),
        ("runtime", {"workspace_mappings": "invalid"}),
        ("runtime", {"workspace_mappings": [{}]}),
        ("runtime", {"working_directory": "/host/demo", "approved": True}),
        ("runtime", {"working_directory": "/host/\ninvalid"}),
    ],
)
def test_invalid_or_mismatched_context_is_rejected(key, value):
    context = ctx()
    context.execution_context["data"]["runtime_context"][key] = value
    with pytest.raises(RuntimeContextError):
        runtime_context(context, ARGS, Settings.from_config(config()))


@pytest.mark.parametrize(
    "second",
    [
        "/workspace/demo",
        "/workspace/demo/",
        "/workspace//demo",
        "//workspace/demo",
        "///workspace/demo",
        "/workspace/demo/sub",
    ],
)
def test_ambiguous_or_overlapping_mapping_roots_are_rejected(second):
    context = ctx()
    context.execution_context["data"]["runtime_context"]["runtime"]["workspace_mappings"].append(
        {"logical_root": second, "execution_root": "/other"}
    )
    with pytest.raises(RuntimeContextError):
        runtime_context(context, ARGS, Settings.from_config(config()))


@pytest.mark.parametrize(
    "first,second",
    [
        (r"C:\Projects\Demo", "c:/projects/demo/"),
        (r"C:\Projects\Demo", r"c:\projects\demo\src"),
        ("//server/share", r"\\SERVER\SHARE"),
    ],
)
def test_remote_windows_equivalent_roots_are_rejected(first, second):
    context = ctx()
    context.execution_context["data"]["runtime_context"]["runtime"]["workspace_mappings"] = [
        {"logical_root": first, "execution_root": "/host/first"},
        {"logical_root": second, "execution_root": "/host/second"},
    ]
    with pytest.raises(RuntimeContextError, match="invalid"):
        runtime_context(context, ARGS, Settings.from_config(config()))


@pytest.mark.parametrize(
    "first,second",
    [
        ("/workspace/demo", "/workspace/demo-other"),
        (r"C:\Projects\Demo", "c:/projects/demo-other"),
        ("//server/share", "//server/share-other"),
    ],
)
def test_distinct_path_components_stay_independent(first, second):
    context = ctx()
    mappings = [
        {"logical_root": first, "execution_root": "/host/first"},
        {"logical_root": second, "execution_root": "/host/second"},
    ]
    context.execution_context["data"]["runtime_context"]["runtime"]["workspace_mappings"] = mappings
    accepted = runtime_context(context, ARGS, Settings.from_config(config()))
    assert accepted["runtime"]["workspace_mappings"] == mappings


def test_context_budget_and_default_distrust():
    settings = Settings.from_config(base_config())
    with pytest.raises(RuntimeContextError, match="untrusted"):
        runtime_context(ctx(), ARGS, settings)
    context = ctx()
    context.execution_context["data"]["runtime_context"]["arguments"]["huge"] = "x" * 16384
    with pytest.raises(RuntimeContextError, match="invalid"):
        runtime_context(context, ARGS, Settings.from_config(config()))
    assert runtime_context(ctx({"data": {}}), ARGS, settings) is None


def test_argument_binding_keeps_integer_and_float_wire_values_distinct():
    context = ctx()
    context.execution_context["data"]["runtime_context"]["arguments"]["number"] = 1.0
    with pytest.raises(RuntimeContextError, match="mismatch"):
        runtime_context(context, {**ARGS, "number": 1}, Settings.from_config(config()))


def test_configuration_requires_guarded_local_tools():
    cfg = config()
    cfg["runtime_context"]["tools"] = ["unguarded"]
    assert any(p.field == "runtime_context.tools" for p in diagnostics(cfg))


async def test_runtime_facts_follow_normal_council_flow_and_do_not_reach_business_callback(setup):
    plugin, council, executor, _ = setup(config=config())
    context = call(name=TOOL, turn=turn(plugin), **ARGS)
    context.execution_context = assertion()
    nxt = Next(result="file")
    result = await plugin.h3(context, nxt)
    assert result.result["status"] == "executed"
    assert nxt.calls == [ARGS] and not executor.calls
    assert len(council.calls) == 1 and not council.policy_calls
    sent = council.calls[0]["tool_call"]["runtime_context"]
    assert sent == {"version": 1, "source": "example.application", "runtime": RUNTIME}
    assert "runtime_context" not in council.calls[0]["tool_call"]["arguments"]


async def test_runtime_facts_without_native_origin_do_not_bypass_model_review(setup):
    plugin, council, _, _ = setup(config=config())
    context = ctx()
    context.args["nomos"] = {
        "description": "Read file",
        "justification": "Requested",
        "target_resource": "note",
        "blast_radius": "one file",
    }
    nxt = Next()
    result = await plugin.h3(context, nxt)
    assert result.result["status"] == "native_context_unavailable"
    assert not nxt.calls and not council.calls and not council.policy_calls


@pytest.mark.parametrize("decision", ["denied", "requires_human_approval"])
async def test_runtime_context_does_not_override_policy_outcomes(setup, decision):
    plugin, council, _, _ = setup(config=config())

    async def refuse(**fields):
        council.calls.append(fields)
        return {"status": decision, "decision_id": "policy-test", "error": "Policy stops this call"}

    council.authorize = refuse
    context = call(name=TOOL, turn=turn(plugin), **ARGS)
    context.execution_context = assertion()
    nxt = Next()
    result = await plugin.h3(context, nxt)
    assert result.result["status"] == decision
    assert not nxt.calls
