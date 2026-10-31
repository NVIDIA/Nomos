# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The plugin's own Ed25519 identity, when the operator configures ``signing_key``.

It signs a receipt for every stored record, and is the signer of records no
Nomos service signed (local tool results, denials, refusals). The plugin never
generates one: evidence must be signed, so without a configured key the plugin
keeps no evidence at all.

The key also certifies, as a Nomos ``ExecutionResult`` (``certify``), the result
of each guarded local tool it dispatched under a verified writ, each Council
answer that authorized nothing, and with ``tool_definitions`` on, the tool
definitions offered to the model. These are the only records cited to the Council
(with verified Executor results); refusals are never cited. The Council treats
them as certified only when the operator has registered this key as a trusted
executor under ``executor_id``.
"""

from __future__ import annotations

import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .wire import canonical, sha256_hex

SIGNER_ID = "nomos.council"
EXECUTOR_PREFIX = "nomos-relay-plugin"


def generate_seed() -> bytes:
    return os.urandom(32)


class PluginSigner:
    def __init__(self, seed: bytes):
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        raw = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key_hex = raw.hex()
        self.key_fingerprint = sha256_hex(raw)

    @property
    def identity(self) -> dict:
        return {
            "type": "plugin",
            "id": SIGNER_ID,
            "public_key_hex": self.public_key_hex,
            "key_fingerprint": self.key_fingerprint,
        }

    def sign(
        self, content_sha256: str, *, evidence_id: str, created_at: float, record_sha256: str | None = None
    ) -> dict:
        signed = {"evidence_id": evidence_id, "content_sha256": content_sha256, "created_at": created_at}
        if record_sha256 is not None:
            signed["record_sha256"] = record_sha256  # binds every other field of the record too
        return {"algorithm": "ed25519", "signed": signed, "value": self._key.sign(canonical(signed)).hex()}

    @property
    def executor_id(self) -> str:
        """The trusted-executor id the operator registers this key under with the Council."""
        return f"{EXECUTOR_PREFIX}:{self.key_fingerprint[:16]}"

    def certify(
        self,
        stdout: str,
        *,
        command: str,
        agent_id: str,
        council_id: str,
        request_id: str,
        success: bool = True,
        error: str | None = None,
        writ: dict | None = None,
    ) -> dict:
        """``stdout`` as a Nomos ``ExecutionResult`` whose evidence payload this key signs.

        The payload has the exact fields, types and canonical bytes of Nomos's ``ExecutionEvidencePayload``.
        ``command`` says what was observed. ``writ`` (a verified v2 writ payload) binds the result to the writ it
        ran under; without one, nothing was authorized to run and the writ fields are empty.
        ``stdout`` must be ASCII-safe JSON text: Nomos hashes it as UTF-8.
        """
        raw = stdout.encode("utf-8")
        writ_id, intent_hash, nonce = (
            (writ["writ_id"], writ["intent_hash"], writ["nonce"]) if writ else ("", "", "")
        )
        result = {
            "success": success,
            "exit_code": None,
            "duration_seconds": 0.0,
            "error": error,
            "verification_stage": "observed",
        }
        payload: dict[str, object] = {
            "evidence_version": "v1",
            "executor_id": self.executor_id,
            "executor_key_fingerprint": self.key_fingerprint,
            "writ_id": writ_id,
            "intent_hash": intent_hash,
            "council_id": council_id,
            "writ_signature": "",
            "target_agent_id": agent_id,
            "nonce": nonce,
            "command": {"command": command, "working_directory": "", "shell": None, "argv": []},
            "attachments": [],
            "result": {
                **result,
                "stdout_sha256": sha256_hex(raw),
                "stderr_sha256": sha256_hex(b""),
                "stdout_bytes": len(raw),
                "stderr_bytes": 0,
            },
            "request_id": request_id,
            "signed_at": time.time(),
        }
        return {
            "contract_version": "v1",
            "executor_id": self.executor_id,
            "writ_id": writ_id,
            **result,
            "stdout": stdout,
            "stderr": "",
            "evidence": {
                "algorithm": "ed25519",
                "public_key_hex": self.public_key_hex,
                "key_fingerprint": self.key_fingerprint,
                "payload": payload,
                "signature": self._key.sign(canonical(payload)).hex(),
            },
        }


def verify_plugin_signature(public_key_hex: str, signature: dict) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature["value"]), canonical(signature["signed"]))
        return signature.get("algorithm") == "ed25519"
    except (InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
        return False
