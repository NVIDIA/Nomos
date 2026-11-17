# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded policy outcomes, distinct from Council votes and transport failures.

Only the authenticated Council service's explicit policy decision can request
human review. Error prose, missing fields and ordinary Council denials cannot.
Human approval may satisfy a review requirement, but never overrides a policy
prohibition. These helpers grant no execution authority by themselves.
"""

from __future__ import annotations

from typing import Any

from .wire import bounded


def policy_result(decision: dict[str, Any]) -> dict[str, Any] | None:
    """Return the safe policy payload only for an explicit non-executing decision."""
    value = decision.get("policy_decision")
    if decision.get("status") != "denied" or value not in ("forbidden", "requires_human_approval"):
        return None
    if decision.get("writ") or decision.get("writ_id"):
        return None
    reason = decision.get("error")
    guidance = decision.get("guidance")
    identity = decision.get("decision_id")
    return {
        "decision": value,
        "reason": bounded(reason, 512) if isinstance(reason, str) else "Policy did not permit execution.",
        "guidance": [bounded(item, 320) for item in guidance[:3] if isinstance(item, str)]
        if isinstance(guidance, list)
        else [],
        "decision_id": bounded(identity, 256) if isinstance(identity, str) else "",
    }


def permits_host_approved_replay(decision: dict[str, Any]) -> bool:
    """Accept policy inspection, not an authorization writ or a legacy approval.

    The caller must have independently verified the host's exact saved approval.
    An older service ignoring ``policy_only`` could return ``approved``; refusing
    that status prevents silently treating its Council decision as policy review.
    """
    if decision.get("writ") or decision.get("writ_id"):
        return False
    if decision.get("status") == "policy_allowed" and decision.get("policy_decision") == "allow":
        return True
    policy = policy_result(decision)
    return policy is not None and policy["decision"] == "requires_human_approval"
