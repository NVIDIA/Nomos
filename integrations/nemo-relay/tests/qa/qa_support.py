# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""QA helpers derived from DESIGN.md and the pinned Nomos v1 wire contract only.

Nothing here is derived from the plugin implementation. Writs follow
``WritOfExecution.canonical_bytes``; executor results follow
``executors/common/evidence.py::certify_execution_result``.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import re
import secrets
import sqlite3
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

COUNCIL_SEED = bytes([1]) * 32
EXECUTOR_SEED = bytes([2]) * 32
PLUGIN_SEED = bytes([3]) * 32
STRANGER_SEED = bytes([4]) * 32

AGENT_ID = "agent-qa"
COUNCIL_ID = "council-qa"
SESSION_ID = "session-qa"
EXECUTOR_ID = "executor-qa"
WORKING_DIRECTORY = "/workspace"
OWNER = (AGENT_ID, COUNCIL_ID, SESSION_ID)

INTENT = {
    "description": "List the workspace files",
    "justification": "The user asked what is in the workspace",
    "target_resource": "/workspace",
    "blast_radius": "read-only, this directory",
}


def private_key(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_bytes(seed: bytes) -> bytes:
    return (
        private_key(seed)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    )


def public_hex(seed: bytes) -> str:
    return public_bytes(seed).hex()


def fingerprint(seed: bytes) -> str:
    return hashlib.sha256(public_bytes(seed)).hexdigest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def uuid7() -> str:
    ms = int(time.time() * 1000)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def evidence_id() -> str:
    return "ev_" + secrets.token_hex(16)


def writ_canonical_bytes(writ: dict) -> bytes:
    data = {
        "chairman_risk_level": writ["chairman_risk_level"],
        "command": writ["command"],
        "council_id": writ["council_id"],
        "expires_at": writ["expires_at"],
        "intent_hash": writ["intent_hash"],
        "issued_at": writ["issued_at"],
        "ledger_entry_hash": writ["ledger_entry_hash"],
        "max_executions": writ["max_executions"],
        "nonce": writ["nonce"],
        "packaged_files": writ["packaged_files"],
        "target_agent_id": writ["target_agent_id"],
        "threshold_used": writ["threshold_used"],
        "working_directory": writ["working_directory"],
        "writ_id": writ["writ_id"],
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def mint_writ(
    command: str,
    *,
    working_directory: str = WORKING_DIRECTORY,
    agent_id: str = AGENT_ID,
    council_id: str = COUNCIL_ID,
    seed: bytes = COUNCIL_SEED,
    writ_id: str | None = None,
    nonce: str | None = None,
    lifetime_s: float = 300.0,
) -> dict:
    now = time.time()
    writ = {
        "writ_id": writ_id or uuid7(),
        "intent_hash": sha("intent:" + command),
        "command": command,
        "working_directory": working_directory,
        "target_agent_id": agent_id,
        "packaged_files": [],
        "issued_at": now - 1.0,
        "expires_at": now + lifetime_s,
        "max_executions": 1,
        "nonce": nonce or secrets.token_hex(32),
        "council_id": council_id,
        "threshold_used": "unanimous",
        "member_votes": [],
        "chairman_risk_level": "low",
        "signature": "",
        "ledger_entry_hash": "",
    }
    writ["signature"] = private_key(seed).sign(writ_canonical_bytes(writ)).hex()
    return writ


def encode_writ(writ: dict) -> str:
    return base64.b64encode(json.dumps(writ).encode("utf-8")).decode("ascii")


def decode_writ_b64(encoded: str) -> dict:
    return json.loads(base64.b64decode(encoded))


def mint_tool_call_writ(fields: dict, *, seed: bytes = COUNCIL_SEED) -> tuple[dict, dict]:
    """Mint the structured Nomos envelope for a fake Council response."""

    request = fields["tool_call"]
    argument_bytes = json.dumps(
        request["arguments"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    now = time.time()
    operation = {
        "kind": "tool_call",
        "schema_version": request["schema_version"],
        "tool_name": request["tool_name"],
        "arguments_sha256": hashlib.sha256(argument_bytes).hexdigest(),
        "canonicalization": "nomos-json-v1",
        "subject": request["subject"],
    }
    payload = {
        "writ_version": 2,
        "writ_id": uuid7(),
        "intent_hash": "a" * 64,
        "operation": operation,
        "target_agent_id": fields.get("agent_id", AGENT_ID),
        "issued_at": now,
        "expires_at": now + 300.0,
        "max_executions": 1,
        "nonce": secrets.token_hex(32),
        "council_id": fields.get("council_id", COUNCIL_ID) or COUNCIL_ID,
        "threshold_used": "unanimous",
        "chairman_risk_level": "low",
        "ledger_entry_hash": "",
    }
    payload_bytes = canonical(payload)
    envelope = {
        "writ_version": 2,
        "payload_base64": base64.b64encode(payload_bytes).decode("ascii"),
        "signature": private_key(seed).sign(payload_bytes).hex(),
    }
    return envelope, payload


def signed_result(
    writ: dict,
    *,
    stdout: str = "hello from executor\n",
    stderr: str = "",
    success: bool = True,
    exit_code: int | None = 0,
    executor_id: str = EXECUTOR_ID,
    seed: bytes = EXECUTOR_SEED,
    duration_seconds: float = 0.25,
    error: str | None = None,
    verification_stage: str = "passed",
) -> dict:
    """An ExecutionResult dict signed exactly as Nomos's certify_execution_result does."""
    out_b = stdout.encode("utf-8", errors="replace")
    err_b = stderr.encode("utf-8", errors="replace")
    payload = {
        "evidence_version": "v1",
        "executor_id": executor_id,
        "executor_key_fingerprint": fingerprint(seed),
        "writ_id": writ["writ_id"],
        "intent_hash": writ["intent_hash"],
        "council_id": writ["council_id"],
        "writ_signature": writ["signature"],
        "target_agent_id": writ["target_agent_id"],
        "nonce": writ["nonce"],
        "command": {
            "command": writ["command"],
            "working_directory": writ["working_directory"],
            "shell": True,
            "argv": [],
        },
        "attachments": [],
        "result": {
            "success": success,
            "exit_code": exit_code,
            "stdout_sha256": hashlib.sha256(out_b).hexdigest(),
            "stderr_sha256": hashlib.sha256(err_b).hexdigest(),
            "stdout_bytes": len(out_b),
            "stderr_bytes": len(err_b),
            "duration_seconds": duration_seconds,
            "error": error,
            "verification_stage": verification_stage,
        },
        "request_id": "",
        "signed_at": time.time(),
    }
    signature = private_key(seed).sign(canonical(payload)).hex()
    return {
        "contract_version": "v1",
        "executor_id": executor_id,
        "writ_id": writ["writ_id"],
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": duration_seconds,
        "error": error,
        "verification_stage": verification_stage,
        "evidence": {
            "algorithm": "ed25519",
            "public_key_hex": public_hex(seed),
            "key_fingerprint": fingerprint(seed),
            "payload": payload,
            "signature": signature,
        },
    }


def trust_dict(*, valid_for_s: float = 3600.0, executors: dict | None = None) -> dict:
    return {
        "policy_version": "qa-trust-1",
        "valid_until": int(time.time() + valid_for_s),
        "council_public_key_hex": public_hex(COUNCIL_SEED),
        "executors": executors if executors is not None else {EXECUTOR_ID: [public_hex(EXECUTOR_SEED)]},
    }


def raw_config(**overrides: Any) -> dict:
    """Minimal valid raw config per DESIGN.md's settings fields."""
    cfg: dict[str, Any] = {
        "connection": {"agent_id": AGENT_ID, "council_id": COUNCIL_ID},
        "session": {"id": SESSION_ID},
        "tool_call": {"subject": "opaque-qa-user"},
        "trust": trust_dict(),
    }
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class FakeCouncil:
    """async authorize(**fields) -> dict. Modes: approve, deny, error, raise, fixed (replay a writ)."""

    def __init__(self, mode: str = "approve") -> None:
        self.mode = mode
        self.queue: list[str] = []
        self.calls: list[dict] = []
        self.fixed_encoded: str | None = None
        self.writ_seed = COUNCIL_SEED
        self.last_writ: dict | None = None

    def denial(self) -> dict:
        return {
            "api_version": "v1",
            "council_id": COUNCIL_ID,
            "status": "denied",
            "writ": None,
            "writ_id": "",
            "decision_id": "dec-denied-qa",
            "guidance": ["Do not do that; explain the target first."],
            "error": "",
            "retryable": False,
            "council_members": [{"member_id": "m1", "decision": "deny"}],
            "chairman": {"decision": "deny", "reasoning": "QA denial reasoning"},
            "missing_permissions": [],
            "debate_rounds": 1,
            "debate_history": [],
        }

    async def authorize(self, **fields: Any) -> dict:
        self.calls.append(copy.deepcopy(fields))
        mode = self.queue.pop(0) if self.queue else self.mode
        if mode == "raise":
            raise RuntimeError("council transport exploded (QA)")
        if mode == "deny":
            return self.denial()
        if mode == "error":
            return {
                "api_version": "v1",
                "council_id": COUNCIL_ID,
                "status": "error",
                "writ": None,
                "error": "council internal error (QA)",
                "decision_id": "dec-error-qa",
            }
        if mode == "fixed" and self.fixed_encoded is not None:
            writ = decode_writ_b64(self.fixed_encoded)
            encoded = self.fixed_encoded
            payload = (
                json.loads(base64.b64decode(writ["payload_base64"]))
                if writ.get("writ_version") == 2
                else writ
            )
        elif fields.get("tool_call") is not None:
            writ, payload = mint_tool_call_writ(fields, seed=self.writ_seed)
            encoded = base64.b64encode(canonical(writ)).decode("ascii")
        else:
            writ = mint_writ(
                fields["command"],
                working_directory=fields.get("working_directory", WORKING_DIRECTORY),
                agent_id=fields.get("agent_id", AGENT_ID),
                council_id=fields.get("council_id", COUNCIL_ID) or COUNCIL_ID,
                seed=self.writ_seed,
            )
            encoded = encode_writ(writ)
            payload = writ
        self.last_writ = writ
        return {
            "api_version": "v1",
            "council_id": COUNCIL_ID,
            "status": "approved",
            "writ": encoded,
            "writ_id": payload["writ_id"],
            "decision_id": "dec-approved-qa",
            "guidance": [],
        }


class FakeExecutor:
    """async execute(writ, agent_id) -> signed ExecutionResult. Modes: ok, tamper, raise."""

    def __init__(self, mode: str = "ok", stdout: str = "QA_STDOUT_MARKER_7f3a\n") -> None:
        self.mode = mode
        self.stdout = stdout
        self.calls: list[tuple[str, str]] = []
        self.results: list[dict] = []

    async def execute(self, writ: str, agent_id: str) -> dict:
        self.calls.append((writ, agent_id))
        if self.mode == "raise":
            raise RuntimeError("executor transport exploded (QA)")
        result = signed_result(decode_writ_b64(writ), stdout=self.stdout)
        if self.mode == "tamper":
            result["stdout"] = self.stdout + "INJECTED_AFTER_SIGNING"
        self.results.append(copy.deepcopy(result))
        return result


class _NullRuntime:
    def __getattr__(self, name: str) -> Any:
        async def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


class FakeContext:
    """Records Relay registrations: (kind, name, callback, priority)."""

    def __init__(self) -> None:
        self.registrations: list[dict] = []
        self.runtime = _NullRuntime()

    def register_llm_request_intercept(self, name, callback, *, priority=0, break_chain=False):
        self.registrations.append(
            {"kind": "llm_request", "name": name, "callback": callback, "priority": priority}
        )

    def register_tool_execution_intercept(self, name, callback, *, priority=0):
        self.registrations.append(
            {"kind": "tool_execution", "name": name, "callback": callback, "priority": priority}
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("register_"):

            def _record(reg_name, callback, *args, priority=0, **kwargs):
                self.registrations.append(
                    {"kind": name, "name": reg_name, "callback": callback, "priority": priority}
                )

            return _record
        raise AttributeError(name)

    def only(self, kind: str) -> dict:
        found = [r for r in self.registrations if r["kind"] == kind]
        assert len(found) == 1, f"expected exactly one {kind} registration, got {self.registrations!r}"
        return found[0]


class FakeToolNext:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[Any] = []
        self.result = result if result is not None else {"output": "QA_LOCAL_OUTPUT_91c2"}

    async def call(self, value: Any):
        from nemo_relay_plugin import ToolExecutionResult

        self.calls.append(copy.deepcopy(value))
        return ToolExecutionResult(result=copy.deepcopy(self.result))


TURN_TOKEN = re.compile(r"nt_[0-9a-f]{32}")


def nomos_text(outcome: Any) -> str:
    """The Nomos message H1 appended: Anthropic instructions, else the last message (DESIGN.md `append_message`)."""
    annotated = outcome.annotated_request
    if annotated is None:
        return outcome.request["content"]["messages"][-1]["content"]
    if (annotated.get("api_specific") or {}).get("api") == "anthropic_messages":
        instructions = annotated["instructions"]
        return instructions if isinstance(instructions, str) else instructions[-1]["text"]
    return annotated["messages"][-1]["content"]


def turn_token(outcome: Any) -> str:
    """The turn token of the Nomos message H1 appended (the last one in it, after any earlier text)."""
    found = TURN_TOKEN.findall(nomos_text(outcome))
    assert found, f"H1 issued no turn token: {outcome!r}"
    return found[-1]


def tamper_database(con: sqlite3.Connection, old: str, new: str) -> int:
    """Replace ``old`` with ``new`` (same length) in every text/blob cell of every table of ``con``.

    Schema-independent: works whatever tables the repository chose.
    """
    assert len(old) == len(new)
    changed = 0
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for table in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
        try:
            rows = con.execute(f'SELECT rowid, * FROM "{table}"').fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            rowid, values = row[0], row[1:]
            for col, value in zip(cols, values, strict=True):
                if isinstance(value, str) and old in value:
                    con.execute(
                        f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (value.replace(old, new), rowid)
                    )
                    changed += 1
                elif isinstance(value, bytes) and old.encode() in value:
                    con.execute(
                        f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?',
                        (value.replace(old.encode(), new.encode()), rowid),
                    )
                    changed += 1
    con.commit()
    return changed


def error_code_matches(exc: BaseException, code: str) -> bool:
    return code == getattr(exc, "code", None) or code in exc.args or code in str(exc)
