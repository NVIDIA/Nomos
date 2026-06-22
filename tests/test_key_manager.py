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
Tests for CouncilKeyManager — key pair lifecycle on disk.
"""

import pytest

from nomos.crypto.key_manager import CouncilKeyManager
from nomos.level_0.crypto.signing import KeyPair, Signer, Verifier


class TestCouncilKeyManager:
    """Tests for key generation, persistence, and loading."""

    def test_generate_on_first_startup(self, tmp_path):
        """First call generates keys and writes them to disk."""
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        kp = mgr.load_or_generate()

        assert (tmp_path / "keys" / "council.key").exists()
        assert (tmp_path / "keys" / "council.pub").exists()
        assert len(kp.public_key) == 32
        assert len(kp.private_key) == 32

    def test_load_existing_on_second_startup(self, tmp_path):
        """Second call loads existing key pair, same keys."""
        key_dir = str(tmp_path / "keys")
        mgr1 = CouncilKeyManager(key_dir)
        kp1 = mgr1.load_or_generate()

        mgr2 = CouncilKeyManager(key_dir)
        kp2 = mgr2.load_or_generate()

        assert kp2.public_key == kp1.public_key
        assert kp2.private_key == kp1.private_key

    def test_sign_verify_roundtrip_after_reload(self, tmp_path):
        """Generate → persist → reload → sign → verify with original pubkey."""
        key_dir = str(tmp_path / "keys")

        mgr1 = CouncilKeyManager(key_dir)
        kp1 = mgr1.load_or_generate()

        mgr2 = CouncilKeyManager(key_dir)
        kp2 = mgr2.load_or_generate()

        signer = Signer(kp2)
        verifier = Verifier()
        verifier.register_public_key(kp1.key_id, kp1.public_key)

        sig = signer.sign(b"writ payload")
        assert verifier.verify(b"writ payload", sig) is True

    def test_private_key_file_permissions(self, tmp_path):
        """Private key file should have restrictive permissions."""
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        mgr.load_or_generate()

        priv_path = tmp_path / "keys" / "council.key"
        mode = oct(priv_path.stat().st_mode & 0o777)
        assert mode == "0o400", f"Expected 0o400, got {mode}"

    def test_corrupted_private_key_raises(self, tmp_path):
        """Invalid private key file raises ValueError."""
        key_dir = tmp_path / "keys"
        key_dir.mkdir(parents=True)
        (key_dir / "council.key").write_bytes(b"too-short")

        mgr = CouncilKeyManager(str(key_dir))
        with pytest.raises(ValueError, match="32 bytes"):
            mgr.load_or_generate()

    def test_mismatched_public_key_raises(self, tmp_path):
        """Public key file that doesn't match private key raises ValueError."""
        key_dir = tmp_path / "keys"
        key_dir.mkdir(parents=True)

        # Generate real key pair
        kp = KeyPair.generate()
        (key_dir / "council.key").write_bytes(kp.private_key)

        # Write a DIFFERENT public key
        other_kp = KeyPair.generate()
        (key_dir / "council.pub").write_bytes(other_kp.public_key)

        mgr = CouncilKeyManager(str(key_dir))
        with pytest.raises(ValueError, match="does not match"):
            mgr.load_or_generate()

    def test_get_public_key_hex(self, tmp_path):
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        mgr.load_or_generate()
        hex_str = mgr.get_public_key_hex()
        assert len(hex_str) == 64  # 32 bytes = 64 hex chars

    def test_export_public_key_to_separate_path(self, tmp_path):
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        kp = mgr.load_or_generate()

        export_path = tmp_path / "public-keys" / "council.pub"
        mgr.export_public_key(export_path)

        assert export_path.read_bytes() == kp.public_key
        assert not (tmp_path / "public-keys" / "council.key").exists()

    def test_export_public_key_can_replace_read_only_export(self, tmp_path):
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        kp = mgr.load_or_generate()

        export_path = tmp_path / "public-keys" / "council.pub"
        mgr.export_public_key(export_path)
        mgr.export_public_key(export_path)

        assert export_path.read_bytes() == kp.public_key

    def test_get_fingerprint(self, tmp_path):
        import hashlib

        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        kp = mgr.load_or_generate()
        expected = hashlib.sha256(kp.public_key).hexdigest()
        assert mgr.get_fingerprint() == expected

    def test_key_pair_property_before_load_raises(self, tmp_path):
        mgr = CouncilKeyManager(str(tmp_path / "keys"))
        with pytest.raises(RuntimeError, match="not loaded"):
            _ = mgr.key_pair

    def test_creates_nested_directories(self, tmp_path):
        """Key manager creates deeply nested directories."""
        deep_path = tmp_path / "a" / "b" / "c" / "keys"
        mgr = CouncilKeyManager(str(deep_path))
        mgr.load_or_generate()
        assert (deep_path / "council.key").exists()
