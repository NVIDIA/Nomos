# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test-only signing identities and service fixtures; never production credentials."""

import base64
import json
import re
import time
import uuid
from copy import deepcopy
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nemo_relay_nomos.tool_call import (
    TOOL_CALL_CANONICALIZATION,
    TOOL_CALL_KIND,
    canonical_tool_arguments,
)
from nemo_relay_nomos.verify import WRIT_FIELDS
from nemo_relay_nomos.wire import canonical, sha256_hex

INTENT = dict(
    description="Print hello", justification="Requested", target_resource="workspace", blast_radius="none"
)
TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}
SIGNING_KEY = "5e" * 32  # the plugin's own test seed: evidence is kept only with a signing key
COUNCIL_KEY = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
EXECUTOR_KEY = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32)


def public(key):
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


LOCAL_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
}


def base_config(**overrides):
    return {
        "session": {"id": "session-a"},
        "signing_key": SIGNING_KEY,
        "connection": {"agent_id": "agent", "council_id": "council", "auth_token": "test-secret"},
        "tool_call": {"subject": "opaque-test-user"},
        "trust": {
            "policy_version": "test-v1",
            "valid_until": time.time() + 3600,
            "council_public_key_hex": public(COUNCIL_KEY),
            "executors": {"executor": [public(EXECUTOR_KEY)]},
        },
        **overrides,
    }


def council_key_reply(key=None, council_id="council"):
    """council_get_public_key's answer (Nomos PublicKeyOutput) for ``key`` (default: the test Council's)."""
    hex_key = public(key or COUNCIL_KEY)
    return {
        "council_id": council_id,
        "public_key_hex": hex_key,
        "fingerprint": sha256_hex(bytes.fromhex(hex_key)),
        "key_id": "test-key",
        "error": "",
    }


def make_writ(fields):
    now = time.time()
    writ = {
        "writ_id": str(uuid.uuid4()),
        "intent_hash": "a" * 64,
        "command": fields["command"],
        "working_directory": fields["working_directory"],
        "target_agent_id": fields["agent_id"],
        "council_id": fields["council_id"],
        "issued_at": now,
        "expires_at": now + 300.0,
        "max_executions": 1,
        "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
        "packaged_files": [],
        "threshold_used": "unanimous",
        "member_votes": [],
        "chairman_risk_level": "low",
        "ledger_entry_hash": "",
    }
    writ["signature"] = COUNCIL_KEY.sign(canonical({k: writ[k] for k in WRIT_FIELDS})).hex()
    return writ


def make_tool_call_writ(fields):
    """Mint the strict v2 envelope used by structured local-tool tests."""

    now = time.time()
    request = fields["tool_call"]
    operation = {
        "kind": TOOL_CALL_KIND,
        "schema_version": request["schema_version"],
        "tool_name": request["tool_name"],
        "arguments_sha256": sha256_hex(canonical_tool_arguments(request["arguments"])),
        "canonicalization": TOOL_CALL_CANONICALIZATION,
        "subject": request["subject"],
    }
    payload = {
        "writ_version": 2,
        "writ_id": str(uuid.uuid4()),
        "intent_hash": "a" * 64,
        "operation": operation,
        "target_agent_id": fields["agent_id"],
        "issued_at": now,
        "expires_at": now + 300.0,
        "max_executions": 1,
        "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
        "council_id": fields["council_id"],
        "threshold_used": "unanimous",
        "chairman_risk_level": "low",
        "ledger_entry_hash": "",
    }
    payload_bytes = canonical(payload)
    return {
        "writ_version": 2,
        "payload_base64": base64.b64encode(payload_bytes).decode(),
        "signature": COUNCIL_KEY.sign(payload_bytes).hex(),
    }


def sign_result(writ, stdout="hello\nsecond\n", stderr="", key=EXECUTOR_KEY):
    fingerprint = sha256_hex(bytes.fromhex(public(key)))
    result = {
        "contract_version": "v1",
        "executor_id": "executor",
        "writ_id": writ["writ_id"],
        "success": True,
        "exit_code": 0,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": 0.001,
        "error": None,
        "verification_stage": "passed",
    }
    payload = {
        "evidence_version": "v1",
        "executor_id": "executor",
        "executor_key_fingerprint": fingerprint,
        **{k: writ[k] for k in ("writ_id", "intent_hash", "council_id", "target_agent_id", "nonce")},
        "writ_signature": writ["signature"],
        "command": {
            "command": writ["command"],
            "working_directory": writ["working_directory"],
            "shell": True,
            "argv": [],
        },
        "attachments": [],
        "result": {
            **{
                k: result[k]
                for k in ("success", "exit_code", "duration_seconds", "error", "verification_stage")
            },
            "stdout_sha256": sha256_hex(stdout),
            "stderr_sha256": sha256_hex(stderr),
            "stdout_bytes": len(stdout.encode()),
            "stderr_bytes": len(stderr.encode()),
        },
        "request_id": "",
        "signed_at": time.time(),
    }
    result["evidence"] = {
        "algorithm": "ed25519",
        "public_key_hex": public(key),
        "key_fingerprint": fingerprint,
        "payload": payload,
        "signature": key.sign(canonical(payload)).hex(),
    }
    return result


class Council:
    """Programmable council: approved | denied | error | raise."""

    def __init__(self, status="approved"):
        self.status, self.calls, self.last_writ = status, [], None
        # council_get_public_key: what it answers ("raise" to fail), and each council_id it was asked for.
        self.key_reply, self.key_calls = council_key_reply(), []

    async def public_key(self, council_id):
        self.key_calls.append(council_id)
        if self.key_reply == "raise":
            raise ConnectionError("council down")
        return self.key_reply

    async def authorize(self, **fields):
        self.calls.append(fields)
        if self.status == "raise":
            raise ConnectionError("council down")
        if self.status == "denied":
            return {
                "status": "denied",
                "decision_id": "intent-1",
                "error": "Denied by OPA policy: non-absolute command",
                "guidance": ["Use absolute executable paths."],
                "missing_permissions": [],
                "retryable": False,
                "council_members": [
                    {"member_id": "blast-containment", "decision": "deny", "reasoning": "too broad"}
                ],
                "chairman": {"threshold": "unanimous", "reasoning": "denied", "risk_level": "high"},
            }
        if self.status != "approved":
            return {"status": self.status, "error": "council_overloaded", "retryable": True}
        self.last_writ = (
            make_tool_call_writ(fields) if fields.get("tool_call") is not None else make_writ(fields)
        )
        return {
            "status": "approved",
            "decision_id": "intent-2",
            "writ": base64.b64encode(canonical(self.last_writ)).decode(),
            "writ_id": (
                json.loads(base64.b64decode(self.last_writ["payload_base64"]))["writ_id"]
                if self.last_writ.get("writ_version") == 2
                else self.last_writ["writ_id"]
            ),
            "council_members": [{"member_id": "command-integrity", "decision": "approve"}],
        }


class Executor:
    def __init__(self):
        self.calls, self.results = [], []

    async def execute(self, writ, agent_id):
        self.calls.append((writ, agent_id))
        result = sign_result(json.loads(base64.b64decode(writ)))
        self.results.append(result)
        return result


class Next:
    def __init__(self, result="local output", error=None):
        self.calls, self.result, self.error = [], result, error

    async def call(self, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return SimpleNamespace(result=self.result, annotation={"original": True})


class Context:
    def __init__(self):
        self.hooks = {}

    def register_llm_request_intercept(self, name, callback, **kw):
        self.hooks[name] = (callback, kw)

    def register_tool_execution_intercept(self, name, callback, **kw):
        self.hooks[name] = (callback, kw)

    def register_scope_sanitize_end_guardrail(self, name, callback, **kw):
        self.hooks[name] = (callback, kw)


def call(name="run_shell", intent=INTENT, call_id="call1", turn=None, **args):
    """A tool call as the model makes it; ``turn`` is the token from the Nomos message it was sent."""
    args = args or {"command": "echo hello"}
    if intent is not None:
        args["nomos"] = deepcopy(intent)
        if turn is not None and isinstance(intent, dict):
            args["nomos"]["turn"] = turn
    return SimpleNamespace(tool_name=name, tool_call_id=call_id, args=args)


def cite(turn, *ids, **kw):
    return call(intent={**INTENT, "evidence": list(ids)}, turn=turn, **kw)


TURN = re.compile(r"nt_[0-9a-f]{32}")


def nomos_message(outcome):
    """The trailing Nomos message H1 appended (raw path)."""
    return outcome.request["content"]["messages"][-1]["content"]


def turn(plugin, *messages, user="list files"):
    """Run H1 for one model request and return the turn token it issued."""
    msgs = list(messages) or [{"role": "user", "content": user}]
    return TURN.search(
        nomos_message(plugin.h1("model", {"content": {"messages": msgs, "tools": [TOOL]}}, None))
    )[0]


def history(user, *steps):
    """A transcript as an application keeps it: the user's request, then each (call, result) pair."""
    messages = [{"role": "user", "content": user}]
    for ctx, result in steps:
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": ctx.tool_call_id,
                        "type": "function",
                        "function": {"name": ctx.tool_name, "arguments": json.dumps(ctx.args)},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": ctx.tool_call_id, "content": json.dumps(result)})
    return messages
