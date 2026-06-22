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
Sandboxed executor: runs actions only with valid capability tokens.

This is the enforcement point for the capability-based security model.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_1.ledger import LedgerChain, LedgerEntryType
from nomos.level_2.capability import CapabilityService

from .actions import Action, ActionExecutionError, ActionHandler, ActionRegistry


class ExecutionStatus(Enum):
    """Status of an execution attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    UNAUTHORIZED = "unauthorized"
    INVALID_TOKEN = "invalid_token"  # noqa: S105
    NO_HANDLER = "no_handler"
    VALIDATION_ERROR = "validation_error"


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""

    status: ExecutionStatus
    action: Action
    token_id: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ledger_sequence: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "action": self.action.to_dict(),
            "token_id": self.token_id,
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "ledger_sequence": self.ledger_sequence,
        }


class SandboxExecutor:
    """
    Executes actions in a sandboxed environment with capability enforcement.

    Key properties:
    - No action executes without a valid capability token
    - All executions are recorded in the ledger
    - Failed executions trigger verification/rollback
    """

    def __init__(
        self,
        capability_service: CapabilityService,
        ledger: LedgerChain,
        registry: ActionRegistry | None = None,
        clock: Clock | None = None,
    ):
        self._capability_service = capability_service
        self._ledger = ledger
        self._registry = registry or ActionRegistry()
        self._clock = clock or SystemClock()

    def register_handler(self, handler: ActionHandler) -> None:
        """Register an action handler."""
        self._registry.register(handler)

    def execute(self, action: Action, token_id: str) -> ExecutionResult:
        """
        Execute an action with capability token validation.

        The execution is:
        1. Token validated
        2. Handler located
        3. Action validated
        4. Execution started (logged)
        5. Action executed
        6. Execution completed (logged)
        """
        started_at = self._clock.now()

        validation = self._capability_service.validate(
            token_id=token_id,
            action_type=action.action_type,
            resource=action.resource,
            operation=action.operation,
        )

        if not validation.valid:
            return ExecutionResult(
                status=ExecutionStatus.INVALID_TOKEN,
                action=action,
                token_id=token_id,
                error=validation.reason,
                started_at=started_at,
                completed_at=self._clock.now(),
            )

        handler = self._registry.get(action.action_type)
        if handler is None:
            return ExecutionResult(
                status=ExecutionStatus.NO_HANDLER,
                action=action,
                token_id=token_id,
                error=f"No handler for action type: {action.action_type}",
                started_at=started_at,
                completed_at=self._clock.now(),
            )

        validation_errors = handler.validate(action)
        if validation_errors:
            return ExecutionResult(
                status=ExecutionStatus.VALIDATION_ERROR,
                action=action,
                token_id=token_id,
                error="; ".join(validation_errors),
                started_at=started_at,
                completed_at=self._clock.now(),
            )

        self._ledger.append(
            entry_type=LedgerEntryType.EXECUTION_STARTED,
            payload={
                "token_id": token_id,
                "intent_id": validation.token.intent_id if validation.token else None,
                "action": action.to_dict(),
                "started_at": started_at.isoformat(),
            },
        )

        self._capability_service.consume(token_id)

        try:
            output = handler.execute(action)
            completed_at = self._clock.now()

            complete_entry = self._ledger.append(
                entry_type=LedgerEntryType.EXECUTION_COMPLETED,
                payload={
                    "token_id": token_id,
                    "action_type": action.action_type,
                    "resource": action.resource,
                    "output": output,
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                },
            )

            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action=action,
                token_id=token_id,
                output=output,
                started_at=started_at,
                completed_at=completed_at,
                ledger_sequence=complete_entry.sequence,
            )

        except ActionExecutionError as e:
            completed_at = self._clock.now()

            self._ledger.append(
                entry_type=LedgerEntryType.EXECUTION_FAILED,
                payload={
                    "token_id": token_id,
                    "action_type": action.action_type,
                    "resource": action.resource,
                    "error": str(e),
                    "details": e.details,
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                },
            )

            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                action=action,
                token_id=token_id,
                error=str(e),
                started_at=started_at,
                completed_at=completed_at,
            )

        except Exception as e:
            completed_at = self._clock.now()

            self._ledger.append(
                entry_type=LedgerEntryType.EXECUTION_FAILED,
                payload={
                    "token_id": token_id,
                    "action_type": action.action_type,
                    "resource": action.resource,
                    "error": str(e),
                    "exception_type": type(e).__name__,
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                },
            )

            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                action=action,
                token_id=token_id,
                error=f"Unexpected error: {e}",
                started_at=started_at,
                completed_at=completed_at,
            )

    def can_rollback(self, action_type: str) -> bool:
        """Check if an action type supports rollback."""
        handler = self._registry.get(action_type)
        return handler is not None and handler.can_rollback()

    def rollback(
        self,
        action: Action,
        execution_result: ExecutionResult,
        token_id: str,
    ) -> ExecutionResult:
        """
        Attempt to rollback a previously executed action.

        Requires a valid capability token for the rollback.
        """
        handler = self._registry.get(action.action_type)
        if handler is None or not handler.can_rollback():
            return ExecutionResult(
                status=ExecutionStatus.NO_HANDLER,
                action=action,
                token_id=token_id,
                error="Rollback not supported for this action type",
            )

        validation = self._capability_service.validate(
            token_id=token_id,
            action_type=action.action_type,
            resource=action.resource,
            operation="rollback",
        )

        if not validation.valid:
            return ExecutionResult(
                status=ExecutionStatus.INVALID_TOKEN,
                action=action,
                token_id=token_id,
                error=validation.reason,
            )

        started_at = self._clock.now()

        self._ledger.append(
            entry_type=LedgerEntryType.ROLLBACK_STARTED,
            payload={
                "token_id": token_id,
                "action": action.to_dict(),
                "original_result": execution_result.to_dict(),
                "started_at": started_at.isoformat(),
            },
        )

        try:
            output = handler.rollback(action, execution_result.output)
            completed_at = self._clock.now()

            self._ledger.append(
                entry_type=LedgerEntryType.ROLLBACK_COMPLETED,
                payload={
                    "token_id": token_id,
                    "action_type": action.action_type,
                    "resource": action.resource,
                    "output": output,
                    "started_at": started_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                },
            )

            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                action=action,
                token_id=token_id,
                output=output,
                started_at=started_at,
                completed_at=completed_at,
            )

        except Exception as e:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                action=action,
                token_id=token_id,
                error=f"Rollback failed: {e}",
                started_at=started_at,
                completed_at=self._clock.now(),
            )
