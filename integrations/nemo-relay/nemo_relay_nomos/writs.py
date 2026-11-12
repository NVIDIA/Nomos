# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""What happens to an approved writ: validate it, claim it, dispatch it.

``DefaultWritHandler`` is the only implementation. ``WritHandler`` is the seam
a custom handler will implement; configuring ``writ_handler.kind = "custom"``
is accepted by the schema but refused here until a loader exists.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .nomos_client import Executor
from .records import executor_record, executor_signer, plugin_record, response
from .repository import EvidenceStore
from .settings import Settings, ToolPolicy
from .signing import PluginSigner
from .tool_call import StructuredToolCall, canonical_tool_arguments
from .verify import EvidenceError, verify_executor_evidence, verify_tool_call_writ, verify_writ
from .wire import bounded, canonical, sha256_hex

ToolCall = Callable[[dict], Awaitable[Any]]

LOCAL_RESULT_COMMAND = (
    "(run by the application, not an Executor) nomos-relay-plugin observed the local tool {tool} return stdout "
    "when called under writ {writ_id} with exactly these arguments: {arguments}"
)


@dataclass(frozen=True)
class WritCall:
    tool_name: str
    tool_call_id: str
    arguments: dict  # without ``nomos``
    command: str | None  # exactly what the v1 Council authorized
    policy: ToolPolicy
    decision_writ_id: str | None  # the writ_id the Council reported alongside the writ
    conversation: str  # the journal this call belongs to
    next_: ToolCall | None = None  # runs the application's tool (local executor)
    requested_at: float | None = None  # when the Council was asked; an older writ is a replayed approval
    structured: StructuredToolCall | None = None  # exact held v2 operation and canonical argument bytes


@dataclass(frozen=True)
class HandlerResult:
    response: dict  # model-visible
    record: dict | None  # stored evidence for this response; None when the plugin has no signing key
    annotation: dict | None = None  # extra host-only annotation (e.g. the local tool's own)
    writ_valid: bool = True  # False when the writ was refused before dispatch


class WritHandler(Protocol):
    async def handle(self, call: WritCall, encoded_writ: str, writ: dict) -> HandlerResult: ...


class DefaultWritHandler:
    def __init__(
        self, settings: Settings, store: EvidenceStore, executor: Executor, signer: PluginSigner | None
    ):
        self.s, self.store, self.executor, self.signer = settings, store, executor, signer

    async def handle(self, call: WritCall, encoded_writ: str, writ: dict) -> HandlerResult:
        conn = self.s.connection
        try:
            if call.policy.executor == "local":
                if call.structured is None or call.command is not None:
                    raise EvidenceError("invalid_tool_policy")
                verified = verify_tool_call_writ(
                    writ,
                    self.s.trust.as_dict(),
                    agent_id=conn.agent_id,
                    council_id=conn.council_id,
                    operation=call.structured.operation,
                    writ_id=call.decision_writ_id,
                    dispatch=True,
                    issued_after=call.requested_at,
                )
                # The continuation receives exactly the bytes whose digest was authorized.
                try:
                    held_bytes = canonical_tool_arguments(call.arguments)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise EvidenceError("tool_arguments_changed") from exc
                if held_bytes != call.structured.arguments_bytes:
                    raise EvidenceError("tool_arguments_changed")
                effective_writ = verified
            elif call.policy.executor == "nomos_http":
                if call.structured is not None or call.command is None:
                    raise EvidenceError("invalid_tool_policy")
                verify_writ(
                    writ,
                    self.s.trust.as_dict(),
                    agent_id=conn.agent_id,
                    council_id=conn.council_id,
                    command=call.command,
                    working_directory=conn.working_directory,
                    writ_id=call.decision_writ_id,
                    dispatch=True,
                    issued_after=call.requested_at,
                )
                effective_writ = writ
            else:
                raise EvidenceError("invalid_tool_policy")
        except EvidenceError as exc:
            return self._refusal(call, str(exc), "The council's writ failed local validation; nothing ran.")
        if self.s.claim_for(call.policy) == "council":
            raise NotImplementedError("council writ claims need a Nomos claim API (council_consume_writ)")
        # The plugin's own ledger always records the nonce, whatever else claims it: this plugin never
        # dispatches one writ twice. For nomos_http the Executor also claims it, in its own store.
        if not self.store.claim_writ(effective_writ["writ_id"], effective_writ["nonce"]):
            return self._refusal(call, "writ_replayed", "This writ was already used; nothing ran.")
        self.store.dispatch(call.conversation, call.tool_call_id)
        if call.policy.executor == "nomos_http":
            return await self._nomos_http(call, encoded_writ, writ)
        return await self._local(call, effective_writ)

    async def _nomos_http(self, call: WritCall, encoded_writ: str, writ: dict) -> HandlerResult:
        try:
            result = await self.executor.execute(encoded_writ, self.s.connection.agent_id)
            if len(canonical(result)) > self.s.limits.max_result_bytes:
                raise ValueError("result_too_large")
        except asyncio.CancelledError:
            raise
        except Exception:
            out, record = self._outcome(
                call,
                "execution_unknown",
                "execution_unknown",
                writ_id=writ["writ_id"],
                execution="unknown",
                message="The executor's outcome is unknown. Do not retry; an operator must reconcile this session.",
            )
            return HandlerResult(out, record)
        now = time.time()
        evidence_id = self.store.new_id()
        try:
            verify_executor_evidence(
                result,
                writ,
                self.s.trust.as_dict(),
                agent_id=self.s.connection.agent_id,
                council_id=self.s.connection.council_id,
                now=now,
            )
            error = None
        except EvidenceError as exc:
            error = str(exc)
        record = (
            None
            if self.signer is None
            else executor_record(
                evidence_id=evidence_id,
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                arguments=call.arguments,
                result=result,
                writ=writ,
                verified=error is None,
                verification_error=error,
                receipt_signer=self.signer,
                policy_version=self.s.trust.policy_version,
                now=now,
            )
        )
        stored = {} if record is None else {"evidence_id": evidence_id}
        if error is not None:
            out = response(
                "evidence_invalid",
                execution="unknown",
                evidence="invalid",
                verification_error=error,
                writ_id=writ["writ_id"],
                **stored,
                **({} if record is None else {"quarantine_id": evidence_id}),
                message="The executor's result failed verification and is "
                + ("quarantined" if record is not None else "withheld")
                + ". The action may have run; do not retry.",
            )
            return HandlerResult(out, record)
        return HandlerResult(self.executor_response(result, writ["writ_id"], **stored), record)

    def executor_response(self, result: dict, writ_id: str, evidence_id: str | None = None) -> dict:
        """The model-visible view of a verified Executor result (``evidence_id`` only when it was stored)."""
        limit = self.s.limits.max_output_chars
        stage = result["verification_stage"]
        execution = (
            "executed"
            if result["success"]
            else "failed"
            if stage in ("passed", "execution", "timeout")
            else ("not_executed")
        )
        out = response(
            "executed" if result["success"] else "execution_failed",
            execution=execution,
            evidence="locally_verified",
            **({} if evidence_id is None else {"evidence_id": evidence_id}),
            writ_id=writ_id,
            exit_code=result["exit_code"],
            stdout=bounded(result["stdout"], limit),
            stderr=bounded(result["stderr"], limit),
            signer=executor_signer(result),
            content_sha256=sha256_hex(canonical(result)),
        )
        if not result["success"]:
            out["error"] = bounded(result["error"] or stage, 1024)
        return out

    async def _local(self, call: WritCall, writ: dict) -> HandlerResult:
        if call.next_ is None:
            raise RuntimeError("a local executor needs the tool continuation")
        annotation = None
        try:
            result = await call.next_(call.arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            content = {"status": "failed", "error": bounded(f"{type(exc).__name__}: {exc}", 2048)}
        else:
            output = getattr(result, "result", result)
            annotation = getattr(result, "annotation", None)
            try:
                encoded = canonical(output)
            except (TypeError, ValueError):  # not JSON: its repr, bounded the same way
                output = repr(output)
                encoded = canonical(output)
            if len(encoded) > self.s.limits.max_result_bytes:
                output = bounded(encoded.decode("utf-8", "replace"), self.s.limits.max_output_chars)
            content = {"status": "executed", "output": output}
        executed = content["status"] == "executed"
        result_fields = {"output": content["output"]} if executed else {"error": content["error"]}
        if self.signer is None:  # no signing key: the result is returned, never stored
            out = response(
                "executed" if executed else "execution_failed",
                execution="executed" if executed else "failed",
                writ_id=writ["writ_id"],
                **result_fields,
            )
            return HandlerResult(out, None, annotation)
        certified = self._certify_local(call, writ, content)
        record = self._plugin(
            call,
            "local_result",
            content,
            self.store.new_id(),
            writ_id=writ["writ_id"],
            certified_result=certified,
        )
        out = response(
            "executed" if executed else "execution_failed",
            execution="executed" if executed else "failed",
            evidence="plugin_certified",
            evidence_id=record["evidence_id"],
            writ_id=writ["writ_id"],
            signer=record["signer"],
            content_sha256=record["content_sha256"],
            **result_fields,
        )
        return HandlerResult(out, record, annotation)

    def _certify_local(self, call: WritCall, writ: dict, content: dict) -> dict:
        """The local result as an ExecutionResult signed with signing_key, bound to the verified writ ``writ``.

        stdout is the plugin's observation: the tool, the digest of its exact arguments and what it returned.
        """
        assert self.signer is not None and call.structured is not None  # only reached with both
        conn = self.s.connection
        observed = {
            "type": "nomos.local_tool_result",
            "version": 1,
            "tool": call.tool_name,
            "tool_call_id": call.tool_call_id,
            "writ_id": writ["writ_id"],
            "arguments_sha256": call.structured.operation["arguments_sha256"],
            **content,
        }
        return self.signer.certify(
            json.dumps(observed, separators=(",", ":"), allow_nan=False),
            command=LOCAL_RESULT_COMMAND.format(
                tool=call.tool_name,
                writ_id=writ["writ_id"],
                arguments=call.structured.arguments_bytes.decode("utf-8"),
            ),
            agent_id=conn.agent_id,
            council_id=conn.council_id,
            request_id=call.tool_call_id,
            success=content["status"] == "executed",
            error=content.get("error"),
            writ=writ,
        )

    def _refusal(self, call: WritCall, status: str, message: str) -> HandlerResult:
        out, record = self._outcome(call, "plugin_refusal", status, message=message)
        return HandlerResult(out, record, writ_valid=False)

    def _outcome(
        self, call: WritCall, kind: str, status: str, *, writ_id: str | None = None, **fields: Any
    ) -> tuple[dict, dict | None]:
        """A response the handler produced, with its signed record. Without a signing key there is no record."""
        extra = {} if writ_id is None else {"writ_id": writ_id}
        if self.signer is None:
            return response(status, **extra, **fields), None
        evidence_id = self.store.new_id()
        out = response(status, evidence="plugin_signed", evidence_id=evidence_id, **extra, **fields)
        return out, self._plugin(call, kind, out, evidence_id, writ_id=writ_id)

    def _plugin(
        self,
        call: WritCall,
        kind: str,
        content: dict,
        evidence_id: str,
        *,
        writ_id: str | None = None,
        certified_result: dict | None = None,
    ) -> dict:
        return plugin_record(
            evidence_id=evidence_id,
            kind=kind,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            arguments=call.arguments,
            content=content,
            signer=self.signer,
            writ_id=writ_id,
            certified_result=certified_result,
            now=time.time(),
        )


def load_writ_handler(
    settings: Settings, *, store: EvidenceStore, executor: Executor, signer: PluginSigner | None
) -> WritHandler:
    if settings.writ_handler.kind == "custom":
        # Reserved: settings.writ_handler.entrypoint / .options will name and configure it.
        raise NotImplementedError("custom writ handlers are not implemented yet")
    return DefaultWritHandler(settings, store, executor, signer)
