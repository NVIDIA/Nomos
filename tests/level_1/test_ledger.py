# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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

    def test_validate_detects_resigned_rewrite(self):
        """A rewrite that recomputes every downstream hash but cannot re-sign
        must still fail validation: the signatures are the only thing the
        attacker cannot regenerate."""
        from nomos.level_0.crypto import ContentHasher

        storage = InMemoryLedgerStorage()
        signer = Signer(KeyPair.generate(key_id="council-key"))
        chain = LedgerChain(storage=storage, signer=signer)
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"writ_id": "w-1"})
        chain.append(entry_type=LedgerEntryType.DECISION_MADE, payload={"writ_id": "w-1"})

        # Rewrite entry 1, then re-thread hashes 1..head; signatures untouched.
        hasher = ContentHasher()
        storage.get(1).payload["writ_id"] = "w-FORGED"
        prev = storage.get(0).content_hash
        for seq in range(1, storage.length()):
            entry = storage.get(seq)
            entry.previous_hash = prev
            entry.content_hash = hasher.hash_json(entry.content_for_hashing())
            prev = entry.content_hash

        with pytest.raises(ChainValidationError, match=r"[Ss]ignature"):
            chain.validate()

    def test_verifier_keys_let_a_read_only_holder_verify(self):
        """A monitor/auditor with only the public key verifies the chain a
        signer wrote; a rewrite that cannot re-sign is rejected."""
        storage = InMemoryLedgerStorage()
        kp = KeyPair.generate(key_id="council-key")
        writer = LedgerChain(storage=storage, signer=Signer(kp))
        writer.initialize(creator_id="test")
        writer.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={"n": 1})

        reader = LedgerChain(storage=storage, verifier_keys={kp.key_id: kp.public_key})
        assert reader.validate() is True
        assert reader.status().signatures_verified is True

        storage.get(1).payload["n"] = 2
        storage.get(1).content_hash = reader._hasher.hash_json(storage.get(1).content_for_hashing())
        with pytest.raises(ChainValidationError, match=r"[Ss]ignature"):
            reader.validate()

    def test_wrong_verifier_key_rejects_chain(self):
        storage = InMemoryLedgerStorage()
        kp = KeyPair.generate(key_id="council-key")
        LedgerChain(storage=storage, signer=Signer(kp)).initialize(creator_id="test")

        other = KeyPair.generate(key_id="council-key")
        reader = LedgerChain(storage=storage, verifier_keys={"council-key": other.public_key})
        with pytest.raises(ChainValidationError, match=r"[Ss]ignature"):
            reader.validate()

    def test_signer_without_public_key_fails_closed_on_validate(self):
        """A signer-backed chain must not fall back to hash-only checking just
        because the signer cannot hand over its public key."""
        from nomos.level_1.ledger.chain import ChainValidationError

        class OpaqueSigner:
            key_id = "opaque"

            def sign(self, data: bytes) -> bytes:
                return Signer(KeyPair.generate(key_id="opaque")).sign(data)

        chain = LedgerChain(signer=OpaqueSigner())
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.POLICY_UPDATED, payload={"n": 1})
        with pytest.raises(ChainValidationError, match="public_key_bytes"):
            chain.validate()

    def test_malformed_verifier_key_is_rejected_at_construction(self):
        """A truncated or non-bytes key must fail loudly up front, not from
        status()/validate() at the first read."""
        with pytest.raises(ValueError, match="council-key"):
            LedgerChain(verifier_keys={"council-key": b"short"})
        with pytest.raises(ValueError, match="council-key"):
            LedgerChain(verifier_keys={"council-key": "ab" * 32})  # type: ignore[dict-item]

    def test_no_keys_means_hash_only_and_says_so(self):
        chain = LedgerChain()
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={})

        status = chain.status()
        assert status.is_valid is True
        assert status.signatures_verified is False
        assert chain.verifier_key_ids == []

    def test_signer_key_is_registered_automatically(self):
        kp = KeyPair.generate(key_id="council-key")
        chain = LedgerChain(signer=Signer(kp))
        chain.initialize(creator_id="test")
        assert chain.verifier_key_ids == ["council-key"]
        assert chain.status().signatures_verified is True

    def test_status(self):
        chain = LedgerChain(chain_id="test-chain")
        chain.initialize(creator_id="test")
        chain.append(entry_type=LedgerEntryType.INTENT_SUBMITTED, payload={})

        status = chain.status()
        assert status.chain_id == "test-chain"
        assert status.length == 2
        assert status.head_sequence == 1
        assert status.is_valid is True
