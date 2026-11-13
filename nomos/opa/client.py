# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client and input shaping for OPA-backed intent filtering."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent
from nomos.level_1.policy.paths import normalized_path_references
from nomos.level_1.policy.shell import extract_shell_command_words

logger = logging.getLogger(__name__)


class OPAEvaluationError(RuntimeError):
    """Raised when OPA cannot return a usable decision."""


@dataclass(frozen=True)
class OPAPolicyDecision:
    """Normalized OPA policy decision."""

    allow: bool
    reasons: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)
    decision_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class OPAHttpClient:
    """Bounded reusable async transport for OPA's REST Data API.

    Admission has its own finite queue and deadline. Network connect/read/write/
    pool deadlines retain timeout_ms each; they are not a total request deadline.
    Redirects fail closed: a policy POST is never silently replayed elsewhere.
    """

    def __init__(self, config: OPAConfig):
        self._url = config.url
        self._decision_path = config.decision_path
        self._timeout_seconds = config.timeout_ms / 1000
        self._admission_seconds = config.admission_timeout_ms / 1000
        self._max_connections = config.max_connections
        self._capacity = config.max_connections + config.max_queued_requests
        self._slots = asyncio.Semaphore(config.max_connections)
        self._reservations = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._client: httpx.AsyncClient | None = None
        self._close_task: asyncio.Task[None] | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Default transport has retries=0. Supplying no custom transport is
            # also necessary for httpx to honor environment proxy configuration.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                limits=httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=self._max_connections,
                ),
                # Preserve urllib's OS/enterprise trust and SSL_CERT_FILE/DIR.
                verify=ssl.create_default_context(),
                trust_env=True,
                follow_redirects=False,
            )
        return self._client

    async def evaluate(self, input_doc: Mapping[str, Any]) -> OPAPolicyDecision:
        """Evaluate once, preserving fail-closed errors and bounded ownership."""
        started = time.perf_counter()
        intent_id = _timing_intent_id(input_doc)
        outcome = "error"
        phase = "admission"
        error: BaseException | None = None
        reserved = acquired = False
        queue_ms = request_ms = 0.0
        try:
            if self._close_task is not None:
                raise OPAEvaluationError("OPA client is closed")
            if self._reservations >= self._capacity:
                raise OPAEvaluationError("OPA request admission queue is full")
            self._reservations += 1
            self._idle.clear()
            reserved = True
            try:
                async with asyncio.timeout(self._admission_seconds):
                    await self._slots.acquire()
            except TimeoutError as exc:
                raise OPAEvaluationError("OPA request admission timed out") from exc
            acquired = True
            queue_ms = (time.perf_counter() - started) * 1000
            phase = "http"
            request_started = time.perf_counter()
            try:
                payload = await self._post(input_doc)
            finally:
                request_ms = (time.perf_counter() - request_started) * 1000
            phase = "decision"
            decision = parse_opa_decision(payload)
            outcome = "allow" if decision.allow else "deny"
            return decision
        except asyncio.CancelledError as exc:
            outcome = "cancelled"
            error = exc
            raise
        except Exception as exc:
            error = exc
            raise
        finally:
            if acquired:
                self._slots.release()
            if reserved:
                self._reservations -= 1
                if self._reservations == 0:
                    self._idle.set()
            if phase == "admission":
                queue_ms = (time.perf_counter() - started) * 1000
            details = {
                "event": "evaluate",
                "transport": "async_httpx",
                "intent_id": intent_id,
                "outcome": outcome,
                "total_ms": (time.perf_counter() - started) * 1000,
                "queue_ms": queue_ms,
                "request_ms": request_ms,
                **_timing_error_details(error, phase),
            }
            logger.info(
                "opa_evaluate_timing transport=async_httpx intent=%s outcome=%s "
                "total_ms=%.1f queue_ms=%.1f request_ms=%.1f "
                "error_class=%s cause_class=%s error_origin=%s network_phase=%s",
                intent_id,
                outcome,
                details["total_ms"],
                queue_ms,
                request_ms,
                details["error_class"],
                details["cause_class"],
                details["error_origin"],
                details["network_phase"],
                extra={"opa_timing": details},
            )

    async def _post(self, input_doc: Mapping[str, Any]) -> dict[str, Any]:
        request_body = json.dumps({"input": input_doc}, separators=(",", ":")).encode("utf-8")
        try:
            response = await self._http_client().post(
                f"{self._url}{self._decision_path}",
                content=request_body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise OPAEvaluationError(f"OPA request failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise OPAEvaluationError(f"OPA returned HTTP {response.status_code}: {response.text}")
        try:
            decoded = json.loads(response.content.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OPAEvaluationError("OPA returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise OPAEvaluationError("OPA response must be a JSON object")
        return decoded

    async def aclose(self) -> None:
        """Reject new evaluations and drain admitted calls before closing sockets."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if self._close_task.done():
                    self._close_task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        await self._idle.wait()
        if self._client is not None:
            await self._client.aclose()


def _timing_intent_id(input_doc: Mapping[str, Any]) -> str:
    intent = input_doc.get("intent")
    value = intent.get("intent_id") if isinstance(intent, Mapping) else None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return "-"


def _timing_error_details(error: BaseException | None, phase: str) -> dict[str, str]:
    """Report transport exception phases without exposing URLs or response text."""
    cause = error.__cause__ if error is not None else None
    origin = "none" if error is None else phase
    network_phase = "unknown"
    if isinstance(cause, httpx.ConnectTimeout | httpx.ConnectError):
        network_phase = "connect"
    elif isinstance(cause, httpx.ReadTimeout | httpx.ReadError):
        network_phase = "read"
    elif isinstance(cause, httpx.WriteTimeout | httpx.WriteError):
        network_phase = "write"
    elif isinstance(cause, httpx.PoolTimeout):
        network_phase = "pool"
    elif isinstance(cause, json.JSONDecodeError | UnicodeDecodeError):
        origin = "json_decode"
    return {
        "error_class": type(error).__name__ if error is not None else "none",
        "cause_class": type(cause).__name__ if cause is not None else "none",
        "error_origin": origin,
        "network_phase": network_phase,
    }


def parse_opa_decision(payload: Mapping[str, Any]) -> OPAPolicyDecision:
    """Normalize OPA Data API responses into the council's decision shape."""
    decision_id = _string_or_empty(payload.get("decision_id"))
    result = payload.get("result")

    if result is None:
        return OPAPolicyDecision(
            allow=False,
            reasons=["OPA decision was undefined"],
            guidance=["Check that the configured OPA decision path returns a result object."],
            decision_id=decision_id,
        )

    if isinstance(result, bool):
        return OPAPolicyDecision(
            allow=result,
            reasons=[] if result else ["OPA policy returned false"],
            decision_id=decision_id,
        )

    if not isinstance(result, Mapping):
        raise OPAEvaluationError("OPA result must be a boolean or object")

    allow = result.get("allow", result.get("allowed"))
    if not isinstance(allow, bool):
        raise OPAEvaluationError("OPA result object must include boolean 'allow'")

    reasons = _string_list(result.get("reasons"))
    reason = result.get("reason")
    if isinstance(reason, str) and reason.strip():
        reasons.append(reason.strip())
    if not allow and not reasons:
        reasons.append("OPA policy denied the intent")

    guidance = _string_list(result.get("guidance"))
    metadata = result.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    return OPAPolicyDecision(
        allow=allow,
        reasons=reasons,
        guidance=guidance,
        decision_id=decision_id,
        metadata=metadata,
    )


def build_intent_filter_input(
    intent: Intent,
    *,
    request_type: str,
    authenticated: bool,
    opa_config: OPAConfig | None = None,
    agent_permissions: Iterable[str] = (),
    agent_councils: Iterable[str] = (),
    required_permissions: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the structured input document sent to OPA for intent filtering."""
    if opa_config is None:
        opa_config = OPAConfig()
    return {
        "request": {
            "type": request_type,
            "authenticated": authenticated,
        },
        "council": {
            "id": intent.council_id,
            "opa_policy_id": opa_config.policy_id,
            "opa_relaxed_bare_command_words": sorted(set(opa_config.relaxed_bare_command_words)),
            "opa_relaxed_working_dir_prefixes": sorted(
                set(opa_config.relaxed_working_dir_prefixes)
            ),
        },
        "agent": {
            "id": intent.agent_id,
            "permissions": sorted(set(agent_permissions)),
            "councils": sorted(set(agent_councils)),
        },
        "required_permissions": sorted(set(required_permissions)),
        "intent": {
            "intent_id": intent.intent_id,
            "agent_id": intent.agent_id,
            "council_id": intent.council_id,
            "action_type": intent.action_type,
            "description": intent.description,
            "target_resource": intent.target_resource,
            "command": intent.command,
            "command_type": str(intent.command_type),
            "shell_command_words": extract_shell_command_words(intent.command)
            if str(intent.command_type) == "shell" and intent.command
            else [],
            "normalized_path_refs": normalized_path_references(
                intent.command,
                working_directory=intent.working_directory,
                target_resource=intent.target_resource,
            ),
            "working_directory": intent.working_directory,
            "target_host": intent.target_host,
            "target_environment": str(intent.target_environment or ""),
            "target_cluster": intent.target_cluster,
            "target_namespace": intent.target_namespace,
            "target_container": intent.target_container,
            "context": intent.context,
            "operation": intent.operation.model_dump(mode="json")
            if intent.operation is not None
            else None,
            "justification": intent.justification,
            "blast_radius": intent.blast_radius,
            "evidence_count": len(intent.evidence),
            "has_rollback_plan": intent.rollback_plan is not None,
            "packaged_files": [
                {
                    "path": packaged_file.path,
                    "sha256": packaged_file.sha256,
                    "mode": packaged_file.mode,
                    "description": packaged_file.description,
                    "size_bytes": len(packaged_file.decoded_bytes()),
                }
                for packaged_file in intent.packaged_files
            ],
        },
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
