"""
Tests for ledger module.

Following Lakos: test ledger using Level 0 mocks (MockClock).
"""

from datetime import UTC, datetime

import pytest

from nomos.level_0.clock import MockClock
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger import (
    ChainValidationError,
    InMemoryLedgerStorage,
    LedgerChain,
    LedgerEntry,
    LedgerEntryType,
)


class TestLedgerEntry:
    """Tests for LedgerEntry."""

    def test_is_genesis(self):
        entry = LedgerEntry(
            sequence=0,
            entry_type=LedgerEntryType.POLICY_UPDATED,
            payload={"test": "data"},
            timestamp=datetime.now(UTC),
            previous_hash=None,
        )
        assert entry.is_genesis() is True

    def test_non_genesis_entry(self):
        from nomos.level_0.crypto import ContentHash, HashAlgorithm

        entry = LedgerEntry(
            sequence=1,
            entry_type=LedgerEntryType.INTENT_SUBMITTED,
            payload={"test": "data"},
            timestamp=datetime.now(UTC),
            previous_hash=ContentHash(HashAlgorithm.SHA256, "abc123"),
        )
        assert entry.is_genesis() is False

    def test_to_dict_roundtrip(self):
        entry = LedgerEntry(
            sequence=0,
            entry_type=LedgerEntryType.INTENT_SUBMITTED,
            payload={"intent_id": "test-123"},
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            previous_hash=None,
        )
        data = entry.to_dict()
        restored = LedgerEntry.from_dict(data)
        assert restored.sequence == entry.sequence
        assert restored.entry_type == entry.entry_type
        assert restored.payload == entry.payload


class TestLedgerChain:
    """Tests for LedgerChain."""

    def test_initialize_creates_genesis(self):
        clock = MockClock()
        chain = LedgerChain(clock=clock)
        genesis = chain.initialize(creator_id="test-creator")

        assert genesis.sequence == 0
        assert genesis.is_genesis()
        assert chain.length() == 1

    def test_cannot_initialize_twice(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")

        with pytest.raises(ChainValidationError, match="already initialized"):
            chain.initialize(creator_id="test")

    def test_append_links_to_previous(self):
        chain = LedgerChain()
        genesis = chain.initialize(creator_id="test")

        entry = chain.append(
            entry_type=LedgerEntryType.INTENT_SUBMITTED,
            payload={"intent_id": "test-123"},
        )

        assert entry.sequence == 1
        assert entry.previous_hash == genesis.content_hash

    def test_append_requires_initialization(self):
        chain = LedgerChain()

        with pytest.raises(ChainValidationError, match="not initialized"):
            chain.append(
                entry_type=LedgerEntryType.INTENT_SUBMITTED,
                payload={},
            )

    def test_get_by_sequence(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "1"})
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "2"})

        entry = chain.get(1)
        assert entry is not None
        assert entry.payload["id"] == "1"

    def test_get_head(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "1"})
        last = chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "2"})

        head = chain.get_head()
        assert head == last

    def test_validate_valid_chain(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={})
        chain.append(entry_type=LedgerEntryType.DECISION_MADE, payload={})

        assert chain.validate() is True

    def test_validate_detects_tampering(self):
        storage = InMemoryLedgerStorage()
        chain = LedgerChain(storage=storage)
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"original": True})

        tampered = storage.get(1)
        tampered.payload["tampered"] = True

        with pytest.raises(ChainValidationError, match="hash mismatch"):
            chain.validate()

    def test_query_by_type(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "1"})
        chain.append(entry_type=LedgerEntryType.DECISION_MADE, payload={"id": "2"})
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"id": "3"})

        intents = chain.query_by_type(LedgerEntryType.INTENT_SUBMITTED)
        assert len(intents) == 2

    def test_query_by_intent(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"intent_id": "abc"})
        chain.append(entry_type=LedgerEntryType.DECISION_MADE, payload={"intent_id": "abc"})
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"intent_id": "xyz"})

        abc_entries = chain.query_by_intent("abc")
        assert len(abc_entries) == 2

    def test_chain_with_signing(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        chain = LedgerChain(signer=signer)

        genesis = chain.initialize(creator_id="test")
        assert len(genesis.signatures) == 1
        assert genesis.signatures[0].signer_id == kp.key_id

    def test_status(self):
        chain = LedgerChain(chain_id="test-chain")
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={})

        status = chain.status()
        assert status.chain_id == "test-chain"
        assert status.length == 2
        assert status.head_sequence == 1
        assert status.is_valid is True
