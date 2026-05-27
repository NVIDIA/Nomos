"""
Identity types for agents and council members.

Provides verifiable identity for all actors in the system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AgentRole(Enum):
    """Predefined agent roles for RBAC."""

    ANONYMOUS = "anonymous"
    APPLICATION = "application"
    SERVICE = "service"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class AgentIdentity:
    """
    Immutable agent identity with cryptographic binding.

    Every agent that submits intents must have a verifiable identity.
    """

    agent_id: str
    public_key_hex: str
    role: AgentRole = AgentRole.APPLICATION
    labels: frozenset[str] = field(default_factory=frozenset)
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        """
        Convert identity to a dictionary.

        Returns
        -------
        dict
            Dictionary representation of the identity.
        """
        return {
            "agent_id": self.agent_id,
            "public_key_hex": self.public_key_hex,
            "role": self.role.value,
            "labels": list(self.labels),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentIdentity":
        """
        Create identity from a dictionary.

        Parameters
        ----------
        data : dict
            Dictionary containing identity data.

        Returns
        -------
        AgentIdentity
            The parsed identity object.
        """
        return cls(
            agent_id=data["agent_id"],
            public_key_hex=data["public_key_hex"],
            role=AgentRole(data.get("role", "application")),
            labels=frozenset(data.get("labels", [])),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else None,
        )


@dataclass(frozen=True)
class CouncilMember:
    """
    A member of the Security Council with voting rights.

    Council members can approve intents and sign ledger entries.
    """

    member_id: str
    public_key_hex: str
    display_name: str
    weight: int = 1
    is_active: bool = True

    def to_dict(self) -> dict:
        """
        Convert council member to a dictionary.

        Returns
        -------
        dict
            Dictionary representation of the council member.
        """
        return {
            "member_id": self.member_id,
            "public_key_hex": self.public_key_hex,
            "display_name": self.display_name,
            "weight": self.weight,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CouncilMember":
        """
        Create council member from a dictionary.

        Parameters
        ----------
        data : dict
            Dictionary containing council member data.

        Returns
        -------
        CouncilMember
            The parsed council member object.
        """
        return cls(
            member_id=data["member_id"],
            public_key_hex=data["public_key_hex"],
            display_name=data["display_name"],
            weight=data.get("weight", 1),
            is_active=data.get("is_active", True),
        )
