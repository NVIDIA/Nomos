# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Writ of Execution — cryptographically signed, single-use authorization token.

A writ is issued by the council after approval and contains the exact
command authorized to execute, temporal constraints, and a nonce for
replay prevention. The executor verifies the signature before running.
"""

import base64
import binascii
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .packaged_file import PackagedFile
from .tool_call import ToolCallOperation


class VoteSummary(BaseModel):
    """Advisory summary of a council member's vote (NOT included in signature)."""

    member_id: str
    decision: str
    risk_concerns: list[str] = Field(default_factory=list)


class WritOfExecution(BaseModel):
    """
    Cryptographically signed, single-use authorization token.

    The council mints a writ after approving an intent. The writ
    contains the exact command, scope, and temporal constraints.
    The executor verifies the signature before running anything.
    """

    # Identity
    writ_id: str = Field(..., description="UUIDv7 identifier")
    intent_hash: str = Field(..., description="SHA-256 hex of canonical Intent")

    # Scope lock
    command: str = Field(..., description="Exact shell command authorized")
    working_directory: str = Field(default="", description="Exact cwd for execution")
    target_agent_id: str = Field(..., description="Which agent may use this writ")
    packaged_files: list[PackagedFile] = Field(
        default_factory=list,
        description="Files the executor must materialize before running the command",
    )

    # Temporal constraints
    issued_at: float = Field(..., description="Unix timestamp (UTC)")
    expires_at: float = Field(..., description="Hard expiration (UTC)")

    # Usage constraints
    max_executions: int = Field(default=1, ge=1, description="Almost always 1")
    nonce: str = Field(..., description="32 random bytes, hex-encoded")

    # Provenance
    council_id: str = Field(default="", description="ID of the issuing council")
    threshold_used: str = Field(default="unanimous", description="'quorum' or 'unanimous'")
    member_votes: list[VoteSummary] = Field(
        default_factory=list, description="Advisory, NOT included in signature"
    )
    chairman_risk_level: str = Field(
        default="medium", description="'low', 'medium', 'high', 'critical'"
    )

    # Cryptographic binding
    signature: str = Field(default="", description="Ed25519 signature, hex-encoded")
    ledger_entry_hash: str = Field(default="", description="Hash of the corresponding ledger entry")

    def canonical_bytes(self) -> bytes:
        """
        Deterministic serialization for signing/verification.

        - Sorted keys, no whitespace
        - Excludes `member_votes` and `signature` fields

        Returns
        -------
        bytes
            The canonical JSON representation as UTF-8 bytes.
        """
        data = {
            "chairman_risk_level": self.chairman_risk_level,
            "command": self.command,
            "council_id": self.council_id,
            "expires_at": self.expires_at,
            "intent_hash": self.intent_hash,
            "issued_at": self.issued_at,
            "ledger_entry_hash": self.ledger_entry_hash,
            "max_executions": self.max_executions,
            "nonce": self.nonce,
            "packaged_files": [f.content_for_hashing() for f in self.packaged_files],
            "target_agent_id": self.target_agent_id,
            "threshold_used": self.threshold_used,
            "working_directory": self.working_directory,
            "writ_id": self.writ_id,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def verify(self, public_key_bytes: bytes) -> bool:
        """
        Verify the writ's signature against a raw 32-byte Ed25519 public key.

        Parameters
        ----------
        public_key_bytes : bytes
            The raw 32-byte Ed25519 public key.

        Returns
        -------
        bool
            True if the signature is valid, False otherwise.
        """
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not self.signature:
            return False

        try:
            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            sig_bytes = bytes.fromhex(self.signature)
            public_key.verify(sig_bytes, self.canonical_bytes())
            return True
        except (InvalidSignature, ValueError):
            return False

    def is_expired(self, now: float | None = None) -> bool:
        """
        Check if the writ has expired.

        Parameters
        ----------
        now : Optional[float], optional
            The current time as a Unix timestamp. If not provided, uses time.time().

        Returns
        -------
        bool
            True if the writ has expired, False otherwise.
        """
        if now is None:
            now = time.time()
        return now > self.expires_at

    def with_signature(self, signature_hex: str) -> "WritOfExecution":
        """
        Attach an externally produced signature over `canonical_bytes()`.

        This is the path for signer providers (e.g. Vault transit) that never
        expose private key bytes to the application.

        Parameters
        ----------
        signature_hex : str
            Hex-encoded Ed25519 signature over canonical_bytes().

        Returns
        -------
        WritOfExecution
            A new WritOfExecution with the signature field set.
        """
        return self.model_copy(update={"signature": signature_hex})

    def sign(self, private_key_bytes: bytes) -> "WritOfExecution":
        """
        Sign the writ with a raw 32-byte Ed25519 private key.

        Parameters
        ----------
        private_key_bytes : bytes
            The raw 32-byte Ed25519 private key.

        Returns
        -------
        WritOfExecution
            A new WritOfExecution with the signature field set.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        sig_bytes = private_key.sign(self.canonical_bytes())
        return self.with_signature(sig_bytes.hex())


class ToolCallWritPayload(BaseModel):
    """Exact bytes signed for a structured application tool authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    writ_version: Literal[2] = 2
    writ_id: str = Field(..., description="UUIDv7 identifier")
    intent_hash: str = Field(..., description="SHA-256 hex of canonical Intent")
    operation: ToolCallOperation
    target_agent_id: str
    issued_at: float
    expires_at: float
    max_executions: Literal[1] = 1
    nonce: str
    council_id: str
    threshold_used: str = "unanimous"
    chairman_risk_level: str = "medium"
    ledger_entry_hash: str = ""

    @field_validator("writ_version", "max_executions", mode="before")
    @classmethod
    def validate_strict_integer_constants(cls, value: object, info) -> object:
        """Prevent coercion from weakening signed wire constants."""

        expected = 2 if info.field_name == "writ_version" else 1
        if type(value) is not int or value != expected:
            raise ValueError(f"{info.field_name} must be integer {expected}")
        return value

    def canonical_bytes(self) -> bytes:
        """Serialize the payload once for signing and transport."""

        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class ToolCallWrit(BaseModel):
    """Signed envelope for a structured tool-call operation.

    The signature covers the decoded payload bytes directly. Verifiers first
    authenticate those immutable bytes, then strictly parse and compare the
    operation with the call they are holding. Never parse and reserialize the
    payload for signature verification: the original wire bytes are binding.
    """

    model_config = ConfigDict(extra="forbid")

    writ_version: Literal[2] = 2
    payload_base64: str
    signature: str = Field(default="", description="Ed25519 signature, hex-encoded")
    member_votes: list[VoteSummary] = Field(
        default_factory=list,
        description="Advisory vote summary; deliberately outside the signature",
    )

    @field_validator("writ_version", mode="before")
    @classmethod
    def validate_writ_version(cls, value: object) -> object:
        """Require the exact integer envelope discriminator."""

        if type(value) is not int or value != 2:
            raise ValueError("writ_version must be integer 2")
        return value

    @model_validator(mode="after")
    def validate_envelope(self) -> "ToolCallWrit":
        self.payload()
        return self

    @classmethod
    def from_payload(
        cls,
        payload: ToolCallWritPayload,
        *,
        member_votes: list[VoteSummary] | None = None,
    ) -> "ToolCallWrit":
        return cls(
            payload_base64=base64.b64encode(payload.canonical_bytes()).decode("ascii"),
            member_votes=member_votes or [],
        )

    def signed_bytes(self) -> bytes:
        """Return the immutable payload bytes covered by the signature."""

        try:
            return base64.b64decode(self.payload_base64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("tool-call writ payload is not valid base64") from exc

    def payload(self) -> ToolCallWritPayload:
        """Decode and strictly validate the unauthenticated payload structure."""

        try:
            return ToolCallWritPayload.model_validate_json(self.signed_bytes())
        except Exception as exc:
            raise ValueError("tool-call writ payload is malformed") from exc

    @property
    def writ_id(self) -> str:
        return self.payload().writ_id

    @property
    def expires_at(self) -> float:
        return self.payload().expires_at

    @property
    def nonce(self) -> str:
        return self.payload().nonce

    def with_signature(self, signature_hex: str) -> "ToolCallWrit":
        return self.model_copy(update={"signature": signature_hex})

    def verify(self, public_key_bytes: bytes) -> bool:
        """Verify the signature over the original payload bytes."""

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not self.signature:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                bytes.fromhex(self.signature), self.signed_bytes()
            )
            return True
        except (InvalidSignature, ValueError):
            return False


AuthorizationWrit = WritOfExecution | ToolCallWrit
