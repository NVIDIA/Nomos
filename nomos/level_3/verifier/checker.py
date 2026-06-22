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
Verifier: checks postconditions and invariants after execution.

This is the last step before an action is considered complete.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_1.ledger import LedgerChain, LedgerEntryType
from nomos.level_3.executor import Action, ExecutionResult

from .conditions import (
    ConditionRegistry,
    ConditionResult,
    Invariant,
    Postcondition,
    SuccessPostcondition,
)


class VerificationStatus(Enum):
    """Status of verification."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"


@dataclass
class VerificationResult:
    """Complete result of verification."""

    status: VerificationStatus
    action: Action
    execution_result: ExecutionResult
    postcondition_results: list[ConditionResult] = field(default_factory=list)
    invariant_results: list[ConditionResult] = field(default_factory=list)
    verified_at: datetime | None = None
    ledger_sequence: int | None = None

    @property
    def all_passed(self) -> bool:
        all_results = self.postcondition_results + self.invariant_results
        return all(r.passed for r in all_results)

    @property
    def failed_conditions(self) -> list[ConditionResult]:
        all_results = self.postcondition_results + self.invariant_results
        return [r for r in all_results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "action": self.action.to_dict(),
            "execution_result": self.execution_result.to_dict(),
            "postcondition_results": [
                {
                    "passed": r.passed,
                    "condition_id": r.condition_id,
                    "message": r.message,
                    "details": r.details,
                }
                for r in self.postcondition_results
            ],
            "invariant_results": [
                {
                    "passed": r.passed,
                    "condition_id": r.condition_id,
                    "message": r.message,
                    "details": r.details,
                }
                for r in self.invariant_results
            ],
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "ledger_sequence": self.ledger_sequence,
        }


class Verifier:
    """
    Verifies execution results against postconditions and invariants.

    Verification is recorded in the ledger for audit.
    """

    def __init__(
        self,
        ledger: LedgerChain,
        registry: ConditionRegistry | None = None,
        clock: Clock | None = None,
    ):
        self._ledger = ledger
        self._registry = registry or ConditionRegistry()
        self._clock = clock or SystemClock()

        self._registry.register_postcondition(SuccessPostcondition())

    def register_postcondition(self, condition: Postcondition) -> None:
        """Register a postcondition."""
        self._registry.register_postcondition(condition)

    def register_invariant(self, invariant: Invariant) -> None:
        """Register an invariant."""
        self._registry.register_invariant(invariant)

    def verify(
        self,
        action: Action,
        execution_result: ExecutionResult,
        context: dict[str, Any] | None = None,
    ) -> VerificationResult:
        """
        Verify an execution result.

        Checks all applicable postconditions and invariants.
        """
        verified_at = self._clock.now()
        context = context or {}

        postconditions = self._registry.get_postconditions(action.action_type)
        postcondition_results = []

        for condition in postconditions:
            try:
                result = condition.check(action, execution_result)
                postcondition_results.append(result)
            except Exception as e:
                postcondition_results.append(
                    ConditionResult(
                        passed=False,
                        condition_id=condition.condition_id,
                        message=f"Condition check failed: {e}",
                        details={"exception": str(e)},
                    )
                )

        invariants = self._registry.get_invariants()
        invariant_results = []

        for invariant in invariants:
            try:
                result = invariant.check(context)
                invariant_results.append(result)
            except Exception as e:
                invariant_results.append(
                    ConditionResult(
                        passed=False,
                        condition_id=invariant.invariant_id,
                        message=f"Invariant check failed: {e}",
                        details={"exception": str(e)},
                    )
                )

        all_results = postcondition_results + invariant_results
        all_passed = all(r.passed for r in all_results)

        if not all_results:
            status = VerificationStatus.SKIPPED
        elif all_passed:
            status = VerificationStatus.PASSED
        elif any(r.passed for r in all_results):
            status = VerificationStatus.PARTIAL
        else:
            status = VerificationStatus.FAILED

        entry_type = (
            LedgerEntryType.VERIFICATION_PASSED
            if status == VerificationStatus.PASSED
            else LedgerEntryType.VERIFICATION_FAILED
        )

        ledger_entry = self._ledger.append(
            entry_type=entry_type,
            payload={
                "action": action.to_dict(),
                "status": status.value,
                "postconditions_checked": len(postcondition_results),
                "postconditions_passed": sum(1 for r in postcondition_results if r.passed),
                "invariants_checked": len(invariant_results),
                "invariants_passed": sum(1 for r in invariant_results if r.passed),
                "failed_conditions": [
                    {"id": r.condition_id, "message": r.message}
                    for r in all_results
                    if not r.passed
                ],
                "verified_at": verified_at.isoformat(),
            },
        )

        return VerificationResult(
            status=status,
            action=action,
            execution_result=execution_result,
            postcondition_results=postcondition_results,
            invariant_results=invariant_results,
            verified_at=verified_at,
            ledger_sequence=ledger_entry.sequence,
        )

    def should_rollback(self, result: VerificationResult) -> bool:
        """Determine if a failed verification should trigger rollback."""
        if result.status == VerificationStatus.PASSED:
            return False

        critical_failures = [
            r
            for r in result.failed_conditions
            if r.details.get("critical", False) or r.condition_id.startswith("critical_")
        ]

        return len(critical_failures) > 0 or result.status == VerificationStatus.FAILED
