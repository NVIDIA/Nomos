# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Implementation regressions for host-observed, turn-bound user requests."""

import json
from copy import deepcopy

import pytest
from support import Next, call, turn


def observation(council):
    items = council.calls[-1]["certified_evidence"]
    found = [
        item for item in items if json.loads(item["stdout"]).get("type") == "nomos.observed_user_request"
    ]
    assert len(found) == 1
    return found[0], json.loads(found[0]["stdout"])


async def test_request_is_automatically_certified_from_immutable_turn(setup):
    plugin, council, _, _ = setup()
    alice = turn(plugin, user="Alice's request")
    turn(plugin, user="Bob's unrelated request")
    await plugin.h3(call(turn=alice), Next())
    item, observed = observation(council)
    assert observed["text"] == "Alice's request"
    assert observed["turn_token"] == alice
    assert observed["conversation_id"] == plugin.store.turn(alice).conversation
    assert observed["status"] == "complete" and observed["omissions"] == []
    assert item["evidence"]["payload"]["request_id"] == "call1"
    assert "human identity" in observed["scope"]
    assert item["evidence"]["payload"]["command"]["command"].endswith("resource authority.")
    assert council.calls[-1]["context"] == "tool=run_shell; latest user request: Alice's request"


@pytest.mark.parametrize(
    "content,reason,status",
    [
        ("A" * 801, "capture_limit", "partial"),
        ("😀" * 800, "serialization_limit", "partial"),
        ([], "no_textual_user_request", "missing"),
        (
            [{"type": "text", "text": "inspect"}, {"type": "image", "url": "example"}],
            "non_text_content",
            "partial",
        ),
        ("bad\ud800", "invalid_unicode", "missing"),
        ({"unexpected": True}, "non_text_content", "missing"),
    ],
)
async def test_request_completeness_is_explicit_and_serialized_whole(setup, content, reason, status):
    plugin, council, _, _ = setup()
    outcome = plugin.h1(
        "model", {"content": {"tools": [], "messages": [{"role": "user", "content": content}]}}, None
    )
    import re

    token = re.search(r"nt_[0-9a-f]{32}", outcome.request["content"]["messages"][-1]["content"]).group()
    await plugin.h3(call(turn=token), Next())
    item, observed = observation(council)
    assert observed["status"] == status
    assert reason in observed["omissions"]
    assert len(item["stdout"]) <= 4000
    assert observed["included_text_chars"] == len(observed["text"])
    assert len(observed["text"]) <= 800


async def test_superseded_request_cannot_be_certified_with_tool_definitions_off(setup):
    plugin, council, _, _ = setup()
    first = turn(plugin, user="old")
    turn(plugin, {"role": "assistant", "content": first}, {"role": "user", "content": "new"})
    result = await plugin.h3(call(turn=first), Next())
    assert result.result["status"] == "unknown_turn"
    assert council.calls == []


async def test_user_request_tampering_fails_existing_signed_evidence_verifier(setup):
    from nemo_relay_nomos.verify import EvidenceError, verify_plugin_certified

    plugin, council, _, _ = setup()
    await plugin.h3(call(turn=turn(plugin)), Next())
    item, _ = observation(council)
    binding = dict(executor_id=plugin.signer.executor_id, agent_id="agent", council_id="council", writ_id="")
    verify_plugin_certified(item, plugin.signer.public_key_hex, **binding)
    changed = deepcopy(item)
    changed["stdout"] += " "
    with pytest.raises(EvidenceError):
        verify_plugin_certified(changed, plugin.signer.public_key_hex, **binding)


def test_turn_omissions_round_trip(setup):
    plugin, _, _, _ = setup()
    opened = plugin.store.open_turn([], "snapshot", user_request_omissions=("non_text_content",))
    assert opened.user_request_omissions == ("non_text_content",)
    assert plugin.store.turn(opened.token) == opened


@pytest.mark.parametrize("content", [[], None, "", {"unsupported": "latest"}])
async def test_empty_latest_message_does_not_fall_back_to_older_text(setup, content):
    plugin, council, _, _ = setup(catalog={"enabled": False})
    token = turn(plugin, {"role": "user", "content": "obsolete"}, {"role": "user", "content": content})
    await plugin.h3(call(turn=token), Next())
    _, observed = observation(council)
    assert observed["status"] == "missing" and observed["text"] == ""


async def test_invalid_unicode_beyond_capture_limit_discards_the_whole_snapshot(setup):
    plugin, council, _, _ = setup()
    token = turn(plugin, user="A" * 800 + "\ud800")
    await plugin.h3(call(turn=token), Next())
    _, observed = observation(council)
    assert observed["status"] == "missing"
    assert observed["omissions"] == ["no_textual_user_request", "invalid_unicode"]
    assert observed["captured_text_chars"] == 0
