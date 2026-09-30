# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Consolidated review-audit payload (NOMOS Tier-1b, 1-to-1 ledger writes).

A normal council review used to emit ~7 ledger entries (submitted, evaluated, vote
opened, N votes, decision). Tier-1b collapses those to a single
``INTENT_REVIEW_RECORDED`` entry written once, after the review resolves, carrying
the full evaluation trace. This module owns the pure payload shape (schema v2) so
it can be unit-tested without a ledger or council.

Redaction contract: NEVER include bearer/auth tokens, raw secret values, private
keys, full stdout/stderr, or raw ``Intent.context``. The caller must use the
Intent persistence serializer; this builder also blanks context defensively so a
future caller cannot accidentally bypass that boundary.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 2


def build_intent_review_ledger_payload(
    *,
    request_type: str,
    intent_dict: dict[str, Any],
    council_id: str,
    agent_id: str,
    final_state: str,
    decision: str,
    denial_reason: str = "",
    approved_at: str | None = None,
    denied_at: str | None = None,
    opa: dict[str, Any] | None = None,
    policy_evaluation: dict[str, Any] | None = None,
    chairman: dict[str, Any] | None = None,
    code_review: dict[str, Any] | None = None,
    quorum: dict[str, Any] | None = None,
    debate_rounds: list[dict[str, Any]] | None = None,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the schema-v2 consolidated review record.

    All evaluation-trace sections default to empty so a deny-before-review path
    (e.g. OPA deny, policy deny) produces a valid record with no votes.
    """
    persisted_intent = dict(intent_dict)
    persisted_intent["context"] = ""
    return {
        "schema_version": SCHEMA_VERSION,
        "request_type": request_type,
        "intent_id": persisted_intent.get("intent_id", ""),
        "council_id": council_id,
        "agent_id": agent_id,
        "intent": persisted_intent,
        "redactions": {"intent_context": "redacted"},
        "opa": opa or {"enabled": False, "allow": True, "reason": ""},
        "policy_evaluation": policy_evaluation or {},
        "chairman": chairman or {},
        "code_review": code_review or {},
        "quorum": quorum or {},
        "debate_rounds": debate_rounds or [],
        "final": {
            "state": final_state,
            "decision": decision,
            "denial_reason": denial_reason,
            "approved_at": approved_at,
            "denied_at": denied_at,
        },
        "timing": timing or {},
    }
