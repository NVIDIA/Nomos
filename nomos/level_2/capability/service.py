# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Capability token service: minting, validation, and lifecycle management.

This is the core service for issuing and managing capability tokens.
"""

import json
from dataclasses import dataclass
from datetime import timedelta

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_0.crypto import ContentHasher, SignerLike
from nomos.level_0.types import (
    CapabilityScope,
    CapabilityStatus,
    CapabilityToken,
    Intent,
)
from nomos.level_1.ledger import LedgerChain, LedgerEntryType

from .store import InMemoryTokenStore, TokenStore


@dataclass
class MintRequest:
    """Request to mint a new capability token."""

    intent: Intent
    scope: CapabilityScope
    ttl_seconds: int = 300
    conditions: list[str] | None = None


@dataclass
class MintResult:
    """Result of token minting."""

    success: bool
    token: CapabilityToken | None = None
    error: str | None = None
    ledger_sequence: int | None = None


@dataclass
class ValidationResult:
    """Result of token validation."""

    valid: bool
    reason: str
    token: CapabilityToken | None = None


class CapabilityService:
    """
    Service for minting and managing capability tokens.

    All token operations are recorded in the ledger.
    """

    def __init__(
        self,
        ledger: LedgerChain,
        store: TokenStore | None = None,
        clock: Clock | None = None,
        signer: SignerLike | None = None,
        issuer_id: str = "council",
        default_ttl_seconds: int = 300,
    ):
        self._ledger = ledger
        self._store = store or InMemoryTokenStore()
        self._clock = clock or SystemClock()
        self._signer = signer
        self._issuer_id = issuer_id
        self._default_ttl = default_ttl_seconds
        self._hasher = ContentHasher()

    def mint(self, request: MintRequest) -> MintResult:
        """
        Mint a new capability token for an approved intent.

        The token is anchored to the ledger and optionally signed.
        """
        now = self._clock.now()
        ttl = request.ttl_seconds or self._default_ttl
        expires_at = now + timedelta(seconds=ttl)

        ledger_entry = self._ledger.append(
            entry_type=LedgerEntryType.TOKEN_MINTED,
            payload={
                "intent_id": request.intent.intent_id,
                "scope": request.scope.to_dict(),
                "issued_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "issuer_id": self._issuer_id,
                "bearer_agent_id": request.intent.agent_id,
            },
        )

        ledger_anchor = str(ledger_entry.content_hash) if ledger_entry.content_hash else ""

        token = CapabilityToken.create(
            intent_id=request.intent.intent_id,
            scope=request.scope,
            issued_at=now,
            expires_at=expires_at,
            ledger_anchor=ledger_anchor,
            issuer_id=self._issuer_id,
            bearer_agent_id=request.intent.agent_id,
            conditions=request.conditions,
        )

        if self._signer:
            content = json.dumps(token.content_for_signing(), sort_keys=True)
            sig = self._signer.sign(content.encode("utf-8"))
            token.signature_hex = sig.hex()

        self._store.save(token)

        return MintResult(
            success=True,
            token=token,
            ledger_sequence=ledger_entry.sequence,
        )

    def validate(
        self,
        token_id: str,
        action_type: str,
        resource: str,
        operation: str,
    ) -> ValidationResult:
        """
        Validate that a token permits a specific action.

        Checks expiration, invocation count, and scope matching.
        """
        token = self._store.get(token_id)
        if token is None:
            return ValidationResult(valid=False, reason="Token not found")

        now = self._clock.now()

        if token.status == CapabilityStatus.REVOKED:
            return ValidationResult(valid=False, reason="Token has been revoked", token=token)

        if token.status == CapabilityStatus.USED:
            return ValidationResult(valid=False, reason="Token has been fully used", token=token)

        if now >= token.expires_at:
            token.status = CapabilityStatus.EXPIRED
            self._store.save(token)
            return ValidationResult(valid=False, reason="Token has expired", token=token)

        if not token.scope.matches(action_type, resource, operation):
            return ValidationResult(
                valid=False,
                reason=f"Token scope does not permit {action_type} on {resource}",
                token=token,
            )

        return ValidationResult(valid=True, reason="Token is valid", token=token)

    def consume(self, token_id: str) -> bool:
        """
        Record that a token was used.

        Increments invocation count and may mark token as USED.
        """
        token = self._store.get(token_id)
        if token is None:
            return False

        token.record_invocation()
        self._store.save(token)
        return True

    def revoke(self, token_id: str, reason: str = "") -> bool:
        """
        Revoke a token, making it unusable.

        Records revocation in the ledger.
        """
        token = self._store.get(token_id)
        if token is None:
            return False

        token.revoke()
        self._store.save(token)

        self._ledger.append(
            entry_type=LedgerEntryType.TOKEN_REVOKED,
            payload={
                "token_id": token_id,
                "intent_id": token.intent_id,
                "reason": reason,
                "revoked_at": self._clock.now().isoformat(),
            },
        )

        return True

    def revoke_by_intent(self, intent_id: str, reason: str = "") -> int:
        """Revoke all tokens for an intent."""
        tokens = self._store.get_by_intent(intent_id)
        count = 0
        for token in tokens:
            if token.status == CapabilityStatus.ACTIVE:
                self.revoke(token.token_id, reason)
                count += 1
        return count

    def get(self, token_id: str) -> CapabilityToken | None:
        """Get a token by ID."""
        return self._store.get(token_id)

    def get_active_for_agent(self, agent_id: str) -> list[CapabilityToken]:
        """Get all active tokens for an agent."""
        tokens = self._store.get_by_agent(agent_id)
        now = self._clock.now()
        return [t for t in tokens if t.is_valid(now)]

    def cleanup_expired(self) -> int:
        """Mark expired tokens and return count."""
        active = self._store.get_active()
        now = self._clock.now()
        count = 0
        for token in active:
            if now >= token.expires_at:
                token.status = CapabilityStatus.EXPIRED
                self._store.save(token)
                count += 1
        return count
