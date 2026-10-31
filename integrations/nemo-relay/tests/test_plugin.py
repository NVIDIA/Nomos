# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Implementation tests: H1 and H3 flows through a registered plugin with fake Nomos services."""

import asyncio
import json
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from support import (
    COUNCIL_KEY,
    EXECUTOR_KEY,
    INTENT,
    LOCAL_TOOL,
    TOOL,
    TURN,
    Context,
    Council,
    Executor,
    Next,
    base_config,
    call,
    cite,
    council_key_reply,
    history,
    nomos_message,
    public,
    turn,
)

from nemo_relay_nomos import NomosPlugin
from nemo_relay_nomos.llm_request import CATALOG_HEADER, TURN_LINE
from nemo_relay_nomos.records import integrity_ok
from nemo_relay_nomos.settings import diagnostics
from nemo_relay_nomos.tool_call import canonical_tool_arguments
from nemo_relay_nomos.wire import canonical, sha256_hex


def raw_request(*tools, user="list files"):
    return {"content": {"messages": [{"role": "user", "content": user}], "tools": list(tools)}}


# ── registration ─────────────────────────────────────────────────────────


def test_register_installs_both_hooks_at_their_priorities(setup):
    _, _, _, ctx = setup()
    assert ctx.hooks["inject-intent"][1] == {"priority": 10}
    assert ctx.hooks["authorize-execution"][1] == {"priority": 90}


def test_exported_annotations_keep_only_status_fields(setup):
    _, _, _, ctx = setup()
    sanitize = ctx.hooks["export-annotation"][0]
    full = {
        "status": "executed",
        "evidence_id": "ev_1",
        "conversation": "cv_secret",
        "submitted": {"command": "/bin/echo hi", "certified_citations": []},
        "council": {
            "status": "approved",
            "decision_id": "d1",
            "votes": [{"member_id": "m", "decision": "approve"}],
        },
        "timings_ms": {"total": 1.0},
    }
    fields = {
        "data": {"x": 1},
        "category_profile": {"tool_result_annotation": {"nomos": full, "tool": {"a": 1}}},
        "metadata": {"m": 1},
    }
    out = sanitize(None, fields)
    assert out["category_profile"]["tool_result_annotation"] == {
        "nomos": {
            "status": "executed",
            "evidence_id": "ev_1",
            "council": {"status": "approved", "decision_id": "d1"},
            "timings_ms": {"total": 1.0},
        },
        "tool": {"a": 1},
    }
    assert (out["data"], out["metadata"]) == ({"x": 1}, {"m": 1})
    assert (
        fields["category_profile"]["tool_result_annotation"]["nomos"] is full and "votes" in full["council"]
    ), "pure"
    other = {"data": None, "category_profile": {"kind": "llm"}, "metadata": None}
    assert sanitize(None, other) == other


def test_authorization_runs_before_relays_caches_and_guardrails(setup):
    # A cache hit returns before every later execution intercept (Relay's response-cache docs): the Council would be
    # skipped. Relay's caches default to 150 (100 in 0.10), NeMo Guardrails to 100; lower runs first.
    _, _, _, ctx = setup()
    assert ctx.hooks["authorize-execution"][1]["priority"] < 100


@pytest.mark.parametrize(
    "patch",
    [
        {"max_denials": 0},
        {"guard": {"include": "run_shell"}},
        {"unknown": 1},
        {"writ_handler": {"kind": "custom"}},
        {"writ_handler": {"claim": "council"}},
        {"tools": {"run_shell": {"executor": "nomos_http"}}},
    ],
)
def test_invalid_config_is_diagnosed_and_refused(tmp_path, patch):
    cfg = {**base_config(), **patch}
    plugin = NomosPlugin(council=Council(), executor=Executor())
    assert plugin.validate(cfg), patch
    with pytest.raises(ValueError):
        plugin.register(Context(), cfg)


async def test_auth_token_from_file_reference_reaches_the_council(setup, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("file-token\n")
    secret.chmod(0o600)
    cfg = base_config()
    cfg["connection"]["auth_token"] = {"file": str(secret)}
    p, council, _, _ = setup(config=cfg)
    t = turn(p)
    await p.h3(call(turn=t), Next())
    assert council.calls[0]["auth_token"] == "file-token"


# ── H1 ───────────────────────────────────────────────────────────────────


def test_h1_injects_intent_only_into_guarded_tools_on_the_raw_path(setup):
    p, *_ = setup()
    out = p.h1("model", raw_request(TOOL, LOCAL_TOOL), None)
    tools = out.request["content"]["tools"]
    assert tools[0]["function"]["parameters"]["required"][-1] == "nomos"
    assert "nomos" not in tools[1]["function"]["parameters"]["properties"]
    assert "nomos" not in TOOL["function"]["parameters"]["properties"], "input mutated"


def test_h1_guard_star_with_exclude_and_reserved_prefix(setup):
    p, *_ = setup(guard={"include": ["*"], "exclude": ["read_*"]})
    reserved = {**TOOL, "function": {**TOOL["function"], "name": "nomos_internal"}}
    tools = p.h1("model", raw_request(TOOL, LOCAL_TOOL, reserved), None).request["content"]["tools"]
    assert ["nomos" in t["function"]["parameters"]["properties"] for t in tools] == [True, False, False]


async def test_h1_lists_evidence_with_tool_and_args_but_never_output(setup):
    p, *_ = setup()
    t = turn(p)
    ctx = call(turn=t, command="echo secret-output")
    executed = (await p.h3(ctx, Next())).result
    out = p.h1(
        "model", {"content": {"messages": history("list files", (ctx, executed)), "tools": [TOOL]}}, None
    )
    catalog = out.request["content"]["messages"][-1]
    assert catalog["role"] == "system" and CATALOG_HEADER in catalog["content"]
    assert executed["evidence_id"] in catalog["content"]
    assert '"command":"echo secret-output"' in catalog["content"]
    assert "hello\nsecond" not in catalog["content"]  # the stored stdout


@pytest.mark.parametrize(
    ("api", "where"),
    [("anthropic_messages", "instructions"), ("openai_responses", "developer"), ("openai_chat", "system")],
)
async def test_h1_places_catalog_per_provider_api_on_the_annotated_path(setup, api, where):
    p, *_ = setup()
    t = turn(p)
    await p.h3(call(turn=t), Next())
    request = raw_request(TOOL)
    annotated = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [TOOL],
        "instructions": "be brief",
        "api_specific": {"api": api},
    }
    out = p.h1("model", request, annotated)
    assert out.request == request, "raw request must stay unchanged when annotated is present"
    if where == "instructions":
        assert out.annotated_request["instructions"].startswith("be brief\n\nNomos turn: nt_")
        assert out.annotated_request["messages"] == annotated["messages"]
    else:
        assert out.annotated_request["messages"][-1]["role"] == where


def test_h1_without_evidence_sends_only_the_turn(setup):
    p, *_ = setup()
    out = p.h1("model", raw_request(TOOL), None)
    assert [m["role"] for m in out.request["content"]["messages"]] == ["user", "system"]
    token = TURN.search(nomos_message(out))[0]
    assert nomos_message(out) == TURN_LINE.format(turn=token)


def test_h1_issues_a_new_turn_for_every_request_with_a_fixed_schema(setup):
    p, *_ = setup()
    first, second = (p.h1("model", raw_request(TOOL), None) for _ in range(2))
    assert TURN.search(nomos_message(first))[0] != TURN.search(nomos_message(second))[0]
    assert first.request["content"]["tools"] == second.request["content"]["tools"], "tools stay cache-stable"


# ── H3: refusals before the council ──────────────────────────────────────


async def test_unguarded_tool_passes_through_with_its_own_annotation(setup):
    p, council, _, _ = setup()
    t = turn(p)
    nxt = Next()
    out = await p.h3(call(turn=t, name="read_file", intent=None, path="/x"), nxt)
    assert out.result == "local output" and out.annotation == {"original": True}
    assert nxt.calls == [{"path": "/x"}] and not council.calls


@pytest.mark.parametrize(
    "intent",
    [None, {}, {**INTENT, "description": " "}, {**INTENT, "extra": 1}, {**INTENT, "evidence": ["nope"]}],
)
async def test_incomplete_intent_is_refused_stored_and_never_sent(setup, intent):
    p, council, executor, _ = setup()
    t = turn(p)
    out = (await p.h3(call(turn=t, intent=intent), Next())).result
    assert out["status"] == "missing_intent" and "nomos" in out["message"]
    assert not council.calls and not executor.calls
    record = p.store.get(out["evidence_id"])
    assert record["kind"] == "plugin_refusal" and integrity_ok(record, p.signer.public_key_hex)


async def test_unknown_citation_is_refused_before_the_council(setup):
    p, council, _, _ = setup()
    t = turn(p)
    out = (await p.h3(cite(t, "ev_" + "0" * 32), Next())).result
    assert out["status"] == "evidence_not_found" and not council.calls


async def test_nomos_http_tool_requires_exactly_its_command_argument(setup):
    p, council, _, _ = setup()
    t = turn(p)
    out = (await p.h3(call(turn=t, command="ls", extra="x"), Next())).result
    assert out["status"] == "invalid_command_arguments" and not council.calls


# ── H3: council outcomes ─────────────────────────────────────────────────


async def test_denial_returns_the_full_council_response_and_stores_it(setup):
    p, council, executor, _ = setup("denied")
    t = turn(p)
    out = await p.h3(call(turn=t), Next())
    data = out.result
    assert data["status"] == "denied" and data["execution_status"] == "not_executed"
    assert data["council"]["council_members"][0]["reasoning"] == "too broad"
    assert data["council"]["chairman"]["risk_level"] == "high" and "writ" not in data["council"]
    assert "Reason: Denied by OPA policy" in data["message"] and "\\n" not in data["message"]
    assert out.annotation["nomos"]["council"]["votes"] == [
        {"member_id": "blast-containment", "decision": "deny"}
    ]
    assert out.annotation["nomos"]["submitted"]["command"] == "echo hello"
    record = p.store.get(data["evidence_id"])
    assert record["kind"] == "council_denial" and record["decision_id"] == "intent-1"
    assert not executor.calls


@pytest.mark.parametrize("status", ["error", "raise"])
async def test_council_failure_is_a_closed_gate(setup, status):
    p, _, executor, _ = setup(status)
    t = turn(p)
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "council_error" and not executor.calls
    assert p.store.get(data["evidence_id"])["kind"] == "council_error"


async def test_denial_cap_stops_asking_the_council(setup):
    p, council, _, _ = setup("denied", max_denials=2)
    t = turn(p)
    for i in range(3):
        last = (await p.h3(call(turn=t, call_id=f"c{i}"), Next())).result
    assert last["status"] == "denial_cap" and len(council.calls) == 2


# ── H3: approved paths ───────────────────────────────────────────────────


async def test_approved_command_runs_on_the_executor_only(setup):
    p, council, executor, _ = setup()
    t = turn(p)
    nxt = Next()
    out = await p.h3(call(turn=t), nxt)
    data = out.result
    assert data["status"] == "executed" and data["evidence_status"] == "locally_verified"
    assert data["stdout"] == "hello\nsecond\n" and data["signer"]["type"] == "executor"
    assert not nxt.calls and len(executor.calls) == 1
    assert council.calls[0]["command"] == "echo hello" and council.calls[0]["certified_evidence"] == []
    assert set(out.annotation["nomos"]["timings_ms"]) == {"council", "execute", "total"}
    record = p.store.get(data["evidence_id"])
    assert record["certified"] and record["content"] == executor.results[0]
    assert integrity_ok(record, p.signer.public_key_hex)


async def test_approved_local_tool_runs_next_without_nomos_and_is_plugin_certified(setup):
    p, council, executor, _ = setup(guard={"include": ["read_file"]})
    t = turn(p)
    nxt = Next(result={"text": "file body"})
    out = await p.h3(call(turn=t, name="read_file", path="/etc/hosts"), nxt)
    data = out.result
    assert nxt.calls == [{"path": "/etc/hosts"}] and not executor.calls
    assert data["status"] == "executed" and data["output"] == {"text": "file body"}
    assert data["evidence_status"] == "plugin_certified" and out.annotation["tool"] == {"original": True}
    assert council.calls[0]["tool_call"] == {
        "schema_version": 1,
        "tool_name": "read_file",
        "arguments": {"path": "/etc/hosts"},
        "subject": "opaque-test-user",
    }
    record = p.store.get(data["evidence_id"])
    assert record["kind"] == "local_result" and record["certified"]


async def test_structured_local_tool_binds_exact_args_and_runs_callback_once(setup):
    cfg = base_config(
        guard={"include": ["read_file"]},
        tools={"read_file": {"executor": "local"}},
        tool_call={"subject": "opaque-user-7d9a"},
    )
    p, council, executor, _ = setup(config=cfg)
    t = turn(p, user="read the hosts file")
    nxt = Next(result={"text": "file body"})

    out = await p.h3(call(turn=t, name="read_file", path="/etc/hosts"), nxt)

    assert out.result["status"] == "executed"
    assert nxt.calls == [{"path": "/etc/hosts"}] and not executor.calls
    submitted = council.calls[0]
    assert not ({"command", "working_directory"} & submitted.keys())
    assert submitted["api_version"] == "v2"
    assert submitted["context"] == "tool=read_file; latest user request: read the hosts file"
    assert submitted["tool_call"] == {
        "schema_version": 1,
        "tool_name": "read_file",
        "arguments": {"path": "/etc/hosts"},
        "subject": "opaque-user-7d9a",
    }


def test_nomos_json_v1_cross_repository_vector():
    arguments = {"metadata": {"z": [True, None, 1.0], "a": 1}, "channel": "café"}
    encoded = canonical_tool_arguments(arguments)

    assert (
        encoded == b'{"channel":"caf\xc3\xa9","metadata":{"a":1,"z":[true,null,1.0]}}'  # codespell:ignore caf
    )
    assert sha256_hex(encoded) == "1bcab5a883853a1708ba45d593afd3a70676d538e09419967e36da76b19a0e1d"


async def test_structured_tool_denial_never_calls_local_callback(setup):
    cfg = base_config(
        guard={"include": ["read_file"]},
        tools={"read_file": {"executor": "local"}},
        tool_call={"subject": "opaque-user"},
    )
    p, _, executor, _ = setup("denied", config=cfg)
    t = turn(p)
    nxt = Next()
    out = await p.h3(call(turn=t, name="read_file", path="/etc/hosts"), nxt)
    assert out.result["status"] == "denied"
    assert not nxt.calls and not executor.calls


async def test_structured_tool_rejects_arguments_over_nomos_limit_before_council(setup):
    cfg = base_config(
        guard={"include": ["read_file"]},
        tools={"read_file": {"executor": "local"}},
        tool_call={"subject": "opaque-user"},
    )
    p, council, _, _ = setup(config=cfg)
    t = turn(p)
    nxt = Next()
    out = await p.h3(call(turn=t, name="read_file", path="x" * 32_001), nxt)
    assert out.result["status"] == "tool_arguments_too_large"
    assert not council.calls and not nxt.calls


async def test_local_tool_exception_is_recorded_as_failed(setup):
    p, *_ = setup(guard={"include": ["read_file"]})
    t = turn(p)
    data = (await p.h3(call(turn=t, name="read_file", path="/x"), Next(error=OSError("boom")))).result
    assert data["execution_status"] == "failed" and "boom" in data["error"]


async def test_local_writ_cannot_be_replayed(setup):
    p, council, _, _ = setup(guard={"include": ["read_file"]})
    t = turn(p)
    fixed = {}

    async def same_writ(**fields):
        if not fixed:
            fixed.update(await Council().authorize(**fields))
        return dict(fixed)

    council.authorize = same_writ
    first = (await p.h3(call(turn=t, name="read_file", call_id="a", path="/x"), Next())).result
    second = (await p.h3(call(turn=t, name="read_file", call_id="b", path="/x"), Next())).result
    assert first["status"] == "executed" and second["status"] == "writ_replayed"


async def test_structured_local_writ_cannot_be_replayed(setup):
    cfg = base_config(
        guard={"include": ["read_file"]},
        tools={"read_file": {"executor": "local"}},
        tool_call={"subject": "opaque-user"},
    )
    p, council, _, _ = setup(config=cfg)
    t = turn(p)
    fixed = {}

    async def same_writ(**fields):
        if not fixed:
            fixed.update(await Council().authorize(**fields))
        return dict(fixed)

    council.authorize = same_writ
    first_next, second_next = Next(), Next()
    first = (await p.h3(call(turn=t, name="read_file", call_id="v2-a", path="/x"), first_next)).result
    second = (await p.h3(call(turn=t, name="read_file", call_id="v2-b", path="/x"), second_next)).result
    assert first["status"] == "executed" and second["status"] == "writ_replayed"
    assert first_next.calls == [{"path": "/x"}] and second_next.calls == []


async def test_structured_tool_dispatches_authorized_snapshot_if_source_args_mutate(setup):
    cfg = base_config(
        guard={"include": ["read_file"]},
        tools={"read_file": {"executor": "local"}},
        tool_call={"subject": "opaque-user"},
    )
    p, council, _, _ = setup(config=cfg)
    t = turn(p)
    ctx = call(turn=t, name="read_file", options={"encoding": "utf-8"})
    original = council.authorize

    async def mutate_after_authorization(**fields):
        decision = await original(**fields)
        ctx.args["options"]["encoding"] = "latin-1"
        return decision

    council.authorize = mutate_after_authorization
    nxt = Next()
    out = (await p.h3(ctx, nxt)).result
    assert out["status"] == "executed"
    assert nxt.calls == [{"options": {"encoding": "utf-8"}}]


async def test_certified_citation_sends_the_full_original(setup):
    p, council, executor, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    await p.h3(cite(t, first["evidence_id"], call_id="call2"), Next())
    assert council.calls[1]["certified_evidence"] == [executor.results[0]]
    assert council.calls[1]["evidence"] == []
    second = await p.h3(cite(t, first["evidence_id"], call_id="call3"), Next())
    assert second.annotation["nomos"]["submitted"]["certified_citations"] == [first["evidence_id"]]
    assert p.store.get(first["evidence_id"])["council_citation_status"] == "accepted"


async def test_a_cited_denial_is_sent_as_certified_evidence_not_asserted(setup):
    p, council, _, _ = setup("denied")
    t = turn(p)
    denied = (await p.h3(call(turn=t), Next())).result
    council.status = "approved"
    await p.h3(cite(t, denied["evidence_id"], call_id="call2"), Next())
    assert council.calls[1]["evidence"] == []
    assert council.calls[1]["certified_evidence"] == [p.store.get(denied["evidence_id"])["certified_result"]]


async def test_accepted_citation_is_not_downgraded_by_a_later_failure(setup):
    p, council, _, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    await p.h3(cite(t, first["evidence_id"], call_id="c2"), Next())
    council.status = "raise"
    await p.h3(cite(t, first["evidence_id"], call_id="c3"), Next())
    assert p.store.get(first["evidence_id"])["council_citation_status"] == "accepted"


# ── writ validation, quarantine, journal ─────────────────────────────────


async def test_writ_for_another_command_is_refused_and_never_dispatched(setup):
    p, council, executor, _ = setup()
    t = turn(p)
    original = council.authorize

    async def swapped(**fields):
        return await original(**{**fields, "command": "rm -rf /"})

    council.authorize = swapped
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "writ_binding_mismatch" and not executor.calls


async def test_tampered_result_is_quarantined_and_blocks_the_session(setup):
    p, _, executor, _ = setup()
    t = turn(p)
    original = executor.execute

    async def tampered(writ, agent_id):
        result = await original(writ, agent_id)
        return {**result, "stdout": "forged"}

    executor.execute = tampered
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "evidence_invalid" and data["execution_status"] == "unknown"
    assert "stdout" not in data and "forged" not in json.dumps(data)
    assert p.store.get(data["quarantine_id"])["evidence_status"] == "invalid"
    assert p.store.catalog([data["quarantine_id"]], p.store.turn(t).citable, 20) == []
    nxt = (await p.h3(call(turn=t, call_id="next"), Next())).result
    assert nxt["status"] == "session_requires_reconciliation"


async def test_executor_failure_is_unknown_and_blocks_further_calls(setup):
    p, _, executor, _ = setup()
    t = turn(p)

    async def down(writ, agent_id):
        raise ConnectionError("lost")

    executor.execute = down
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "execution_unknown" and data["execution_status"] == "unknown"
    assert p.store.get(data["evidence_id"])["kind"] == "execution_unknown"
    assert (await p.h3(call(turn=t, call_id="c2"), Next())).result[
        "status"
    ] == "session_requires_reconciliation"


async def test_repeated_call_id_replays_and_conflicting_args_are_refused(setup):
    p, council, executor, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    again = (await p.h3(call(turn=t), Next())).result
    assert again == first and len(executor.calls) == 1 and len(council.calls) == 1
    conflict = (await p.h3(call(turn=t, command="echo other"), Next())).result
    assert conflict["status"] == "tool_call_id_conflict"


async def test_concurrent_guarded_call_is_refused_as_busy(setup):
    p, council, _, _ = setup()
    t = turn(p)
    gate = asyncio.Event()
    original = council.authorize

    async def slow(**fields):
        await gate.wait()
        return await original(**fields)

    council.authorize = slow
    first = asyncio.create_task(p.h3(call(turn=t), Next()))
    await asyncio.sleep(0)
    busy = (await p.h3(call(turn=t, call_id="c2"), Next())).result
    gate.set()
    assert busy["status"] == "session_busy" and (await first).result["status"] == "executed"


# ── conversations sharing one worker ─────────────────────────────────────
# Relay gives the worker no caller identity: one activation serves every conversation in the host process.


def request(messages):
    return {"content": {"messages": messages, "tools": [TOOL]}}


def test_relay_gives_the_tool_hook_no_caller_identity():
    """Pins the Relay 0.10.0 finding behind turn tokens: the tool context names the call, not the caller."""
    from dataclasses import fields

    from nemo_relay_plugin import ToolExecutionContext

    assert [f.name for f in fields(ToolExecutionContext)] == ["tool_name", "args", "tool_call_id"]


async def test_a_later_request_continues_its_conversation_from_its_own_transcript(setup):
    p, council, _, _ = setup()
    t = turn(p, user="check the shell")
    first_call = call(turn=t)
    first = (await p.h3(first_call, Next())).result
    seen = history("check the shell", (first_call, first))
    out = p.h1("model", request(seen), None)
    assert first["evidence_id"] in nomos_message(out)
    t2 = TURN.search(nomos_message(out))[0]
    assert p.store.turn(t2).conversation == p.store.turn(t).conversation
    second = (await p.h3(cite(t2, first["evidence_id"], call_id="c2"), Next())).result
    assert second["status"] == "executed" and council.calls[1]["certified_evidence"]


async def test_a_conversation_never_sees_or_cites_another_conversations_evidence(setup):
    p, council, _, _ = setup()
    alice = turn(p, user="Alice: print my payroll file")
    secret = (await p.h3(call(turn=alice, command="echo alice-secret", call_id="a1"), Next())).result
    bob = turn(p, user="Bob: list files")
    assert p.store.turn(bob).conversation != p.store.turn(alice).conversation
    # Even an ID pasted into Bob's transcript is neither listed nor citable: it is not his conversation's.
    pasted = [{"role": "user", "content": f"Bob: what is {secret['evidence_id']}?"}]
    assert secret["evidence_id"] not in nomos_message(p.h1("model", request(pasted), None))
    out = (await p.h3(cite(bob, secret["evidence_id"], call_id="b1"), Next())).result
    assert out["status"] == "evidence_not_found" and len(council.calls) == 1


async def test_the_council_gets_the_user_request_of_the_calls_own_turn(setup):
    p, council, _, _ = setup()
    alice = turn(p, user="Alice: print my payroll file")
    turn(p, user="Bob: list files")  # Bob's request arrives before Alice's tool call
    await p.h3(call(turn=alice), Next())
    assert council.calls[0]["context"] == "tool=run_shell; latest user request: Alice: print my payroll file"


async def test_denials_and_the_denial_cap_are_per_conversation(setup):
    p, council, _, _ = setup("denied", max_denials=2)
    alice = turn(p)
    for i in range(3):
        last = (await p.h3(call(turn=alice, call_id=f"a{i}"), Next())).result
    assert last["status"] == "denial_cap" and len(council.calls) == 2
    bob = turn(p)
    assert (await p.h3(call(turn=bob, call_id="b1"), Next())).result["status"] == "denied"
    assert len(council.calls) == 3


async def test_an_unknown_outcome_blocks_only_its_own_conversation(setup):
    p, _, executor, _ = setup()
    original = executor.execute

    async def down(writ, agent_id):
        raise ConnectionError("lost")

    executor.execute = down
    alice, bob = turn(p), turn(p)
    assert (await p.h3(call(turn=alice), Next())).result["status"] == "execution_unknown"
    later = (await p.h3(call(turn=alice, call_id="a2"), Next())).result
    assert later["status"] == "session_requires_reconciliation"
    executor.execute = original
    assert (await p.h3(call(turn=bob, call_id="b1"), Next())).result["status"] == "executed"


async def test_conversations_run_concurrently_and_each_is_serialized(setup):
    p, council, _, _ = setup()
    gate = asyncio.Event()
    original = council.authorize

    async def slow(**fields):
        await gate.wait()
        return await original(**fields)

    council.authorize = slow
    alice, bob = turn(p), turn(p)
    first = asyncio.create_task(p.h3(call(turn=alice), Next()))
    await asyncio.sleep(0)
    busy = (await p.h3(call(turn=alice, call_id="a2"), Next())).result
    other = asyncio.create_task(p.h3(call(turn=bob, call_id="b1"), Next()))
    await asyncio.sleep(0)
    gate.set()
    assert busy["status"] == "session_busy"
    assert (await first).result["status"] == "executed" and (await other).result["status"] == "executed"


async def test_the_same_tool_call_id_in_two_conversations_never_replays_across(setup):
    p, _, executor, _ = setup()
    alice, bob = turn(p), turn(p)
    a = (await p.h3(call(turn=alice, call_id="call_0"), Next())).result
    b = (await p.h3(call(turn=bob, call_id="call_0"), Next())).result
    assert a["evidence_id"] != b["evidence_id"] and len(executor.calls) == 2


@pytest.mark.parametrize("token", ["nt_" + "0" * 32, "not-a-token", None])
async def test_a_missing_or_unknown_turn_is_refused_and_taught(setup, token):
    p, council, _, _ = setup()
    turn(p)
    intent = {**INTENT, "turn": token} if token else INTENT
    out = (await p.h3(call(intent=intent), Next())).result
    expected = "unknown_turn" if token and token.startswith("nt_") else "missing_intent"
    assert out["status"] == expected and "turn" in out["message"] and not council.calls


async def test_a_transcript_naming_two_conversations_starts_a_new_one_with_the_stricter_state(setup):
    p, council, executor, _ = setup()
    alice, bob = turn(p), turn(p)
    a_call = call(turn=alice)
    a = (await p.h3(a_call, Next())).result

    async def down(writ, agent_id):
        raise ConnectionError("lost")

    original, executor.execute = executor.execute, down
    b_call = call(turn=bob, call_id="b1")
    b = (await p.h3(b_call, Next())).result
    assert b["status"] == "execution_unknown"
    executor.execute = original
    merged = turn(p, *history("both", (a_call, a), (b_call, b)))
    conversation = p.store.turn(merged).conversation
    assert conversation not in {p.store.turn(alice).conversation, p.store.turn(bob).conversation}
    out = (await p.h3(call(turn=merged, call_id="m1"), Next())).result
    assert out["status"] == "session_requires_reconciliation", "it inherits Bob's block"
    assert (await p.h3(call(turn=alice, call_id="a2"), Next())).result["status"] == "executed", (
        "Alice is unchanged"
    )


async def test_one_conversation_cannot_fill_the_store_for_the_others(setup):
    p, _, _, _ = setup(limits={"max_records": 8})  # each conversation may hold 25%: two records
    alice, bob = turn(p), turn(p)
    for i in range(2):
        assert (await p.h3(call(turn=alice, call_id=f"a{i}"), Next())).result["status"] == "executed"
    full = (await p.h3(call(turn=alice, call_id="a2"), Next())).result
    assert full["status"] == "storage_quota_exceeded"
    assert (await p.h3(call(turn=bob, call_id="b1"), Next())).result["status"] == "executed"


async def test_retention_frees_the_journal_so_a_long_lived_worker_keeps_working(setup, monkeypatch):
    p, _, _, _ = setup(limits={"max_invocations": 4, "retention_seconds": 60})  # one conversation: one call
    now = time.time()
    monkeypatch.setattr("nemo_relay_nomos.repository.time.time", lambda: now)
    assert (await p.h3(call(turn=turn(p), call_id="c1"), Next())).result["status"] == "executed"
    full = (await p.h3(call(turn=turn(p), call_id="c2"), Next())).result
    assert full["status"] == "executed"
    for i in range(2):
        await p.h3(call(turn=turn(p), call_id=f"x{i}"), Next())
    assert (await p.h3(call(turn=turn(p), call_id="c5"), Next())).result["status"] == "storage_quota_exceeded"
    now += 121  # past expiry, and past the tombstone period that follows it
    assert (await p.h3(call(turn=turn(p), call_id="c6"), Next())).result["status"] == "executed"


# ── the plugin's signing key ─────────────────────────────────────────────


def without_key():
    cfg = base_config()
    del cfg["signing_key"]
    return cfg


def records_stored(p):
    return p.store.db.execute("SELECT count(*) FROM records").fetchone()[0]


async def test_without_a_signing_key_no_evidence_is_offered_or_stored(setup):
    cfg = without_key()
    assert any(d.code == "nomos.config.evidence_disabled" for d in NomosPlugin().validate(cfg))
    p, council, executor, _ = setup(config=cfg)
    assert p.signer is None and not p.evidence
    t = turn(p)
    ok_call = call(turn=t)
    ok = (await p.h3(ok_call, Next())).result
    council.status = "denied"
    denied_call = call(turn=t, call_id="c2")
    denied = (await p.h3(denied_call, Next())).result
    # The Executor's signature is still verified before its output is returned; nothing is stored.
    assert ok["status"] == "executed" and ok["evidence_status"] == "locally_verified" and ok["stdout"]
    assert denied["status"] == "denied" and denied["evidence_status"] == "unavailable"
    assert "evidence_id" not in ok and "evidence_id" not in denied and records_stored(p) == 0
    # The model is offered nothing to cite: no evidence field, and the Nomos message is the turn line alone.
    out = p.h1("model", request(history("list files", (ok_call, ok), (denied_call, denied))), None)
    nomos = out.request["content"]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
    assert "evidence" not in nomos["properties"] and CATALOG_HEADER not in nomos_message(out)
    council.status = "approved"
    cited = (await p.h3(cite(t, "ev_" + "0" * 32, call_id="c3"), Next())).result
    assert cited["status"] == "missing_intent" and "unexpected field 'evidence'" in cited["message"]
    assert '"evidence"' not in cited["message"].split(" Problems:")[0], "the teaching text offers no evidence"
    assert len(council.calls) == 2 and records_stored(p) == 0


async def test_without_a_signing_key_local_results_are_returned_and_tampering_still_blocks(setup):
    p, _, executor, _ = setup(config={**without_key(), "guard": {"include": ["run_shell", "read_file"]}})
    t = turn(p)
    local = (
        await p.h3(call(turn=t, name="read_file", call_id="l1", path="/x"), Next(result={"text": "body"}))
    ).result
    assert (
        local["status"] == "executed" and local["output"] == {"text": "body"} and "evidence_id" not in local
    )
    original = executor.execute

    async def tampered(writ, agent_id):
        return {**(await original(writ, agent_id)), "stdout": "forged"}

    executor.execute = tampered
    bad = (await p.h3(call(turn=t, call_id="c1"), Next())).result
    assert bad["status"] == "evidence_invalid" and "stdout" not in bad and "quarantine_id" not in bad
    after = (await p.h3(call(turn=t, call_id="c2"), Next())).result
    assert after["status"] == "session_requires_reconciliation" and records_stored(p) == 0


async def test_a_plugin_with_a_signing_key_rejects_a_record_stripped_of_its_receipt(setup):
    p, council, _, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    row = p.store.db.execute("SELECT record FROM records WHERE id=?", (first["evidence_id"],)).fetchone()
    record = json.loads(row[0])
    record["receipt"] = None  # stripping the receipt does not make a record pass as unsigned
    blob = canonical(record)
    with p.store.db:
        p.store.db.execute(
            "UPDATE records SET record=?, digest=? WHERE id=?", (blob, sha256_hex(blob), first["evidence_id"])
        )
    out = (await p.h3(cite(t, first["evidence_id"], call_id="c2"), Next())).result
    assert out["status"] == "evidence_integrity_failure" and len(council.calls) == 1


# ── lifetime and trust ───────────────────────────────────────────────────


async def test_a_new_activation_starts_empty(setup):
    cfg = base_config()
    p, _, _, _ = setup(config=cfg)
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    p.close()
    p2, council2, _, _ = setup(config=cfg)
    # The transcript still shows the old turn and evidence; the new worker knows neither.
    seen = history("list files", (call(turn=t), first))
    assert CATALOG_HEADER not in nomos_message(p2.h1("model", {"content": {"messages": seen}}, None))
    old = (await p2.h3(cite(t, first["evidence_id"], call_id="c2"), Next())).result
    assert old["status"] == "unknown_turn" and not council2.calls
    t2 = turn(p2, *seen)
    out = (await p2.h3(cite(t2, first["evidence_id"], call_id="c2"), Next())).result
    assert out["status"] == "evidence_not_found" and not council2.calls


async def test_two_activations_never_share_records(setup):
    cfg = base_config()
    p, _, _, _ = setup(config=cfg)
    t = turn(p)
    p2, council2, _, _ = setup(config=cfg)
    t2 = turn(p2)
    first = (await p.h3(call(turn=t), Next())).result
    out = (await p2.h3(cite(t2, first["evidence_id"], call_id="c2"), Next())).result
    assert out["status"] == "evidence_not_found" and not council2.calls


async def test_revoked_executor_key_makes_old_evidence_uncitable(setup):
    p, council, _, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    object.__setattr__(p.settings.trust, "executors", {"executor": ["ab" * 32]})
    out = (await p.h3(cite(t, first["evidence_id"], call_id="c2"), Next())).result
    assert out["status"] == "untrusted_executor" and len(council.calls) == 1


async def test_record_tampered_in_the_store_is_not_listed_or_citable(setup):
    p, council, _, _ = setup()
    t = turn(p)
    first = (await p.h3(call(turn=t), Next())).result
    row = p.store.db.execute("SELECT record FROM records WHERE id=?", (first["evidence_id"],)).fetchone()
    record = json.loads(row[0])
    record["content"]["stdout"] = "forged"
    blob = canonical(record)
    with p.store.db:
        p.store.db.execute(
            "UPDATE records SET record=?, digest=? WHERE id=?", (blob, sha256_hex(blob), first["evidence_id"])
        )
    seen = history("list files", (call(turn=t), first))
    assert CATALOG_HEADER not in nomos_message(p.h1("model", {"content": {"messages": seen}}, None))
    out = (await p.h3(cite(t, first["evidence_id"], call_id="c2"), Next())).result
    assert out["status"] == "evidence_integrity_failure" and len(council.calls) == 1


async def test_expired_trust_refuses_without_contacting_the_council(setup):
    p, council, _, _ = setup()
    t = turn(p)
    object.__setattr__(p.settings.trust, "valid_until", time.time() - 1)
    out = (await p.h3(call(turn=t), Next())).result
    assert out["status"] == "trust_policy_expired" and not council.calls


# ── the Council's key fetched from the Council (trust.council_public_key_source = "council") ──


def fetched_key_config(**overrides):
    cfg = base_config(**overrides)
    del cfg["trust"]["council_public_key_hex"]
    cfg["trust"]["council_public_key_source"] = "council"
    return cfg


async def test_the_fetched_council_key_is_asked_for_once_and_verifies_writs(setup):
    p, council, executor, _ = setup(config=fetched_key_config())
    assert p.settings.trust.council_public_key_hex is None, "nothing is fetched before the first guarded call"
    first = (await p.h3(call(turn=turn(p)), Next())).result
    second = (await p.h3(call(turn=turn(p), call_id="call2"), Next())).result
    assert first["status"] == second["status"] == "executed", (first, second)
    assert council.key_calls == ["council"] and len(executor.calls) == 2
    assert p.settings.trust.council_public_key_hex == public(COUNCIL_KEY)


async def test_writs_are_verified_against_the_fetched_key_not_any_key(setup):
    p, council, executor, _ = setup(config=fetched_key_config())
    council.key_reply = council_key_reply(Ed25519PrivateKey.generate())  # not the key that signs the writs
    out = (await p.h3(call(turn=turn(p)), Next())).result
    assert out["status"] == "invalid_signature" and not executor.calls, out


async def test_a_fetched_key_is_never_swapped_by_a_later_reply(setup):
    p, council, executor, _ = setup(config=fetched_key_config())
    assert (await p.h3(call(turn=turn(p)), Next())).result["status"] == "executed"
    council.key_reply = council_key_reply(Ed25519PrivateKey.generate())
    assert (await p.h3(call(turn=turn(p), call_id="call2"), Next())).result["status"] == "executed"
    assert len(council.key_calls) == 1 and p.settings.trust.council_public_key_hex == public(COUNCIL_KEY)


@pytest.mark.parametrize(
    "reply",
    [
        "raise",
        {**council_key_reply(), "public_key_hex": "", "error": "Council signer not initialized"},
        council_key_reply(council_id="another-council"),
        {**council_key_reply(), "public_key_hex": "AB" * 32},
        {**council_key_reply(), "public_key_hex": "ab" * 31},
        ["not", "an", "object"],
    ],
    ids=["unreachable", "error", "other_council", "upper_case", "short", "not_an_object"],
)
async def test_without_a_usable_council_key_nothing_is_submitted_and_the_next_call_retries(setup, reply):
    p, council, executor, _ = setup(config=fetched_key_config())
    council.key_reply = reply
    out = (await p.h3(call(turn=turn(p)), Next())).result
    assert out["status"] == "council_key_unavailable" and out["execution_status"] == "not_executed", out
    assert not council.calls and not executor.calls
    council.key_reply = council_key_reply()
    again = (await p.h3(call(turn=turn(p), call_id="call2"), Next())).result
    assert again["status"] == "executed" and len(council.key_calls) == 2, again


async def test_concurrent_first_calls_fetch_the_council_key_once(setup):
    p, council, _, _ = setup(config=fetched_key_config())
    answer = council.public_key

    async def slow(council_id):
        await asyncio.sleep(0.05)
        return await answer(council_id)

    council.public_key = slow
    a, b = await asyncio.gather(
        p.h3(call(turn=turn(p)), Next()), p.h3(call(turn=turn(p), call_id="call2"), Next())
    )
    assert a.result["status"] == b.result["status"] == "executed"
    assert council.key_calls == ["council"]


def test_a_pinned_council_key_is_never_fetched_and_validate_warns_only_when_it_is_not(setup):
    p, council, _, _ = setup()
    assert p.settings.trust.council_public_key_source == "pinned" and not council.key_calls
    codes = {d.code for d in NomosPlugin().validate(fetched_key_config())}
    assert "nomos.config.council_key_fetched" in codes
    assert "nomos.config.council_key_fetched" not in {d.code for d in NomosPlugin().validate(base_config())}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda c: c["trust"].pop("council_public_key_hex"), "trust.council_public_key_hex"),
        (
            lambda c: c["trust"].update(council_public_key_source="council"),
            "trust.council_public_key_source",
        ),
        (
            lambda c: (
                c["trust"].pop("council_public_key_hex"),
                c["trust"].update(council_public_key_source="council"),
                c["connection"].update(mcp_url="http://nomos.example.com/mcp", allow_insecure_http=True),
            ),
            "trust.council_public_key_source",
        ),
    ],
    ids=["pinned_without_a_key", "both_pinned_and_fetched", "fetched_over_plain_http"],
)
def test_council_key_source_config_errors(change, field):
    cfg = base_config()
    change(cfg)
    assert field in [d.field for d in diagnostics(cfg, environ={})]


def test_a_fetched_council_key_over_https_or_loopback_is_valid():
    for url in ("https://nomos.example.com/mcp", "http://127.0.0.1:9901/mcp"):
        cfg = fetched_key_config()
        cfg["connection"]["mcp_url"] = url
        assert diagnostics(cfg, environ={}) == [], url


async def test_storage_failure_reports_unknown_without_leaking_paths(setup):
    p, *_ = setup()
    t = turn(p)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error at /secret/path")

    p.store.begin = broken
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "storage_unavailable" and "/secret/path" not in json.dumps(data)


async def test_signed_refusal_from_the_executor_is_reported_as_not_run(setup):
    p, _, executor, _ = setup()
    t = turn(p)

    original = executor.execute

    async def tool_not_allowed(writ, agent_id):
        result = await original(writ, agent_id)
        payload = dict(result["evidence"]["payload"])
        payload["result"] = {
            **payload["result"],
            "success": False,
            "exit_code": None,
            "error": "Tool not allowed: date",
            "verification_stage": "tool_whitelist",
        }
        result.update(
            success=False, exit_code=None, error="Tool not allowed: date", verification_stage="tool_whitelist"
        )
        result["evidence"] = {
            **result["evidence"],
            "payload": payload,
            "signature": EXECUTOR_KEY.sign(canonical(payload)).hex(),
            "public_key_hex": public(EXECUTOR_KEY),
        }
        return result

    executor.execute = tool_not_allowed
    data = (await p.h3(call(turn=t), Next())).result
    assert data["status"] == "execution_failed" and data["execution_status"] == "not_executed"
    assert data["evidence_status"] == "locally_verified" and data["error"] == "Tool not allowed: date"
