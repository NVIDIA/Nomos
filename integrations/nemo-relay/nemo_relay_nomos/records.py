# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Evidence records: what is stored for every governed response, and how it is cited.

Every record names its signer, its creation time and the SHA-256 of its
content, and carries a plugin receipt signed over that hash. A verified
executor-signed ``ExecutionResult`` is ``certified``, and so are the local
results and Council decisions the plugin certified. Only certified records
are cited; refusals and unknown outcomes are plugin-signed and never cited.
"""

from __future__ import annotations

import json
from typing import Any

from .signing import PluginSigner, verify_plugin_signature
from .wire import bounded, canonical, sha256_hex

KINDS = (
    "executor_result",
    "local_result",
    "council_denial",
    "council_error",
    "execution_unknown",
    "plugin_refusal",
)
CATALOG_KINDS = ("executor_result", "local_result", "council_denial", "council_error")
CERTIFIED_PLUGIN_KINDS = ("local_result", "council_denial", "council_error")  # carry a certified_result
CONTRACT_VERSION = "nomos-plugin/v1"


def response(
    status: str, *, execution: str = "not_executed", evidence: str = "unavailable", **fields: Any
) -> dict:
    """A model-visible tool result. ``evidence_status`` describes the record named by ``evidence_id``."""
    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "execution_status": execution,
        "evidence_status": evidence,
        **fields,
    }


def _base(
    *,
    evidence_id: str,
    kind: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    content: Any,
    now: float,
) -> dict:
    arguments = {k: v for k, v in arguments.items() if k != "nomos"}  # the intent is not the call
    content_sha256 = sha256_hex(canonical(content))
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "arguments": arguments,
        "arguments_sha256": sha256_hex(canonical(arguments)),
        "created_at": now,
        "content": content,
        "content_sha256": content_sha256,
    }


# Added by the repository when a record is read back; not part of what was signed.
STORE_FIELDS = ("council_citation_status", "expires_at", "parent_digest")


def record_sha256(record: dict) -> str:
    """SHA-256 over every signed field: all but the receipt (and a plugin signature, which copies it)."""
    plugin_signed = (record.get("signer") or {}).get("type") == "plugin"
    skip = {"receipt", *STORE_FIELDS, *(("signature",) if plugin_signed else ())}
    return sha256_hex(canonical({k: v for k, v in record.items() if k not in skip}))


def _seal(record: dict, signer: PluginSigner) -> dict:
    receipt = signer.sign(
        record["content_sha256"],
        evidence_id=record["evidence_id"],
        created_at=record["created_at"],
        record_sha256=record_sha256(record),
    )
    record["receipt"] = {"signer": signer.identity, **receipt}
    if record["signer"]["type"] == "plugin":
        record["signature"] = {k: receipt[k] for k in ("algorithm", "signed", "value")}
    return record


def executor_signer(result: dict) -> dict:
    """The Executor's identity as its signed evidence names it."""
    ev = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    return {
        "type": "executor",
        "id": str(payload.get("executor_id") or result.get("executor_id") or ""),
        "key_fingerprint": str(ev.get("key_fingerprint") or ""),
        "public_key_hex": str(ev.get("public_key_hex") or ""),
    }


def executor_record(
    *,
    evidence_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    result: dict,
    writ: dict,
    verified: bool,
    verification_error: str | None,
    receipt_signer: PluginSigner,
    policy_version: str,
    now: float,
) -> dict:
    record = _base(
        evidence_id=evidence_id,
        kind="executor_result",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=arguments,
        content=result,
        now=now,
    )
    ev = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    record.update(
        signer=executor_signer(result),
        signature={
            "algorithm": str(ev.get("algorithm") or ""),
            "signed": "executor evidence payload (binds writ, command and output digests)",
            "value": str(ev.get("signature") or ""),
        },
        certified=verified,
        evidence_status="locally_verified" if verified else "invalid",
        verification_error=verification_error,
        writ=writ,
        writ_id=writ.get("writ_id"),
        decision_id=None,
        policy_version=policy_version,
    )
    return _seal(record, receipt_signer)


def plugin_record(
    *,
    evidence_id: str,
    kind: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    content: dict,
    signer: PluginSigner,
    decision_id: str | None = None,
    writ_id: str | None = None,
    certified_result: dict | None = None,
    now: float,
) -> dict:
    """A plugin-signed record. ``certified_result`` (a local result or Council decision only) is the
    ``ExecutionResult`` the plugin certified for it; the record is then cited as certified evidence."""
    if kind not in KINDS or kind == "executor_result":
        raise ValueError(f"not a plugin record kind: {kind}")
    if certified_result is not None and kind not in CERTIFIED_PLUGIN_KINDS:
        raise ValueError(f"not certified by the plugin: {kind}")
    record = _base(
        evidence_id=evidence_id,
        kind=kind,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=arguments,
        content=content,
        now=now,
    )
    record.update(
        signer=signer.identity,
        certified=certified_result is not None,
        evidence_status="plugin_signed" if certified_result is None else "plugin_certified",
        verification_error=None,
        writ=None,
        writ_id=writ_id,
        decision_id=decision_id,
        policy_version=None,
    )
    if certified_result is not None:
        record["certified_result"] = certified_result  # covered by the receipt's record_sha256
    return _seal(record, signer)


def outcome(record: dict) -> str:
    kind, content = record["kind"], record.get("content") or {}
    if kind == "council_denial":
        return "denied"
    if kind == "council_error":
        return "error"
    if kind == "execution_unknown":
        return "unknown"
    if kind == "plugin_refusal":
        return "not_executed"
    if kind == "local_result":
        return "executed" if content.get("status") == "executed" else "failed"
    if content.get("success"):
        return "executed"
    return (
        "failed"
        if content.get("verification_stage") in ("passed", "execution", "timeout")
        else "not_executed"
    )


def catalog_entry(record: dict, max_argument_chars: int) -> dict:
    arguments = record["arguments"]
    if len(canonical(arguments)) > max_argument_chars:
        text = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        arguments = {"_truncated": bounded(text, max_argument_chars)}
    return {
        "evidence_id": record["evidence_id"],
        "tool_name": record["tool_name"],
        "arguments": arguments,
        "outcome": outcome(record),
        "certified": bool(record.get("certified")),
        "created_at": record["created_at"],
    }


def citation(record: dict) -> dict | None:
    """What citing ``record`` sends as ``certified_evidence``; None when it cannot be cited (not certified)."""
    if not record.get("certified"):
        return None
    if record["kind"] == "executor_result":
        return record["content"]
    if record["kind"] in CERTIFIED_PLUGIN_KINDS:
        return record["certified_result"]
    return None


def integrity_ok(record: dict, expected_public_key_hex: str | None = None) -> bool:
    """Content hash and plugin receipt agree; optionally pinned to the plugin's own key."""
    try:
        if sha256_hex(canonical(record["content"])) != record["content_sha256"]:
            return False
        receipt = record["receipt"]
        signed = receipt["signed"]
        if (
            signed["content_sha256"] != record["content_sha256"]
            or signed["evidence_id"] != record["evidence_id"]
        ):
            return False
        if signed["record_sha256"] != record_sha256(record):  # arguments, kind, signer, status, …
            return False
        key = receipt["signer"]["public_key_hex"]
        if expected_public_key_hex is not None and key != expected_public_key_hex:
            return False
        return verify_plugin_signature(key, receipt)
    except (KeyError, TypeError, ValueError):
        return False
