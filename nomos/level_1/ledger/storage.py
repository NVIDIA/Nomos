# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Ledger storage backends.

Provides pluggable storage for ledger entries.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from nomos.level_0.crypto import ContentHash

from .entry import LedgerEntry, LedgerEntryType


@dataclass(frozen=True)
class ChainHead:
    """The two facts about the head that the next entry is built from.

    Storage that keeps them on the chain record can hand them to the builder
    without reading the head entry back.
    """

    sequence: int
    content_hash: ContentHash | None


# Builds the next entry from the current head (None only before genesis).
# May be invoked once per retry attempt with a fresh head; must be pure.
EntryBuilder = Callable[[LedgerEntry | ChainHead | None], LedgerEntry]
# Builds the genesis entry.
GenesisBuilder = Callable[[], LedgerEntry]


class LedgerAppendError(Exception):
    """A distributed append failed after bounded retries (callers fail closed)."""


class LedgerStorage(ABC):
    """Abstract storage backend for ledger entries."""

    @abstractmethod
    def append(self, entry: LedgerEntry) -> None:
        """Append an entry to storage."""
        pass

    @abstractmethod
    def get(self, sequence: int) -> LedgerEntry | None:
        """Get entry by sequence number."""
        pass

    @abstractmethod
    def get_head(self) -> LedgerEntry | None:
        """Get the latest entry."""
        pass

    @abstractmethod
    def length(self) -> int:
        """Return number of entries."""
        pass

    @abstractmethod
    def iterate(self) -> Iterator[LedgerEntry]:
        """Iterate over all entries in order."""
        pass

    def get_review_by_intent(self, intent_id: str) -> LedgerEntry | None:
        """Find a committed consolidated review; indexed stores should override."""
        for entry in self.iterate():
            if (
                entry.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED
                and entry.payload.get("intent_id") == intent_id
            ):
                return entry
        return None

    def get_descending(self, start: int, limit: int) -> list[LedgerEntry]:
        """Up to ``limit`` entries from ``start`` downwards, newest first.

        Backends that can answer this in one read should override it; this
        default costs one ``get`` per entry.
        """
        entries: list[LedgerEntry] = []
        sequence = start
        while sequence >= 0 and len(entries) < limit:
            entry = self.get(sequence)
            if entry is not None:
                entries.append(entry)
            sequence -= 1
        return entries


class AtomicLedgerStorage(LedgerStorage):
    """Storage that owns the append critical section.

    Distributed backends (multiple concurrent writers) must assign sequence
    numbers inside their own transaction: the in-process read-head-then-append
    pattern in LedgerChain is only safe for single-writer storage.
    """

    @abstractmethod
    def append_atomic(self, build: EntryBuilder) -> LedgerEntry:
        """Build and append the next entry inside the storage transaction.

        The builder receives the current head and must return the fully
        constructed (hashed, signed) next entry. It may be called once per
        retry attempt with a fresh head.
        """
        pass

    @abstractmethod
    def initialize_atomic(self, build_genesis: GenesisBuilder) -> LedgerEntry:
        """Create the genesis entry if absent; return the existing one otherwise.

        Idempotent across replicas racing at startup: the loser of the race
        receives the winner's genesis.
        """
        pass


class InMemoryLedgerStorage(LedgerStorage):
    """In-memory storage for testing and development."""

    def __init__(self):
        self._entries: list[LedgerEntry] = []

    def append(self, entry: LedgerEntry) -> None:
        expected_seq = len(self._entries)
        if entry.sequence != expected_seq:
            raise ValueError(f"Expected sequence {expected_seq}, got {entry.sequence}")
        self._entries.append(entry)

    def get(self, sequence: int) -> LedgerEntry | None:
        if 0 <= sequence < len(self._entries):
            return self._entries[sequence]
        return None

    def get_head(self) -> LedgerEntry | None:
        return self._entries[-1] if self._entries else None

    def length(self) -> int:
        return len(self._entries)

    def iterate(self) -> Iterator[LedgerEntry]:
        return iter(self._entries)

    def clear(self) -> None:
        """Clear all entries (for testing)."""
        self._entries.clear()


class FileLedgerStorage(LedgerStorage):
    """
    File-based append-only storage.

    Each entry is stored as a JSON line (JSONL format).
    """

    def __init__(self, path: Path):
        self._path = path
        self._entries: list[LedgerEntry] = []
        self._load()

    def _load(self) -> None:
        """Load existing entries from file."""
        if self._path.exists():
            with open(self._path) as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        self._entries.append(LedgerEntry.from_dict(data))

    def append(self, entry: LedgerEntry) -> None:
        expected_seq = len(self._entries)
        if entry.sequence != expected_seq:
            raise ValueError(f"Expected sequence {expected_seq}, got {entry.sequence}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")
        self._entries.append(entry)

    def get(self, sequence: int) -> LedgerEntry | None:
        if 0 <= sequence < len(self._entries):
            return self._entries[sequence]
        return None

    def get_head(self) -> LedgerEntry | None:
        return self._entries[-1] if self._entries else None

    def length(self) -> int:
        return len(self._entries)

    def iterate(self) -> Iterator[LedgerEntry]:
        return iter(self._entries)
