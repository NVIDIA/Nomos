# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from support import (
    COUNCIL_KEY,
    EXECUTOR_KEY,
    WRIT_FIELDS,
    Next,
    base_config,
    call,
    make_tool_call_writ,
    make_writ,
    public,
    sign_result,
)

from nemo_relay_nomos.verify import (
    EvidenceError,
    decode_tool_call_writ,
    verify_executor_evidence,
    verify_tool_call_writ,
)
from nemo_relay_nomos.wire import canonical, sha256_hex


def sample():
    writ = make_writ(
        dict(agent_id="agent", council_id="council", command="echo hello", working_directory="/workspace")
    )
    result = sign_result(writ)
    return writ, result, base_config()["trust"]


def verify(result, writ, trust):
    verify_executor_evidence(result, writ, trust, agent_id="agent", council_id="council")


def resign(result):
    ev = result["evidence"]
    ev["signature"] = EXECUTOR_KEY.sign(canonical(ev["payload"])).hex()


@pytest.mark.parametrize(
    "path,value",
    [
        (("stdout",), "forged"),
        (("stderr",), "forged"),
        (("success",), False),
        (("exit_code",), 1),
        (("duration_seconds",), 0.2),
        (("error",), "forged"),
        (("verification_stage",), "failed"),
        (("executor_id",), "different"),
        (("writ_id",), "different"),
        (("contract_version",), "v2"),
        (("evidence", "algorithm"), "rsa"),
        (("evidence", "key_fingerprint"), "a" * 64),
        (("evidence", "signature"), "0" * 128),
        (("evidence", "public_key_hex"), public(COUNCIL_KEY)),
    ],
)
def test_outer_field_tamper_fails(path, value):
    writ, result, trust = sample()
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(EvidenceError):
        verify(result, writ, trust)


@pytest.mark.parametrize(
    "path,value",
    [
        (("executor_id",), "different"),
        (("executor_key_fingerprint",), "b" * 64),
        (("writ_id",), "different"),
        (("council_id",), "different"),
        (("target_agent_id",), "different"),
        (("intent_hash",), "b" * 64),
        (("nonce",), "b" * 64),
        (("writ_signature",), "0" * 128),
        (("command", "command"), "rm -rf /"),
        (("command", "working_directory"), "/"),
        (("result", "stdout_sha256"), "b" * 64),
        (("result", "stderr_sha256"), "b" * 64),
        (("result", "stdout_bytes"), 200),
        (("result", "stderr_bytes"), 200),
        (("evidence_version",), "v2"),
        (("signed_at",), 0),
        (("attachments",), [{"path": "invented"}]),
    ],
)
def test_signed_but_misbound_payload_fails(path, value):
    writ, result, trust = sample()
    target = result["evidence"]["payload"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    resign(result)
    with pytest.raises(EvidenceError):
        verify(result, writ, trust)


@pytest.mark.parametrize(
    "key", ["stdout_sha256", "stderr_sha256", "stdout_bytes", "stderr_bytes", "success", "error"]
)
def test_missing_signed_fields_never_default_to_verified(key):
    writ, result, trust = sample()
    del result["evidence"]["payload"]["result"][key]
    resign(result)
    with pytest.raises(EvidenceError):
        verify(result, writ, trust)


def test_valid_self_signed_executor_is_not_trusted():
    writ, _, trust = sample()
    with pytest.raises(EvidenceError, match="untrusted_executor"):
        verify(sign_result(writ, key=COUNCIL_KEY), writ, trust)


def test_trust_expiry_and_nan_fail_closed():
    writ, result, trust = sample()
    trust["valid_until"] = time.time() - 1
    with pytest.raises(EvidenceError, match="trust_policy_expired"):
        verify(result, writ, trust)
    trust["valid_until"] = time.time() + 30
    result["duration_seconds"] = float("nan")
    with pytest.raises(EvidenceError):
        verify(result, writ, trust)


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "changed"),
        ("working_directory", "/elsewhere"),
        ("target_agent_id", "other"),
        ("council_id", "other"),
        ("writ_id", "other"),
        ("max_executions", 2),
        ("expires_at", 0),
    ],
)
async def test_bad_writ_never_reaches_executor(setup, field, value):
    p, c, e, _ = setup()
    original = c.authorize

    async def alter(**fields):
        decision = await original(**fields)
        writ = json.loads(base64.b64decode(decision["writ"]))
        writ[field] = value
        writ["signature"] = COUNCIL_KEY.sign(canonical({k: writ[k] for k in WRIT_FIELDS})).hex()
        decision["writ"] = base64.b64encode(canonical(writ)).decode()
        return decision

    c.authorize = alter
    result = await p.h3(call(), Next())
    assert result.result["execution_status"] == "not_executed" and not e.calls


def test_wire_canonicalization_matches_pinned_nomos_contract():
    path = next(
        (
            candidate
            for parent in Path(__file__).resolve().parents
            # the enclosing Nomos (integrations/nemo-relay), or the pinned submodule (standalone repository)
            for candidate in (
                parent / "trusted_executor_contract/models.py",
                parent / "vendor/nomos/trusted_executor_contract/models.py",
            )
            if candidate.exists()
        ),
        None,
    )
    if path is None:
        pytest.skip(
            "Contract compatibility check requires the Nomos source (the enclosing Nomos, or vendor/nomos)"
        )
    spec = importlib.util.spec_from_file_location("nomos_contract_compat", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    writ, result, trust = sample()
    actual_writ = module.WritOfExecution.model_validate(writ)
    actual_result = module.ExecutionResult.model_validate(result)
    assert actual_writ.verify(bytes.fromhex(public(COUNCIL_KEY)))
    assert actual_result.evidence.verify()
    assert actual_writ.canonical_bytes() == canonical({k: writ[k] for k in WRIT_FIELDS})
    assert actual_result.evidence.payload.canonical_bytes() == canonical(result["evidence"]["payload"])
    verify(actual_result.model_dump(mode="json"), actual_writ.model_dump(mode="json"), trust)


def structured_sample():
    operation = {
        "kind": "tool_call",
        "schema_version": 1,
        "tool_name": "slack--send_message",
        "arguments_sha256": sha256_hex(b'{"channel":"me"}'),
        "canonicalization": "nomos-json-v1",
        "subject": "opaque-user",
    }
    fields = {
        "agent_id": "agent",
        "council_id": "council",
        "tool_call": {
            "schema_version": 1,
            "tool_name": "slack--send_message",
            "arguments": {"channel": "me"},
            "subject": "opaque-user",
        },
    }
    outer = make_tool_call_writ(fields)
    encoded = base64.b64encode(canonical(outer)).decode()
    payload = json.loads(base64.b64decode(outer["payload_base64"]))
    return encoded, outer, payload, operation, base_config()["trust"]


def test_valid_structured_writ_verifies_original_payload_bytes():
    encoded, _, payload, operation, trust = structured_sample()
    outer = decode_tool_call_writ(encoded)
    verified = verify_tool_call_writ(
        outer,
        trust,
        agent_id="agent",
        council_id="council",
        operation=operation,
        writ_id=payload["writ_id"],
        dispatch=True,
        issued_after=payload["issued_at"],
    )
    assert verified["operation"] == operation and verified["nonce"] == payload["nonce"]


def test_structured_writ_signature_accepts_noncanonical_original_bytes():
    _, outer, payload, operation, trust = structured_sample()
    payload_bytes = json.dumps(payload, indent=2, sort_keys=False).encode()
    outer["payload_base64"] = base64.b64encode(payload_bytes).decode()
    outer["signature"] = COUNCIL_KEY.sign(payload_bytes).hex()
    verified = verify_tool_call_writ(
        outer,
        trust,
        agent_id="agent",
        council_id="council",
        operation=operation,
        writ_id=payload["writ_id"],
        dispatch=True,
    )
    assert verified["writ_id"] == payload["writ_id"]


def test_structured_writ_payload_tamper_fails_signature_before_parse():
    _, outer, _, operation, trust = structured_sample()
    payload_bytes = bytearray(base64.b64decode(outer["payload_base64"]))
    payload_bytes[-2] ^= 1
    outer["payload_base64"] = base64.b64encode(payload_bytes).decode()
    with pytest.raises(EvidenceError, match="invalid_signature"):
        verify_tool_call_writ(
            outer,
            trust,
            agent_id="agent",
            council_id="council",
            operation=operation,
            writ_id="irrelevant",
        )


@pytest.mark.parametrize("shape", ["unknown", "duplicate"])
def test_signed_structured_payload_with_unknown_or_duplicate_field_is_rejected(shape):
    _, outer, payload, operation, trust = structured_sample()
    if shape == "unknown":
        payload["unexpected"] = True
        payload_bytes = canonical(payload)
    else:
        payload_bytes = canonical(payload).replace(b"{", b'{"writ_version":2,', 1)
    outer["payload_base64"] = base64.b64encode(payload_bytes).decode()
    outer["signature"] = COUNCIL_KEY.sign(payload_bytes).hex()
    with pytest.raises(EvidenceError, match="invalid_writ_payload"):
        verify_tool_call_writ(
            outer,
            trust,
            agent_id="agent",
            council_id="council",
            operation=operation,
            writ_id=payload["writ_id"],
        )


@pytest.mark.parametrize("case", ["expired", "stale", "wrong_writ_id"])
def test_structured_writ_rejects_invalid_time_or_response_binding(case):
    _, outer, payload, operation, trust = structured_sample()
    now = time.time()
    if case == "expired":
        payload["issued_at"], payload["expires_at"] = now - 20, now - 10
    elif case == "stale":
        payload["issued_at"], payload["expires_at"] = now - 120, now + 60
    payload_bytes = canonical(payload)
    outer["payload_base64"] = base64.b64encode(payload_bytes).decode()
    outer["signature"] = COUNCIL_KEY.sign(payload_bytes).hex()
    with pytest.raises(EvidenceError):
        verify_tool_call_writ(
            outer,
            trust,
            agent_id="agent",
            council_id="council",
            operation=operation,
            writ_id="other" if case == "wrong_writ_id" else payload["writ_id"],
            dispatch=True,
            issued_after=now if case == "stale" else None,
            now=now,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_agent_id", "other"),
        ("council_id", "other"),
        ("max_executions", 2),
        ("nonce", "not-hex"),
        (
            "operation",
            {
                "kind": "tool_call",
                "schema_version": 1,
                "tool_name": "other",
                "arguments_sha256": "b" * 64,
                "canonicalization": "nomos-json-v1",
                "subject": "opaque-user",
            },
        ),
    ],
)
def test_signed_structured_writ_with_wrong_binding_is_rejected(field, value):
    _, outer, payload, operation, trust = structured_sample()
    payload[field] = value
    payload_bytes = canonical(payload)
    outer["payload_base64"] = base64.b64encode(payload_bytes).decode()
    outer["signature"] = COUNCIL_KEY.sign(payload_bytes).hex()
    with pytest.raises(EvidenceError):
        verify_tool_call_writ(
            outer,
            trust,
            agent_id="agent",
            council_id="council",
            operation=operation,
            writ_id=payload["writ_id"],
            dispatch=True,
        )


def test_structured_writ_rejects_unknown_and_duplicate_outer_fields():
    _, outer, _, _, _ = structured_sample()
    unknown = {**outer, "member_votes": []}
    with pytest.raises(EvidenceError, match="invalid_writ_contract"):
        decode_tool_call_writ(base64.b64encode(canonical(unknown)).decode())

    duplicate = (
        '{"writ_version":2,"writ_version":2,"payload_base64":"'
        + outer["payload_base64"]
        + '","signature":"'
        + outer["signature"]
        + '"}'
    ).encode()
    with pytest.raises(EvidenceError, match="invalid_writ_contract"):
        decode_tool_call_writ(base64.b64encode(duplicate).decode())
