# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for crypto module.

Following Lakos: test hashing and signing primitives independently.
"""

import pytest

from nomos.level_0.crypto import (
    ContentHash,
    ContentHasher,
    HashAlgorithm,
    KeyPair,
    Signature,
    Signer,
    Verifier,
)


class TestContentHash:
    """Tests for ContentHash value object."""

    def test_from_string_roundtrip(self):
        original = ContentHash(HashAlgorithm.SHA256, "abcd1234")
        string_form = str(original)
        parsed = ContentHash.from_string(string_form)
        assert parsed == original

    def test_to_bytes(self):
        ch = ContentHash(HashAlgorithm.SHA256, "deadbeef")
        assert ch.to_bytes() == bytes.fromhex("deadbeef")

    def test_str_format(self):
        ch = ContentHash(HashAlgorithm.SHA256, "abc123")
        assert str(ch) == "sha256:abc123"


class TestContentHasher:
    """Tests for ContentHasher."""

    def test_hash_bytes_deterministic(self):
        hasher = ContentHasher()
        data = b"hello world"
        hash1 = hasher.hash_bytes(data)
        hash2 = hasher.hash_bytes(data)
        assert hash1 == hash2

    def test_hash_string_deterministic(self):
        hasher = ContentHasher()
        data = "hello world"
        hash1 = hasher.hash_string(data)
        hash2 = hasher.hash_string(data)
        assert hash1 == hash2

    def test_hash_json_deterministic(self):
        hasher = ContentHasher()
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}
        hash1 = hasher.hash_json(data1)
        hash2 = hasher.hash_json(data2)
        assert hash1 == hash2

    def test_different_data_different_hash(self):
        hasher = ContentHasher()
        hash1 = hasher.hash_string("hello")
        hash2 = hasher.hash_string("world")
        assert hash1 != hash2

    def test_sha512_algorithm(self):
        hasher = ContentHasher(HashAlgorithm.SHA512)
        result = hasher.hash_string("test")
        assert result.algorithm == HashAlgorithm.SHA512
        assert len(result.hex_digest) == 128

    def test_blake2b_algorithm(self):
        hasher = ContentHasher(HashAlgorithm.BLAKE2B)
        result = hasher.hash_string("test")
        assert result.algorithm == HashAlgorithm.BLAKE2B

    def test_chain_hashes(self):
        hasher = ContentHasher()
        h1 = hasher.hash_string("first")
        h2 = hasher.hash_string("second")
        chained = hasher.chain_hashes(h1, h2)
        assert chained.algorithm == hasher.algorithm

    def test_verify_bytes(self):
        hasher = ContentHasher()
        data = b"test data"
        expected = hasher.hash_bytes(data)
        assert hasher.verify(data, expected) is True
        assert hasher.verify(b"wrong data", expected) is False

    def test_verify_string(self):
        hasher = ContentHasher()
        data = "test string"
        expected = hasher.hash_string(data)
        assert hasher.verify(data, expected) is True

    def test_verify_json(self):
        hasher = ContentHasher()
        data = {"key": "value", "num": 42}
        expected = hasher.hash_json(data)
        assert hasher.verify(data, expected) is True


class TestKeyPairAndSigning:
    """Tests for KeyPair, Signer, and Verifier."""

    def test_keypair_generate(self):
        kp = KeyPair.generate()
        assert kp.public_key is not None
        assert kp.private_key is not None
        assert kp.key_id.startswith("key-")

    def test_keypair_generate_custom_id(self):
        kp = KeyPair.generate(key_id="my-key")
        assert kp.key_id == "my-key"

    def test_signer_creates_signature(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        sig = signer.sign(b"hello")
        assert isinstance(sig, Signature)
        assert sig.signer_id == kp.key_id

    def test_signature_roundtrip(self):
        original = Signature(signer_id="test", signature_bytes=b"\x01\x02\x03")
        hex_sig = original.hex()
        restored = Signature.from_hex("test", hex_sig)
        assert restored.signature_bytes == original.signature_bytes

    def test_verifier_verifies_valid_signature(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        verifier = Verifier()
        verifier.register_key_pair(kp)

        data = b"message to sign"
        sig = signer.sign(data)

        assert verifier.verify(data, sig) is True

    def test_verifier_rejects_invalid_signature(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        verifier = Verifier()
        verifier.register_key_pair(kp)

        sig = signer.sign(b"original message")
        assert verifier.verify(b"different message", sig) is False

    def test_verifier_rejects_unknown_signer(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        verifier = Verifier()

        sig = signer.sign(b"message")
        assert verifier.verify(b"message", sig) is False

    def test_sign_and_verify_hex(self):
        kp = KeyPair.generate()
        signer = Signer(kp)
        verifier = Verifier()
        verifier.register_key_pair(kp)

        hex_data = "deadbeef"
        sig = signer.sign_hex(hex_data)
        assert verifier.verify_hex(hex_data, sig) is True


class TestKeyPairSerialization:
    """Tests for key serialization, loading, and fingerprinting (Phase 0.1)."""

    def test_from_raw_bytes_roundtrip(self):
        """generate → serialize → from_raw_bytes → sign → verify"""
        original = KeyPair.generate(key_id="test-key")
        restored = KeyPair.from_raw_bytes(
            private_key=original.private_key,
            public_key=original.public_key,
            key_id="test-key",
        )
        assert restored.public_key == original.public_key
        assert restored.private_key == original.private_key
        assert restored.key_id == "test-key"

        # Sign with restored, verify with original
        signer = Signer(restored)
        verifier = Verifier()
        verifier.register_key_pair(original)
        sig = signer.sign(b"test data")
        assert verifier.verify(b"test data", sig) is True

    def test_from_private_key_bytes_derives_public(self):
        """from_private_key_bytes re-derives the correct public key."""
        original = KeyPair.generate(key_id="derive-test")
        derived = KeyPair.from_private_key_bytes(original.private_key, key_id="derive-test")
        assert derived.public_key == original.public_key

    def test_deterministic_same_key_bytes_same_object(self):
        """Same raw bytes produce functionally identical key pairs."""
        kp1 = KeyPair.generate(key_id="det-test")
        kp2 = KeyPair.from_raw_bytes(kp1.private_key, kp1.public_key, key_id="det-test")
        kp3 = KeyPair.from_private_key_bytes(kp1.private_key, key_id="det-test")

        assert kp2.public_key == kp1.public_key
        assert kp3.public_key == kp1.public_key

    def test_tampered_signature_fails(self):
        """Modifying a signature makes verification fail."""
        kp = KeyPair.generate()
        signer = Signer(kp)
        verifier = Verifier()
        verifier.register_key_pair(kp)

        sig = signer.sign(b"important data")
        # Tamper with the signature bytes
        tampered = Signature(
            signer_id=sig.signer_id,
            signature_bytes=bytes([b ^ 0xFF for b in sig.signature_bytes]),
        )
        assert verifier.verify(b"important data", tampered) is False

    def test_from_raw_bytes_rejects_wrong_length(self):
        """from_raw_bytes rejects keys that aren't 32 bytes."""
        with pytest.raises(ValueError, match="32 bytes"):
            KeyPair.from_raw_bytes(b"short", b"x" * 32)
        with pytest.raises(ValueError, match="32 bytes"):
            KeyPair.from_raw_bytes(b"x" * 32, b"short")

    def test_from_private_key_bytes_rejects_wrong_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            KeyPair.from_private_key_bytes(b"too-short")

    def test_fingerprint_is_sha256_of_public_key(self):
        """fingerprint() returns SHA-256 hex of the raw public key."""
        import hashlib

        kp = KeyPair.generate()
        expected = hashlib.sha256(kp.public_key).hexdigest()
        assert kp.fingerprint() == expected

    def test_fingerprint_deterministic(self):
        kp = KeyPair.generate()
        assert kp.fingerprint() == kp.fingerprint()

    def test_public_key_hex_roundtrip(self):
        kp = KeyPair.generate()
        hex_str = kp.public_key_hex()
        assert bytes.fromhex(hex_str) == kp.public_key
