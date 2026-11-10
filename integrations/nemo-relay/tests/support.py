# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test-only signing identities and service fixtures; never production credentials."""

import base64
import json
import re
import time
import uuid
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nemo_relay_plugin import ScopeStackEntry, ScopeStackSnapshot, ScopeType

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


# Test-only host simulation. Origins live on Python objects, never in model JSON.
_ORIGINS = {}


_NATIVE_AGENT = ContextVar("fixture-native-agent", default=None)


class Origin(str):
    """An internal fixture handle for a captured request, not a model argument."""

    def __deepcopy__(self, memo):
        """Preserve host-only fixture identity when copying transcript containers."""
        return self

    def __new__(cls, plugin, snapshot):
        """Carry a request snapshot in a test-only string without adding model-visible fields."""
        value = super().__new__(cls, snapshot.token)
        value.plugin, value.snapshot = plugin, snapshot
        _ORIGINS[str(value)] = value
        return value


class NativeRuntime:
    """Supply host-owned scope identities to H3 in orchestration unit tests."""

    def __init__(self):
        """Initialize isolated fixture state for native ownership and callback observations."""
        self.root, self.agent = str(uuid.uuid4()), str(uuid.uuid4())

    async def current_scope_stack_snapshot(self):
        """Expose native root and application Agent scopes without consulting model text."""
        return ScopeStackSnapshot(
            [
                ScopeStackEntry(self.root, ScopeType.AGENT),
                ScopeStackEntry(_NATIVE_AGENT.get() or self.agent, ScopeType.AGENT),
            ]
        )


class Context:
    def __init__(self):
        """Initialize isolated fixture state for native ownership and callback observations."""
        self.hooks = {}
        self.runtime = NativeRuntime()

    def register_llm_execution_intercept(self, name, callback, **kw):
        """Capture the unary execution callback and its registration priority."""
        self.hooks[name] = (callback, kw)

    def register_llm_stream_execution_intercept(self, name, callback, **kw):
        """Capture the streaming execution callback and its registration priority."""
        self.hooks[name] = (callback, kw)

    def register_scope_sanitize_end_guardrail(self, name, callback, **kw):
        self.hooks[name] = (callback, kw)

    def register_tool_execution_intercept(self, name, callback, **kw):
        """Capture authorization while preserving the synthetic host Agent binding."""

        async def host_callback(ctx, continuation):
            """Restore the proposal owner around its synthetic tool-execution callback."""
            origin = getattr(ctx, "origin", None)
            token = _NATIVE_AGENT.set(origin.snapshot.conversation if origin else self.runtime.agent)
            try:
                return await callback(ctx, continuation)
            finally:
                _NATIVE_AGENT.reset(token)

        if type(self.runtime) is NativeRuntime:
            self.hooks[name] = (host_callback, kw)
            callback.__self__.h3 = host_callback
        else:
            self.hooks[name] = (callback, kw)


def prepare(plugin, model_name, request, annotated=None, *, agent=None):
    """Exercise decoded request preparation; real codec dispatch has separate acceptance tests."""
    decoded = deepcopy(annotated if annotated is not None else request.get("content", {}))
    origins = {getattr(m, "origin", None) for m in decoded.get("messages", [])}
    agents = {o.snapshot.conversation for o in origins if o is not None}
    agent = agent or (next(iter(agents)) if len(agents) == 1 else str(uuid.uuid4()))
    prepared, snapshot = plugin._prepare_request(agent, request, decoded)
    return SimpleNamespace(
        request=request if annotated is not None else {**request, "content": prepared},
        annotated_request=prepared if annotated is not None else None,
        origin=Origin(plugin, snapshot),
    )


def call(name="run_shell", intent=INTENT, call_id="call1", turn=None, **args):
    """Simulate a host's proposal binding and its later H3 invocation, without tracking arguments."""
    args = args or {"command": "echo hello"}
    if intent is not None:
        args["nomos"] = deepcopy(intent)
    origin = _ORIGINS.get(str(turn)) if turn is not None else None
    if origin is not None:
        _NATIVE_AGENT.set(origin.snapshot.conversation)
        if (
            origin.plugin.store is not None
            and origin.plugin.store.native_turn(origin.snapshot.conversation, call_id) is None
        ):
            origin.plugin.store.bind_native_calls(origin.snapshot, [(call_id, name)])
    return SimpleNamespace(tool_name=name, tool_call_id=call_id, args=args, origin=origin)


def cite(turn, *ids, **kw):
    return call(intent={**INTENT, "evidence": list(ids)}, turn=turn, **kw)


TURN = re.compile(r"nt_[0-9a-f]{32}")  # internal fixture handles only


def nomos_message(outcome):
    """The optional evidence catalog, without any request tracking instruction."""
    messages = (outcome.annotated_request or outcome.request.get("content", {})).get("messages", [])
    return next(
        (
            m["content"]
            for m in reversed(messages)
            if m.get("role") in ("system", "developer")
            and m.get("content", "").startswith("Nomos evidence catalog")
        ),
        "",
    )


def turn(plugin, *messages, user="list files"):
    """Capture a unit-test model request and return its host-only origin handle."""
    msgs = list(messages) or [{"role": "user", "content": user}]
    return prepare(plugin, "model", {"content": {"messages": msgs, "tools": [TOOL]}}).origin


class TranscriptMessage(dict):
    """A normal JSON message with fixture-only native ownership on the Python object."""


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
    origins = [ctx.origin for ctx, _ in steps if getattr(ctx, "origin", None)]
    origin = origins[0] if origins and len({o.snapshot.conversation for o in origins}) == 1 else None
    owned = []
    for message in messages:
        item = TranscriptMessage(message)
        item.origin = origin
        owned.append(item)
    return owned
