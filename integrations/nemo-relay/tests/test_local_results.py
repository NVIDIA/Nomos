# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""With a signing_key, a guarded local tool's result is certified by the plugin's key."""

import base64
import json
from copy import deepcopy

import pytest
from support import INTENT, LOCAL_TOOL, TURN, Next, base_config, call, history, nomos_message
from test_tool_definitions import nomos_contract

from nemo_relay_nomos.records import integrity_ok
from nemo_relay_nomos.settings import Settings, diagnostics
from nemo_relay_nomos.signing import PluginSigner
from nemo_relay_nomos.tool_call import canonical_tool_arguments
from nemo_relay_nomos.verify import EvidenceError, verify_plugin_certified
from nemo_relay_nomos.wire import sha256_hex

GUARDED = {"include": ["read_file"]}


def local_call(p, call_id="c1", **kw):
    """H1 for a request offering the local tool, then the model's call to it in that turn."""
    out = p.h1(
        "model", {"content": {"messages": [{"role": "user", "content": "go"}], "tools": [LOCAL_TOOL]}}, None
    )
    return call(
        turn=TURN.search(nomos_message(out))[0], name="read_file", call_id=call_id, path="/etc/hosts", **kw
    )


def writ_payload(council):
    return json.loads(base64.b64decode(council.last_writ["payload_base64"]))


def certified_result(p, data):
    return p.store.get(data["evidence_id"])["certified_result"]


# ── settings ─────────────────────────────────────────────────────────────


def test_the_removed_option_is_diagnosed():
    # Local results are always certified with a signing_key: plugin_signed ones would be asserted evidence.
    (problem,) = diagnostics(base_config(local_results="certified"))
    assert "'local_results' was unexpected" in problem.message


# ── certified ────────────────────────────────────────────────────────────


async def test_the_result_is_certified_and_bound_to_the_tool_its_arguments_and_the_writ(setup):
    p, council, *_ = setup(guard=GUARDED)
    data = (await p.h3(local_call(p, call_id="c-4"), Next(result={"text": "file body"}))).result
    assert data["status"] == "executed" and data["output"] == {"text": "file body"}
    assert data["evidence_status"] == "plugin_certified"
    record = p.store.get(data["evidence_id"])
    assert record["kind"] == "local_result" and record["certified"]
    assert record["evidence_status"] == "plugin_certified" and integrity_ok(record, p.signer.public_key_hex)

    result = record["certified_result"]
    writ = writ_payload(council)
    payload = result["evidence"]["payload"]
    signer = PluginSigner(bytes.fromhex(base_config()["signing_key"]))
    assert result["executor_id"] == payload["executor_id"] == signer.executor_id
    assert result["writ_id"] == payload["writ_id"] == writ["writ_id"] == data["writ_id"]
    assert (payload["intent_hash"], payload["nonce"]) == (writ["intent_hash"], writ["nonce"])
    assert (payload["council_id"], payload["target_agent_id"], payload["request_id"]) == (
        "council",
        "agent",
        "c-4",
    )
    assert result["success"] is True and result["error"] is None
    arguments = canonical_tool_arguments({"path": "/etc/hosts"})
    assert sha256_hex(arguments) == writ["operation"]["arguments_sha256"]
    assert (
        "read_file" in payload["command"]["command"] and arguments.decode() in payload["command"]["command"]
    )
    assert json.loads(result["stdout"]) == {
        "type": "nomos.local_tool_result",
        "version": 1,
        "tool": "read_file",
        "tool_call_id": "c-4",
        "writ_id": writ["writ_id"],
        "arguments_sha256": writ["operation"]["arguments_sha256"],
        "status": "executed",
        "output": {"text": "file body"},
    }
    assert result["stdout"].isascii()


async def test_a_near_limit_result_fits_the_space_held_back_before_dispatch(setup):
    # Quotes are escaped once in the record and twice in the certified copy's stdout: the worst case.
    limits = {"max_result_bytes": 65536, "max_output_chars": 256, "max_storage_bytes": 4_194_304}
    p, *_ = setup(guard=GUARDED, limits=limits)
    output = '"' * 32766  # 65534 canonical bytes: just within max_result_bytes, so kept whole
    data = (await p.h3(local_call(p), Next(result=output))).result
    assert data["output"] == output
    (size,) = p.store.db.execute(
        "SELECT length(record) FROM records WHERE id=?", (data["evidence_id"],)
    ).fetchone()
    assert size <= p.settings.limits.reserve_bytes


def test_a_signing_key_holds_back_the_certified_copy_too():
    cfg = base_config()
    del cfg["signing_key"]  # without a key nothing is stored or certified
    plain = Settings.from_config(cfg).limits.reserve_bytes
    assert Settings.from_config(base_config()).limits.reserve_bytes > 2 * plain


async def test_a_failed_tool_is_certified_as_failed(setup):
    p, *_ = setup(guard=GUARDED)
    data = (await p.h3(local_call(p), Next(error=OSError("boom")))).result
    result = certified_result(p, data)
    assert data["status"] == "execution_failed" and result["success"] is False
    assert result["error"] == "OSError: boom" == result["evidence"]["payload"]["result"]["error"]
    assert json.loads(result["stdout"])["status"] == "failed"


async def test_nomos_parses_and_verifies_the_result_with_its_own_contract(setup):
    contract = nomos_contract()
    p, *_ = setup(guard=GUARDED)
    sent = certified_result(p, (await p.h3(local_call(p), Next(result="ok"))).result)
    result = contract.ExecutionResult.model_validate(sent)
    evidence = result.evidence
    assert evidence.verify(), "Nomos's ExecutionEvidence.verify() over its own canonical bytes"
    # The rest of nomos/nat/functions.py _validate_certified_evidence_result and
    # _validate_evidence_payload_matches_result, which a registered key then passes.
    assert evidence.payload.executor_key_fingerprint == evidence.key_fingerprint
    assert (evidence.payload.council_id, evidence.payload.target_agent_id) == ("council", "agent")
    assert result.executor_id == evidence.payload.executor_id and result.writ_id == evidence.payload.writ_id
    assert sha256_hex(result.stdout.encode()) == evidence.payload.result.stdout_sha256
    assert sha256_hex(result.stderr.encode()) == evidence.payload.result.stderr_sha256
    for name in ("success", "exit_code", "error", "verification_stage"):
        assert getattr(result, name) == getattr(evidence.payload.result, name)


async def test_non_ascii_arguments_and_output_still_verify_under_nomos(setup):
    contract = nomos_contract()
    p, *_ = setup(guard=GUARDED)
    out = p.h1(
        "model", {"content": {"messages": [{"role": "user", "content": "go"}], "tools": [LOCAL_TOOL]}}, None
    )
    ctx = call(turn=TURN.search(nomos_message(out))[0], name="read_file", path="/sandbox/résumé — 日本.txt")
    sent = certified_result(p, (await p.h3(ctx, Next(result="naïve ✓"))).result)
    assert "résumé — 日本" in sent["evidence"]["payload"]["command"]["command"]
    assert contract.ExecutionResult.model_validate(sent).evidence.verify()


class Opaque:
    """A tool result that is not JSON: the plugin keeps its repr."""

    def __repr__(self):
        return "x" * 100_000


async def test_a_non_json_output_is_bounded_like_a_json_one(setup):
    limits = {"max_result_bytes": 65536, "max_output_chars": 256, "max_storage_bytes": 4_194_304}
    p, *_ = setup(guard=GUARDED, limits=limits)
    data = (await p.h3(local_call(p), Next(result=Opaque()))).result
    assert data["output"].startswith('"xxx') and "truncated" in data["output"]
    assert len(data["output"]) < 300
    record = p.store.get(data["evidence_id"])
    assert record["content"]["output"] == data["output"]
    assert len(record["certified_result"]["stdout"]) < 1000


async def test_a_cited_certified_local_result_goes_to_the_council_as_certified_evidence(setup):
    p, council, *_ = setup(guard=GUARDED)
    first = local_call(p)
    data = (await p.h3(first, Next(result="manifest: disposable"))).result
    messages = history("go", (first, data))
    out = p.h1("model", {"content": {"messages": messages, "tools": [LOCAL_TOOL]}}, None)
    message = nomos_message(out)
    assert f"{data['evidence_id']} | read_file | executed | certified |" in message, "listed as certified"
    second = call(
        intent={**INTENT, "evidence": [data["evidence_id"]]},
        turn=TURN.search(message)[0],
        name="read_file",
        call_id="c2",
        path="/etc/hosts",
    )
    later = await p.h3(second, Next())
    sent = council.calls[1]
    assert sent["certified_evidence"] == [certified_result(p, data)] and sent["evidence"] == []
    assert later.annotation["nomos"]["submitted"]["certified_citations"] == [data["evidence_id"]]


async def test_a_cited_certified_result_that_fails_the_recheck_is_refused_before_the_council(
    setup, monkeypatch
):
    p, council, *_ = setup(guard=GUARDED)
    first = local_call(p)
    data = (await p.h3(first, Next(result="manifest: disposable"))).result
    out = p.h1("model", {"content": {"messages": history("go", (first, data)), "tools": [LOCAL_TOOL]}}, None)
    second = call(
        intent={**INTENT, "evidence": [data["evidence_id"]]},
        turn=TURN.search(nomos_message(out))[0],
        name="read_file",
        call_id="c2",
        path="/etc/hosts",
    )
    altered = deepcopy(certified_result(p, data))
    altered["stdout"] = altered["stdout"].replace("manifest: disposable", "tampered")
    # A record that passes its integrity check but whose certified copy does not re-verify.
    monkeypatch.setattr("nemo_relay_nomos.plugin.citation", lambda record: altered)
    later = await p.h3(second, Next())
    assert later.result["status"] == "output_digest_mismatch"
    assert len(council.calls) == 1, "the Council is not asked"


async def test_the_shell_executor_path_is_unaffected(setup):
    p, council, executor, _ = setup()
    out = p.h1("model", {"content": {"messages": [], "tools": [LOCAL_TOOL]}}, None)
    data = (await p.h3(call(turn=TURN.search(nomos_message(out))[0]), Next())).result
    record = p.store.get(data["evidence_id"])
    assert record["kind"] == "executor_result" and record["content"] == executor.results[0]
    assert "certified_result" not in record


async def test_without_a_signing_key_nothing_is_stored_or_certified(setup):
    cfg = base_config(guard=GUARDED)
    del cfg["signing_key"]
    p, *_ = setup(config=cfg)
    data = (await p.h3(local_call(p), Next())).result
    assert data["status"] == "executed" and "evidence_id" not in data
    assert p.store.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0


# ── the re-check made when it is cited ───────────────────────────────────


async def certified(setup):
    p, *_ = setup(guard=GUARDED)
    return p, certified_result(p, (await p.h3(local_call(p), Next(result="ok"))).result)


def recheck(p, result, writ_id, **kw):
    verify_plugin_certified(
        result,
        kw.get("key", p.signer.public_key_hex),
        executor_id=kw.get("executor_id", p.signer.executor_id),
        agent_id="agent",
        council_id=kw.get("council_id", "council"),
        writ_id=writ_id,
    )


async def test_the_recheck_accepts_what_the_plugin_signed(setup):
    p, result = await certified(setup)
    recheck(p, result, result["writ_id"])


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(stdout=r["stdout"].replace("ok", "no")), "output_digest_mismatch"),
        (lambda r: r.update(success=False), "result_field_mismatch"),
        (lambda r: r["evidence"]["payload"].update(writ_id="other"), "invalid_signature"),
        (lambda r: r.update(writ_id="other"), "result_identity_mismatch"),
        (lambda r: r.pop("evidence"), "invalid_evidence_contract"),
    ],
)
async def test_the_recheck_refuses_an_altered_result(setup, change, reason):
    p, result = await certified(setup)
    altered = deepcopy(result)
    change(altered)
    with pytest.raises(EvidenceError, match=reason):
        recheck(p, altered, result["writ_id"])


async def test_the_recheck_refuses_another_key_or_council(setup):
    p, result = await certified(setup)
    other = PluginSigner(bytes([7]) * 32)
    with pytest.raises(EvidenceError, match="untrusted_executor"):
        recheck(p, result, result["writ_id"], key=other.public_key_hex)
    with pytest.raises(EvidenceError, match="result_identity_mismatch"):
        recheck(p, result, result["writ_id"], executor_id=other.executor_id)
    with pytest.raises(EvidenceError, match="evidence_writ_mismatch"):
        recheck(p, result, result["writ_id"], council_id="another")
    with pytest.raises(EvidenceError, match="result_identity_mismatch"):
        recheck(p, result, "another record's writ")
