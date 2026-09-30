# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Writ Factory — mints cryptographically signed Writs of Execution.

After the council approves an intent, the factory:
1. Generates a unique writ ID and nonce
2. Computes the intent hash
3. Sets temporal constraints based on risk level
4. Signs the canonical writ bytes with the council's Ed25519 private key
5. Stores writ metadata in the configured durable store
6. Returns the complete shell or structured tool-call authorization writ
"""

import asyncio
import base64
import json
import logging
import secrets
import time

from nomos.crypto.signer_provider import InMemorySigner, SignerProvider
from nomos.level_0.crypto.hashing import ContentHasher
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import Intent
from nomos.level_0.types.tool_call import ToolCallOperation
from nomos.level_0.types.writ import (
    AuthorizationWrit,
    ToolCallWrit,
    ToolCallWritPayload,
    VoteSummary,
    WritOfExecution,
)
from nomos.store.base import DurableStore

logger = logging.getLogger(__name__)

# TTL mapping by risk level (seconds)
_TTL_BY_RISK = {
    "low": 300,  # 5 minutes
    "medium": 120,  # 2 minutes
    "high": 60,  # 1 minute
    "critical": 30,  # 30 seconds
}

TOOL_CALL_OPERATION_CONTEXT_KEY = "authorized_tool_call"


def _generate_writ_id() -> str:
    """
    Generate a time-sortable writ identifier.

    Format is 'writ-' followed by a 48-bit millisecond timestamp
    and 32-bit random data in hex.

    Returns
    -------
    str
        The generated writ ID.
    """
    ts_ms = int(time.time() * 1000)
    rand = secrets.token_bytes(4)
    return f"writ-{ts_ms:012x}{rand.hex()}"


def _compute_intent_hash(intent: Intent) -> str:
    """
    Compute SHA-256 of canonical intent data.

    Parameters
    ----------
    intent : Intent
        The intent to compute the hash for.

    Returns
    -------
    str
        The computed SHA-256 hex digest.
    """
    hasher = ContentHasher()
    data = {
        "intent_id": intent.intent_id,
        "agent_id": intent.agent_id,
        "action_type": intent.action_type,
        "command": intent.command or "",
        "command_type": intent.command_type or "",
        "packaged_files": [f.content_for_hashing() for f in intent.packaged_files],
        "working_directory": intent.working_directory or "",
        "target_resource": intent.target_resource,
        "description": intent.description,
    }
    operation = intent.execution_context.get(TOOL_CALL_OPERATION_CONTEXT_KEY)
    if operation is not None:
        data[TOOL_CALL_OPERATION_CONTEXT_KEY] = operation
    result = hasher.hash_json(data)
    return result.hex_digest


class WritFactory:
    """
    Mints signed Writs of Execution after council approval.

    Dependencies:
    - signer: SignerProvider for signing writs (or council_key_pair for
      back-compat, wrapped in an in-memory signer)
    - council_id: Identifier of the issuing council
    - store: DurableStore for persisting writ metadata (optional)
    """

    def __init__(
        self,
        council_key_pair: KeyPair | None = None,
        council_id: str = "",
        store: DurableStore | None = None,
        *,
        signer: SignerProvider | None = None,
    ):
        if signer is None:
            if council_key_pair is None:
                raise ValueError("WritFactory requires a signer or a council_key_pair")
            signer = InMemorySigner(council_key_pair)
        self._signer = signer
        self._council_id = council_id
        self._store = store

    async def mint_writ(
        self,
        intent: Intent,
        target_agent_id: str,
        threshold_used: str,
        member_votes: list[VoteSummary],
        chairman_risk_level: str,
        ledger_entry_hash: str = "",
        working_directory: str = "",
    ) -> AuthorizationWrit:
        """
        Mint a new signed writ for an approved intent.

        Parameters
        ----------
        intent : Intent
            The intent that has been approved.
        target_agent_id : str
            The agent authorized to use this writ.
        threshold_used : str
            The threshold mechanism used for approval (e.g., 'quorum').
        member_votes : List[VoteSummary]
            Advisory summary of the council's vote.
        chairman_risk_level : str
            Risk level determined by the chairman.
        ledger_entry_hash : str, optional
            Hash of the corresponding ledger entry, by default "".
        working_directory : str, optional
            Working directory for execution, by default "".

        Returns
        -------
        AuthorizationWrit
            A signed legacy shell writ, or a v2 tool-call writ when the intent
            carries a structured tool operation.
        """
        now = time.time()
        risk = chairman_risk_level.lower()
        ttl = _TTL_BY_RISK.get(risk, _TTL_BY_RISK["medium"])

        operation_data = intent.execution_context.get(TOOL_CALL_OPERATION_CONTEXT_KEY)
        operation = (
            ToolCallOperation.model_validate(operation_data) if operation_data is not None else None
        )
        if operation is not None and intent.packaged_files:
            raise ValueError("tool-call writs do not support packaged files")

        writ: AuthorizationWrit
        if operation is None:
            writ = WritOfExecution(
                writ_id=_generate_writ_id(),
                intent_hash=_compute_intent_hash(intent),
                command=intent.command or "",
                working_directory=working_directory or intent.working_directory or "",
                target_agent_id=target_agent_id,
                packaged_files=intent.packaged_files,
                issued_at=now,
                expires_at=now + ttl,
                max_executions=1,
                nonce=secrets.token_hex(32),
                council_id=self._council_id,
                threshold_used=threshold_used,
                member_votes=member_votes,
                chairman_risk_level=risk,
                ledger_entry_hash=ledger_entry_hash,
            )
            bytes_to_sign = writ.canonical_bytes()
        else:
            payload = ToolCallWritPayload(
                writ_id=_generate_writ_id(),
                intent_hash=_compute_intent_hash(intent),
                operation=operation,
                target_agent_id=target_agent_id,
                issued_at=now,
                expires_at=now + ttl,
                nonce=secrets.token_hex(32),
                council_id=self._council_id,
                threshold_used=threshold_used,
                chairman_risk_level=risk,
                ledger_entry_hash=ledger_entry_hash,
            )
            writ = ToolCallWrit.from_payload(payload, member_votes=member_votes)
            bytes_to_sign = writ.signed_bytes()

        # Sign the writ. The signer may be remote (Vault transit), so run it
        # off the event loop.
        signature = await asyncio.to_thread(self._signer.sign, bytes_to_sign)
        signed_writ = writ.with_signature(signature.signature_bytes.hex())

        # Persist to the deployment's durable store if available.
        if self._store is not None:
            try:
                if isinstance(signed_writ, ToolCallWrit):
                    stored_writ = {
                        **signed_writ.payload().model_dump(mode="json"),
                        "signature": signed_writ.signature,
                    }
                else:
                    stored_writ = signed_writ.model_dump(mode="json")
                await self._store.store_writ(signed_writ.writ_id, stored_writ)
                # Also record the intent decision
                await self._store.store_intent(
                    intent.intent_id,
                    {
                        "intent_id": intent.intent_id,
                        "command": intent.command or "",
                        "agent_id": intent.agent_id,
                        "decision": "approved",
                        "writ_id": signed_writ.writ_id,
                        "threshold_used": threshold_used,
                        "chairman_risk_level": risk,
                        "decided_at": now,
                    },
                )
                logger.info(
                    "Writ %s minted and stored for intent %s", signed_writ.writ_id, intent.intent_id
                )
            except Exception as e:
                logger.warning("Failed to store writ in durable storage: %s", type(e).__name__)

        return signed_writ

    @staticmethod
    def serialize_writ(writ: AuthorizationWrit) -> str:
        """
        Serialize a writ to a base64-encoded JSON blob.

        This is the opaque token returned to the requesting agent.
        Strips ``member_votes`` since they are advisory-only and not
        covered by the Ed25519 signature — including them would allow
        tampering without invalidating the signature.

        Parameters
        ----------
        writ : AuthorizationWrit
            The writ to serialize.

        Returns
        -------
        str
            A base64-encoded JSON string representing the writ.
        """
        data = writ.model_dump(mode="json")
        data.pop("member_votes", None)
        json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(json_bytes).decode("ascii")

    @staticmethod
    def deserialize_writ(blob: str) -> AuthorizationWrit:
        """
        Deserialize a base64-encoded shell or tool-call authorization writ.

        Parameters
        ----------
        blob : str
            The base64-encoded JSON string.

        Returns
        -------
        WritOfExecution
            The parsed WritOfExecution object.
        """
        json_bytes = base64.b64decode(blob)
        raw = json.loads(json_bytes)
        if isinstance(raw, dict) and raw.get("writ_version") == 2:
            return ToolCallWrit.model_validate(raw)
        return WritOfExecution.model_validate(raw)
