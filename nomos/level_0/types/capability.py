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
Capability token types: scoped, expiring, ledger-anchored permissions.

Tokens are the only way to execute privileged actions.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class CapabilityStatus(Enum):
    """Token lifecycle states."""

    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True)
class CapabilityScope:
    """
    Defines exactly what a capability token permits.

    Scopes are conjunctive: all constraints must be satisfied.
    """

    action_type: str
    resource_pattern: str
    allowed_operations: frozenset[str] = field(default_factory=frozenset)
    max_invocations: int = 1
    parameters_constraint: dict[str, Any] | None = None

    def matches(self, action_type: str, resource: str, operation: str) -> bool:
        """
        Check if this scope permits the given action.

        Parameters
        ----------
        action_type : str
            The type of action to check.
        resource : str
            The resource being accessed.
        operation : str
            The operation being performed.

        Returns
        -------
        bool
            True if the scope permits the action, False otherwise.
        """
        import fnmatch

        if self.action_type != action_type and self.action_type != "*":
            return False
        if not fnmatch.fnmatch(resource, self.resource_pattern):
            return False
        if self.allowed_operations and operation not in self.allowed_operations:
            return False
        return True

    def to_dict(self) -> dict:
        """
        Convert capability scope to a dictionary.

        Returns
        -------
        dict
            Dictionary representation of the scope.
        """
        return {
            "action_type": self.action_type,
            "resource_pattern": self.resource_pattern,
            "allowed_operations": list(self.allowed_operations),
            "max_invocations": self.max_invocations,
            "parameters_constraint": self.parameters_constraint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityScope":
        """
        Create capability scope from a dictionary.

        Parameters
        ----------
        data : dict
            Dictionary containing capability scope data.

        Returns
        -------
        CapabilityScope
            The parsed capability scope object.
        """
        return cls(
            action_type=data["action_type"],
            resource_pattern=data["resource_pattern"],
            allowed_operations=frozenset(data.get("allowed_operations", [])),
            max_invocations=data.get("max_invocations", 1),
            parameters_constraint=data.get("parameters_constraint"),
        )


@dataclass
class CapabilityToken:
    """
    A scoped, expiring capability token anchored to a ledger entry.

    This is what the executor requires to perform any privileged action.
    """

    token_id: str
    intent_id: str
    scope: CapabilityScope
    issued_at: datetime
    expires_at: datetime
    ledger_anchor: str
    issuer_id: str
    bearer_agent_id: str
    status: CapabilityStatus = CapabilityStatus.ACTIVE
    invocation_count: int = 0
    conditions: list[str] = field(default_factory=list)
    signature_hex: str | None = None

    @classmethod
    def create(
        cls,
        intent_id: str,
        scope: CapabilityScope,
        issued_at: datetime,
        expires_at: datetime,
        ledger_anchor: str,
        issuer_id: str,
        bearer_agent_id: str,
        conditions: list[str] | None = None,
    ) -> "CapabilityToken":
        """
        Factory to create a new capability token.

        Parameters
        ----------
        intent_id : str
            The ID of the intent this token is created for.
        scope : CapabilityScope
            The scope of permissions granted by this token.
        issued_at : datetime
            The time this token is issued.
        expires_at : datetime
            The time this token expires.
        ledger_anchor : str
            The ledger entry hash anchoring this token.
        issuer_id : str
            The ID of the issuer.
        bearer_agent_id : str
            The ID of the agent allowed to bear this token.
        conditions : Optional[List[str]], optional
            Additional conditions for the token, by default None.

        Returns
        -------
        CapabilityToken
            The newly created capability token.
        """
        return cls(
            token_id=f"cap-{uuid.uuid4().hex[:16]}",
            intent_id=intent_id,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            ledger_anchor=ledger_anchor,
            issuer_id=issuer_id,
            bearer_agent_id=bearer_agent_id,
            conditions=conditions or [],
        )

    def is_valid(self, current_time: datetime) -> bool:
        """Check if token is currently valid."""
        if self.status != CapabilityStatus.ACTIVE:
            return False
        if current_time >= self.expires_at:
            return False
        if self.invocation_count >= self.scope.max_invocations:
            return False
        return True

    def can_execute(
        self, action_type: str, resource: str, operation: str, current_time: datetime
    ) -> bool:
        """Check if token permits this specific execution."""
        if not self.is_valid(current_time):
            return False
        return self.scope.matches(action_type, resource, operation)

    def record_invocation(self) -> None:
        """Record that this token was used."""
        self.invocation_count += 1
        if self.invocation_count >= self.scope.max_invocations:
            self.status = CapabilityStatus.USED

    def revoke(self) -> None:
        """Revoke this token."""
        self.status = CapabilityStatus.REVOKED

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "intent_id": self.intent_id,
            "scope": self.scope.to_dict(),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ledger_anchor": self.ledger_anchor,
            "issuer_id": self.issuer_id,
            "bearer_agent_id": self.bearer_agent_id,
            "status": self.status.value,
            "invocation_count": self.invocation_count,
            "conditions": self.conditions,
            "signature_hex": self.signature_hex,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityToken":
        return cls(
            token_id=data["token_id"],
            intent_id=data["intent_id"],
            scope=CapabilityScope.from_dict(data["scope"]),
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            ledger_anchor=data["ledger_anchor"],
            issuer_id=data["issuer_id"],
            bearer_agent_id=data["bearer_agent_id"],
            status=CapabilityStatus(data.get("status", "active")),
            invocation_count=data.get("invocation_count", 0),
            conditions=data.get("conditions", []),
            signature_hex=data.get("signature_hex"),
        )

    def content_for_signing(self) -> dict:
        """Return canonical content for signature computation."""
        return {
            "token_id": self.token_id,
            "intent_id": self.intent_id,
            "scope": self.scope.to_dict(),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ledger_anchor": self.ledger_anchor,
            "issuer_id": self.issuer_id,
            "bearer_agent_id": self.bearer_agent_id,
            "conditions": self.conditions,
        }
