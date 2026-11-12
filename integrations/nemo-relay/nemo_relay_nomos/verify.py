# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict verification of Nomos command-v1 and structured-tool-v2 wire contracts.

The wire dictionaries are validated, never normalized before signature checks.
No embedded public key is trusted without an operator-configured allowlist.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator

from .wire import canonical, sha256_hex

MAX_WRIT_BYTES = 65_536
MAX_WRIT_LIFETIME_SECONDS = 3_600


class EvidenceError(ValueError):
    """A stable, non-secret reason for rejecting evidence."""


def _object(properties: dict, *, extra: bool = False) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": extra,
    }


S = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1, "maxLength": 256}
HEX = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
SIGNATURE = {"type": "string", "pattern": "^[0-9a-f]{128}$"}
NUMBER = {"type": "number", "minimum": 0}
STRINGS = {"type": "array", "items": S}
RESULT_FIELDS = {
    "success": {"type": "boolean"},
    "exit_code": {"type": ["integer", "null"]},
    "duration_seconds": NUMBER,
    "error": {"type": ["string", "null"]},
    "verification_stage": NONEMPTY,
}
WRIT_FIELDS = {
    "writ_id": NONEMPTY,
    "intent_hash": HEX,
    "command": {"type": "string", "minLength": 1, "maxLength": 32768},
    "working_directory": NONEMPTY,
    "target_agent_id": NONEMPTY,
    "packaged_files": {"const": []},
    "issued_at": NUMBER,
    "expires_at": NUMBER,
    "max_executions": {"const": 1, "type": "integer"},
    "nonce": HEX,
    "council_id": NONEMPTY,
    "threshold_used": NONEMPTY,
    "chairman_risk_level": NONEMPTY,
    "ledger_entry_hash": S,
}
WRIT_SCHEMA = _object({**WRIT_FIELDS, "signature": SIGNATURE}, extra=True)
TOOL_CALL_OPERATION_SCHEMA = _object(
    {
        "kind": {"const": "tool_call"},
        "schema_version": {"const": 1, "type": "integer"},
        "tool_name": NONEMPTY,
        "arguments_sha256": HEX,
        "canonicalization": {"const": "nomos-json-v1"},
        "subject": {"type": "string", "minLength": 1, "maxLength": 512},
    }
)
TOOL_CALL_PAYLOAD_SCHEMA = _object(
    {
        "writ_version": {"const": 2, "type": "integer"},
        "writ_id": NONEMPTY,
        "intent_hash": HEX,
        "operation": TOOL_CALL_OPERATION_SCHEMA,
        "target_agent_id": NONEMPTY,
        "issued_at": NUMBER,
        "expires_at": NUMBER,
        "max_executions": {"const": 1, "type": "integer"},
        "nonce": HEX,
        "council_id": NONEMPTY,
        "threshold_used": NONEMPTY,
        "chairman_risk_level": NONEMPTY,
        "ledger_entry_hash": S,
    }
)
TOOL_CALL_WRIT_SCHEMA = _object(
    {
        "writ_version": {"const": 2, "type": "integer"},
        "payload_base64": {"type": "string", "minLength": 1, "maxLength": MAX_WRIT_BYTES * 2},
        "signature": SIGNATURE,
    }
)
PAYLOAD_SCHEMA = _object(
    {
        "evidence_version": {"const": "v1"},
        "executor_id": NONEMPTY,
        "executor_key_fingerprint": HEX,
        "writ_id": NONEMPTY,
        "intent_hash": HEX,
        "council_id": NONEMPTY,
        "writ_signature": SIGNATURE,
        "target_agent_id": NONEMPTY,
        "nonce": HEX,
        "command": _object(
            {
                "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                "working_directory": NONEMPTY,
                "shell": {"type": ["boolean", "null"]},
                "argv": STRINGS,
            }
        ),
        "attachments": {"const": []},  # command-only baseline; no packaged files or output artifacts
        "result": _object(
            {
                **RESULT_FIELDS,
                "stdout_sha256": HEX,
                "stderr_sha256": HEX,
                "stdout_bytes": {"type": "integer", "minimum": 0},
                "stderr_bytes": {"type": "integer", "minimum": 0},
            }
        ),
        "request_id": S,
        "signed_at": NUMBER,
    }
)
RESULT_SCHEMA = _object(
    {
        **RESULT_FIELDS,
        "contract_version": {"const": "v1"},
        "executor_id": NONEMPTY,
        "writ_id": NONEMPTY,
        "stdout": S,
        "stderr": S,
        "evidence": _object(
            {
                "algorithm": {"const": "ed25519"},
                "public_key_hex": HEX,
                "key_fingerprint": HEX,
                "payload": PAYLOAD_SCHEMA,
                "signature": SIGNATURE,
            }
        ),
    }
)


def _validate(schema: dict, value: object, reason: str) -> None:
    if not Draft202012Validator(schema).is_valid(value):
        raise EvidenceError(reason)
    try:
        canonical(value)  # rejects NaN, infinity, non-JSON types and invalid Unicode
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EvidenceError(reason) from exc


def check_trust(trust: dict, now: float | None = None) -> None:
    if (
        not math.isfinite(trust["valid_until"])
        or (time.time() if now is None else now) >= trust["valid_until"]
    ):
        raise EvidenceError("trust_policy_expired")


def _signature(key: str, signature: str, payload: dict) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(signature), canonical(payload)
        )
    except (InvalidSignature, ValueError, TypeError, UnicodeError) as exc:
        raise EvidenceError("invalid_signature") from exc


def _signature_bytes(key: str, signature: str, payload: bytes) -> None:
    """Verify immutable wire bytes without reserializing signed JSON."""

    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(bytes.fromhex(signature), payload)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise EvidenceError("invalid_signature") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_non_finite(_: str) -> None:
    raise ValueError("non-finite number")


def _strict_json_object(raw: bytes, reason: str) -> dict:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise EvidenceError(reason) from exc


def verify_writ(
    writ: dict,
    trust: dict,
    *,
    agent_id: str,
    council_id: str,
    command: str,
    working_directory: str,
    writ_id: str,
    dispatch: bool = False,
    issued_after: float | None = None,
    now: float | None = None,
) -> None:
    now = time.time() if now is None else now
    check_trust(trust, now)
    _validate(WRIT_SCHEMA, writ, "invalid_writ_contract")
    _signature(trust["council_public_key_hex"], writ["signature"], {k: writ[k] for k in WRIT_FIELDS})
    expected = {
        "target_agent_id": agent_id,
        "council_id": council_id,
        "command": command,
        "working_directory": working_directory,
        "writ_id": writ_id,
    }
    if any(writ[k] != v for k, v in expected.items()):
        raise EvidenceError("writ_binding_mismatch")
    if writ["issued_at"] >= writ["expires_at"] or writ["issued_at"] > now + 30:
        raise EvidenceError("invalid_writ_time")
    if dispatch and now >= writ["expires_at"]:
        raise EvidenceError("writ_expired")
    if issued_after is not None and writ["issued_at"] < issued_after - 30:
        raise EvidenceError("writ_not_fresh")  # minted before this request: a replayed approval


def decode_writ(encoded: str) -> dict:
    try:
        if not isinstance(encoded, str) or len(encoded) > 65536:
            raise ValueError()
        writ = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(writ, dict):
            raise ValueError()
        return writ
    except (ValueError, TypeError, UnicodeError) as exc:
        raise EvidenceError("invalid_writ_encoding") from exc


def decode_tool_call_writ(encoded: str) -> dict:
    """Strictly decode the outer v2 envelope; the signed payload remains raw."""

    try:
        if not isinstance(encoded, str) or len(encoded) > MAX_WRIT_BYTES * 2:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_WRIT_BYTES:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise EvidenceError("invalid_writ_encoding") from exc
    writ = _strict_json_object(raw, "invalid_writ_contract")
    _validate(TOOL_CALL_WRIT_SCHEMA, writ, "invalid_writ_contract")
    return writ


def verify_tool_call_writ(
    writ: dict,
    trust: dict,
    *,
    agent_id: str,
    council_id: str,
    operation: dict,
    writ_id: str,
    dispatch: bool = False,
    issued_after: float | None = None,
    now: float | None = None,
) -> dict:
    """Authenticate a v2 envelope and bind it to the exact held structured operation."""

    now = time.time() if now is None else now
    check_trust(trust, now)
    _validate(TOOL_CALL_WRIT_SCHEMA, writ, "invalid_writ_contract")
    try:
        payload_bytes = base64.b64decode(writ["payload_base64"], validate=True)
        if not payload_bytes or len(payload_bytes) > MAX_WRIT_BYTES:
            raise ValueError()
    except (ValueError, TypeError, binascii.Error) as exc:
        raise EvidenceError("invalid_writ_payload") from exc

    # Signature verification intentionally precedes JSON parsing. The producer's
    # exact bytes, rather than a verifier reserialization, are the signed object.
    _signature_bytes(trust["council_public_key_hex"], writ["signature"], payload_bytes)
    payload = _strict_json_object(payload_bytes, "invalid_writ_payload")
    _validate(TOOL_CALL_PAYLOAD_SCHEMA, payload, "invalid_writ_payload")

    expected = {
        "target_agent_id": agent_id,
        "council_id": council_id,
        "writ_id": writ_id,
        "operation": operation,
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise EvidenceError("writ_binding_mismatch")
    issued_at, expires_at = payload["issued_at"], payload["expires_at"]
    try:
        finite_times = math.isfinite(issued_at) and math.isfinite(expires_at)
    except OverflowError as exc:
        raise EvidenceError("invalid_writ_time") from exc
    if (
        not finite_times
        or issued_at >= expires_at
        or expires_at - issued_at > MAX_WRIT_LIFETIME_SECONDS
        or issued_at > now + 30
    ):
        raise EvidenceError("invalid_writ_time")
    if dispatch and now >= expires_at:
        raise EvidenceError("writ_expired")
    if issued_after is not None and issued_at < issued_after - 30:
        raise EvidenceError("writ_not_fresh")
    return {
        "writ_id": payload["writ_id"],
        "intent_hash": payload["intent_hash"],
        "nonce": payload["nonce"],
        "operation": payload["operation"],
        "expires_at": payload["expires_at"],
    }


def verify_executor_evidence(
    result: dict, writ: dict, trust: dict, *, agent_id: str, council_id: str, now: float | None = None
) -> None:
    """Raise on any mismatch. Success means locally verified, not council-accepted."""
    now = time.time() if now is None else now
    check_trust(trust, now)
    _validate(RESULT_SCHEMA, result, "invalid_evidence_contract")
    verify_writ(
        writ,
        trust,
        agent_id=agent_id,
        council_id=council_id,
        command=writ["command"],
        working_directory=writ["working_directory"],
        writ_id=writ["writ_id"],
        now=now,
    )
    ev, payload = result["evidence"], result["evidence"]["payload"]
    keys = trust["executors"].get(payload["executor_id"], [])
    if ev["public_key_hex"] not in keys:
        raise EvidenceError("untrusted_executor")
    fingerprint = sha256_hex(bytes.fromhex(ev["public_key_hex"]))
    if ev["key_fingerprint"] != fingerprint or payload["executor_key_fingerprint"] != fingerprint:
        raise EvidenceError("fingerprint_mismatch")
    _signature(ev["public_key_hex"], ev["signature"], payload)
    for key in ("writ_id", "intent_hash", "council_id", "target_agent_id", "nonce"):
        if payload[key] != writ[key]:
            raise EvidenceError("evidence_writ_mismatch")
    if payload["writ_signature"] != writ["signature"]:
        raise EvidenceError("evidence_writ_mismatch")
    if (
        payload["command"]["command"] != writ["command"]
        or payload["command"]["working_directory"] != writ["working_directory"]
    ):
        raise EvidenceError("evidence_command_mismatch")
    if result["executor_id"] != payload["executor_id"] or result["writ_id"] != writ["writ_id"]:
        raise EvidenceError("result_identity_mismatch")
    _result_matches_payload(result, payload["result"])
    if (
        not math.isfinite(payload["signed_at"])
        or payload["signed_at"] < writ["issued_at"] - 30
        or payload["signed_at"] > now + 30
    ):
        raise EvidenceError("invalid_evidence_time")


def verify_plugin_certified(
    result: dict,
    signer_public_key_hex: str,
    *,
    executor_id: str,
    agent_id: str,
    council_id: str,
    writ_id: str,
) -> None:
    """Raise unless ``result`` is an ExecutionResult this plugin's key certified for its record.

    ``writ_id`` is the record's: a local result's writ (verified before the tool ran), or "" for a Council decision,
    which no writ authorized. This re-checks what was signed: the key, the ids and the output.
    """
    try:
        ev, payload = result["evidence"], result["evidence"]["payload"]
        fingerprint = sha256_hex(bytes.fromhex(signer_public_key_hex))
        if (
            ev["algorithm"] != "ed25519"
            or ev["public_key_hex"] != signer_public_key_hex
            or ev["key_fingerprint"] != fingerprint
            or payload["executor_key_fingerprint"] != fingerprint
        ):
            raise EvidenceError("untrusted_executor")
        _signature(signer_public_key_hex, ev["signature"], payload)
        if (
            result["executor_id"] != payload["executor_id"]
            or payload["executor_id"] != executor_id
            or result["writ_id"] != payload["writ_id"]
            or payload["writ_id"] != writ_id
        ):
            raise EvidenceError("result_identity_mismatch")
        if payload["council_id"] != council_id or payload["target_agent_id"] != agent_id:
            raise EvidenceError("evidence_writ_mismatch")
        _result_matches_payload(result, payload["result"])
    except EvidenceError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EvidenceError("invalid_evidence_contract") from exc


def _result_matches_payload(result: dict, signed_result: dict) -> None:
    if any(result[key] != signed_result[key] for key in RESULT_FIELDS):
        raise EvidenceError("result_field_mismatch")
    for channel in ("stdout", "stderr"):
        try:
            # Strict: a string that only matches its digest after replacement is not what was signed.
            raw = result[channel].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise EvidenceError("output_digest_mismatch") from exc
        if (
            sha256_hex(raw) != signed_result[channel + "_sha256"]
            or len(raw) != signed_result[channel + "_bytes"]
        ):
            raise EvidenceError("output_digest_mismatch")
