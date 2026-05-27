"""
Ledger chain: hash-linked sequence of entries with validation.

Provides tamper-evidence through cryptographic linking.
"""

import uuid
from dataclasses import dataclass

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_0.crypto import ContentHash, ContentHasher, Signer, Verifier

from .entry import GenesisPayload, LedgerEntry, LedgerEntryType
from .storage import InMemoryLedgerStorage, LedgerStorage


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
        signer: Signer | None = None,
        chain_id: str | None = None,
    ):
        self._storage = storage or InMemoryLedgerStorage()
        self._hasher = hasher or ContentHasher()
        self._clock = clock or SystemClock()
        self._signer = signer
        self._chain_id = chain_id or f"chain-{uuid.uuid4().hex[:12]}"
        self._verifier = Verifier()

    @property
    def chain_id(self) -> str:
        return self._chain_id

    def initialize(self, creator_id: str, description: str = "") -> LedgerEntry:
        """Create the genesis entry for a new chain."""
        if self._storage.length() > 0:
            raise ChainValidationError("Chain already initialized")

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

        self._storage.append(genesis)
        return genesis

    def append(
        self,
        entry_type: LedgerEntryType,
        payload: dict,
        metadata: dict | None = None,
    ) -> LedgerEntry:
        """Append a new entry to the chain."""
        if self._storage.length() == 0:
            raise ChainValidationError("Chain not initialized. Call initialize() first.")

        head = self._storage.get_head()
        if head is None or head.content_hash is None:
            raise ChainValidationError("Invalid chain state: no head hash")

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
        """
        if self._storage.length() == 0:
            return True

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

            prev_hash = entry.content_hash

        return True

    def status(self) -> ChainStatus:
        """Get current chain status."""
        head = self.get_head()
        return ChainStatus(
            length=self._storage.length(),
            head_hash=head.content_hash if head else None,
            head_sequence=head.sequence if head else -1,
            is_valid=self._is_valid_cached(),
            chain_id=self._chain_id,
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
