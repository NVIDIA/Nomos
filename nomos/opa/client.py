# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client and input shaping for OPA-backed intent filtering."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

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
    """Small async wrapper around OPA's REST Data API."""

    def __init__(self, config: OPAConfig):
        self._url = config.url
        self._decision_path = config.decision_path
        self._timeout_seconds = config.timeout_ms / 1000

    async def evaluate(self, input_doc: Mapping[str, Any]) -> OPAPolicyDecision:
        """Evaluate the configured OPA decision document with the supplied input."""
        started = time.perf_counter()
        intent_id = _timing_intent_id(input_doc)
        outcome = "error"
        error: BaseException | None = None
        try:
            payload = await asyncio.to_thread(self._post_timed, input_doc, started, intent_id)
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
            details = {
                "event": "evaluate",
                "intent_id": intent_id,
                "outcome": outcome,
                "total_ms": (time.perf_counter() - started) * 1000,
                **_timing_error_details(error),
            }
            logger.info(
                "opa_evaluate_timing intent=%s outcome=%s total_ms=%.1f "
                "error_class=%s cause_class=%s error_origin=%s network_phase=unknown",
                intent_id,
                outcome,
                details["total_ms"],
                details["error_class"],
                details["cause_class"],
                details["error_origin"],
                extra={"opa_timing": details},
            )

    def _post_timed(
        self, input_doc: Mapping[str, Any], submitted: float, intent_id: str
    ) -> dict[str, Any]:
        """Measure dispatch and worker time without altering urllib or its deadlines.

        A started thread can finish after its caller is cancelled; emitting this
        record here preserves that evidence without draining or retrying the call.
        """
        started = time.perf_counter()
        error: BaseException | None = None
        try:
            return self._post(input_doc)
        except Exception as exc:
            error = exc
            raise
        finally:
            details = {
                "event": "worker",
                "intent_id": intent_id,
                "outcome": "error" if error is not None else "completed",
                "queue_ms": (started - submitted) * 1000,
                "worker_ms": (time.perf_counter() - started) * 1000,
                **_timing_error_details(error),
            }
            logger.info(
                "opa_worker_timing intent=%s outcome=%s queue_ms=%.1f worker_ms=%.1f "
                "error_class=%s cause_class=%s error_origin=%s network_phase=unknown",
                intent_id,
                details["outcome"],
                details["queue_ms"],
                details["worker_ms"],
                details["error_class"],
                details["cause_class"],
                details["error_origin"],
                extra={"opa_timing": details},
            )

    def _post(self, input_doc: Mapping[str, Any]) -> dict[str, Any]:
        request_body = json.dumps({"input": input_doc}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - URL is operator-configured OPA.
            f"{self._url}{self._decision_path}",
            data=request_body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise OPAEvaluationError(f"OPA returned HTTP {exc.code}: {details}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise OPAEvaluationError(f"OPA request failed: {exc}") from exc

        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise OPAEvaluationError(f"OPA returned invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise OPAEvaluationError("OPA response must be a JSON object")
        return decoded


def _timing_intent_id(input_doc: Mapping[str, Any]) -> str:
    intent = input_doc.get("intent")
    value = intent.get("intent_id") if isinstance(intent, Mapping) else None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return "-"


def _timing_error_details(error: BaseException | None) -> dict[str, str]:
    """Describe exception origin, not a network phase we did not measure."""
    cause = error.__cause__ if error is not None else None
    origin = "none" if error is None else "unknown"
    if isinstance(cause, urllib.error.HTTPError):
        origin = "http_status"
    elif isinstance(cause, urllib.error.URLError):
        origin = (
            "urllib_wrapped_timeout" if isinstance(cause.reason, TimeoutError) else "urllib_wrapped"
        )
    elif isinstance(cause, TimeoutError):
        origin = "direct_timeout"
    elif isinstance(cause, json.JSONDecodeError):
        origin = "json_decode"
    return {
        "error_class": type(error).__name__ if error is not None else "none",
        "cause_class": type(cause).__name__ if cause is not None else "none",
        "error_origin": origin,
        "network_phase": "unknown",
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
