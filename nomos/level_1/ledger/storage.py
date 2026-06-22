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
Ledger storage backends.

Provides pluggable storage for ledger entries.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path

from .entry import LedgerEntry

# Builds the next entry from the current head (None only before genesis).
# May be invoked once per retry attempt with a fresh head; must be pure.
EntryBuilder = Callable[[LedgerEntry | None], LedgerEntry]
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
