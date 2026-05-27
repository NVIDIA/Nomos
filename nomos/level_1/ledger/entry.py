"""
Ledger entry: the atomic unit of the permissioned action ledger.

Each entry is signed, timestamped, and hash-linked to its predecessor.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from nomos.level_0.crypto import ContentHash, Signature


class LedgerEntryType(Enum):
    """Types of events recorded in the ledger."""

    INTENT_SUBMITTED = "intent_submitted"
    INTENT_EVALUATED = "intent_evaluated"
    DECISION_MADE = "decision_made"
    QUORUM_VOTE = "quorum_vote"
    TOKEN_MINTED = "token_minted"  # noqa: S105
    TOKEN_REVOKED = "token_revoked"  # noqa: S105
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_COMPLETED = "rollback_completed"
    DEBATE_VOTE = "debate_vote"
    POLICY_UPDATED = "policy_updated"
    MEMBER_ADDED = "member_added"
    MEMBER_REMOVED = "member_removed"


@dataclass
class LedgerEntry:
    """
    An immutable, signed entry in the permissioned action ledger.

    Each entry contains:
    - Sequence number for ordering
    - Hash of previous entry (chain link)
    - Entry type and payload
    - Timestamp and signatures
    """

    sequence: int
    entry_type: LedgerEntryType
    payload: dict[str, Any]
    timestamp: datetime
    previous_hash: ContentHash | None
    content_hash: ContentHash | None = None
    signatures: list[Signature] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def content_for_hashing(self) -> dict:
        """Return canonical content for hash computation."""
        return {
            "sequence": self.sequence,
            "entry_type": self.entry_type.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "previous_hash": str(self.previous_hash) if self.previous_hash else None,
        }

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "entry_type": self.entry_type.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "previous_hash": str(self.previous_hash) if self.previous_hash else None,
            "content_hash": str(self.content_hash) if self.content_hash else None,
            "signatures": [
                {"signer_id": s.signer_id, "signature_hex": s.hex(), "algorithm": s.algorithm}
                for s in self.signatures
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LedgerEntry":
        return cls(
            sequence=data["sequence"],
            entry_type=LedgerEntryType(data["entry_type"]),
            payload=data["payload"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            previous_hash=ContentHash.from_string(data["previous_hash"])
            if data.get("previous_hash")
            else None,
            content_hash=ContentHash.from_string(data["content_hash"])
            if data.get("content_hash")
            else None,
            signatures=[
                Signature.from_hex(
                    s["signer_id"], s["signature_hex"], s.get("algorithm", "ed25519")
                )
                for s in data.get("signatures", [])
            ],
            metadata=data.get("metadata", {}),
        )

    def is_genesis(self) -> bool:
        """Check if this is the genesis (first) entry."""
        return self.sequence == 0 and self.previous_hash is None


@dataclass(frozen=True)
class GenesisPayload:
    """Payload for the genesis entry."""

    chain_id: str
    created_at: datetime
    creator_id: str
    description: str = "Nomos Permissioned Action Ledger"

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "created_at": self.created_at.isoformat(),
            "creator_id": self.creator_id,
            "description": self.description,
        }
