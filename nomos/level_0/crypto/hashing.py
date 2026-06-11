# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Content hashing primitives for tamper-evident data structures.

Provides deterministic hashing of arbitrary content for ledger entries.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class HashAlgorithm(Enum):
    """Supported hash algorithms."""

    SHA256 = "sha256"
    SHA384 = "sha384"
    SHA512 = "sha512"
    BLAKE2B = "blake2b"


@dataclass(frozen=True)
class ContentHash:
    """
    Immutable content hash with algorithm metadata.

    The hash is stored as hex string for JSON serialization.
    """

    algorithm: HashAlgorithm
    hex_digest: str

    def __str__(self) -> str:
        return f"{self.algorithm.value}:{self.hex_digest}"

    @classmethod
    def from_string(cls, s: str) -> "ContentHash":
        """Parse from 'algorithm:hexdigest' format."""
        algo_str, hex_digest = s.split(":", 1)
        return cls(algorithm=HashAlgorithm(algo_str), hex_digest=hex_digest)

    def to_bytes(self) -> bytes:
        """Return raw hash bytes."""
        return bytes.fromhex(self.hex_digest)


class ContentHasher:
    """
    Deterministic content hasher for structured data.

    Ensures identical content always produces identical hashes
    regardless of dict key ordering or whitespace.
    """

    def __init__(self, algorithm: HashAlgorithm = HashAlgorithm.SHA256):
        self._algorithm = algorithm

    @property
    def algorithm(self) -> HashAlgorithm:
        return self._algorithm

    def hash_bytes(self, data: bytes) -> ContentHash:
        """Hash raw bytes."""
        if self._algorithm == HashAlgorithm.BLAKE2B:
            h = hashlib.blake2b(data)
        else:
            h = hashlib.new(self._algorithm.value, data)  # type: ignore[assignment]
        return ContentHash(algorithm=self._algorithm, hex_digest=h.hexdigest())

    def hash_string(self, data: str) -> ContentHash:
        """Hash UTF-8 encoded string."""
        return self.hash_bytes(data.encode("utf-8"))

    def hash_json(self, data: Any) -> ContentHash:
        """
        Hash JSON-serializable data deterministically.

        Keys are sorted, no whitespace, to ensure consistency.
        """
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return self.hash_string(canonical)

    def chain_hashes(self, *hashes: ContentHash) -> ContentHash:
        """
        Compute hash of concatenated hashes (for hash chains).

        Used to link ledger entries: H(prev_hash || current_hash)
        """
        combined = b"".join(h.to_bytes() for h in hashes)
        return self.hash_bytes(combined)

    def verify(self, data: bytes | str | Any, expected: ContentHash) -> bool:
        """Verify that data matches expected hash."""
        if isinstance(data, bytes):
            actual = self.hash_bytes(data)
        elif isinstance(data, str):
            actual = self.hash_string(data)
        else:
            actual = self.hash_json(data)
        return actual == expected
