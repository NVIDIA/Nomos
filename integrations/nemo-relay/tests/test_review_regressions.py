# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the structured authorization review findings."""

import base64
import json
from dataclasses import replace

import pytest
from support import COUNCIL_KEY, Next, base_config, call, turn

from nemo_relay_nomos.settings import diagnostics
from nemo_relay_nomos.tool_call import canonical_tool_arguments
from nemo_relay_nomos.wire import canonical


@pytest.mark.parametrize("include", [["read_file"], ["*"], ["read_*"], ["read_[ab]"]])
def test_implicit_local_policy_requires_subject(include):
    config = base_config(guard={"include": include})
    config.pop("tool_call")
    assert [d.field for d in diagnostics(config)] == ["tool_call.subject"]


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # the default guarded run_shell has an explicit remote policy
        {"guard": {"include": ["*"]}, "default_executor": "nomos_http"},
        {"guard": {"include": ["read_file"], "exclude": ["read_file"]}},
        {"guard": {"include": ["*"], "exclude": ["*"]}},
    ],
)
def test_remote_or_excluded_tools_do_not_require_subject(overrides):
    config = base_config(**overrides)
    config.pop("tool_call")
    assert diagnostics(config) == []


@pytest.mark.parametrize("subject", ["user\nother", "user\x00", "user\x7f", "user\u2028", "user\u2029"])
def test_subject_rejects_control_and_line_separator_characters(subject):
    assert any(
        d.field == "tool_call.subject" for d in diagnostics(base_config(tool_call={"subject": subject}))
    )


async def test_local_context_is_from_the_calls_own_turn(setup):
    plugin, council, _, _ = setup(guard={"include": ["read_file"]})
    alice = turn(plugin, user="Alice: read the hosts file")
    turn(plugin, user="Bob: delete the hosts file")
    result = await plugin.h3(call(name="read_file", turn=alice, path="/etc/hosts"), Next())
    assert result.result["status"] == "executed"
    assert council.calls[0]["context"] == "tool=read_file; latest user request: Alice: read the hosts file"


@pytest.mark.parametrize("name", ["read_file", "run_shell"])
async def test_context_marks_truncated_user_request(setup, name):
    plugin, council, _, _ = setup(guard={"include": ["read_file", "run_shell"]})
    token = turn(plugin, user="U" * 801)
    arguments = {"path": "/etc/hosts"} if name == "read_file" else {"command": "echo hello"}
    result = await plugin.h3(call(name=name, turn=token, **arguments), Next())
    assert result.result["status"] == "executed"
    assert council.calls[0]["context"] == (
        f"tool={name}; latest user request: " + "U" * 800 + " [user request truncated to 800 characters]"
    )


@pytest.mark.parametrize("changed", ["subject", "issued_at", "expires_at"])
async def test_signed_mismatch_is_a_stored_refusal_before_dispatch(setup, changed):
    plugin, council, executor, _ = setup(guard={"include": ["read_file"]})
    authorize = council.authorize

    async def altered_writ(**fields):
        decision = await authorize(**fields)
        envelope = json.loads(base64.b64decode(decision["writ"]))
        payload = json.loads(base64.b64decode(envelope["payload_base64"]))
        if changed == "subject":
            payload["operation"]["subject"] = "another-subject"
        else:
            payload[changed] = 10**400
        raw = canonical(payload)
        envelope["payload_base64"] = base64.b64encode(raw).decode()
        envelope["signature"] = COUNCIL_KEY.sign(raw).hex()
        decision["writ"] = base64.b64encode(canonical(envelope)).decode()
        return decision

    council.authorize = altered_writ
    token = turn(plugin)
    ctx = call(name="read_file", turn=token, path="/etc/hosts")
    next_ = Next()
    result = (await plugin.h3(ctx, next_)).result
    expected = "writ_binding_mismatch" if changed == "subject" else "invalid_writ_time"
    assert result["status"] == expected
    assert plugin.store.get(result["evidence_id"])["kind"] == "plugin_refusal"
    assert (await plugin.h3(ctx, next_)).result == result
    assert not next_.calls and not executor.calls
    assert len(council.calls) == 1


@pytest.mark.parametrize("sign", [1, -1])
def test_integer_digit_boundary(sign):
    arguments = {"nested": [{"value": sign * (10**640 - 1)}]}
    assert json.loads(canonical_tool_arguments(arguments)) == arguments
    with pytest.raises(ValueError, match="tool_integer_too_large"):
        canonical_tool_arguments({"nested": [{"value": sign * 10**640}]})
    with pytest.raises(ValueError, match="tool_integer_too_large"):
        canonical_tool_arguments({"value": sign * 10**4300})


def test_integer_scan_does_not_loop_on_cycles():
    arguments = {"nested": []}
    arguments["nested"].append(arguments)
    with pytest.raises(ValueError, match="Circular reference"):
        canonical_tool_arguments(arguments)


async def test_oversized_nested_integer_is_refused_before_authorization(setup):
    plugin, council, executor, _ = setup(guard={"include": ["read_file"]})
    token = turn(plugin)
    next_ = Next()
    result = (
        await plugin.h3(call(name="read_file", turn=token, metadata=[{"value": 10**640}]), next_)
    ).result
    assert result["status"] == "tool_integer_too_large"
    assert not council.calls and not executor.calls and not next_.calls


async def test_unencodable_arguments_are_refused_with_a_stable_status(setup):
    plugin, council, executor, _ = setup(guard={"include": ["read_file"]})
    token = turn(plugin)
    next_ = Next()
    result = (await plugin.h3(call(name="read_file", turn=token, path="\ud800"), next_)).result
    assert result["status"] == "invalid_arguments"
    assert "codec" not in result["message"] and "32,000" not in result["message"]
    assert not council.calls and not executor.calls and not next_.calls


async def test_a_large_council_denial_fits_the_space_reserved_for_it(setup):
    """The journal keeps the Council's response; its size must be within what a call reserves.

    The store holds two reservations: one for the denial's outcome, one for the next call."""
    limits = {"max_result_bytes": 1024, "max_output_chars": 256, "max_storage_bytes": 200_000}
    cfg = base_config(limits={**limits, "conversation_share_percent": 100})
    del cfg["signing_key"]
    plugin, council, *_ = setup(config=cfg)
    approve = council.authorize

    async def verbose_denial(**fields):
        council.calls.append(fields)
        return {"status": "denied", "decision_id": "intent-1", "guidance": ["é" * 8192] * 50}

    council.authorize = verbose_denial
    token = turn(plugin)
    denied = (await plugin.h3(call(turn=token, call_id="denied"), Next())).result
    assert denied["status"] == "denied"
    (journal,) = plugin.store.db.execute("SELECT length(outcome) FROM calls WHERE id='denied'").fetchone()
    assert journal <= plugin.settings.limits.reserve_bytes
    council.authorize = approve
    after = (await plugin.h3(call(turn=token, call_id="after"), Next())).result
    assert after["status"] != "storage_quota_exceeded", after


async def test_invalid_changed_held_arguments_are_refused(setup, monkeypatch):
    plugin, _, executor, _ = setup(guard={"include": ["read_file"]})
    handle = plugin.handler.handle

    async def change_held_arguments(held, encoded, writ):
        return await handle(replace(held, arguments={"value": 10**640}), encoded, writ)

    monkeypatch.setattr(plugin.handler, "handle", change_held_arguments)
    token = turn(plugin)
    next_ = Next()
    result = (await plugin.h3(call(name="read_file", turn=token, path="/etc/hosts"), next_)).result
    assert result["status"] == "tool_arguments_changed"
    assert not next_.calls and not executor.calls


async def test_changed_held_arguments_are_refused_before_callback(setup, monkeypatch):
    plugin, _, executor, _ = setup(guard={"include": ["read_file"]})
    handle = plugin.handler.handle

    async def change_held_arguments(held, encoded, writ):
        return await handle(replace(held, arguments={"path": "/etc/shadow"}), encoded, writ)

    monkeypatch.setattr(plugin.handler, "handle", change_held_arguments)
    token = turn(plugin)
    next_ = Next()
    result = (await plugin.h3(call(name="read_file", turn=token, path="/etc/hosts"), next_)).result
    assert result["status"] == "tool_arguments_changed"
    assert not next_.calls and not executor.calls
