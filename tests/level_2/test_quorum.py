"""
Tests for quorum management.

Following Lakos: test using Level 0 and Level 1 components.
"""

import pytest

from nomos.level_0.clock import MockClock
from nomos.level_0.types import CouncilMember
from nomos.level_1.ledger import LedgerChain
from nomos.level_2.quorum import (
    QuorumManager,
    QuorumStatus,
    Vote,
    VoteRecord,
    VoteType,
)


class TestVoteRecord:
    """Tests for VoteRecord."""

    def test_approve_weight(self):
        record = VoteRecord(intent_id="test", required_weight=2)
        record.add_vote(
            Vote(
                voter_id="voter-1",
                vote_type=VoteType.APPROVE,
                weight=1,
                timestamp=None,
            )
        )
        assert record.approve_weight == 1

    def test_is_decided_with_quorum(self):
        record = VoteRecord(intent_id="test", required_weight=2)
        record.add_vote(Vote(voter_id="v1", vote_type=VoteType.APPROVE, weight=1, timestamp=None))
        assert record.is_decided() is False

        record.add_vote(Vote(voter_id="v2", vote_type=VoteType.APPROVE, weight=1, timestamp=None))
        assert record.is_decided() is True

    def test_get_outcome(self):
        record = VoteRecord(intent_id="test", required_weight=2)
        record.add_vote(Vote(voter_id="v1", vote_type=VoteType.APPROVE, weight=2, timestamp=None))
        assert record.get_outcome() == VoteType.APPROVE


class TestQuorumManager:
    """Tests for QuorumManager."""

    @pytest.fixture
    def manager(self):
        clock = MockClock()
        ledger = LedgerChain(clock=clock)
        ledger.initialize(creator_id="test")
        return QuorumManager(ledger=ledger, clock=clock)

    @pytest.fixture
    def members(self, manager):
        m1 = CouncilMember(member_id="alice", public_key_hex="abc", display_name="Alice", weight=1)
        m2 = CouncilMember(member_id="bob", public_key_hex="def", display_name="Bob", weight=1)
        m3 = CouncilMember(member_id="carol", public_key_hex="ghi", display_name="Carol", weight=2)
        manager.register_member(m1)
        manager.register_member(m2)
        manager.register_member(m3)
        return [m1, m2, m3]

    def test_open_vote(self, manager, members):
        record = manager.open_vote("intent-123", required_weight=2)
        assert record.intent_id == "intent-123"
        assert record.required_weight == 2
        assert record.opened_at is not None

    def test_cast_vote(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        result = manager.cast_vote("intent-123", "alice", VoteType.APPROVE)

        assert result.status == QuorumStatus.PENDING
        assert result.record.approve_weight == 1

    def test_quorum_reached(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        manager.cast_vote("intent-123", "alice", VoteType.APPROVE)
        result = manager.cast_vote("intent-123", "bob", VoteType.APPROVE)

        assert result.status == QuorumStatus.APPROVED

    def test_single_heavy_voter(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        result = manager.cast_vote("intent-123", "carol", VoteType.APPROVE)

        assert result.status == QuorumStatus.APPROVED

    def test_deny_quorum(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        manager.cast_vote("intent-123", "alice", VoteType.DENY)
        result = manager.cast_vote("intent-123", "bob", VoteType.DENY)

        assert result.status == QuorumStatus.DENIED

    def test_cannot_vote_twice(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        manager.cast_vote("intent-123", "alice", VoteType.APPROVE)
        result = manager.cast_vote("intent-123", "alice", VoteType.APPROVE)

        assert "already voted" in result.message.lower()

    def test_unknown_voter(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        result = manager.cast_vote("intent-123", "unknown", VoteType.APPROVE)

        assert "unknown voter" in result.message.lower()

    def test_get_pending_votes(self, manager, members):
        manager.open_vote("intent-1", required_weight=2)
        manager.open_vote("intent-2", required_weight=2)
        manager.cast_vote("intent-1", "carol", VoteType.APPROVE)

        pending = manager.get_pending_votes()
        assert len(pending) == 1
        assert pending[0].intent_id == "intent-2"

    def test_finalize_vote_records_all_ballots_before_closing(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        dave = CouncilMember(member_id="dave", public_key_hex="jkl", display_name="Dave", weight=1)
        manager.register_member(dave)

        manager.cast_vote("intent-123", "alice", VoteType.APPROVE, auto_close=False)
        manager.cast_vote("intent-123", "bob", VoteType.APPROVE, auto_close=False)
        manager.cast_vote("intent-123", "dave", VoteType.DENY, auto_close=False)

        result = manager.finalize_vote("intent-123")

        assert result.status == QuorumStatus.APPROVED
        assert len(result.record.votes) == 3
        assert result.record.closed_at is not None

    def test_cannot_revise_closed_vote(self, manager, members):
        manager.open_vote("intent-123", required_weight=2)
        manager.cast_vote("intent-123", "alice", VoteType.APPROVE)
        manager.cast_vote("intent-123", "bob", VoteType.APPROVE)

        result = manager.revise_vote("intent-123", "alice", VoteType.DENY, debate_round=1)

        assert result.status == QuorumStatus.APPROVED
        assert "already closed" in result.message.lower()
        assert result.record.outcome == VoteType.APPROVE
        assert result.record.approve_weight == 2
