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
Council key pair lifecycle management.

Handles generation, persistence, and loading of the council's Ed25519
signing key pair. The private key is stored on disk (volume mount),
never in Redis. The public key is exported for executor verification.
"""

import logging
import os
import tempfile
from pathlib import Path

from nomos.level_0.crypto.signing import KeyPair

logger = logging.getLogger(__name__)

PRIVATE_KEY_FILENAME = "council.key"
PUBLIC_KEY_FILENAME = "council.pub"
DEFAULT_KEY_ID = "council-signing-key"


class CouncilKeyManager:
    """
    Manages the council's Ed25519 key pair on disk.

    On first startup, generates a new key pair and writes it to
    the configured directory. On subsequent startups, loads the
    existing key pair from disk.
    """

    def __init__(self, key_dir: str):
        self._key_dir = Path(key_dir)
        self._key_pair: KeyPair | None = None

    @property
    def key_pair(self) -> KeyPair:
        """Return the loaded key pair (call load_or_generate first)."""
        if self._key_pair is None:
            raise RuntimeError("Key pair not loaded. Call load_or_generate() first.")
        return self._key_pair

    @property
    def private_key_path(self) -> Path:
        return self._key_dir / PRIVATE_KEY_FILENAME

    @property
    def public_key_path(self) -> Path:
        return self._key_dir / PUBLIC_KEY_FILENAME

    def load_or_generate(self) -> KeyPair:
        """
        Load existing key pair from disk, or generate a new one.

        The private key file is created with restrictive permissions
        (owner-read-only).

        Returns
        -------
        KeyPair
            The loaded or newly generated key pair.
        """
        if self.private_key_path.exists():
            self._key_pair = self._load_from_disk()
            logger.info(
                "Loaded council key pair from %s (fingerprint: %s)",
                self._key_dir,
                self._key_pair.fingerprint()[:16],
            )
        else:
            self._key_pair = self._generate_and_persist()
            logger.info(
                "Generated new council key pair at %s (fingerprint: %s)",
                self._key_dir,
                self._key_pair.fingerprint()[:16],
            )
        return self._key_pair

    def _load_from_disk(self) -> KeyPair:
        """
        Load key pair from raw bytes on disk.

        Returns
        -------
        KeyPair
            The key pair loaded from disk.

        Raises
        ------
        ValueError
            If the private key file is invalid or public key doesn't match.
        """
        private_bytes = self.private_key_path.read_bytes()
        if len(private_bytes) != 32:
            raise ValueError(
                f"Invalid private key file {self.private_key_path}: "
                f"expected 32 bytes, got {len(private_bytes)}"
            )
        key_pair = KeyPair.from_private_key_bytes(private_bytes, key_id=DEFAULT_KEY_ID)

        # Verify public key matches if the pub file exists
        if self.public_key_path.exists():
            stored_pub = self.public_key_path.read_bytes()
            if stored_pub != key_pair.public_key:
                raise ValueError(
                    "Public key file does not match derived public key. Key files may be corrupted."
                )

        return key_pair

    def _generate_and_persist(self) -> KeyPair:
        """
        Generate a new key pair and write to disk.

        Returns
        -------
        KeyPair
            The newly generated key pair.
        """
        self._key_dir.mkdir(parents=True, exist_ok=True)

        key_pair = KeyPair.generate(key_id=DEFAULT_KEY_ID)

        # Write private key with restrictive permissions
        self.private_key_path.write_bytes(key_pair.private_key)
        try:
            os.chmod(self.private_key_path, 0o400)
        except OSError:
            logger.warning("Could not set restrictive permissions on %s", self.private_key_path)

        # Write public key (world-readable is fine)
        self.public_key_path.write_bytes(key_pair.public_key)

        return key_pair

    def get_public_key_hex(self) -> str:
        """Return the public key as hex string."""
        return self.key_pair.public_key_hex()

    def get_public_key_bytes(self) -> bytes:
        """Return the raw 32-byte public key."""
        return self.key_pair.public_key

    def get_fingerprint(self) -> str:
        """Return SHA-256 fingerprint of the public key."""
        return self.key_pair.fingerprint()

    def export_public_key(self, path: str | Path) -> None:
        """Write the public key to a separate verification-only path."""
        export_path = Path(path)
        export_dir = export_path.parent
        export_dir.mkdir(parents=True, exist_ok=True)

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=export_dir,
                prefix=f".{export_path.name}.",
                delete=False,
            ) as tmp_file:
                tmp_file.write(self.key_pair.public_key)
                tmp_path = Path(tmp_file.name)

            try:
                os.chmod(tmp_path, 0o444)
            except OSError:
                logger.warning("Could not set public permissions on %s", tmp_path)

            os.replace(tmp_path, export_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
