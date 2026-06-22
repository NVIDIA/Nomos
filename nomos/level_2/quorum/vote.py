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
Vote types for quorum-based approval.

Represents individual votes and vote records.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from nomos.level_0.crypto import Signature


class VoteType(Enum):
    """Types of votes."""

    APPROVE = "approve"
    DENY = "deny"
    ABSTAIN = "abstain"


@dataclass
class Vote:
    """An individual vote on an intent."""

    voter_id: str
    vote_type: VoteType
    weight: int
    timestamp: datetime
    reason: str = ""
    signature: Signature | None = None
    conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "voter_id": self.voter_id,
            "vote_type": self.vote_type.value,
            "weight": self.weight,
            "timestamp": self.timestamp.isoformat(),
            "reason": self.reason,
            "signature": {
                "signer_id": self.signature.signer_id,
                "signature_hex": self.signature.hex(),
            }
            if self.signature
            else None,
            "conditions": self.conditions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Vote":
        sig_data = data.get("signature")
        signature = None
        if sig_data:
            signature = Signature.from_hex(
                sig_data["signer_id"],
                sig_data["signature_hex"],
            )

        return cls(
            voter_id=data["voter_id"],
            vote_type=VoteType(data["vote_type"]),
            weight=data["weight"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            reason=data.get("reason", ""),
            signature=signature,
            conditions=data.get("conditions", []),
        )


@dataclass
class VoteRecord:
    """Complete voting record for an intent."""

    intent_id: str
    required_weight: int
    votes: dict[str, Vote] = field(default_factory=dict)
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    outcome: VoteType | None = None

    @property
    def approve_weight(self) -> int:
        """Total weight of approval votes."""
        return sum(v.weight for v in self.votes.values() if v.vote_type == VoteType.APPROVE)

    @property
    def deny_weight(self) -> int:
        """Total weight of denial votes."""
        return sum(v.weight for v in self.votes.values() if v.vote_type == VoteType.DENY)

    @property
    def total_weight(self) -> int:
        """Total weight of all votes."""
        return sum(v.weight for v in self.votes.values())

    def has_voted(self, voter_id: str) -> bool:
        """Check if a voter has already voted."""
        return voter_id in self.votes

    def add_vote(self, vote: Vote) -> None:
        """Add a vote to the record."""
        self.votes[vote.voter_id] = vote

    def is_decided(self) -> bool:
        """Check if quorum has been reached in either direction."""
        return (
            self.approve_weight >= self.required_weight or self.deny_weight >= self.required_weight
        )

    def get_outcome(self) -> VoteType | None:
        """Get the outcome if decided."""
        if self.approve_weight >= self.required_weight:
            return VoteType.APPROVE
        if self.deny_weight >= self.required_weight:
            return VoteType.DENY
        return None

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "required_weight": self.required_weight,
            "votes": {k: v.to_dict() for k, v in self.votes.items()},
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "outcome": self.outcome.value if self.outcome else None,
            "approve_weight": self.approve_weight,
            "deny_weight": self.deny_weight,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoteRecord":
        return cls(
            intent_id=data["intent_id"],
            required_weight=data["required_weight"],
            votes={k: Vote.from_dict(v) for k, v in data.get("votes", {}).items()},
            opened_at=datetime.fromisoformat(data["opened_at"]) if data.get("opened_at") else None,
            closed_at=datetime.fromisoformat(data["closed_at"]) if data.get("closed_at") else None,
            outcome=VoteType(data["outcome"]) if data.get("outcome") else None,
        )
