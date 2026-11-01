# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `records`: evidence records, citation, integrity, catalog entries, response envelope."""

from __future__ import annotations

import copy
import json

import pytest
import qa_support as qa

from nemo_relay_nomos import records
from nemo_relay_nomos.signing import PluginSigner, verify_plugin_signature

EVERY_KEY = {
    "evidence_id",
    "kind",
    "tool_name",
    "tool_call_id",
    "arguments",
    "arguments_sha256",
    "created_at",
    "content",
    "content_sha256",
    "signer",
    "signature",
    "receipt",
    "certified",
    "evidence_status",
    "writ_id",
    "decision_id",
    "policy_version",
}
NOW = 1_800_000_000.0
STDOUT = "RECORD_STDOUT_MARKER_c0ffee\n"


@pytest.fixture
def signer():
    return PluginSigner(qa.PLUGIN_SEED)


def _executor_record(signer, *, verified=True, args=None, result=None, writ=None):
    writ = writ or qa.mint_writ("echo hi")
    result = result or qa.signed_result(writ, stdout=STDOUT)
    return (
        records.executor_record(
            evidence_id=qa.evidence_id(),
            tool_name="run_shell",
            tool_call_id="call-exec",
            arguments=args if args is not None else {"command": "echo hi"},
            result=result,
            writ=writ,
            verified=verified,
            verification_error=None if verified else "stdout_hash_mismatch",
            receipt_signer=signer,
            policy_version="qa-trust-1",
            now=NOW,
        ),
        result,
        writ,
    )


def _plugin_record(
    signer, *, kind="local_result", content=None, args=None, decision_id=None, writ_id=None, **certified
):
    return records.plugin_record(
        evidence_id=qa.evidence_id(),
        kind=kind,
        tool_name="qa_local",
        tool_call_id="call-local",
        arguments=args if args is not None else {"path": "/workspace/x"},
        content=content if content is not None else {"output": "LOCAL_MARKER"},
        signer=signer,
        decision_id=decision_id,
        writ_id=writ_id,
        now=NOW,
        **certified,
    )


def _certified_result(signer, stdout="CERTIFIED_STDOUT_MARKER_b7"):
    """An `ExecutionResult` the plugin certified (DESIGN.md `signing`: `PluginSigner.certify`)."""
    return signer.certify(
        stdout,
        command="qa observation",
        agent_id=qa.AGENT_ID,
        council_id=qa.COUNCIL_ID,
        request_id="call-local",
    )


# ---- KINDS and response ---------------------------------------------------------------------------


def test_kinds_are_the_specified_tuple():
    want = (
        "executor_result",
        "local_result",
        "council_denial",
        "council_error",
        "execution_unknown",
        "plugin_refusal",
    )
    assert want == records.KINDS, f"KINDS is {records.KINDS!r}"


def test_response_defaults_carry_the_envelope():
    got = records.response("missing_intent")
    assert got.get("contract_version") == "nomos-plugin/v1", f"response('missing_intent') -> {got!r}"
    assert got.get("status") == "missing_intent", f"status in {got!r}"
    assert got.get("execution_status") == "not_executed", f"default execution_status in {got!r}"
    assert got.get("evidence_status") == "unavailable", f"default evidence_status in {got!r}"


def test_response_passes_statuses_and_fields_through():
    got = records.response(
        "ok", execution="executed", evidence="locally_verified", evidence_id="ev_x", extra=1
    )
    for k, v in {
        "status": "ok",
        "execution_status": "executed",
        "evidence_status": "locally_verified",
        "evidence_id": "ev_x",
        "extra": 1,
    }.items():
        assert got.get(k) == v, f"response(... {k}) -> {got!r}"


# ---- every record ---------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["executor-verified", "executor-unverified", "plugin"])
def test_every_record_has_the_specified_keys(signer, which):
    if which == "plugin":
        rec = _plugin_record(signer)
    else:
        rec, _, _ = _executor_record(signer, verified=which == "executor-verified")
    missing = EVERY_KEY - set(rec)
    assert not missing, f"{which} record lacks {sorted(missing)!r}"
    assert set(rec["signer"]) >= {"type", "id", "key_fingerprint", "public_key_hex"}, (
        f"signer {rec['signer']!r}"
    )


@pytest.mark.parametrize("which", ["executor", "plugin"])
def test_record_content_sha256_is_sha_of_canonical_content(signer, which):
    rec = _plugin_record(signer) if which == "plugin" else _executor_record(signer)[0]
    want = qa.sha(qa.canonical(rec["content"]))
    assert rec["content_sha256"] == want, f"{which} content_sha256 {rec['content_sha256']!r}, want {want!r}"


@pytest.mark.parametrize("which", ["executor", "plugin"])
def test_record_receipt_is_plugin_signature_over_content_sha256(signer, which):
    rec = _plugin_record(signer) if which == "plugin" else _executor_record(signer)[0]
    receipt = rec["receipt"]
    assert verify_plugin_signature(qa.public_hex(qa.PLUGIN_SEED), receipt), (
        f"{which} receipt {receipt!r} invalid"
    )
    assert receipt["signed"]["content_sha256"] == rec["content_sha256"], f"{which} receipt signs {receipt!r}"
    assert receipt["signed"]["evidence_id"] == rec["evidence_id"], f"{which} receipt evidence_id {receipt!r}"


@pytest.mark.parametrize("which", ["executor", "plugin"])
def test_record_arguments_never_contain_nomos(signer, which):
    args = {"command": "echo hi", "nomos": dict(qa.INTENT)}
    rec = _plugin_record(signer, args=args) if which == "plugin" else _executor_record(signer, args=args)[0]
    assert "nomos" not in rec["arguments"], f"{which} record arguments {rec['arguments']!r} kept nomos"
    assert len(rec["arguments_sha256"]) == 64, f"arguments_sha256 {rec['arguments_sha256']!r}"


def test_record_created_at_is_now(signer):
    assert _plugin_record(signer)["created_at"] == NOW
    assert _executor_record(signer)[0]["created_at"] == NOW


# ---- executor records -----------------------------------------------------------------------------


def test_executor_record_content_is_the_original_result(signer):
    rec, result, _ = _executor_record(signer)
    assert rec["content"] == result, (
        "executor record content must be the original ExecutionResult, unmodified"
    )


def test_executor_record_signer_is_the_executor(signer):
    rec, result, _ = _executor_record(signer)
    s = rec["signer"]
    assert s["type"] == "executor", f"signer.type {s['type']!r}"
    assert s["id"] == result["executor_id"], f"signer.id {s['id']!r}, executor {result['executor_id']!r}"
    assert s["public_key_hex"] == qa.public_hex(qa.EXECUTOR_SEED), f"signer key {s['public_key_hex']!r}"
    assert s["key_fingerprint"] == qa.fingerprint(qa.EXECUTOR_SEED), (
        f"signer fingerprint {s['key_fingerprint']!r}"
    )


def test_executor_record_signature_is_the_executor_evidence_signature(signer):
    rec, result, _ = _executor_record(signer)
    want = result["evidence"]["signature"]
    sig = rec["signature"]
    assert sig == want or (isinstance(sig, dict) and sig.get("value") == want), (
        f"executor record signature {sig!r} is not the executor evidence signature {want!r}"
    )


def test_executor_record_certified_only_when_verified(signer):
    ok, _, _ = _executor_record(signer, verified=True)
    bad, _, _ = _executor_record(signer, verified=False)
    assert ok["certified"] is True and ok["evidence_status"] == "locally_verified", (
        f"verified record: certified={ok['certified']!r} status={ok['evidence_status']!r}"
    )
    assert bad["certified"] is False and bad["evidence_status"] == "invalid", (
        f"unverified record: certified={bad['certified']!r} status={bad['evidence_status']!r}"
    )


def test_executor_record_carries_writ_and_policy(signer):
    rec, _, writ = _executor_record(signer)
    assert rec["writ_id"] == writ["writ_id"], f"writ_id {rec['writ_id']!r}, writ {writ['writ_id']!r}"
    assert rec["policy_version"] == "qa-trust-1", f"policy_version {rec['policy_version']!r}"
    assert rec["kind"] == "executor_result", f"kind {rec['kind']!r}"


# ---- plugin records -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["local_result", "council_denial", "council_error", "execution_unknown", "plugin_refusal"]
)
def test_plugin_record_is_signed_by_the_plugin(signer, kind):
    rec = _plugin_record(signer, kind=kind, decision_id="dec-1", writ_id="w-1")
    assert rec["kind"] == kind, f"kind {rec['kind']!r} for {kind!r}"
    ident = PluginSigner(qa.PLUGIN_SEED).identity
    for k in ("type", "id", "key_fingerprint", "public_key_hex"):
        assert rec["signer"][k] == ident[k], f"{kind} signer.{k} {rec['signer'][k]!r}, want {ident[k]!r}"
    # Spec clarification (architect, 2026-10-12): the receipt is the signature plus the signer identity.
    receipt = {k: v for k, v in rec["receipt"].items() if k != "signer"}
    assert rec["signature"] == receipt, f"{kind}: signature must equal the receipt minus its signer"
    assert rec["receipt"]["signer"] == rec["signer"], f"{kind}: receipt signer must be the plugin"
    assert (rec["decision_id"], rec["writ_id"]) == ("dec-1", "w-1"), f"{kind}: ids {rec['decision_id']!r}"


# Spec change (every citation certified): only `plugin_refusal` and `execution_unknown` stay uncertified.
@pytest.mark.parametrize("kind", ["execution_unknown", "plugin_refusal"])
def test_refusal_and_unknown_plugin_records_are_never_certified(signer, kind):
    rec = _plugin_record(signer, kind=kind)
    assert rec["certified"] is False, f"{kind}: plugin record certified={rec['certified']!r}"
    assert rec["evidence_status"] == "plugin_signed", f"{kind}: evidence_status {rec['evidence_status']!r}"


@pytest.mark.parametrize("kind", ["local_result", "council_denial", "council_error"])
def test_certified_plugin_record_carries_its_certified_result(signer, kind):
    result = _certified_result(signer)
    rec = _plugin_record(signer, kind=kind, certified_result=result)
    assert rec["certified"] is True, f"{kind}: certified plugin record certified={rec['certified']!r}"
    assert rec["evidence_status"] == "plugin_certified", f"{kind}: evidence_status {rec['evidence_status']!r}"
    assert rec.get("certified_result") == result, f"{kind}: record must carry the certified_result whole"
    assert records.integrity_ok(rec, qa.public_hex(qa.PLUGIN_SEED)) is True, f"fresh certified {kind} failed"


@pytest.mark.parametrize("kind", ["local_result", "council_denial", "council_error"])
def test_certified_result_is_covered_by_the_receipt(signer, kind):
    rec = _plugin_record(signer, kind=kind, certified_result=_certified_result(signer))
    bad = copy.deepcopy(rec)
    bad["certified_result"]["stdout"] = "FORGED_AFTER_SIGNING"
    assert records.integrity_ok(bad) is False, f"{kind}: tampered certified_result passed integrity"


# ---- citation -------------------------------------------------------------------------------------
# Spec change (every citation certified): `citation(record) -> dict | None`, the `certified_evidence` item.


def test_citation_of_certified_executor_record_is_the_full_original(signer):
    rec, result, _ = _executor_record(signer, verified=True)
    got = records.citation(rec)
    assert got == result, f"citation of a certified executor record must be the full original, got {got!r}"


@pytest.mark.parametrize("kind", ["local_result", "council_denial", "council_error"])
def test_citation_of_certified_plugin_record_is_its_certified_result(signer, kind):
    result = _certified_result(signer)
    rec = _plugin_record(signer, kind=kind, certified_result=result)
    got = records.citation(rec)
    assert got == result, f"citation of a certified {kind} must be its certified_result, got {got!r}"


def test_citation_of_unverified_executor_record_is_none(signer):
    rec, _, _ = _executor_record(signer, verified=False)
    got = records.citation(rec)
    assert got is None, f"an unverified executor record must not be citable, citation() -> {got!r}"


@pytest.mark.parametrize("kind", ["execution_unknown", "plugin_refusal"])
def test_citation_of_refusal_or_unknown_record_is_none(signer, kind):
    rec = _plugin_record(signer, kind=kind, content={"output": "NOT_CITABLE_MARKER"})
    got = records.citation(rec)
    assert got is None, f"a {kind} record can never be cited, citation() -> {got!r}"


# ---- integrity_ok ---------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["executor", "plugin"])
def test_integrity_ok_true_for_a_fresh_record(signer, which):
    rec = _plugin_record(signer) if which == "plugin" else _executor_record(signer)[0]
    assert records.integrity_ok(rec) is True, f"fresh {which} record failed integrity"
    assert records.integrity_ok(rec, qa.public_hex(qa.PLUGIN_SEED)) is True, f"{which}: own key rejected"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: r["content"].__setitem__("injected", True),
        lambda r: r.__setitem__("content_sha256", "0" * 64),
        lambda r: r["receipt"].__setitem__("value", "00" * 64),
        lambda r: r["receipt"]["signed"].__setitem__("content_sha256", "1" * 64),
        lambda r: r.__delitem__("receipt"),
        lambda r: r.__delitem__("content"),
    ],
    ids=["content", "content_sha256", "receipt-value", "receipt-signed", "no-receipt", "no-content"],
)
@pytest.mark.parametrize("which", ["executor", "plugin"])
def test_integrity_ok_false_after_tampering(signer, which, tamper):
    rec = _plugin_record(signer) if which == "plugin" else _executor_record(signer)[0]
    bad = copy.deepcopy(rec)
    tamper(bad)
    assert records.integrity_ok(bad) is False, f"tampered {which} record passed integrity"


def test_integrity_ok_with_expected_key_rejects_a_receipt_by_another_plugin_key(signer):
    other = PluginSigner(qa.STRANGER_SEED)
    rec = _plugin_record(other)
    assert records.integrity_ok(rec) is True, "self-consistent record by another key must pass without a key"
    assert records.integrity_ok(rec, qa.public_hex(qa.PLUGIN_SEED)) is False, (
        "record receipted by STRANGER key passed integrity_ok(expected=PLUGIN key)"
    )


@pytest.mark.parametrize("value", [None, {}, [], "x", {"content": {}, "content_sha256": 5, "receipt": "r"}])
def test_integrity_ok_never_raises(value):
    try:
        got = records.integrity_ok(value)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"integrity_ok({value!r}) raised {exc!r}")
    assert got is False, f"integrity_ok({value!r}) -> {got!r}"


# ---- catalog_entry --------------------------------------------------------------------------------


def test_catalog_entry_has_the_specified_fields_and_no_output(signer):
    rec, _, _ = _executor_record(signer)
    entry = records.catalog_entry(rec, 400)
    want_keys = {"evidence_id", "tool_name", "arguments", "outcome", "certified", "created_at"}
    assert set(entry) == want_keys, f"catalog entry keys {sorted(entry)!r}"
    assert (entry["evidence_id"], entry["tool_name"], entry["certified"], entry["created_at"]) == (
        rec["evidence_id"],
        "run_shell",
        True,
        NOW,
    ), f"catalog entry {entry!r}"
    assert entry["outcome"] in {"executed", "failed", "denied", "error", "not_executed"}, f"outcome {entry!r}"
    assert "RECORD_STDOUT_MARKER" not in json.dumps(entry), f"catalog entry leaked output: {entry!r}"


def test_catalog_entry_bounds_arguments(signer):
    long = "A" * 5000
    rec = _plugin_record(signer, args={"blob": long})
    entry = records.catalog_entry(rec, 400)
    assert isinstance(entry["arguments"], dict), f"arguments must stay a dict: {entry['arguments']!r}"
    assert long not in json.dumps(entry["arguments"]), (
        "5000-char argument not bounded at max_argument_chars=400"
    )
