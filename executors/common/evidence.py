"""Executor identity and signed execution evidence helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_executor_contract import (
    ExecutedCommandEvidence,
    ExecutionAttachmentEvidence,
    ExecutionEvidence,
    ExecutionEvidencePayload,
    ExecutionResult,
    ExecutionResultEvidence,
    WritOfExecution,
)


@dataclass(frozen=True)
class ExecutorIdentity:
    """Executor-owned Ed25519 signing identity."""

    private_key: Ed25519PrivateKey
    public_key_bytes: bytes

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.public_key_bytes).hexdigest()

    def sign(self, payload: bytes) -> bytes:
        return self.private_key.sign(payload)


def load_or_create_executor_identity(
    private_key_path: str = "",
    public_key_path: str = "",
) -> ExecutorIdentity:
    """Load an executor key from disk, or generate and persist one if missing.

    If ``private_key_path`` is empty, an ephemeral in-memory key is generated.
    That is useful for tests, but deployed executors should provide a writable
    key path so evidence can be verified across restarts.
    """
    if private_key_path:
        path = Path(private_key_path)
        if path.exists():
            private_key = _load_private_key(path)
        else:
            private_key = Ed25519PrivateKey.generate()
            _write_private_key(path, private_key)
    else:
        private_key = Ed25519PrivateKey.generate()

    public_key_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if public_key_path:
        _write_public_key(Path(public_key_path), public_key_bytes)

    return ExecutorIdentity(private_key=private_key, public_key_bytes=public_key_bytes)


def certify_execution_result(
    serialized_writ: str,
    result: ExecutionResult,
    identity: ExecutorIdentity,
    *,
    request_id: str = "",
    command_shell: bool | None = None,
    command_argv: list[str] | None = None,
) -> ExecutionResult:
    """Attach executor-signed evidence to a result when the writ can be parsed."""
    try:
        writ = WritOfExecution.model_validate_json(base64.b64decode(serialized_writ))
    except Exception:
        return result

    if command_shell is None:
        command_shell = result.executed_command_shell
    if command_argv is None:
        command_argv = result.executed_command_argv

    stdout_bytes = result.stdout.encode("utf-8", errors="replace")
    stderr_bytes = result.stderr.encode("utf-8", errors="replace")
    payload = ExecutionEvidencePayload(
        executor_id=result.executor_id,
        executor_key_fingerprint=identity.key_fingerprint,
        writ_id=writ.writ_id,
        intent_hash=writ.intent_hash,
        council_id=writ.council_id,
        writ_signature=writ.signature,
        target_agent_id=writ.target_agent_id,
        nonce=writ.nonce,
        command=ExecutedCommandEvidence(
            command=writ.command,
            working_directory=writ.working_directory,
            # Data-model field assignment, not process execution.
            shell=command_shell,  # nosec B604
            argv=command_argv or [],
        ),
        attachments=[
            ExecutionAttachmentEvidence(
                path=packaged.path,
                sha256=packaged.sha256,
                mode=packaged.mode,
                size_bytes=_packaged_size(packaged),
                description=packaged.description,
            )
            for packaged in writ.packaged_files
        ],
        result=ExecutionResultEvidence(
            success=result.success,
            exit_code=result.exit_code,
            stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr_bytes).hexdigest(),
            stdout_bytes=len(stdout_bytes),
            stderr_bytes=len(stderr_bytes),
            duration_seconds=result.duration_seconds,
            error=result.error,
            verification_stage=result.verification_stage,
        ),
        request_id=request_id,
        signed_at=time.time(),
    )
    evidence = ExecutionEvidence(
        public_key_hex=identity.public_key_hex,
        key_fingerprint=identity.key_fingerprint,
        payload=payload,
        signature=identity.sign(payload.canonical_bytes()).hex(),
    )
    return result.model_copy(update={"evidence": evidence})


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    if len(data) == 32:
        return Ed25519PrivateKey.from_private_bytes(data)
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"Executor identity key at {path} is not an Ed25519 private key")
    return key


def _write_private_key(path: Path, private_key: Ed25519PrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _write_public_key(path: Path, public_key_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(public_key_bytes)
    os.chmod(path, 0o644)


def _packaged_size(packaged) -> int | None:
    try:
        return len(packaged.decoded_bytes())
    except ValueError:
        return None
