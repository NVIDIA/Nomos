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
Action definitions and registry.

Actions are the executable units that capability tokens authorize.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    """
    An action to be executed.

    Actions are created from intents after capability token validation.
    """

    action_type: str
    resource: str
    operation: str
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "resource": self.resource,
            "operation": self.operation,
            "parameters": self.parameters,
            "metadata": self.metadata,
        }


class ActionHandler(ABC):
    """
    Abstract base for action handlers.

    Each action type has a corresponding handler that knows how to execute it.
    """

    @property
    @abstractmethod
    def action_type(self) -> str:
        """The action type this handler processes."""
        pass

    @property
    def supported_operations(self) -> list[str]:
        """Operations this handler supports. Empty means all."""
        return []

    @abstractmethod
    def execute(self, action: Action) -> dict[str, Any]:
        """
        Execute the action and return results.

        Raises ActionExecutionError on failure.
        """
        pass

    def validate(self, action: Action) -> list[str]:
        """
        Validate action before execution.

        Returns list of validation errors (empty if valid).
        """
        errors = []
        if action.action_type != self.action_type:
            errors.append(f"Handler for {self.action_type} cannot handle {action.action_type}")
        if self.supported_operations and action.operation not in self.supported_operations:
            errors.append(f"Unsupported operation: {action.operation}")
        return errors

    def can_rollback(self) -> bool:
        """Whether this handler supports rollback."""
        return False

    def rollback(self, action: Action, execution_result: dict[str, Any]) -> dict[str, Any]:
        """Rollback a previously executed action."""
        raise NotImplementedError("Rollback not supported")


class ActionExecutionError(Exception):
    """Raised when action execution fails."""

    def __init__(self, message: str, action: Action, details: dict | None = None):
        self.action = action
        self.details = details or {}
        super().__init__(message)


class ActionRegistry:
    """
    Registry of action handlers.

    Maps action types to their handlers.
    """

    def __init__(self):
        self._handlers: dict[str, ActionHandler] = {}

    def register(self, handler: ActionHandler) -> None:
        """Register an action handler."""
        self._handlers[handler.action_type] = handler

    def unregister(self, action_type: str) -> bool:
        """Unregister an action handler."""
        return self._handlers.pop(action_type, None) is not None

    def get(self, action_type: str) -> ActionHandler | None:
        """Get handler for an action type."""
        return self._handlers.get(action_type)

    def has_handler(self, action_type: str) -> bool:
        """Check if a handler exists for the action type."""
        return action_type in self._handlers

    def list_action_types(self) -> list[str]:
        """List all registered action types."""
        return list(self._handlers.keys())


class NoOpHandler(ActionHandler):
    """A no-op handler for testing."""

    @property
    def action_type(self) -> str:
        return "system.noop"

    def execute(self, action: Action) -> dict[str, Any]:
        return {"status": "success", "message": "No operation performed"}


class LogHandler(ActionHandler):
    """A logging handler for testing and debugging."""

    def __init__(self):
        self._log: list[Action] = []

    @property
    def action_type(self) -> str:
        return "system.log"

    def execute(self, action: Action) -> dict[str, Any]:
        self._log.append(action)
        return {
            "status": "success",
            "logged": True,
            "log_count": len(self._log),
        }

    def get_log(self) -> list[Action]:
        return self._log.copy()

    def clear_log(self) -> None:
        self._log.clear()
