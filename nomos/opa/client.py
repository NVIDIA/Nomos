# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client and input shaping for OPA-backed intent filtering."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent
from nomos.level_1.policy.paths import normalized_path_references
from nomos.level_1.policy.shell import extract_shell_command_words
from nomos.opa.shell_analysis import analyze_shell_command
from nomos_runtime import (
    ClientTLSConfig,
    require_secure_url,
    strict_transport_profile,
    urlopen_no_redirect,
)


class OPAEvaluationError(RuntimeError):
    """Raised when OPA cannot return a usable decision."""


@dataclass(frozen=True)
class OPAPolicyDecision:
    """Normalized OPA policy decision, distinct from authorization API status.

    Only an explicit ``decision="requires_human_approval"`` requests a human.
    Legacy boolean-only policies leave ``decision`` unset and cannot request one.
    """

    allow: bool
    reasons: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)
    decision_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    decision: Literal["allow", "deny", "requires_human_approval"] | None = None


class OPAHttpClient:
    """Small async wrapper around OPA's REST Data API."""

    def __init__(self, config: OPAConfig):
        self._url = config.url
        self._decision_path = config.decision_path
        self._timeout_seconds = config.timeout_ms / 1000
        self._tls = ClientTLSConfig.from_env("COUNCIL_OPA")
        require_secure_url(
            self._url,
            tls=self._tls,
            label="COUNCIL_OPA_URL",
            strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
        )

    async def evaluate(self, input_doc: Mapping[str, Any]) -> OPAPolicyDecision:
        """Evaluate the configured OPA decision document with the supplied input."""
        payload = await asyncio.to_thread(self._post, input_doc)
        return parse_opa_decision(payload)

    def _post(self, input_doc: Mapping[str, Any]) -> dict[str, Any]:
        request_body = json.dumps({"input": input_doc}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - URL is operator-configured OPA.
            f"{self._url}{self._decision_path}",
            data=request_body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen_no_redirect(
                request,
                timeout=self._timeout_seconds,
                **self._tls.urllib_kwargs(),
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8", errors="replace")
            except (OSError, http.client.HTTPException):
                details = ""
            raise OPAEvaluationError(f"OPA returned HTTP {exc.code}: {details}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # urllib turns only a failure to send into URLError. What goes wrong
            # after the request is sent arrives as it is: a TLS alert (a refused
            # client certificate under TLS 1.3 comes after the handshake), a
            # closed or reset connection, a truncated body. URLError and
            # TimeoutError are OSError as well.
            raise OPAEvaluationError(f"OPA request failed: {exc}") from exc

        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise OPAEvaluationError(f"OPA returned invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise OPAEvaluationError("OPA response must be a JSON object")
        return decoded


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
            metadata={"undefined": True},
        )

    if isinstance(result, bool):
        return OPAPolicyDecision(
            allow=result,
            reasons=[] if result else ["OPA policy returned false"],
            decision_id=decision_id,
        )

    if not isinstance(result, Mapping):
        raise OPAEvaluationError("OPA result must be a boolean or object")

    # The explicit outcome is authoritative. Legacy boolean-only policies remain
    # usable, but obsolete or contradictory discriminators must fail closed.
    if "policy_decision" in result:
        raise OPAEvaluationError("OPA policy_decision is obsolete; use decision")
    decision = result.get("decision")
    if "decision" in result and decision not in ("allow", "deny", "requires_human_approval"):
        raise OPAEvaluationError("OPA decision is invalid")
    has_allow = "allow" in result or "allowed" in result
    for key in ("allow", "allowed"):
        if key in result and not isinstance(result[key], bool):
            raise OPAEvaluationError(f"OPA {key} must be boolean")
    if "allow" in result and "allowed" in result and result["allow"] != result["allowed"]:
        raise OPAEvaluationError("OPA allow contradicts allowed")
    allow = result.get("allow", result.get("allowed"))
    if decision is not None:
        expected_allow = decision == "allow"
        if has_allow and allow != expected_allow:
            raise OPAEvaluationError("OPA allow contradicts decision")
        allow = expected_allow
    elif not has_allow:
        raise OPAEvaluationError("OPA result object must include decision or boolean allow")

    reasons = _string_list(result.get("reasons"))
    reason = result.get("reason")
    if isinstance(reason, str) and reason.strip():
        reasons.append(reason.strip())
    if not allow and not reasons:
        reasons.append(
            "OPA policy requires human approval"
            if decision == "requires_human_approval"
            else "OPA policy denied the intent"
        )

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
        decision=decision,
    )


def _workspace_root(value: Any) -> str | None:
    """Accept a literal absolute host path without probing or rewriting it.

    The Council service may run on a different machine or operating system.
    Accept POSIX and Windows roots, but do not expand variables, resolve symlinks
    or claim containment. Null means a configured argument is missing/invalid;
    a policy requiring workspace context must handle that explicitly.
    """
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        return None
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return value
    return None


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
    material = getattr(intent, "_tool_review_material", None)
    tool_input: dict[str, Any] = {}
    if material is not None and intent.operation is not None:
        # Use the held, validated request, never a model description or digest lookup.
        # Keep this live input out of Intent serialization and the signed operation.
        tool_input["tool_arguments"] = json.loads(material.canonical_arguments())
        workspace_key = opa_config.workspace_arguments.get(material.tool_name)
        if workspace_key is not None:
            tool_input["workspace"] = _workspace_root(
                tool_input["tool_arguments"].get(workspace_key)
            )
        command_key = opa_config.shell_tool_arguments.get(material.tool_name)
        if opa_config.policy_id == "local-bash" and command_key:
            tool_input["shell_analysis"] = analyze_shell_command(
                tool_input["tool_arguments"].get(command_key)
            )
    return {
        "request": {
            "type": request_type,
            "authenticated": authenticated,
        },
        "council": {
            "id": intent.council_id,
            "opa_policy_id": opa_config.policy_id,
            # Only operator configuration supplies prohibitions. A tool's JSON
            # arguments cannot replace this separate council-owned namespace.
            "opa_denied_shell_commands": sorted(set(opa_config.denied_shell_commands)),
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
            **tool_input,
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
