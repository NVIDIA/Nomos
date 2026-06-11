"""
Digital signature primitives for non-repudiation.

Provides Ed25519 signing for ledger entries and capability tokens.
"""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Signature:
    """Digital signature with signer identity."""

    signer_id: str
    signature_bytes: bytes
    algorithm: str = "ed25519"

    def hex(self) -> str:
        return self.signature_bytes.hex()

    @classmethod
    def from_hex(cls, signer_id: str, hex_sig: str, algorithm: str = "ed25519") -> "Signature":
        return cls(signer_id=signer_id, signature_bytes=bytes.fromhex(hex_sig), algorithm=algorithm)


@runtime_checkable
class SignerLike(Protocol):
    """Anything that signs bytes under a stable key id.

    Satisfied by the local Signer below and by the signer providers in
    nomos.crypto.signer_provider (including Vault transit, which never
    exposes private key bytes).
    """

    @property
    def key_id(self) -> str: ...

    def sign(self, data: bytes) -> Signature: ...


@dataclass(frozen=True)
class KeyPair:
    """Ed25519 key pair for signing operations."""

    public_key: bytes
    private_key: bytes
    key_id: str

    @classmethod
    def generate(cls, key_id: str | None = None) -> "KeyPair":
        """Generate a new Ed25519 key pair."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key()

            return cls(
                public_key=public_key.public_bytes_raw(),
                private_key=private_key.private_bytes_raw(),
                key_id=key_id or f"key-{secrets.token_hex(8)}",
            )
        except ImportError as err:
            raise ImportError(
                "cryptography package required for signing: pip install cryptography"
            ) from err

    @classmethod
    def from_raw_bytes(
        cls,
        private_key: bytes,
        public_key: bytes,
        key_id: str | None = None,
    ) -> "KeyPair":
        """Construct a KeyPair from raw 32-byte Ed25519 key material."""
        if len(private_key) != 32:
            raise ValueError(f"Ed25519 private key must be 32 bytes, got {len(private_key)}")
        if len(public_key) != 32:
            raise ValueError(f"Ed25519 public key must be 32 bytes, got {len(public_key)}")
        return cls(
            public_key=public_key,
            private_key=private_key,
            key_id=key_id or f"key-{secrets.token_hex(8)}",
        )

    @classmethod
    def from_private_key_bytes(cls, raw: bytes, key_id: str | None = None) -> "KeyPair":
        """Derive a KeyPair from raw 32-byte private key (re-derives public key)."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if len(raw) != 32:
            raise ValueError(f"Ed25519 private key must be 32 bytes, got {len(raw)}")
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        pub = priv.public_key()
        return cls(
            public_key=pub.public_bytes_raw(),
            private_key=raw,
            key_id=key_id or f"key-{secrets.token_hex(8)}",
        )

    def public_key_hex(self) -> str:
        return self.public_key.hex()

    def fingerprint(self) -> str:
        """SHA-256 fingerprint of the public key (hex)."""
        return hashlib.sha256(self.public_key).hexdigest()


class Signer:
    """Signs content using Ed25519."""

    def __init__(self, key_pair: KeyPair):
        self._key_pair = key_pair
        self._private_key = self._load_private_key()

    def _load_private_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return Ed25519PrivateKey.from_private_bytes(self._key_pair.private_key)

    @property
    def key_id(self) -> str:
        return self._key_pair.key_id

    def sign(self, data: bytes) -> Signature:
        """Sign raw bytes and return signature."""
        sig_bytes = self._private_key.sign(data)
        return Signature(signer_id=self._key_pair.key_id, signature_bytes=sig_bytes)

    def sign_hex(self, hex_data: str) -> Signature:
        """Sign hex-encoded data."""
        return self.sign(bytes.fromhex(hex_data))


class Verifier:
    """Verifies Ed25519 signatures."""

    def __init__(self):
        self._public_keys: dict[str, bytes] = {}

    def register_public_key(self, key_id: str, public_key: bytes) -> None:
        """Register a public key for verification."""
        self._public_keys[key_id] = public_key

    def register_key_pair(self, key_pair: KeyPair) -> None:
        """Register public key from a key pair."""
        self._public_keys[key_pair.key_id] = key_pair.public_key

    def verify(self, data: bytes, signature: Signature) -> bool:
        """Verify signature against registered public key."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key_bytes = self._public_keys.get(signature.signer_id)
        if public_key_bytes is None:
            return False

        try:
            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            public_key.verify(signature.signature_bytes, data)
            return True
        except InvalidSignature:
            return False

    def verify_hex(self, hex_data: str, signature: Signature) -> bool:
        """Verify signature against hex-encoded data."""
        return self.verify(bytes.fromhex(hex_data), signature)
