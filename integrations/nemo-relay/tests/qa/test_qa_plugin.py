# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `plugin`: NomosPlugin validate/register, H1 and H3 orchestration."""

from __future__ import annotations

import base64
import copy
import json

import pytest
import qa_support as qa
from nemo_relay_plugin import (
    ConfigDiagnostic,
    LlmRequestInterceptOutcome,
    ToolExecutionContext,
    ToolExecutionInterceptOutcome,
)

from nemo_relay_nomos import records
from nemo_relay_nomos.llm_request import NOMOS_FIELD, NOMOS_FIELD_WITHOUT_EVIDENCE, TURN_LINE
from nemo_relay_nomos.plugin import NomosPlugin
from nemo_relay_nomos.repository import EvidenceStore
from nemo_relay_nomos.settings import Settings

USER_TEXT = "Please show me what is in the workspace directory"
STDOUT_MARKER = "QA_STDOUT_MARKER_7f3a"
LOCAL_MARKER = "QA_LOCAL_OUTPUT_91c2"
PLUGIN_KEY = qa.public_hex(qa.PLUGIN_SEED)
ENVELOPE = ("contract_version", "status", "execution_status", "evidence_status")


def _config(**overrides) -> dict:
    raw = qa.raw_config(
        guard={"include": ["run_shell", "qa_local"]},
        tools={
            "run_shell": {"executor": "nomos_http", "command_argument": "command"},
            "qa_local": {"executor": "local"},
        },
        signing_key=qa.PLUGIN_SEED.hex(),
    )
    raw.update(overrides)
    return raw


def _fn_tool(name: str, prop: str = "command") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {prop: {"type": "string"}}, "required": [prop]},
        },
    }


def _messages(user_text: str = USER_TEXT) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": user_text}]


def _raw_request(user_text: str = USER_TEXT, messages: list[dict] | None = None) -> dict:
    return {
        "headers": {},
        "content": {
            "model": "qa-model",
            "messages": messages if messages is not None else _messages(user_text),
            "tools": [_fn_tool("run_shell"), _fn_tool("qa_local", "path"), _fn_tool("read_file", "path")],
        },
    }


def _shell_args(command: str = "echo hi", **intent_overrides) -> dict:
    return {"command": command, "nomos": {**qa.INTENT, **intent_overrides}}


class Harness:
    def __init__(self, raw: dict, council: qa.FakeCouncil, executor: qa.FakeExecutor):
        self.raw = raw
        self.council = council
        self.executor = executor
        self.settings = Settings.from_config(raw, environ={})
        self.store = EvidenceStore(qa.OWNER, self.settings.limits)
        self.plugin = NomosPlugin(council=council, executor=executor, store=self.store, environ={})
        self.ctx = qa.FakeContext()
        self.next = qa.FakeToolNext()
        self._n = 0
        self.new_conversation()

    def new_conversation(self, user_text: str = USER_TEXT) -> None:
        """Start another agent conversation on the same plugin: an empty transcript and no turn yet."""
        self.user_text = user_text
        self.turn: str | None = (
            None  # the token from the latest Nomos message, as a compliant model copies it
        )
        self.exchanges: list[tuple[str, str, dict, dict]] = []

    def transcript(self) -> list[dict]:
        """The messages an agent loop would send next: its request, then every tool call and result so far."""
        messages = _messages(self.user_text)
        for name, call_id, args, result in self.exchanges:
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
        return messages

    async def start(self) -> Harness:
        await qa.maybe_await(self.plugin.register(self.ctx, copy.deepcopy(self.raw)))
        self.h1 = self.ctx.only("llm_request")["callback"]
        self.h3 = self.ctx.only("tool_execution")["callback"]
        return self

    async def model(
        self, request: dict | None = None, annotated: dict | None = None
    ) -> LlmRequestInterceptOutcome:
        out = await qa.maybe_await(
            self.h1("qa-model", request or _raw_request(messages=self.transcript()), annotated)
        )
        assert isinstance(out, LlmRequestInterceptOutcome), f"h1 returned {out!r}"
        self.turn = qa.turn_token(out)
        return out

    async def tool(self, name: str, args: dict, call_id: str | None = None) -> ToolExecutionInterceptOutcome:
        """A tool call as a compliant agent makes it: after a model request, carrying that request's turn."""
        self._n += 1
        if self.turn is None:
            await self.model()
        args = copy.deepcopy(args)
        if isinstance(args.get("nomos"), dict) and self.settings.guard.matches(name):
            args["nomos"].setdefault("turn", self.turn)
        ctx = ToolExecutionContext(tool_name=name, args=args, tool_call_id=call_id or f"call-{self._n}")
        out = await qa.maybe_await(self.h3(ctx, self.next))
        assert isinstance(out, ToolExecutionInterceptOutcome), f"h3({name}, {args!r}) returned {out!r}"
        self.exchanges.append((name, ctx.tool_call_id, args, out.result))
        return out

    def record(self, out: ToolExecutionInterceptOutcome) -> dict:
        eid = out.result.get("evidence_id")
        assert eid, f"outcome {out.result!r} carries no evidence_id though a record must be stored"
        return self.store.get(eid)


@pytest.fixture
async def harness(qa_council, qa_executor):
    h = await Harness(_config(), qa_council, qa_executor).start()
    yield h
    h.store.close()


async def _make(council=None, executor=None, **overrides) -> Harness:
    return await Harness(
        _config(**overrides), council or qa.FakeCouncil(), executor or qa.FakeExecutor()
    ).start()


def _stored(h: Harness) -> int:
    """How many records the store holds (DESIGN.md exposes `EvidenceStore.db` for inspection)."""
    return h.store.db.execute("SELECT count(*) FROM records").fetchone()[0]


def _assert_envelope(result: dict, ctx: str, *, stored: bool = True):
    for k in ENVELOPE:
        assert k in result, f"{ctx}: model-visible response {result!r} lacks {k!r}"
    assert result["contract_version"] == "nomos-plugin/v1", (
        f"{ctx}: contract_version {result['contract_version']!r}"
    )
    if stored:
        assert str(result.get("evidence_id", "")).startswith("ev_"), (
            f"{ctx}: evidence_id missing in {result!r}"
        )


# ---- identity, validate, register -----------------------------------------------------------------


def test_plugin_id_is_nomos_council():
    assert NomosPlugin.plugin_id == "nomos.council", f"plugin_id {NomosPlugin.plugin_id!r}"


async def test_register_installs_h1_at_10_and_h3_at_10000(harness):
    assert harness.ctx.only("llm_request")["priority"] == 10, f"registrations {harness.ctx.registrations!r}"
    assert harness.ctx.only("tool_execution")["priority"] == 10000, (
        f"registrations {harness.ctx.registrations!r}"
    )


async def test_validate_of_a_valid_config_is_empty():
    got = await qa.maybe_await(NomosPlugin(environ={}).validate(_config()))
    # Spec change (architect, 2026-10-12, security audit F17): a valid config yields no ERROR diagnostics;
    # WARNING diagnostics advise against inline secrets.
    errors = [d for d in got if getattr(d.level, "value", d.level) == "error"]
    assert errors == [], f"validate(valid config) -> {got!r}"


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"writ_handler": {"kind": "custom"}}, "custom writ handlers are not implemented yet"),
        ({"writ_handler": {"claim": "council"}}, "claim"),
        ({"tools": {"run_shell": {"executor": "nomos_http"}}}, None),
    ],
    ids=["custom-handler", "council-claim", "nomos_http-without-command_argument"],
)
async def test_validate_returns_config_diagnostics_for_semantic_errors(overrides, needle):
    got = await qa.maybe_await(NomosPlugin(environ={}).validate(_config(**overrides)))
    assert got, f"validate({overrides!r}) returned no diagnostics"
    assert all(isinstance(d, ConfigDiagnostic) for d in got), f"validate({overrides!r}) -> {got!r}"
    if needle:
        assert any(needle in d.message.lower() or needle in d.message for d in got), (
            f"validate({overrides!r}) diagnostics {[d.message for d in got]!r} must mention {needle!r}"
        )


# ---- H3: unguarded --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [{"path": "/workspace/a"}, {"path": "x", "nomos": {"description": "ignored"}}, {}]
)
async def test_unguarded_tool_passes_through_unchanged_and_stores_nothing(harness, args):
    out = await harness.tool("read_file", args)
    assert harness.next.calls == [args], f"next_.call got {harness.next.calls!r}, want [{args!r}] unchanged"
    assert out.result == harness.next.result, f"unguarded result {out.result!r} != next result"
    assert harness.council.calls == [] and harness.executor.calls == [], "unguarded tool reached Nomos"
    assert _stored(harness) == 0, "unguarded tool stored a record"


async def test_reserved_nomos_prefix_is_never_guarded_even_with_star():
    h = await _make(guard={"include": ["*"]})
    try:
        out = await h.tool("nomos_evidence_list", {"limit": 5})
        assert h.next.calls == [{"limit": 5}], (
            f"nomos_evidence_list under '*' was not passed through: {out.result!r}"
        )
        assert h.council.calls == [], "nomos_ tool reached the Council"
    finally:
        h.store.close()


@pytest.mark.parametrize(
    "guard,tool,guarded",
    [
        ({"include": ["*"]}, "anything_at_all", True),
        ({"include": ["*"], "exclude": ["read_*"]}, "read_file", False),
        ({"include": ["run_*"]}, "run_other", True),
        ({"include": ["run_shell", "qa_local"]}, "other_tool", False),
    ],
)
async def test_guard_config_decides_which_tools_are_intercepted(guard, tool, guarded):
    h = await _make(guard=guard)
    try:
        out = await h.tool(tool, {"x": "1"})
        if guarded:
            assert h.next.calls == [], f"guard={guard!r}: guarded {tool} without intent reached next_"
            assert out.result.get("status") == "missing_intent", f"guard={guard!r} {tool}: {out.result!r}"
        else:
            assert h.next.calls == [{"x": "1"}], f"guard={guard!r}: unguarded {tool} not passed through"
    finally:
        h.store.close()


# ---- H3: missing intent ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"command": "echo hi"},
        {"command": "echo hi", "nomos": None},
        {"command": "echo hi", "nomos": "please"},
        {"command": "echo hi", "nomos": {}},
        _shell_args(description=""),
        _shell_args(justification="   "),
        _shell_args(blast_radius=5),
        _shell_args(target_resource="x" * 4097),
        {"command": "echo hi", "nomos": {k: v for k, v in qa.INTENT.items() if k != "justification"}},
    ],
    ids=["absent", "null", "string", "empty", "blank-desc", "ws-just", "int-blast", "4097-target", "no-just"],
)
async def test_missing_or_incomplete_intent_is_refused_without_calling_council(harness, args):
    out = await harness.tool("run_shell", args)
    assert out.result.get("status") == "missing_intent", f"args={args!r}: result {out.result!r}"
    assert harness.council.calls == [], f"args={args!r}: Council was called"
    assert harness.executor.calls == [] and harness.next.calls == [], f"args={args!r}: something ran"
    rec = harness.record(out)
    assert rec["kind"] == "plugin_refusal", f"args={args!r}: stored kind {rec['kind']!r}"
    _assert_envelope(out.result, f"missing_intent args={args!r}")


async def test_intent_field_of_4096_chars_reaches_the_council(harness):
    await harness.tool("run_shell", _shell_args(target_resource="x" * 4096))
    assert len(harness.council.calls) == 1, (
        "a 4096-char intent field must be accepted and sent to the Council"
    )


# ---- H3: citations --------------------------------------------------------------------------------


async def test_unknown_citation_is_refused_without_calling_council(harness):
    ghost = "ev_" + "0" * 32
    out = await harness.tool("run_shell", _shell_args(evidence=[ghost]))
    assert harness.council.calls == [], f"citing unknown {ghost} called the Council"
    assert "not_found" in str(out.result.get("status")), f"status must name the reason: {out.result!r}"
    assert harness.record(out)["kind"] == "plugin_refusal", "unknown citation must store a plugin_refusal"


async def test_citation_failing_integrity_is_refused_without_calling_council(harness):
    first = await harness.tool("run_shell", _shell_args())
    ev = first.result["evidence_id"]
    changed = qa.tamper_database(harness.store.db, STDOUT_MARKER, "QA_STDOUT_MARKER_XXXX")
    assert changed, "could not find the stored stdout to tamper"
    out = await harness.tool("run_shell", _shell_args(evidence=[ev]))
    assert len(harness.council.calls) == 1, f"citing tampered {ev} called the Council"
    assert "integrity" in str(out.result.get("status")), f"status must name the reason: {out.result!r}"


async def test_certified_citation_is_sent_as_the_full_original_in_certified_evidence(harness):
    first = await harness.tool("run_shell", _shell_args())
    ev = first.result["evidence_id"]
    await harness.tool("run_shell", _shell_args("echo again", evidence=[ev]))
    assert len(harness.council.calls) == 2, f"council calls {len(harness.council.calls)}"
    fields = harness.council.calls[1]
    assert fields.get("certified_evidence") == [harness.executor.results[0]], (
        f"certified_evidence for cited {ev} is {fields.get('certified_evidence')!r}; want the full original result"
    )
    assert ev not in json.dumps(fields.get("evidence") or []), (
        f"certified {ev} also sent as asserted evidence"
    )


# Spec change (every citation certified): citations go only to `certified_evidence`; `evidence` is always [].


async def test_cited_local_result_is_sent_as_its_certified_result(harness):
    first = await harness.tool("qa_local", {"path": "/workspace/a", "nomos": dict(qa.INTENT)})
    rec = harness.record(first)
    await harness.tool("run_shell", _shell_args(evidence=[rec["evidence_id"]]))
    fields = harness.council.calls[-1]
    assert fields.get("certified_evidence") == [rec["certified_result"]], (
        f"certified_evidence {fields.get('certified_evidence')!r}; want the local result's certified_result"
    )
    assert fields.get("evidence") == [], (
        f"a cited local result also sent as asserted: {fields.get('evidence')!r}"
    )


async def test_cited_denial_is_sent_as_its_certified_result(harness):
    harness.council.queue = ["deny", "approve"]
    denied = await harness.tool("run_shell", _shell_args("echo denied"))
    rec = harness.record(denied)
    out = await harness.tool("run_shell", _shell_args("echo again", evidence=[rec["evidence_id"]]))
    assert len(harness.council.calls) == 2, f"citing a denial did not reach the Council: {out.result!r}"
    fields = harness.council.calls[-1]
    assert fields.get("certified_evidence") == [rec["certified_result"]], (
        f"certified_evidence {fields.get('certified_evidence')!r}; want the denial's certified_result"
    )
    assert fields.get("evidence") == [], f"a cited denial also sent as asserted: {fields.get('evidence')!r}"


async def test_council_evidence_field_is_always_empty(harness):
    first = await harness.tool("run_shell", _shell_args())
    local = await harness.tool("qa_local", {"path": "/workspace/a", "nomos": dict(qa.INTENT)})
    ids = [first.result["evidence_id"], local.result["evidence_id"]]
    await harness.tool("run_shell", _shell_args("echo cited", evidence=ids))
    assert len(harness.council.calls) == 3, f"council calls {len(harness.council.calls)}"
    for i, fields in enumerate(harness.council.calls):
        assert fields.get("evidence") == [], f"council call {i}: evidence {fields.get('evidence')!r}, want []"


async def test_without_a_signing_key_council_evidence_field_is_still_empty():
    h = await _without_key()
    try:
        await h.tool("run_shell", _shell_args())
        assert h.council.calls and h.council.calls[0].get("evidence") == [], (
            f"evidence without a key: {h.council.calls[0].get('evidence')!r}, want []"
        )
    finally:
        h.store.close()


async def test_cited_refusal_is_not_citable_and_never_reaches_the_council(harness):
    ghost = "ev_" + "0" * 32
    refused = await harness.tool("run_shell", _shell_args(evidence=[ghost]))
    refusal = harness.record(refused)
    assert refusal["kind"] == "plugin_refusal", f"setup: stored {refusal['kind']!r}"
    out = await harness.tool("run_shell", _shell_args("echo again", evidence=[refusal["evidence_id"]]))
    assert out.result.get("status") == "evidence_not_citable", f"citing a plugin_refusal: {out.result!r}"
    assert harness.council.calls == [], "a cited refusal reached the Council"
    assert harness.record(out)["kind"] == "plugin_refusal", (
        "an uncitable citation must store a plugin_refusal"
    )


# ---- H3: intent built for the Council -------------------------------------------------------------


async def test_council_receives_the_intent_command_and_context(harness):
    await harness.model(_raw_request(USER_TEXT))
    await harness.tool("run_shell", _shell_args("ls -la"))
    f = harness.council.calls[0]
    for k, v in qa.INTENT.items():
        assert f.get(k) == v, f"council field {k}={f.get(k)!r}, want {v!r}"
    assert f.get("command") == "ls -la", f"command for nomos_http tool is {f.get('command')!r}, want 'ls -la'"
    assert f.get("agent_id") == qa.AGENT_ID and f.get("council_id") == qa.COUNCIL_ID, f"identity {f!r}"
    assert f.get("working_directory") == "/workspace", f"working_directory {f.get('working_directory')!r}"
    want_ctx = f"tool=run_shell; latest user request: {USER_TEXT}"
    assert f.get("context") == want_ctx, f"context {f.get('context')!r}, want {want_ctx!r}"


async def test_local_tool_uses_structured_name_and_arguments(harness):
    args = {"path": "/workspace/a", "b": 2}
    await harness.tool("qa_local", {**args, "nomos": dict(qa.INTENT)})
    assert harness.council.calls[0]["tool_call"] == {
        "schema_version": 1,
        "tool_name": "qa_local",
        "arguments": args,
        "subject": "opaque-qa-user",
    }
    assert "command" not in harness.council.calls[0]


async def test_latest_user_text_is_bounded_to_800_chars(harness):
    long_text = "U" * 900
    await harness.model(_raw_request(long_text))
    await harness.tool("run_shell", _shell_args())
    ctx = harness.council.calls[0].get("context", "")
    assert long_text not in ctx, f"900-char user text kept whole in context (len {len(ctx)})"
    assert ctx.startswith("tool=run_shell; latest user request: " + "U" * 100), f"context {ctx[:120]!r}"


# ---- H3: denial and council errors ----------------------------------------------------------------


async def test_denial_returns_full_council_response_minus_writ_and_stores_council_denial(harness):
    harness.council.mode = "deny"
    out = await harness.tool("run_shell", _shell_args())
    want = {k: v for k, v in harness.council.denial().items() if k != "writ"}
    assert out.result.get("status") == "denied", f"denial status {out.result!r}"
    assert out.result.get("council") == want, f"council key {out.result.get('council')!r}, want {want!r}"
    assert harness.record(out)["kind"] == "council_denial", "denial must store a council_denial record"
    assert harness.executor.calls == [] and harness.next.calls == [], "a denied call ran"
    _assert_envelope(out.result, "denied")


async def test_council_error_status_is_council_error_with_council_response(harness):
    harness.council.mode = "error"
    out = await harness.tool("run_shell", _shell_args())
    assert out.result.get("status") == "council_error", f"status for council status=error: {out.result!r}"
    council = out.result.get("council")
    assert isinstance(council, dict) and council.get("error") == "council internal error (QA)", (
        f"{out.result!r}"
    )
    assert "writ" not in council, f"council key kept writ: {council!r}"
    assert harness.record(out)["kind"] == "council_error", "council error must store a council_error record"
    assert harness.executor.calls == [] and harness.next.calls == [], "a council-error call ran"


async def test_council_exception_is_council_error_and_nothing_runs(harness):
    harness.council.mode = "raise"
    out = await harness.tool("run_shell", _shell_args())
    assert out.result.get("status") == "council_error", f"status after Council raised: {out.result!r}"
    assert harness.record(out)["kind"] == "council_error", (
        "Council exception must store a council_error record"
    )
    assert harness.executor.calls == [] and harness.next.calls == [], "a call ran after the Council raised"
    _assert_envelope(out.result, "council raised")


# Spec change (DESIGN.md "Certified Council decisions"): with a signing key, denials and errors are certified.


@pytest.mark.parametrize("mode", ["deny", "error", "raise"])
async def test_council_decision_is_plugin_certified(harness, mode):
    harness.council.mode = mode
    out = await harness.tool("run_shell", _shell_args())
    assert out.result.get("evidence_status") == "plugin_certified", f"{mode}: model result {out.result!r}"
    rec = harness.record(out)
    assert rec["kind"] in {"council_denial", "council_error"}, f"{mode}: stored {rec['kind']!r}"
    assert rec["certified"] is True and rec["evidence_status"] == "plugin_certified", (
        f"{mode}: record {rec!r}"
    )
    assert isinstance(rec.get("certified_result"), dict), f"{mode}: record lacks certified_result"
    assert records.catalog_entry(rec, 400)["certified"] is True, f"{mode}: catalog must list it as certified"
    assert records.citation(rec) == rec["certified_result"], (
        f"{mode}: citation() must be its certified_result"
    )


@pytest.mark.parametrize(
    "mode,status,decision_id",
    [("deny", "denied", "dec-denied-qa"), ("error", "council_error", "dec-error-qa")],
)
async def test_certified_council_decision_is_the_observed_answer_signed_by_the_plugin(
    harness, mode, status, decision_id
):
    harness.council.mode = mode
    out = await harness.tool("run_shell", _shell_args(), call_id="call-decided")
    cert = harness.record(out)["certified_result"]
    # Spec change (2026-10-30): success is false, with an error saying the answer authorized nothing.
    assert (cert.get("success"), cert.get("exit_code"), cert.get("verification_stage")) == (
        False,
        None,
        "observed",
    ), f"{mode}: certified result fields {cert!r}"
    assert "authorized nothing" in str(cert.get("error")), f"{mode}: error {cert.get('error')!r}"
    observed = json.loads(cert["stdout"])
    want = {
        "type": "nomos.council_decision",
        "version": 1,
        "tool": "run_shell",
        "tool_call_id": "call-decided",
        "decision_id": decision_id,
        "status": status,
    }
    for k, v in want.items():
        assert observed.get(k) == v, f"{mode}: stdout {k}={observed.get(k)!r}, want {v!r}"
    # Spec change (2026-10-30): only the answer's structured parts, never the Council's free text.
    assert set(observed) == {
        "type",
        "version",
        "tool",
        "tool_call_id",
        "decision_id",
        "arguments_sha256",
        "status",
        "guidance",
        "missing_permissions",
        "retryable",
    }, f"{mode}: stdout keys {sorted(observed)!r}"
    assert isinstance(observed["guidance"], list) and len(observed["guidance"]) <= 5, f"{mode}: guidance"
    assert isinstance(observed["missing_permissions"], list) and len(observed["missing_permissions"]) <= 20
    assert isinstance(observed["retryable"], bool), f"{mode}: retryable {observed['retryable']!r}"
    council = out.result.get("council") or {}
    texts = [m.get("reasoning") for m in council.get("council_members") or [] if isinstance(m, dict)]
    texts += [(council.get("chairman") or {}).get("reasoning"), council.get("error")]
    for text in filter(None, texts):
        assert json.dumps(text)[1:-1] not in cert["stdout"], f"{mode}: free text {text!r} certified"
    ev = cert["evidence"]
    payload = ev["payload"]
    assert ev["public_key_hex"] == PLUGIN_KEY and ev["key_fingerprint"] == qa.fingerprint(qa.PLUGIN_SEED), (
        f"{mode}: certified with key {ev['public_key_hex']!r}"
    )
    want_payload = {
        "executor_id": "nomos-relay-plugin:" + qa.fingerprint(qa.PLUGIN_SEED)[:16],
        "writ_id": "",
        "intent_hash": "",
        "nonce": "",
        "request_id": "call-decided",
        "council_id": qa.COUNCIL_ID,
        "target_agent_id": qa.AGENT_ID,
    }
    for k, v in want_payload.items():
        assert payload.get(k) == v, f"{mode}: evidence payload {k}={payload.get(k)!r}, want {v!r}"
    assert payload["result"]["stdout_sha256"] == qa.sha(cert["stdout"]), f"{mode}: stdout digest mismatch"
    qa.private_key(qa.PLUGIN_SEED).public_key().verify(bytes.fromhex(ev["signature"]), qa.canonical(payload))


# ---- H3: approved ---------------------------------------------------------------------------------


async def test_approved_nomos_http_runs_executor_and_never_next(harness):
    out = await harness.tool("run_shell", _shell_args())
    assert len(harness.executor.calls) == 1, f"executor calls {harness.executor.calls!r}"
    writ_b64, agent = harness.executor.calls[0]
    assert agent == qa.AGENT_ID and qa.decode_writ_b64(writ_b64)["command"] == "echo hi", (
        "executor got wrong writ"
    )
    assert harness.next.calls == [], "approved nomos_http tool also called next_"
    assert out.result.get("execution_status") == "executed", f"result {out.result!r}"
    assert out.result.get("evidence_status") == "locally_verified", f"result {out.result!r}"
    rec = harness.record(out)
    assert rec["kind"] == "executor_result" and rec["certified"] is True, f"stored {rec['kind']!r}"
    _assert_envelope(out.result, "approved nomos_http")


async def test_approved_local_tool_calls_next_with_nomos_stripped(harness):
    args = {"path": "/workspace/a"}
    out = await harness.tool("qa_local", {**args, "nomos": dict(qa.INTENT)})
    assert harness.next.calls == [args], f"next_.call got {harness.next.calls!r}, want [{args!r}]"
    assert harness.executor.calls == [], "local tool reached the executor"
    rec = harness.record(out)
    assert rec["kind"] == "local_result", f"stored {rec['kind']!r}"
    assert "nomos" not in rec["arguments"], f"stored arguments {rec['arguments']!r} kept nomos"
    _assert_envelope(out.result, "approved local")


async def test_tampered_executor_result_is_quarantined_and_blocks_the_session():
    h = await _make(executor=qa.FakeExecutor(mode="tamper"))
    try:
        out = await h.tool("run_shell", _shell_args())
        assert out.result.get("execution_status") == "unknown", f"tampered result: {out.result!r}"
        assert out.result.get("evidence_status") == "invalid", f"tampered result: {out.result!r}"
        assert STDOUT_MARKER not in json.dumps(out.result), (
            f"tampered stdout reached the model: {out.result!r}"
        )
        assert STDOUT_MARKER not in json.dumps(out.annotation), "tampered stdout in annotation"
        await h.tool("run_shell", _shell_args("echo next"))
        assert len(h.council.calls) == 1, "session not blocked: Council called after quarantine"
        assert len(h.executor.calls) == 1, "session not blocked: executor called after quarantine"
    finally:
        h.store.close()


async def test_quarantined_result_response_carries_the_evidence_id_of_its_stored_record():
    # "All model-visible responses carry ... evidence_id (when a record was stored)"; the quarantine is stored.
    h = await _make(executor=qa.FakeExecutor(mode="tamper"))
    try:
        out = await h.tool("run_shell", _shell_args())
        rec = h.record(out)
        assert rec["evidence_status"] == "invalid", f"quarantined record status {rec['evidence_status']!r}"
        listed = qa.nomos_text(await h.model())
        assert rec["evidence_id"] not in listed and STDOUT_MARKER not in listed, "quarantined record listed"
    finally:
        h.store.close()


async def test_executor_exception_is_unknown_outcome_and_blocks_the_session():
    h = await _make(executor=qa.FakeExecutor(mode="raise"))
    try:
        out = await h.tool("run_shell", _shell_args())
        assert out.result.get("execution_status") == "unknown", f"executor raised: {out.result!r}"
        if out.result.get("evidence_id"):
            assert h.record(out)["kind"] == "execution_unknown", (
                "executor exception must store execution_unknown"
            )
        await h.tool("run_shell", _shell_args("echo next"))
        assert len(h.council.calls) == 1, "session not blocked after an unknown execution outcome"
    finally:
        h.store.close()


async def test_untrusted_writ_never_reaches_executor():
    council = qa.FakeCouncil()
    council.writ_seed = qa.STRANGER_SEED
    h = await _make(council=council)
    try:
        out = await h.tool("run_shell", _shell_args())
        assert h.executor.calls == [], f"writ signed by an untrusted key was dispatched: {out.result!r}"
        assert out.result.get("execution_status") != "executed", f"result {out.result!r}"
    finally:
        h.store.close()


async def test_replayed_writ_is_refused_and_nothing_runs():
    council = qa.FakeCouncil(mode="fixed")
    args = {"path": "/workspace/a"}
    envelope, _ = qa.mint_tool_call_writ(
        {
            "agent_id": qa.AGENT_ID,
            "council_id": qa.COUNCIL_ID,
            "tool_call": {
                "schema_version": 1,
                "tool_name": "qa_local",
                "arguments": args,
                "subject": "opaque-qa-user",
            },
        }
    )
    council.fixed_encoded = base64.b64encode(qa.canonical(envelope)).decode("ascii")
    h = await _make(council=council)
    try:
        await h.tool("qa_local", {**args, "nomos": dict(qa.INTENT)}, call_id="call-a")
        out = await h.tool("qa_local", {**args, "nomos": dict(qa.INTENT)}, call_id="call-b")
        assert out.result.get("status") == "writ_replayed", f"second use of one writ: {out.result!r}"
        assert len(h.next.calls) == 1, f"replayed writ ran next_ {len(h.next.calls)} times"
    finally:
        h.store.close()


# ---- H3: journal ----------------------------------------------------------------------------------


async def test_repeated_tool_call_id_with_same_args_replays_without_rerunning(harness):
    first = await harness.tool("run_shell", _shell_args(), call_id="call-same")
    again = await harness.tool("run_shell", _shell_args(), call_id="call-same")
    assert len(harness.council.calls) == 1 and len(harness.executor.calls) == 1, "repeated call id re-ran"
    assert again.result == first.result, f"replay {again.result!r} != first {first.result!r}"


async def test_repeated_tool_call_id_with_different_args_is_refused(harness):
    await harness.tool("run_shell", _shell_args("echo one"), call_id="call-same")
    out = await harness.tool("run_shell", _shell_args("echo two"), call_id="call-same")
    assert len(harness.council.calls) == 1 and len(harness.executor.calls) == 1, (
        f"conflicting id ran: {out.result!r}"
    )
    assert out.result.get("execution_status") != "executed", f"conflicting id result {out.result!r}"


# ---- H3: denial cap -------------------------------------------------------------------------------


async def test_more_than_max_denials_hits_denial_cap_without_council():
    h = await _make(council=qa.FakeCouncil(mode="deny"), max_denials=2)
    try:
        outs = [await h.tool("run_shell", _shell_args(f"echo {i}")) for i in range(4)]
        assert [o.result.get("status") for o in outs[:2]] == ["denied", "denied"], (
            f"{[o.result for o in outs]!r}"
        )
        assert outs[3].result.get("status") == "denial_cap", (
            f"4th call with max_denials=2: {outs[3].result!r}"
        )
        assert len(h.council.calls) <= 3, f"Council called {len(h.council.calls)} times with max_denials=2"
        _assert_envelope(outs[3].result, "denial_cap", stored=False)
    finally:
        h.store.close()


async def test_denial_count_is_consecutive_and_reset_by_approval():
    council = qa.FakeCouncil(mode="deny")
    council.queue = ["deny", "deny", "approve", "deny", "deny"]
    h = await _make(council=council, max_denials=3)
    try:
        for i in range(5):
            await h.tool("run_shell", _shell_args(f"echo {i}"))
        assert len(h.council.calls) == 5, (
            f"deny,deny,approve,deny,deny with max 3 called Council {len(h.council.calls)}x"
        )
    finally:
        h.store.close()


# ---- H3: envelope, annotation, records ------------------------------------------------------------


async def test_guarded_outcomes_carry_nomos_annotation_with_evidence_id_and_timings(harness):
    harness.council.queue = ["approve", "deny"]
    outs = [
        await harness.tool("run_shell", {"command": "x"}),
        await harness.tool("run_shell", _shell_args()),
        await harness.tool("run_shell", _shell_args("echo 2")),
    ]
    for out in outs:
        ann = out.annotation
        assert isinstance(ann, dict) and isinstance(ann.get("nomos"), dict), (
            f"annotation {ann!r} for {out.result!r}"
        )
        assert ann["nomos"].get("evidence_id") == out.result.get("evidence_id"), f"annotation {ann!r}"
        assert "timings_ms" in ann["nomos"], f"annotation lacks timings_ms: {ann!r}"


async def test_every_stored_record_is_signed_and_passes_integrity(harness):
    harness.council.queue = ["approve", "deny", "error", "approve"]
    outs = [
        await harness.tool("run_shell", {"command": "x"}),
        await harness.tool("run_shell", _shell_args()),
        await harness.tool("run_shell", _shell_args("echo 2")),
        await harness.tool("run_shell", _shell_args("echo 3")),
        await harness.tool("qa_local", {"path": "/a", "nomos": dict(qa.INTENT)}),
        await harness.tool("run_shell", _shell_args(evidence=["ev_" + "1" * 32])),
    ]
    for out in outs:
        rec = harness.record(out)
        for k in ("signer", "created_at", "content_sha256", "signature", "receipt"):
            assert k in rec, f"{rec.get('kind')} record lacks {k!r}"
        assert "nomos" not in rec["arguments"], f"{rec['kind']} record arguments kept nomos"
        assert records.integrity_ok(rec, PLUGIN_KEY), (
            f"{rec['kind']} record fails integrity_ok with plugin key"
        )


# ---- H1 -------------------------------------------------------------------------------------------


async def test_h1_raw_path_injects_intent_into_guarded_tools_only(harness):
    req = _raw_request()
    out = await harness.model(req)
    assert out.annotated_request is None, f"raw path returned annotated {out.annotated_request!r}"
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in out.request["content"]["tools"]}
    assert tools["run_shell"]["properties"].get("nomos") == NOMOS_FIELD, "run_shell lacks nomos"
    assert tools["run_shell"]["required"][-1] == "nomos", f"required {tools['run_shell']['required']!r}"
    assert tools["qa_local"]["properties"].get("nomos") == NOMOS_FIELD, "qa_local lacks nomos"
    assert "nomos" not in tools["read_file"]["properties"], "unguarded read_file was given nomos"


async def test_h1_on_a_fresh_conversation_appends_only_the_turn(harness):
    req = _raw_request()
    out = await harness.model(req)
    msgs = out.request["content"]["messages"]
    assert msgs[:-1] == req["content"]["messages"], f"fresh conversation changed its messages: {msgs!r}"
    assert msgs[-1] == {"role": "system", "content": TURN_LINE.format(turn=harness.turn)}, (
        f"fresh conversation must get the turn line alone: {msgs[-1]!r}"
    )


async def test_h1_issues_a_new_turn_on_every_request(harness):
    first, second = qa.turn_token(await harness.model()), qa.turn_token(await harness.model())
    assert first != second, "every model request must get its own turn token"


async def test_h1_appends_catalog_with_id_tool_and_arguments_but_never_output(harness):
    first = await harness.tool("run_shell", _shell_args("echo hi"))
    ev = first.result["evidence_id"]
    out = await harness.model()
    msgs = out.request["content"]["messages"]
    assert msgs[-1]["role"] == "system", f"catalog must be appended as a system message: {msgs[-1]!r}"
    text = msgs[-1]["content"]
    for part in (ev, "run_shell", '{"command":"echo hi"}'):
        assert part in text, f"catalog {text!r} must contain {part!r}"
    assert STDOUT_MARKER not in text, "the catalog leaked tool output"


async def test_h1_catalog_disabled_appends_nothing():
    h = await _make(catalog={"enabled": False})
    try:
        first = await h.tool("run_shell", _shell_args())
        out = await h.model()
        assert out.request["content"]["messages"][-1]["content"] == TURN_LINE.format(turn=h.turn), (
            "catalog appended while disabled"
        )
        assert first.result["evidence_id"] not in json.dumps(out.request["content"]["messages"][-1])
    finally:
        h.store.close()


def _annotated(api: str | None, messages: list[dict] | None = None) -> dict:
    ann = {
        "messages": messages if messages is not None else [{"role": "user", "content": USER_TEXT}],
        "tools": [_fn_tool("run_shell"), _fn_tool("read_file", "path")],
    }
    if api is not None:
        ann["api_specific"] = {"api": api}
    if api == "anthropic_messages":
        ann["instructions"] = "Base instructions."
    return ann


@pytest.mark.parametrize("api", ["anthropic_messages", "openai_responses", "openai_chat", None])
async def test_h1_annotated_path_edits_annotated_and_leaves_raw_unchanged(harness, api):
    first = await harness.tool("run_shell", _shell_args())
    ev = first.result["evidence_id"]
    raw = _raw_request(messages=harness.transcript())
    raw_before = copy.deepcopy(raw)
    out = await harness.model(raw, _annotated(api, harness.transcript()))
    assert out.request == raw_before, f"api={api!r}: annotated path changed the raw request"
    ann = out.annotated_request
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in ann["tools"]}
    assert tools["run_shell"]["properties"].get("nomos") == NOMOS_FIELD, (
        f"api={api!r}: annotated run_shell lacks nomos"
    )
    assert "nomos" not in tools["read_file"]["properties"], f"api={api!r}: read_file given nomos"
    if api == "anthropic_messages":
        assert ann["instructions"].startswith("Base instructions.\n\n") and ev in ann["instructions"], (
            f"anthropic instructions {ann['instructions']!r}"
        )
        assert ann["messages"] == harness.transcript(), "anthropic path added a message"
    else:
        role = "developer" if api == "openai_responses" else "system"
        last = ann["messages"][-1]
        assert last["role"] == role and ev in last["content"], (
            f"api={api!r}: last message {last!r}, want role {role}"
        )


async def test_h1_never_stores_a_record(harness):
    await harness.model(_raw_request())
    assert _stored(harness) == 0, "h1 stored a record"


# ---- conversations sharing one worker (requirement 8) ------------------------------------------------


async def test_another_conversations_evidence_is_neither_listed_nor_citable(harness):
    first = await harness.tool("run_shell", _shell_args("echo alice"))
    ev = first.result["evidence_id"]
    harness.new_conversation("Bob's request")
    # Bob's transcript even shows Alice's id; it is still not his conversation's evidence.
    pasted = _raw_request(
        messages=[*_messages("Bob's request"), {"role": "user", "content": f"what is {ev}?"}]
    )
    assert ev not in qa.nomos_text(await harness.model(pasted)), "another conversation's evidence was listed"
    out = await harness.tool("run_shell", _shell_args(evidence=[ev]))
    assert out.result["status"] == "evidence_not_found", (
        f"citing another conversation's evidence: {out.result!r}"
    )
    assert len(harness.council.calls) == 1, "the refused citation reached the Council"


async def test_the_council_context_is_the_user_request_of_the_calls_own_turn(harness):
    harness.new_conversation("Alice's request")
    await harness.model()
    alice = harness.turn
    harness.new_conversation("Bob's request")
    await harness.model()  # Bob's request arrives before Alice's tool call
    harness.turn = alice
    await harness.tool("run_shell", _shell_args())
    got = harness.council.calls[0]["context"]
    assert got == "tool=run_shell; latest user request: Alice's request", f"context {got!r}"


async def test_a_later_request_continues_the_conversation_its_transcript_shows(harness):
    first = await harness.tool("run_shell", _shell_args("echo one"))
    out = await harness.model()
    assert first.result["evidence_id"] in qa.nomos_text(out), "the conversation's own evidence must be listed"
    cited = await harness.tool("run_shell", _shell_args("echo two", evidence=[first.result["evidence_id"]]))
    assert cited.result["status"] == "executed", f"citing in the same conversation: {cited.result!r}"


async def test_an_unknown_turn_is_refused_and_taught_without_the_council(harness):
    await harness.model()
    harness.turn = "nt_" + "0" * 32  # well formed, but never issued by this worker
    out = await harness.tool("run_shell", _shell_args())
    assert out.result["status"] == "unknown_turn" and "turn" in out.result["message"], f"{out.result!r}"
    assert harness.council.calls == [], "a call without a live turn reached the Council"


async def test_a_missing_turn_is_missing_intent(harness):
    await harness.model()
    ctx = ToolExecutionContext(tool_name="run_shell", args=_shell_args(), tool_call_id="no-turn")
    out = await qa.maybe_await(harness.h3(ctx, harness.next))
    assert out.result["status"] == "missing_intent" and "turn" in out.result["message"], f"{out.result!r}"
    assert harness.council.calls == [], "a call without a turn reached the Council"


async def test_an_unknown_outcome_blocks_only_its_own_conversation():
    h = await _make(executor=qa.FakeExecutor(mode="raise"))
    try:
        first = await h.tool("run_shell", _shell_args())
        assert first.result["execution_status"] == "unknown", f"{first.result!r}"
        blocked = await h.tool("run_shell", _shell_args("echo again"))
        assert blocked.result["status"] == "session_requires_reconciliation", f"{blocked.result!r}"
        h.executor.mode = "ok"
        h.new_conversation("another user")
        other = await h.tool("run_shell", _shell_args())
        assert other.result["status"] == "executed", (
            f"the block reached another conversation: {other.result!r}"
        )
    finally:
        h.store.close()


async def test_denials_count_per_conversation():
    h = await _make(council=qa.FakeCouncil(mode="deny"), max_denials=1)
    try:
        await h.tool("run_shell", _shell_args())
        capped = await h.tool("run_shell", _shell_args("echo again"))
        assert capped.result["status"] == "denial_cap", f"{capped.result!r}"
        h.new_conversation("another user")
        other = await h.tool("run_shell", _shell_args())
        assert other.result["status"] == "denied", f"the cap reached another conversation: {other.result!r}"
    finally:
        h.store.close()


# ---- the plugin's signing key (DESIGN.md "The plugin's signing key") ---------------------------------


async def _without_key() -> Harness:
    raw = _config()
    raw.pop("signing_key")
    return await Harness(raw, qa.FakeCouncil(), qa.FakeExecutor()).start()


def _stored(h: Harness) -> int:
    return h.store.db.execute("SELECT count(*) FROM records").fetchone()[0]


async def test_without_a_signing_key_validate_warns_that_evidence_is_disabled():
    raw = _config()
    raw.pop("signing_key")
    found = NomosPlugin(environ={}).validate(raw)
    assert any(getattr(d, "code", None) == "nomos.config.evidence_disabled" for d in found), found


async def test_without_a_signing_key_the_model_is_offered_no_evidence():
    h = await _without_key()
    try:
        first = await h.tool("run_shell", _shell_args())
        out = await h.model()
        tools = {t["function"]["name"]: t["function"]["parameters"] for t in out.request["content"]["tools"]}
        assert tools["run_shell"]["properties"]["nomos"] == NOMOS_FIELD_WITHOUT_EVIDENCE, (
            "evidence field offered"
        )
        assert qa.nomos_text(out) == TURN_LINE.format(turn=h.turn), "a catalog was offered without a key"
        assert "evidence_id" not in first.result
    finally:
        h.store.close()


async def test_without_a_signing_key_nothing_is_stored_and_executor_output_is_still_verified():
    h = await _without_key()
    try:
        ok = await h.tool("run_shell", _shell_args())
        assert ok.result["status"] == "executed" and ok.result["evidence_status"] == "locally_verified"
        assert STDOUT_MARKER in ok.result["stdout"] and "evidence_id" not in ok.result
        h.council.mode = "deny"
        denied = await h.tool("run_shell", _shell_args("echo again"))
        assert denied.result["status"] == "denied" and "evidence_id" not in denied.result
        missing = await h.tool("run_shell", {"command": "echo x"})
        assert missing.result["status"] == "missing_intent" and "evidence_id" not in missing.result
        assert _stored(h) == 0, "a plugin without a signing key must store no evidence"
    finally:
        h.store.close()


async def test_without_a_signing_key_a_cited_evidence_field_is_refused_before_the_council():
    h = await _without_key()
    try:
        out = await h.tool("run_shell", _shell_args(evidence=[qa.evidence_id()]))
        assert out.result["status"] == "missing_intent", f"{out.result!r}"
        assert h.council.calls == [], "a call citing evidence reached the Council without a key"
    finally:
        h.store.close()


async def test_without_a_signing_key_a_tampered_executor_result_is_withheld_and_blocks():
    h = await _without_key()
    h.executor.mode = "tamper"
    try:
        bad = await h.tool("run_shell", _shell_args())
        assert bad.result["status"] == "evidence_invalid" and "stdout" not in bad.result, f"{bad.result!r}"
        h.executor.mode = "ok"
        after = await h.tool("run_shell", _shell_args("echo after"))
        assert after.result["status"] == "session_requires_reconciliation", f"{after.result!r}"
        assert _stored(h) == 0
    finally:
        h.store.close()
