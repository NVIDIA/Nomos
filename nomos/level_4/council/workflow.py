# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Intent workflow: tracks an intent through its full lifecycle.

From submission to execution to verification.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

from nomos.level_0.types import CapabilityToken, Intent
from nomos.level_1.policy import EvaluationResult
from nomos.level_2.quorum import VoteRecord
from nomos.level_3.executor import ExecutionResult
from nomos.level_3.verifier import VerificationResult


class WorkflowState(Enum):
    """States in the intent workflow."""

    SUBMITTED = "submitted"
    EVALUATING = "evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    TOKEN_MINTED = "token_minted"  # noqa: S105
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class IntentWorkflow:
    """
    Tracks an intent through its complete lifecycle.

    This is the audit trail for a single intent from submission to completion.
    """

    intent: Intent
    state: WorkflowState = WorkflowState.SUBMITTED
    submitted_at: datetime | None = None

    evaluation_result: EvaluationResult | None = None
    evaluated_at: datetime | None = None

    vote_record: VoteRecord | None = None
    approved_at: datetime | None = None
    denied_at: datetime | None = None
    denial_reason: str = ""

    capability_token: CapabilityToken | None = None
    token_minted_at: datetime | None = None

    execution_result: ExecutionResult | None = None
    executed_at: datetime | None = None

    verification_result: VerificationResult | None = None
    verified_at: datetime | None = None

    rollback_result: ExecutionResult | None = None
    rolled_back_at: datetime | None = None

    completed_at: datetime | None = None
    error: str | None = None

    ledger_sequences: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def intent_id(self) -> str:
        return self.intent.intent_id

    @property
    def is_terminal(self) -> bool:
        """Check if workflow is in a terminal state."""
        return self.state in (
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
            WorkflowState.DENIED,
            WorkflowState.EXPIRED,
            WorkflowState.ROLLED_BACK,
        )

    @property
    def succeeded(self) -> bool:
        return self.state == WorkflowState.COMPLETED

    # Valid state transitions — maps each state to the set of states it
    # can move to.  Any transition not listed here is illegal.
    _VALID_TRANSITIONS: ClassVar[dict[WorkflowState, set[WorkflowState]]] = {
        WorkflowState.SUBMITTED: {WorkflowState.EVALUATING, WorkflowState.FAILED},
        WorkflowState.EVALUATING: {
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.APPROVED,
            WorkflowState.DENIED,
            WorkflowState.FAILED,
        },
        WorkflowState.AWAITING_APPROVAL: {
            WorkflowState.APPROVED,
            WorkflowState.DENIED,
            WorkflowState.EXPIRED,
            WorkflowState.FAILED,
        },
        WorkflowState.APPROVED: {
            WorkflowState.TOKEN_MINTED,
            WorkflowState.EXECUTING,
            WorkflowState.EXPIRED,
            WorkflowState.FAILED,
        },
        WorkflowState.TOKEN_MINTED: {WorkflowState.EXECUTING, WorkflowState.FAILED},
        WorkflowState.EXECUTING: {WorkflowState.EXECUTED, WorkflowState.FAILED},
        WorkflowState.EXECUTED: {
            WorkflowState.VERIFYING,
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
        },
        WorkflowState.VERIFYING: {
            WorkflowState.VERIFIED,
            WorkflowState.VERIFICATION_FAILED,
            WorkflowState.FAILED,
        },
        WorkflowState.VERIFIED: {WorkflowState.COMPLETED},
        WorkflowState.VERIFICATION_FAILED: {
            WorkflowState.ROLLING_BACK,
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
        },
        WorkflowState.ROLLING_BACK: {WorkflowState.ROLLED_BACK, WorkflowState.FAILED},
        # Terminal states — no outgoing transitions
        WorkflowState.ROLLED_BACK: set(),
        WorkflowState.COMPLETED: set(),
        WorkflowState.FAILED: set(),
        WorkflowState.DENIED: set(),
        WorkflowState.EXPIRED: set(),
    }

    def transition_to(self, new_state: WorkflowState) -> None:
        """Transition to a new state.

        Raises ``ValueError`` if the transition is not allowed by the
        state machine.
        """
        allowed = self._VALID_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Invalid workflow transition: {self.state.value} -> {new_state.value}. "
                f"Allowed: {sorted(s.value for s in allowed)}"
            )
        self.state = new_state

    def record_ledger_sequence(self, sequence: int) -> None:
        """Record a ledger sequence number associated with this workflow."""
        self.ledger_sequences.append(sequence)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.to_dict(),
            "state": self.state.value,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "evaluation_result": self.evaluation_result.to_dict()
            if self.evaluation_result
            else None,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "vote_record": self.vote_record.to_dict() if self.vote_record else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "denied_at": self.denied_at.isoformat() if self.denied_at else None,
            "denial_reason": self.denial_reason,
            "capability_token": self.capability_token.to_dict() if self.capability_token else None,
            "token_minted_at": self.token_minted_at.isoformat() if self.token_minted_at else None,
            "execution_result": self.execution_result.to_dict() if self.execution_result else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "verification_result": self.verification_result.to_dict()
            if self.verification_result
            else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "rollback_result": self.rollback_result.to_dict() if self.rollback_result else None,
            "rolled_back_at": self.rolled_back_at.isoformat() if self.rolled_back_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "ledger_sequences": self.ledger_sequences,
            "metadata": self.metadata,
        }

    def to_durable_dict(self) -> dict:
        """Serialize the *resumable* workflow state for a shared store (NOMOS-5b).

        Unlike ``to_dict`` (the full audit view), this carries only what another
        council replica needs to resume a multi-step flow and answer status
        queries: the intent, state, lifecycle timestamps, the minted capability
        token, error/denial, ledger sequences, and metadata. The heavy
        intermediate results (evaluation/vote/execution/verification) are
        intentionally omitted — they are already persisted in the durable
        ledger, so re-storing them here would duplicate the audit surface.
        """
        return {
            "intent": self.intent.to_dict(),
            "state": self.state.value,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "denied_at": self.denied_at.isoformat() if self.denied_at else None,
            "denial_reason": self.denial_reason,
            "capability_token": self.capability_token.to_dict() if self.capability_token else None,
            "token_minted_at": self.token_minted_at.isoformat() if self.token_minted_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "rolled_back_at": self.rolled_back_at.isoformat() if self.rolled_back_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "ledger_sequences": list(self.ledger_sequences),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_durable_dict(cls, data: dict) -> "IntentWorkflow":
        """Reconstruct a resumable workflow from ``to_durable_dict`` output."""

        def _dt(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if value else None

        token = data.get("capability_token")
        return cls(
            intent=Intent.from_dict(data["intent"]),
            state=WorkflowState(data["state"]),
            submitted_at=_dt("submitted_at"),
            evaluated_at=_dt("evaluated_at"),
            approved_at=_dt("approved_at"),
            denied_at=_dt("denied_at"),
            denial_reason=data.get("denial_reason", ""),
            capability_token=CapabilityToken.from_dict(token) if token else None,
            token_minted_at=_dt("token_minted_at"),
            executed_at=_dt("executed_at"),
            verified_at=_dt("verified_at"),
            rolled_back_at=_dt("rolled_back_at"),
            completed_at=_dt("completed_at"),
            error=data.get("error"),
            ledger_sequences=list(data.get("ledger_sequences", [])),
            metadata=dict(data.get("metadata", {})),
        )

    def summary(self) -> dict[str, Any]:
        """Return a concise summary of the workflow."""
        return {
            "intent_id": self.intent_id,
            "action_type": self.intent.action_type,
            "target_resource": self.intent.target_resource,
            "state": self.state.value,
            "succeeded": self.succeeded,
            "is_terminal": self.is_terminal,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }
