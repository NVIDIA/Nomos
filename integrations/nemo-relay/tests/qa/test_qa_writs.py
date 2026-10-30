# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `writs`: DefaultWritHandler and load_writ_handler."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest
import qa_support as qa
from nemo_relay_plugin import ToolExecutionResult

from nemo_relay_nomos import records, writs
from nemo_relay_nomos import settings as st
from nemo_relay_nomos.repository import EvidenceStore
from nemo_relay_nomos.signing import PluginSigner
from nemo_relay_nomos.tool_call import StructuredToolCall

LOCAL_OUT = "WRIT_LOCAL_OUTPUT_MARKER_5d"


def _settings(**overrides):
    raw = qa.raw_config(
        guard={"include": ["run_shell", "qa_local"]},
        tools={
            "run_shell": {"executor": "nomos_http", "command_argument": "command"},
            "qa_local": {"executor": "local"},
        },
        signing_key=qa.PLUGIN_SEED.hex(),
    )
    raw.update(overrides)
    return st.Settings.from_config(raw, environ={})


class _Next:
    def __init__(self):
        self.calls = []

    async def __call__(self, args):
        self.calls.append(copy.deepcopy(args))
        return ToolExecutionResult(result={"output": LOCAL_OUT})


@pytest.fixture
def env():
    settings = _settings()
    store = EvidenceStore(qa.OWNER, settings.limits)
    executor = qa.FakeExecutor()
    handler = writs.DefaultWritHandler(settings, store, executor, PluginSigner(qa.PLUGIN_SEED))
    yield settings, store, executor, handler
    store.close()


CONVERSATION = "cv_" + "c" * 32  # DESIGN.md `writs`: the journal the call belongs to


def _shell_call(command="echo hi", call_id="call-shell", writ_id=None):
    return writs.WritCall(
        tool_name="run_shell",
        tool_call_id=call_id,
        arguments={"command": command},
        command=command,
        policy=st.ToolPolicy("nomos_http", "command"),
        decision_writ_id=writ_id,
        conversation=CONVERSATION,
        next_=None,
    )


def _local_call(next_, writ, call_id="call-local", args=None):
    args = args or {"path": "/workspace/a"}
    writ_id = writ["writ_id"]
    return writs.WritCall(
        tool_name="qa_local",
        tool_call_id=call_id,
        arguments=args,
        command=None,
        policy=st.ToolPolicy("local", None),
        decision_writ_id=writ_id,
        conversation=CONVERSATION,
        next_=next_,
        structured=StructuredToolCall.build("qa_local", args, "opaque-qa-user"),
    )


def _local_writ(args=None):
    arguments = args or {"path": "/workspace/a"}
    return qa.mint_tool_call_writ(
        {
            "agent_id": qa.AGENT_ID,
            "council_id": qa.COUNCIL_ID,
            "tool_call": {
                "schema_version": 1,
                "tool_name": "qa_local",
                "arguments": arguments,
                "subject": "opaque-qa-user",
            },
        }
    )


def _envelope_ok(resp: dict, ctx: str):
    for k in ("contract_version", "status", "execution_status", "evidence_status"):
        assert k in resp, f"{ctx}: model-visible response {resp!r} lacks {k!r}"
    assert resp["contract_version"] == "nomos-plugin/v1", (
        f"{ctx}: contract_version {resp['contract_version']!r}"
    )


# ---- WritCall shape --------------------------------------------------------------------------------


def test_writcall_positional_order_and_next_default():
    call = writs.WritCall("t", "id", {"a": 1}, "cmd", st.ToolPolicy("nomos_http", "command"), "w-1", "cv-1")
    got = (
        call.tool_name,
        call.tool_call_id,
        call.arguments,
        call.command,
        call.decision_writ_id,
        call.conversation,
        call.next_,
    )
    assert got == ("t", "id", {"a": 1}, "cmd", "w-1", "cv-1", None), f"WritCall positional fields -> {got!r}"


# ---- nomos_http ------------------------------------------------------------------------------------


async def test_nomos_http_dispatches_to_executor_and_certifies(env):
    _, _, executor, handler = env
    writ = qa.mint_writ("echo hi")
    enc = qa.encode_writ(writ)
    res = await handler.handle(_shell_call(writ_id=writ["writ_id"]), enc, writ)
    assert executor.calls == [(enc, qa.AGENT_ID)], f"executor calls {executor.calls!r}"
    rec = res.record
    assert rec["kind"] == "executor_result" and rec["certified"] is True, (
        f"record kind={rec['kind']!r} certified={rec['certified']!r} for a genuine signed result"
    )
    assert rec["content"] == executor.results[0], "record content must be the original ExecutionResult"
    _envelope_ok(res.response, "nomos_http approved")
    assert res.response["execution_status"] == "executed", f"execution_status {res.response!r}"
    assert res.response["evidence_status"] == "locally_verified", f"evidence_status {res.response!r}"


@pytest.mark.parametrize("mode", ["tamper", "untrusted-key", "wrong-executor-id"])
async def test_unverifiable_executor_result_is_quarantined(env, mode):
    _, _, executor, handler = env
    writ = qa.mint_writ("echo hi")
    enc = qa.encode_writ(writ)
    if mode == "tamper":
        executor.mode = "tamper"
    else:
        seed = qa.STRANGER_SEED if mode == "untrusted-key" else qa.EXECUTOR_SEED
        exec_id = "executor-unregistered" if mode == "wrong-executor-id" else qa.EXECUTOR_ID

        async def _exec(w, agent_id):
            executor.calls.append((w, agent_id))
            return qa.signed_result(
                qa.decode_writ_b64(w), stdout=executor.stdout, seed=seed, executor_id=exec_id
            )

        executor.execute = _exec
    res = await handler.handle(_shell_call(writ_id=writ["writ_id"]), enc, writ)
    assert res.record["evidence_status"] == "invalid", (
        f"{mode}: record evidence_status {res.record['evidence_status']!r}"
    )
    assert res.record["certified"] is False, (
        f"{mode}: quarantined record certified={res.record['certified']!r}"
    )
    assert res.response["execution_status"] == "unknown", f"{mode}: response {res.response!r}"
    assert "QA_STDOUT_MARKER" not in json.dumps(res.response), (
        f"{mode}: output reached the model: {res.response!r}"
    )
    if res.annotation is not None:
        assert "QA_STDOUT_MARKER" not in json.dumps(res.annotation), f"{mode}: output in annotation"


@pytest.mark.parametrize(
    "mutate",
    [
        "other-command",
        "other-agent",
        "other-council",
        "untrusted-council-key",
        "expired",
        "bad-signature",
        "dir",
    ],
)
async def test_invalid_writ_never_dispatches(env, mutate):
    _, _, executor, handler = env
    kw = {}
    if mutate == "other-command":
        kw["command"] = "rm -rf /"
    elif mutate == "other-agent":
        kw["agent_id"] = "agent-intruder"
    elif mutate == "other-council":
        kw["council_id"] = "council-other"
    elif mutate == "untrusted-council-key":
        kw["seed"] = qa.STRANGER_SEED
    elif mutate == "expired":
        kw["lifetime_s"] = -10.0
    elif mutate == "dir":
        kw["working_directory"] = "/etc"
    writ = qa.mint_writ(kw.pop("command", "echo hi"), **kw)
    if mutate == "bad-signature":
        writ["signature"] = "00" * 64
    enc = qa.encode_writ(writ)
    try:
        res = await handler.handle(_shell_call(writ_id=writ["writ_id"]), enc, writ)
    except Exception:  # noqa: BLE001 - the spec does not say whether the handler raises or refuses
        res = None
    assert executor.calls == [], f"writ with {mutate} reached the executor"
    if res is not None:
        assert res.response.get("execution_status") != "executed", f"{mutate}: response {res.response!r}"


# ---- claims -----------------------------------------------------------------------------------------


async def test_local_tool_auto_claim_uses_local_ledger_and_refuses_replay(env):
    _, store, _, handler = env
    nxt = _Next()
    envelope, writ = _local_writ()
    call = _local_call(nxt, writ)
    enc = qa.encode_writ(envelope)
    first = await handler.handle(call, enc, envelope)
    assert len(nxt.calls) == 1, f"first handle ran next_ {len(nxt.calls)} times"
    call2 = _local_call(nxt, writ, call_id="call-local-2")
    second = await handler.handle(call2, enc, envelope)
    assert second.response["status"] == "writ_replayed", f"replayed writ status {second.response!r}"
    assert len(nxt.calls) == 1, f"replayed writ ran next_ again ({len(nxt.calls)} calls)"
    assert store.claim_writ(writ["writ_id"], writ["nonce"]) is False, (
        "local_ledger claim not in store.claim_writ"
    )
    _envelope_ok(first.response, "local first")
    _envelope_ok(second.response, "local replay")


async def test_local_tool_refuses_a_nonce_already_in_the_ledger(env):
    _, store, _, handler = env
    nxt = _Next()
    envelope, writ = _local_writ()
    call = _local_call(nxt, writ)
    store.claim_writ(writ["writ_id"], writ["nonce"])
    res = await handler.handle(call, qa.encode_writ(envelope), envelope)
    assert res.response["status"] == "writ_replayed", f"pre-claimed nonce status {res.response!r}"
    assert nxt.calls == [], "pre-claimed writ ran next_"


async def test_explicit_local_ledger_claim_for_nomos_http_refuses_replay():
    settings = _settings(writ_handler={"claim": "local_ledger"})
    store = EvidenceStore(qa.OWNER, settings.limits)
    try:
        executor = qa.FakeExecutor()
        handler = writs.DefaultWritHandler(settings, store, executor, PluginSigner(qa.PLUGIN_SEED))
        writ = qa.mint_writ("echo hi")
        store.claim_writ(writ["writ_id"], writ["nonce"])
        res = await handler.handle(_shell_call(writ_id=writ["writ_id"]), qa.encode_writ(writ), writ)
        assert res.response["status"] == "writ_replayed", f"claim=local_ledger replay status {res.response!r}"
        assert executor.calls == [], "claim=local_ledger replayed writ reached the executor"
    finally:
        store.close()


# ---- local ------------------------------------------------------------------------------------------


async def test_local_tool_runs_next_with_arguments_and_records_local_result(env):
    _, _, executor, handler = env
    nxt = _Next()
    args = {"path": "/workspace/café", "mode": "r"}
    envelope, writ = _local_writ(args)
    call = _local_call(nxt, writ, args=args)
    res = await handler.handle(call, qa.encode_writ(envelope), envelope)
    assert nxt.calls == [args], f"next_ called with {nxt.calls!r}, want [{args!r}]"
    assert executor.calls == [], "a local tool reached the executor"
    rec = res.record
    assert rec["kind"] == "local_result", f"local record kind {rec['kind']!r}"
    assert LOCAL_OUT in json.dumps(rec["content"]), f"local output not in record content {rec['content']!r}"
    # Spec change (every citation certified): with a signing key the local result is always certified.
    assert rec["certified"] is True and rec["evidence_status"] == "plugin_certified", f"local record {rec!r}"
    assert isinstance(rec.get("certified_result"), dict), (
        f"certified local record lacks certified_result {rec!r}"
    )
    assert res.response["evidence_status"] == "plugin_certified", f"local response {res.response!r}"
    assert records.integrity_ok(rec, qa.public_hex(qa.PLUGIN_SEED)), "local record fails integrity"


@pytest.mark.parametrize("executor", ["local", "nomos_http"])
async def test_handler_refuses_a_writ_contract_for_the_wrong_executor(env, executor):
    """The handler must not accept a legacy writ locally or a tool-call writ remotely."""

    _, _, remote, handler = env
    nxt = _Next()
    if executor == "local":
        legacy = qa.mint_writ("qa_local {}")
        call = writs.WritCall(
            "qa_local",
            "call-mismatch",
            {},
            "qa_local {}",
            st.ToolPolicy("local", None),
            legacy["writ_id"],
            CONVERSATION,
            nxt,
        )
        encoded, decoded = qa.encode_writ(legacy), legacy
    else:
        envelope, payload = _local_writ()
        structured = StructuredToolCall.build("qa_local", {"path": "/workspace/a"}, "opaque-qa-user")
        call = writs.WritCall(
            "run_shell",
            "call-mismatch",
            {"command": "echo hi"},
            "echo hi",
            st.ToolPolicy("nomos_http", "command"),
            payload["writ_id"],
            CONVERSATION,
            None,
            structured=structured,
        )
        encoded, decoded = qa.encode_writ(envelope), envelope

    result = await handler.handle(call, encoded, decoded)

    assert result.response["status"] == "invalid_tool_policy"
    assert nxt.calls == []
    assert remote.calls == []


# ---- load_writ_handler ------------------------------------------------------------------------------


def test_load_writ_handler_default_returns_default_handler(env):
    settings, store, executor, _ = env
    h = writs.load_writ_handler(settings, store=store, executor=executor, signer=PluginSigner(qa.PLUGIN_SEED))
    assert isinstance(h, writs.DefaultWritHandler), f"load_writ_handler(kind=default) -> {h!r}"


def test_load_writ_handler_custom_raises_not_implemented(env):
    settings, store, executor, _ = env
    custom = dataclasses.replace(
        settings, writ_handler=dataclasses.replace(settings.writ_handler, kind="custom", entrypoint="pkg:H")
    )
    with pytest.raises(NotImplementedError) as info:
        writs.load_writ_handler(custom, store=store, executor=executor, signer=PluginSigner(qa.PLUGIN_SEED))
    assert str(info.value) == "custom writ handlers are not implemented yet", f"message {str(info.value)!r}"
