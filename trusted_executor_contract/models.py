# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared contract models for trusted executor services.

The council issues a signed Writ of Execution. A trusted executor receives
that writ over this HTTP contract, verifies it locally, and returns a
structured result. These models are the stable boundary third-party
executors should conform to.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

CONTRACT_VERSION = "v1"

AGENT_ID_HEADER = "X-Agent-ID"

EXECUTE_ENDPOINT = "/v1/writs/execute"
HEALTH_ENDPOINT = "/v1/health"
WELL_KNOWN_ENDPOINT = "/.well-known/nomos/trusted-executor.json"

LEGACY_EXECUTE_ENDPOINT = "/execute"
LEGACY_HEALTH_ENDPOINT = "/health"


class VoteSummary(BaseModel):
    """Advisory summary of a council member's vote."""

    member_id: str
    decision: str
    risk_concerns: list[str] = Field(default_factory=list)


class PackagedFile(BaseModel):
    """A file bundled into a writ for materialization before execution."""

    path: str = Field(..., min_length=1)
    content_base64: str = Field(..., min_length=1)
    sha256: str = ""
    mode: str = "0600"
    description: str = ""

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("packaged file path must not contain null bytes")
        normalized = posixpath.normpath(value)
        if normalized in {"", "."}:
            raise ValueError("packaged file path must name a file")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise ValueError(
                "packaged file path must be relative and stay within working_directory"
            )
        return normalized

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = value.strip()
        if len(mode) == 4:
            if not mode.startswith("0"):
                raise ValueError("four-digit mode strings must start with 0")
            mode_digits = mode[1:]
        else:
            mode_digits = mode
        if len(mode_digits) != 3 or any(ch not in "01234567" for ch in mode_digits):
            raise ValueError("mode must be an octal string such as 0600 or 0755")
        return "0" + mode_digits

    @model_validator(mode="after")
    def validate_hash(self) -> PackagedFile:
        content = self.decoded_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if self.sha256 and self.sha256.lower() != digest:
            raise ValueError("packaged file sha256 does not match decoded content")
        self.sha256 = digest
        return self

    def decoded_bytes(self) -> bytes:
        try:
            return base64.b64decode(self.content_base64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as e:
            raise ValueError("content_base64 must be valid base64") from e

    def content_for_hashing(self) -> dict[str, str]:
        return {
            "path": self.path,
            "content_base64": self.content_base64,
            "sha256": self.sha256,
            "mode": self.mode,
            "description": self.description,
        }


class WritOfExecution(BaseModel):
    """Cryptographically signed, single-use authorization token."""

    writ_id: str = Field(..., description="UUIDv7 identifier")
    intent_hash: str = Field(..., description="SHA-256 hex of canonical Intent")

    command: str = Field(..., description="Exact shell command authorized")
    working_directory: str = Field(default="", description="Exact cwd for execution")
    target_agent_id: str = Field(..., description="Which agent may use this writ")
    packaged_files: list[PackagedFile] = Field(default_factory=list)

    issued_at: float = Field(..., description="Unix timestamp (UTC)")
    expires_at: float = Field(..., description="Hard expiration (UTC)")

    max_executions: int = Field(default=1, ge=1)
    nonce: str = Field(..., description="32 random bytes, hex-encoded")

    council_id: str = Field(default="")
    threshold_used: str = Field(default="unanimous")
    member_votes: list[VoteSummary] = Field(default_factory=list)
    chairman_risk_level: str = Field(default="medium")

    signature: str = Field(default="", description="Ed25519 signature, hex-encoded")
    ledger_entry_hash: str = Field(default="")

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for signing/verification."""
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
        """Verify the writ's Ed25519 signature."""
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
        """Check if the writ has expired."""
        if now is None:
            now = time.time()
        return now > self.expires_at


class ExecuteWritRequest(BaseModel):
    """HTTP request body for submitting a writ to a trusted executor."""

    writ: str = Field(..., min_length=1, description="Base64-encoded writ JSON")
    request_id: str = Field(default="", description="Optional caller correlation id")
    dry_run: bool = Field(default=False, description="Validate without executing if supported")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionContext(BaseModel):
    """Caller context supplied outside the writ body."""

    caller_agent_id: str = ""
    request_id: str = ""
    remote_addr: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class ExecutedCommandEvidence(BaseModel):
    """Command material that an executor claims it submitted to its runtime."""

    command: str = ""
    working_directory: str = ""
    shell: bool | None = None
    argv: list[str] = Field(default_factory=list)


class ExecutionAttachmentEvidence(BaseModel):
    """Attachment manifest included in executor-signed evidence."""

    path: str
    sha256: str
    mode: str = ""
    size_bytes: int | None = None
    description: str = ""


class ExecutionResultEvidence(BaseModel):
    """Tamper-evident digest of the result returned by an executor."""

    success: bool = False
    exit_code: int | None = None
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    verification_stage: str = ""


class ExecutionEvidencePayload(BaseModel):
    """Canonical executor claim that is signed after execution."""

    evidence_version: str = "v1"
    executor_id: str = ""
    executor_key_fingerprint: str = ""
    writ_id: str = ""
    intent_hash: str = ""
    council_id: str = ""
    writ_signature: str = ""
    target_agent_id: str = ""
    nonce: str = ""
    command: ExecutedCommandEvidence = Field(default_factory=ExecutedCommandEvidence)
    attachments: list[ExecutionAttachmentEvidence] = Field(default_factory=list)
    result: ExecutionResultEvidence = Field(default_factory=ExecutionResultEvidence)
    request_id: str = ""
    signed_at: float = 0.0

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for executor evidence signing."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class ExecutionEvidence(BaseModel):
    """Executor-signed evidence binding a writ to the returned result."""

    algorithm: str = "ed25519"
    public_key_hex: str = ""
    key_fingerprint: str = ""
    payload: ExecutionEvidencePayload = Field(default_factory=ExecutionEvidencePayload)
    signature: str = ""

    def verify(self) -> bool:
        """Verify the evidence signature against the embedded executor public key."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key_hex))
            public_key.verify(bytes.fromhex(self.signature), self.payload.canonical_bytes())
            return True
        except (InvalidSignature, ValueError):
            return False


class ExecutionResult(BaseModel):
    """Result of writ verification and execution."""

    contract_version: str = CONTRACT_VERSION
    executor_id: str = ""
    writ_id: str = ""
    success: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    error: str | None = None
    verification_stage: str = Field(default="pending", description="Which check failed or passed")
    evidence: ExecutionEvidence | None = None
    executed_command_shell: bool | None = Field(default=None, exclude=True)
    executed_command_argv: list[str] = Field(default_factory=list, exclude=True)


class ExecutorCapabilities(BaseModel):
    """Advertised executor capabilities for client and deployment discovery."""

    execution_modes: list[str] = Field(default_factory=list)
    supports_packaged_files: bool = True
    supports_dry_run: bool = False
    supports_async_execution: bool = False
    supported_verification_stages: list[str] = Field(
        default_factory=lambda: [
            "deserialize",
            "agent_binding",
            "signature",
            "temporal",
            "nonce",
            "directory",
            "blocked_pattern",
            "tool_whitelist",
            "denied_args",
            "denied_path",
            "command",
            "attachment",
            "execution",
            "timeout",
            "passed",
            "internal_error",
        ]
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutorHealth(BaseModel):
    """Health response for a trusted executor."""

    status: str = "ok"
    contract_version: str = CONTRACT_VERSION
    executor_id: str = ""
    key_fingerprint: str = ""
    uptime_seconds: float = 0.0
    capabilities: ExecutorCapabilities = Field(default_factory=ExecutorCapabilities)


class ContractDescriptor(BaseModel):
    """Machine-readable contract descriptor served from the well-known endpoint."""

    name: str = "nomos.trusted-executor"
    contract_version: str = CONTRACT_VERSION
    execute_endpoint: str = EXECUTE_ENDPOINT
    health_endpoint: str = HEALTH_ENDPOINT
    agent_id_header: str = AGENT_ID_HEADER
    request_schema: str = "ExecuteWritRequest"
    result_schema: str = "ExecutionResult"
    capabilities: ExecutorCapabilities = Field(default_factory=ExecutorCapabilities)


@runtime_checkable
class TrustedExecutor(Protocol):
    """Python interface implemented by trusted executor backends."""

    async def execute_writ(
        self,
        serialized_writ: str,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        """Verify and execute a serialized writ."""


def build_agent_binding_error(error: str, writ_id: str = "") -> ExecutionResult:
    """Build a standard agent-binding failure response."""
    return ExecutionResult(
        writ_id=writ_id,
        success=False,
        error=error,
        verification_stage="agent_binding",
    )


def validate_agent_binding(writ_blob: str, caller_agent_id: str) -> ExecutionResult | None:
    """Validate caller agent identity matches writ.target_agent_id."""
    if not caller_agent_id:
        return build_agent_binding_error(f"Missing required {AGENT_ID_HEADER} header")

    try:
        json_bytes = base64.b64decode(writ_blob)
        writ = WritOfExecution.model_validate_json(json_bytes)
    except Exception as e:
        return build_agent_binding_error(f"Invalid writ payload for agent binding check: {e}")

    if caller_agent_id != writ.target_agent_id:
        return build_agent_binding_error(
            (
                "Agent binding mismatch: caller does not match writ target "
                f"(caller={caller_agent_id!r}, target={writ.target_agent_id!r})"
            ),
            writ_id=writ.writ_id,
        )

    return None
