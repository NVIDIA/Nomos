# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Ledger chain: hash-linked sequence of entries with validation.

Provides tamper-evidence through cryptographic linking.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_0.crypto import ContentHash, ContentHasher, SignerLike, Verifier

from .entry import GenesisPayload, LedgerEntry, LedgerEntryType
from .storage import AtomicLedgerStorage, InMemoryLedgerStorage, LedgerStorage

logger = logging.getLogger(__name__)


class ChainValidationError(Exception):
    """Raised when ledger chain validation fails."""

    def __init__(self, message: str, sequence: int | None = None):
        self.sequence = sequence
        super().__init__(
            f"Chain validation failed at seq {sequence}: {message}" if sequence else message
        )


@dataclass
class ChainStatus:
    """Status of the ledger chain."""

    length: int
    head_hash: ContentHash | None
    head_sequence: int
    is_valid: bool
    chain_id: str
    # True only when at least one verifier key is registered and every
    # entry's signatures verified as part of is_valid. False means the chain
    # was checked for hash/link integrity only.
    signatures_verified: bool = False


class LedgerChain:
    """
    An append-only, hash-chained ledger.

    Properties:
    - Tamper-evident: any modification breaks the chain
    - Non-repudiable: entries are signed by their creators
    - Verifiable: anyone with public keys can validate
    """

    def __init__(
        self,
        storage: LedgerStorage | None = None,
        hasher: ContentHasher | None = None,
        clock: Clock | None = None,
        signer: SignerLike | None = None,
        chain_id: str | None = None,
        verifier_keys: Mapping[str, bytes] | None = None,
    ):
        """
        Args:
            verifier_keys: key_id -> raw Ed25519 public key. Read-only holders
                (monitors, auditors) pass the council keys here so validate()
                verifies signatures. A chain built with a signer registers that
                signer's own public key automatically.
        """
        self._storage = storage or InMemoryLedgerStorage()
        self._hasher = hasher or ContentHasher()
        self._clock = clock or SystemClock()
        self._signer = signer
        self._chain_id = chain_id or f"chain-{uuid.uuid4().hex[:12]}"
        self._verifier = Verifier()
        for key_id, public_key in (verifier_keys or {}).items():
            if not isinstance(public_key, bytes) or len(public_key) != 32:
                raise ValueError(f"verifier key {key_id!r} must be 32 raw Ed25519 public-key bytes")
            self._verifier.register_public_key(key_id, public_key)
        self._signer_key_registered = False
        self._register_signer_key(strict=False)

    def _register_signer_key(self, *, strict: bool) -> None:
        """Trust our own signer's public key so validate() can verify what we wrote.

        Signer providers expose ``public_key_bytes()``; the Vault provider
        fetches it over the network, so a failure at construction is logged and
        retried from validate(), where it is fatal (fail closed).
        """
        if self._signer_key_registered or self._signer is None:
            return
        public_key_bytes = getattr(self._signer, "public_key_bytes", None)
        if not callable(public_key_bytes):
            return
        try:
            self._verifier.register_public_key(self._signer.key_id, public_key_bytes())
        except Exception as exc:
            if strict:
                raise ChainValidationError(
                    f"Cannot verify signatures: signer public key unavailable ({exc})"
                ) from exc
            logger.warning("ledger signer public key unavailable at init: %s", exc)
            return
        self._signer_key_registered = True

    @property
    def verifier_key_ids(self) -> list[str]:
        """Key ids validate() will verify signatures against."""
        return self._verifier.key_ids()

    @property
    def chain_id(self) -> str:
        return self._chain_id

    def _build_genesis(self, creator_id: str, description: str = "") -> LedgerEntry:
        """Construct, hash, and sign the genesis entry."""
        payload = GenesisPayload(
            chain_id=self._chain_id,
            created_at=self._clock.now(),
            creator_id=creator_id,
            description=description or "Nomos Permissioned Action Ledger",
        )

        genesis = LedgerEntry(
            sequence=0,
            entry_type=LedgerEntryType.POLICY_UPDATED,
            payload=payload.to_dict(),
            timestamp=self._clock.now(),
            previous_hash=None,
        )

        genesis.content_hash = self._hasher.hash_json(genesis.content_for_hashing())

        if self._signer:
            sig = self._signer.sign(genesis.content_hash.to_bytes())
            genesis.signatures.append(sig)
        return genesis

    def _build_next(
        self,
        head: LedgerEntry,
        entry_type: LedgerEntryType,
        payload: dict,
        metadata: dict | None,
    ) -> LedgerEntry:
        """Construct, hash, and sign the entry that follows `head`."""
        entry = LedgerEntry(
            sequence=head.sequence + 1,
            entry_type=entry_type,
            payload=payload,
            timestamp=self._clock.now(),
            previous_hash=head.content_hash,
            metadata=metadata or {},
        )

        entry.content_hash = self._hasher.hash_json(entry.content_for_hashing())

        if self._signer:
            sig = self._signer.sign(entry.content_hash.to_bytes())
            entry.signatures.append(sig)
        return entry

    def initialize(self, creator_id: str, description: str = "") -> LedgerEntry:
        """Create the genesis entry for a new chain.

        With atomic (distributed) storage this is idempotent: replicas racing
        at startup all receive the one committed genesis. Single-writer
        storage keeps the historical behavior of raising when already
        initialized.
        """
        if isinstance(self._storage, AtomicLedgerStorage):
            return self._storage.initialize_atomic(
                lambda: self._build_genesis(creator_id, description)
            )

        if self._storage.length() > 0:
            raise ChainValidationError("Chain already initialized")

        genesis = self._build_genesis(creator_id, description)
        self._storage.append(genesis)
        return genesis

    def append(
        self,
        entry_type: LedgerEntryType,
        payload: dict,
        metadata: dict | None = None,
    ) -> LedgerEntry:
        """Append a new entry to the chain."""
        if isinstance(self._storage, AtomicLedgerStorage):
            # The storage owns the critical section: it locks the chain,
            # supplies the head, and commits whatever the builder returns.
            # The builder may run once per retry attempt with a fresh head.
            def build(head: LedgerEntry | None) -> LedgerEntry:
                if head is None or head.content_hash is None:
                    raise ChainValidationError("Chain not initialized. Call initialize() first.")
                return self._build_next(head, entry_type, payload, metadata)

            return self._storage.append_atomic(build)

        if self._storage.length() == 0:
            raise ChainValidationError("Chain not initialized. Call initialize() first.")

        head = self._storage.get_head()
        if head is None or head.content_hash is None:
            raise ChainValidationError("Invalid chain state: no head hash")

        entry = self._build_next(head, entry_type, payload, metadata)
        self._storage.append(entry)
        return entry

    def get(self, sequence: int) -> LedgerEntry | None:
        """Get entry by sequence number."""
        return self._storage.get(sequence)

    def get_head(self) -> LedgerEntry | None:
        """Get the latest entry."""
        return self._storage.get_head()

    def get_head_hash(self) -> ContentHash | None:
        """Get the hash of the latest entry."""
        head = self.get_head()
        return head.content_hash if head else None

    def length(self) -> int:
        """Return number of entries in the chain."""
        return self._storage.length()

    def validate(self) -> bool:
        """
        Validate the entire chain integrity.

        Returns True if valid, raises ChainValidationError if not.

        When any verifier key is registered (the chain's own signer, or
        ``verifier_keys``), every entry's signatures are verified as well:
        content hashes are unkeyed and can be recomputed by anyone who can
        write to storage, so the signatures are the only part of an entry a
        rewrite cannot regenerate.
        """
        if self._storage.length() == 0:
            return True

        self._register_signer_key(strict=True)
        verify_sigs = bool(self._verifier.key_ids())
        prev_hash: ContentHash | None = None

        for entry in self._storage.iterate():
            expected_hash = self._hasher.hash_json(entry.content_for_hashing())

            if entry.content_hash != expected_hash:
                raise ChainValidationError(
                    f"Content hash mismatch: expected {expected_hash}, got {entry.content_hash}",
                    sequence=entry.sequence,
                )

            if entry.sequence == 0:
                if entry.previous_hash is not None:
                    raise ChainValidationError(
                        "Genesis entry must have no previous hash", sequence=0
                    )
            else:
                if entry.previous_hash != prev_hash:
                    raise ChainValidationError(
                        f"Chain broken: expected prev_hash {prev_hash}, got {entry.previous_hash}",
                        sequence=entry.sequence,
                    )

            if verify_sigs and not self.verify_signatures(entry):
                raise ChainValidationError(
                    "Signature verification failed"
                    if entry.signatures
                    else "Entry is unsigned but verifier keys are registered",
                    sequence=entry.sequence,
                )

            prev_hash = entry.content_hash

        return True

    def status(self) -> ChainStatus:
        """Get current chain status."""
        head = self.get_head()
        is_valid = self._is_valid_cached()
        return ChainStatus(
            length=self._storage.length(),
            head_hash=head.content_hash if head else None,
            head_sequence=head.sequence if head else -1,
            is_valid=is_valid,
            chain_id=self._chain_id,
            signatures_verified=is_valid and bool(self._verifier.key_ids()),
        )

    def _is_valid_cached(self) -> bool:
        """Check validity without raising."""
        try:
            return self.validate()
        except ChainValidationError:
            return False

    def query_by_type(self, entry_type: LedgerEntryType) -> list[LedgerEntry]:
        """Query entries by type."""
        return [e for e in self._storage.iterate() if e.entry_type == entry_type]

    def query_by_intent(self, intent_id: str) -> list[LedgerEntry]:
        """Query all entries related to an intent."""
        return [e for e in self._storage.iterate() if e.payload.get("intent_id") == intent_id]

    def register_verifier_key(self, key_id: str, public_key: bytes) -> None:
        """Register a public key for signature verification."""
        self._verifier.register_public_key(key_id, public_key)

    def verify_signatures(self, entry: LedgerEntry) -> bool:
        """Verify all signatures on an entry."""
        if not entry.signatures or entry.content_hash is None:
            return False

        for sig in entry.signatures:
            if not self._verifier.verify(entry.content_hash.to_bytes(), sig):
                return False
        return True
