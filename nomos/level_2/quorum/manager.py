# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Quorum manager: handles multi-party approval workflows.

Manages voting records, quorum thresholds, and vote recording.
"""

from dataclasses import dataclass
from enum import Enum

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_0.types import CouncilMember
from nomos.level_1.ledger import LedgerChain, LedgerEntryType

from .vote import Vote, VoteRecord, VoteType


class QuorumStatus(Enum):
    """Status of a quorum vote."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class QuorumConfig:
    """Configuration for quorum requirements."""

    default_required_weight: int = 2
    default_timeout_seconds: int = 3600
    allow_self_approval: bool = False
    require_reason: bool = False


@dataclass
class QuorumResult:
    """Result of a quorum check or vote."""

    status: QuorumStatus
    record: VoteRecord
    message: str


class QuorumManager:
    """
    Manages multi-party approval for high-risk intents.

    Tracks votes, enforces quorum thresholds, and records decisions.
    """

    def __init__(
        self,
        ledger: LedgerChain,
        config: QuorumConfig | None = None,
        clock: Clock | None = None,
    ):
        self._ledger = ledger
        self._config = config or QuorumConfig()
        self._clock = clock or SystemClock()
        self._members: dict[str, CouncilMember] = {}
        self._records: dict[str, VoteRecord] = {}

    def register_member(self, member: CouncilMember) -> None:
        """Register a council member who can vote."""
        self._members[member.member_id] = member

    def unregister_member(self, member_id: str) -> bool:
        """Unregister a council member."""
        return self._members.pop(member_id, None) is not None

    def get_member(self, member_id: str) -> CouncilMember | None:
        """Get a council member by ID."""
        return self._members.get(member_id)

    def get_active_members(self) -> list[CouncilMember]:
        """Get all active council members."""
        return [m for m in self._members.values() if m.is_active]

    def total_available_weight(self) -> int:
        """Total voting weight of active members."""
        return sum(m.weight for m in self.get_active_members())

    def open_vote(
        self,
        intent_id: str,
        required_weight: int | None = None,
    ) -> VoteRecord:
        """
        Open a new vote for an intent.

        Returns the vote record for tracking.
        """
        if intent_id in self._records:
            return self._records[intent_id]

        weight = required_weight or self._config.default_required_weight

        record = VoteRecord(
            intent_id=intent_id,
            required_weight=weight,
            opened_at=self._clock.now(),
        )

        self._records[intent_id] = record

        self._ledger.append(
            entry_type=LedgerEntryType.QUORUM_VOTE,
            payload={
                "action": "opened",
                "intent_id": intent_id,
                "required_weight": weight,
                "opened_at": record.opened_at.isoformat(),  # type: ignore[union-attr]
            },
        )

        return record

    def cast_vote(
        self,
        intent_id: str,
        voter_id: str,
        vote_type: VoteType,
        reason: str = "",
        *,
        auto_close: bool = True,
    ) -> QuorumResult:
        """
        Cast a vote on an intent.

        Returns the current quorum status after the vote.
        """
        record = self._records.get(intent_id)
        if record is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=VoteRecord(intent_id=intent_id, required_weight=0),
                message="No vote open for this intent",
            )

        if record.closed_at is not None:
            return QuorumResult(
                status=QuorumStatus.APPROVED
                if record.outcome == VoteType.APPROVE
                else QuorumStatus.DENIED,
                record=record,
                message="Vote is already closed",
            )

        member = self._members.get(voter_id)
        if member is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=record,
                message=f"Unknown voter: {voter_id}",
            )

        if not member.is_active:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=record,
                message=f"Voter {voter_id} is not active",
            )

        if record.has_voted(voter_id):
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=record,
                message=f"Voter {voter_id} has already voted",
            )

        vote = Vote(
            voter_id=voter_id,
            vote_type=vote_type,
            weight=member.weight,
            timestamp=self._clock.now(),
            reason=reason,
        )

        record.add_vote(vote)

        self._ledger.append(
            entry_type=LedgerEntryType.QUORUM_VOTE,
            payload={
                "action": "voted",
                "intent_id": intent_id,
                "voter_id": voter_id,
                "vote_type": vote_type.value,
                "weight": member.weight,
                "reason": reason,
                "timestamp": vote.timestamp.isoformat(),
            },
        )

        if auto_close and record.is_decided():
            return self._close_vote(record)

        return QuorumResult(
            status=QuorumStatus.PENDING,
            record=record,
            message=f"Vote recorded. Approve: {record.approve_weight}/{record.required_weight}",
        )

    def revise_vote(
        self,
        intent_id: str,
        voter_id: str,
        vote_type: VoteType,
        reason: str = "",
        debate_round: int = 0,
        *,
        auto_close: bool = True,
    ) -> QuorumResult:
        """
        Replace an existing vote during a debate round.

        Unlike ``cast_vote``, this allows a member who already voted to
        change their position.  The revised vote is recorded as a
        ``DEBATE_VOTE`` ledger entry so the audit trail shows the change.
        """
        record = self._records.get(intent_id)
        if record is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=VoteRecord(intent_id=intent_id, required_weight=0),
                message="No vote open for this intent",
            )

        if record.closed_at is not None:
            return QuorumResult(
                status=QuorumStatus.APPROVED
                if record.outcome == VoteType.APPROVE
                else QuorumStatus.DENIED,
                record=record,
                message="Vote is already closed",
            )

        member = self._members.get(voter_id)
        if member is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=record,
                message=f"Unknown voter: {voter_id}",
            )

        previous = record.votes.get(voter_id)
        changed = previous is not None and previous.vote_type != vote_type

        vote = Vote(
            voter_id=voter_id,
            vote_type=vote_type,
            weight=member.weight,
            timestamp=self._clock.now(),
            reason=reason,
        )

        # Replace the existing vote (dict key assignment)
        record.add_vote(vote)

        self._ledger.append(
            entry_type=LedgerEntryType.DEBATE_VOTE,
            payload={
                "action": "revised" if changed else "maintained",
                "intent_id": intent_id,
                "voter_id": voter_id,
                "vote_type": vote_type.value,
                "previous_vote": previous.vote_type.value if previous else None,
                "weight": member.weight,
                "reason": reason,
                "debate_round": debate_round,
                "timestamp": vote.timestamp.isoformat(),
            },
        )

        if auto_close and record.is_decided():
            return self._close_vote(record)

        return QuorumResult(
            status=QuorumStatus.PENDING,
            record=record,
            message=f"Vote revised (round {debate_round}). "
            f"Approve: {record.approve_weight}/{record.required_weight}",
        )

    def finalize_vote(self, intent_id: str, *, allow_pending: bool = False) -> QuorumResult:
        """Finalize a vote after a batch of ballots has been recorded."""
        record = self._records.get(intent_id)
        if record is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=VoteRecord(intent_id=intent_id, required_weight=0),
                message="No vote found",
            )

        if record.closed_at is not None:
            status = (
                QuorumStatus.APPROVED if record.outcome == VoteType.APPROVE else QuorumStatus.DENIED
            )
            return QuorumResult(status=status, record=record, message="Vote is already closed")

        if allow_pending:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=record,
                message=f"Approve: {record.approve_weight}/{record.required_weight}",
            )

        if record.is_decided():
            return self._close_vote(record)

        active_member_ids = {member.member_id for member in self.get_active_members()}
        all_active_members_voted = active_member_ids.issubset(record.votes.keys())
        if all_active_members_voted and not allow_pending:
            return self._close_vote(record, outcome=VoteType.DENY)

        return QuorumResult(
            status=QuorumStatus.PENDING,
            record=record,
            message=f"Approve: {record.approve_weight}/{record.required_weight}",
        )

    def _close_vote(self, record: VoteRecord, outcome: VoteType | None = None) -> QuorumResult:
        """Close a vote and record the outcome."""
        record.closed_at = self._clock.now()
        record.outcome = outcome or record.get_outcome()

        status = (
            QuorumStatus.APPROVED if record.outcome == VoteType.APPROVE else QuorumStatus.DENIED
        )

        self._ledger.append(
            entry_type=LedgerEntryType.DECISION_MADE,
            payload={
                "intent_id": record.intent_id,
                "outcome": record.outcome.value if record.outcome else "undecided",
                "approve_weight": record.approve_weight,
                "deny_weight": record.deny_weight,
                "required_weight": record.required_weight,
                "closed_at": record.closed_at.isoformat(),
            },
        )

        return QuorumResult(
            status=status,
            record=record,
            message=f"Vote closed: {status.value}",
        )

    def get_status(self, intent_id: str) -> QuorumResult:
        """Get the current status of a vote."""
        record = self._records.get(intent_id)
        if record is None:
            return QuorumResult(
                status=QuorumStatus.PENDING,
                record=VoteRecord(intent_id=intent_id, required_weight=0),
                message="No vote found",
            )

        if record.closed_at:
            status = (
                QuorumStatus.APPROVED if record.outcome == VoteType.APPROVE else QuorumStatus.DENIED
            )
        else:
            status = QuorumStatus.PENDING

        return QuorumResult(
            status=status,
            record=record,
            message=f"Approve: {record.approve_weight}/{record.required_weight}",
        )

    def get_pending_votes(self) -> list[VoteRecord]:
        """Get all pending vote records."""
        return [r for r in self._records.values() if r.closed_at is None]
