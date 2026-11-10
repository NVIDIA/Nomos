# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native identity boundaries and malformed/partial stream proposals fail closed."""

import uuid
from types import SimpleNamespace

import pytest
from support import base_config

from nemo_relay_nomos.native_context import ChatStreamCalls, NativeContextError, agent_identity, tool_calls
from nemo_relay_nomos.settings import Settings, SettingsError


def entry(value=None, kind="agent"):
    """Create a typed native scope entry using a canonical UUID."""
    return SimpleNamespace(scope_id=value or str(uuid.uuid4()), scope_type=kind)


def snapshot(*entries):
    """Build an ordered native scope stack for Agent-provenance validation."""
    return SimpleNamespace(scopes=list(entries))


@pytest.mark.parametrize(
    "value",
    [
        None,
        snapshot(),
        snapshot(entry()),
        snapshot(entry(), entry(kind="function")),
        snapshot(entry("bad"), entry()),
        snapshot(*[entry()] * 2),
        snapshot(*[entry() for _ in range(129)]),
    ],
)
def test_no_invented_agent_for_missing_invalid_or_root_only_snapshot(value):
    """Require genuine application Agent ancestry instead of manufacturing an owner."""
    with pytest.raises(NativeContextError):
        agent_identity(value)


def test_nearest_application_agent_is_selected_without_using_model_text():
    """Select the nearest application Agent solely from native scope information."""
    root, parent, child = entry(), entry(), entry()
    assert agent_identity(snapshot(root, parent, child, entry(kind="function"))) == child.scope_id


def test_single_agent_root_requires_explicit_host_assertion():
    """Only the operator opt-in accepts the reconstructed singleton shape."""
    owner = entry()
    with pytest.raises(NativeContextError):
        agent_identity(snapshot(owner))
    assert agent_identity(snapshot(owner), allow_single_agent_root=True) == owner.scope_id


@pytest.mark.parametrize(
    "value",
    [
        None,
        snapshot(),
        snapshot(entry(kind="function")),
        snapshot(entry("bad")),
        snapshot(entry(), entry(kind="function")),
        snapshot(*[entry()] * 2),
        snapshot(*[entry() for _ in range(129)]),
    ],
)
def test_single_agent_opt_in_preserves_other_identity_boundaries(value):
    """The host assertion cannot excuse malformed identity or root descendants."""
    with pytest.raises(NativeContextError):
        agent_identity(value, allow_single_agent_root=True)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"tool_calls": {}},
        {"tool_calls": [None]},
        {"tool_calls": [{"id": None, "name": "read"}]},
        {"tool_calls": [{"id": "x" * 257, "name": "read"}]},
        {"tool_calls": [{"id": "call", "name": ""}]},
        {"tool_calls": [{"id": "call", "name": "x" * 129}]},
        {"tool_calls": [{"id": "same", "name": "a"}, {"id": "same", "name": "b"}]},
    ],
)
def test_unary_call_identities_must_be_bounded_complete_and_unambiguous(value):
    """Reject incomplete, oversized or duplicate provider proposal identities."""
    with pytest.raises(NativeContextError):
        tool_calls(value, 5)


def test_unary_no_calls_and_call_count_limit():
    """Allow text-only responses while bounding the number of returned proposal identities."""
    assert tool_calls({}, 1) == tool_calls({"tool_calls": None}, 1) == []
    with pytest.raises(NativeContextError):
        tool_calls({"tool_calls": [{"id": "a", "name": "read"}, {"id": "b", "name": "read"}]}, 1)


def chunk(delta, finish=None, index=0):
    """Wrap supplied choices in the supported chat-stream wire shape."""
    return {"choices": [{"index": index, "delta": delta, "finish_reason": finish}]}


def call_delta(call_id="call", name="read", index=0):
    """Create a tool-call delta with independently controllable identity fragments."""
    return {"tool_calls": [{"index": index, "id": call_id, "function": {"name": name}}]}


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"choices": {}},
        {"choices": [None]},
        chunk({}, index=True),
        chunk({}, index=-1),
        chunk(False),
        chunk({"tool_calls": {}}),
        chunk({"tool_calls": [None]}),
        chunk({"tool_calls": [{"index": -1}]}),
        chunk(call_delta(call_id=None)),
        chunk(call_delta(call_id="x" * 257)),
        chunk({"tool_calls": [{"index": 0, "function": False}]}),
        chunk(call_delta(name=1)),
        chunk(call_delta(name="x" * 129)),
    ],
)
def test_malformed_stream_deltas_are_not_usable_bindings(value):
    """Reject malformed chat chunks before their contents can become origin bindings."""
    with pytest.raises(NativeContextError):
        ChatStreamCalls(2).observe(value)


def test_fragmented_names_repeated_ids_and_usage_only_chunks():
    """Assemble valid identity fragments without retaining text or usage-only chunks."""
    collector = ChatStreamCalls(2)
    collector.observe({"usage": {"output_tokens": 10}})
    collector.observe(chunk(call_delta(name="read_")))
    collector.observe(
        chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "call", "function": {"name": "file", "arguments": "not retained"}}
                ]
            },
            "tool_calls",
        )
    )
    assert collector.complete() == [("call", "read_file")]
    assert "not retained" not in repr(collector.calls)


def test_stream_id_change_and_post_finish_delta_are_rejected():
    """Refuse changing IDs and tool fragments arriving after terminal completion."""
    collector = ChatStreamCalls(2)
    collector.observe(chunk(call_delta()))
    with pytest.raises(NativeContextError, match="ID changed"):
        collector.observe(chunk(call_delta(call_id="different")))
    collector.observe(chunk({}, "tool_calls"))
    with pytest.raises(NativeContextError, match="continued a finished"):
        collector.observe(chunk(call_delta(index=1)))


def test_stream_identity_and_finished_choice_counts_are_bounded():
    """Bound both identity fragments and remembered terminal choices in a stream."""
    collector = ChatStreamCalls(1)
    collector.observe(chunk(call_delta()))
    with pytest.raises(NativeContextError, match="identity limit"):
        collector.observe(chunk(call_delta(call_id="other", index=1)))
    collector.observe(chunk({}, "tool_calls"))
    with pytest.raises(NativeContextError, match="completed-choice limit"):
        collector.observe(chunk({}, "tool_calls", index=1))


def test_stream_complete_requires_identity_and_unique_ids():
    """Require complete, unique identities before publishing a finished stream."""
    collector = ChatStreamCalls(2)
    collector.observe(chunk({"tool_calls": [{"index": 0, "function": {"name": "read"}}]}, "tool_calls"))
    with pytest.raises(NativeContextError, match="tool-call ID"):
        collector.complete()
    collector = ChatStreamCalls(2)
    collector.observe(
        chunk(
            {"tool_calls": [call_delta()["tool_calls"][0], call_delta(index=1)["tool_calls"][0]]},
            "tool_calls",
        )
    )
    with pytest.raises(NativeContextError, match="ambiguous"):
        collector.complete()


def test_single_agent_root_setting_defaults_to_strict():
    """Existing installations do not implicitly trust reconstructed singleton roots."""
    assert Settings.from_config(base_config()).allow_single_agent_root is False
    for enabled in (False, True):
        cfg = base_config(correlation={"allow_single_agent_root": enabled})
        assert Settings.from_config(cfg).allow_single_agent_root is enabled


@pytest.mark.parametrize("value", [None, [], {}, 2])
def test_single_agent_root_setting_rejects_invalid_values(value):
    """Schema validation refuses invalid host assertions at registration time."""
    with pytest.raises(SettingsError):
        Settings.from_config(base_config(correlation={"allow_single_agent_root": value}))
