# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Round-trip tests for IntentWorkflow durable (resumable-subset) serialization.

Foundation for the shared workflow store: the durable record must
faithfully reconstruct the state another council replica needs to resume a
multi-step flow and answer status queries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nomos.level_0.types import CapabilityToken, Intent
from nomos.level_0.types.capability import CapabilityScope
from nomos.level_4.council.workflow import IntentWorkflow, WorkflowState


def _intent() -> Intent:
    return Intent.create(
        agent_id="agent-1",
        description="durable workflow round-trip test",
        target_resource="/workspace",
        command="/bin/echo hi",
    )


def test_durable_roundtrip_resumable_fields():
    now = datetime.now(UTC)
    wf = IntentWorkflow(
        intent=_intent(),
        state=WorkflowState.AWAITING_APPROVAL,
        submitted_at=now,
        evaluated_at=now,
        metadata={"decision_expires_at": now.isoformat(), "k": "v"},
    )
    wf.record_ledger_sequence(41)
    wf.record_ledger_sequence(42)

    restored = IntentWorkflow.from_durable_dict(wf.to_durable_dict())

    assert restored.intent_id == wf.intent_id
    assert restored.intent.command == wf.intent.command
    assert restored.state == WorkflowState.AWAITING_APPROVAL
    assert restored.submitted_at == now
    assert restored.evaluated_at == now
    assert restored.ledger_sequences == [41, 42]
    assert restored.metadata == {"decision_expires_at": now.isoformat(), "k": "v"}
    assert restored.capability_token is None


def test_durable_roundtrip_with_capability_token():
    now = datetime.now(UTC)
    intent = _intent()
    scope = CapabilityScope(action_type="file.delete", resource_pattern="/workspace/*")
    token = CapabilityToken.create(
        intent_id=intent.intent_id,
        scope=scope,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        ledger_anchor="anchor-hash",
        issuer_id="council-prod",
        bearer_agent_id="agent-1",
    )
    wf = IntentWorkflow(
        intent=intent,
        state=WorkflowState.APPROVED,
        submitted_at=now,
        approved_at=now,
        capability_token=token,
        token_minted_at=now,
    )

    restored = IntentWorkflow.from_durable_dict(wf.to_durable_dict())

    assert restored.state == WorkflowState.APPROVED
    assert restored.approved_at == now
    assert restored.capability_token is not None
    assert restored.capability_token.token_id == token.token_id
    assert restored.capability_token.scope.action_type == "file.delete"
    assert restored.capability_token.bearer_agent_id == "agent-1"


def test_durable_roundtrip_denied_terminal():
    now = datetime.now(UTC)
    wf = IntentWorkflow(
        intent=_intent(),
        state=WorkflowState.DENIED,
        submitted_at=now,
        denied_at=now,
        denial_reason="policy denied",
        completed_at=now,
        error="denied by council",
    )

    restored = IntentWorkflow.from_durable_dict(wf.to_durable_dict())

    assert restored.state == WorkflowState.DENIED
    assert restored.is_terminal
    assert restored.denial_reason == "policy denied"
    assert restored.error == "denied by council"
    assert restored.denied_at == now
    assert restored.completed_at == now
