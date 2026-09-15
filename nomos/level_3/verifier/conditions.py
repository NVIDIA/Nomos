# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Postconditions and invariants for verification.

These define what must be true after an action executes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from nomos.level_3.executor import Action, ExecutionResult


@dataclass
class ConditionResult:
    """Result of evaluating a condition."""

    passed: bool
    condition_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class Postcondition(ABC):
    """
    Abstract postcondition that must hold after action execution.

    Postconditions are specific to an action type and verify
    that the action achieved its intended effect.
    """

    @property
    @abstractmethod
    def condition_id(self) -> str:
        """Unique identifier for this condition."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description."""
        pass

    @property
    def action_types(self) -> list[str]:
        """Action types this condition applies to. Empty means all."""
        return []

    @abstractmethod
    def check(self, action: Action, result: ExecutionResult) -> ConditionResult:
        """
        Check if the postcondition holds.

        Returns ConditionResult with pass/fail status.
        """
        pass


class Invariant(ABC):
    """
    System invariant that must always hold.

    Invariants are checked regardless of action type and ensure
    global system properties are maintained.
    """

    @property
    @abstractmethod
    def invariant_id(self) -> str:
        """Unique identifier for this invariant."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description."""
        pass

    @abstractmethod
    def check(self, context: dict[str, Any]) -> ConditionResult:
        """
        Check if the invariant holds.

        Context contains relevant system state.
        """
        pass


class ConditionRegistry:
    """Registry of postconditions and invariants."""

    def __init__(self):
        self._postconditions: dict[str, Postcondition] = {}
        self._invariants: dict[str, Invariant] = {}
        self._action_conditions: dict[str, list[str]] = {}

    def register_postcondition(self, condition: Postcondition) -> None:
        """Register a postcondition."""
        self._postconditions[condition.condition_id] = condition

        for action_type in condition.action_types:
            if action_type not in self._action_conditions:
                self._action_conditions[action_type] = []
            self._action_conditions[action_type].append(condition.condition_id)

    def register_invariant(self, invariant: Invariant) -> None:
        """Register an invariant."""
        self._invariants[invariant.invariant_id] = invariant

    def get_postconditions(self, action_type: str) -> list[Postcondition]:
        """Get all postconditions for an action type."""
        condition_ids = self._action_conditions.get(action_type, [])
        global_ids = self._action_conditions.get("*", [])
        all_ids = set(condition_ids + global_ids)
        return [self._postconditions[cid] for cid in all_ids if cid in self._postconditions]

    def get_invariants(self) -> list[Invariant]:
        """Get all registered invariants."""
        return list(self._invariants.values())

    def get_postcondition(self, condition_id: str) -> Postcondition | None:
        """Get a specific postcondition."""
        return self._postconditions.get(condition_id)

    def get_invariant(self, invariant_id: str) -> Invariant | None:
        """Get a specific invariant."""
        return self._invariants.get(invariant_id)


class SuccessPostcondition(Postcondition):
    """Simple postcondition that checks execution succeeded."""

    @property
    def condition_id(self) -> str:
        return "execution_success"

    @property
    def description(self) -> str:
        return "Execution must complete successfully"

    @property
    def action_types(self) -> list[str]:
        return ["*"]

    def check(self, action: Action, result: ExecutionResult) -> ConditionResult:
        if result.succeeded:
            return ConditionResult(
                passed=True,
                condition_id=self.condition_id,
                message="Execution completed successfully",
            )
        return ConditionResult(
            passed=False,
            condition_id=self.condition_id,
            message=f"Execution failed: {result.error}",
            details={"status": result.status.value, "error": result.error},
        )
