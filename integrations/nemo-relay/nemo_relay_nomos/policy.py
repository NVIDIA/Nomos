# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded policy outcomes, distinct from Council votes and transport failures.

Only the authenticated Council service's explicit response status can request
human review. Error prose, missing fields and ordinary Council denials cannot.
Human approval may satisfy a review requirement, but never overrides a policy
prohibition. These helpers grant no execution authority by themselves.
"""

from __future__ import annotations

from typing import Any

from .wire import bounded


def policy_result(decision: dict[str, Any]) -> dict[str, Any] | None:
    """Bound human-review feedback only when the single status requests it.

    A legacy ``denied`` response stays denied even when old auxiliary fields
    claim that human review is possible. Mixed old/new inspection responses
    are rejected rather than assigning precedence to conflicting classifiers.
    """
    if decision.get("status") != "requires_human_approval" or not _non_executing_response(decision):
        return None
    reason = decision.get("error")
    guidance = decision.get("guidance")
    identity = decision.get("decision_id")
    return {
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
    return decision.get("status") in (
        "policy_allowed",
        "requires_human_approval",
    ) and _non_executing_response(decision)


def _non_executing_response(decision: dict[str, Any]) -> bool:
    """Reject mixed protocols and anything claiming authorization or execution.

    The core API normally has no execution outcome fields. If one is present,
    it must explicitly say nothing executed. A writ or writ identifier must
    be absent or empty; even false/numeric placeholders are malformed.
    """
    if "policy_decision" in decision:
        return False
    if any(decision.get(field) not in (None, "") for field in ("writ", "writ_id")):
        return False
    return all(
        field not in decision or decision[field] == "not_executed"
        for field in ("execution_status", "execution")
    )
