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
Agent authentication and authorization.

Authenticates agents via SHA-256 hashed tokens stored in Redis,
checks permissions against required scopes, and enforces rate
limiting on failed authentication attempts.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field

from nomos.auth.permissions import PermissionScope
from nomos.store.base import DurableStore, EphemeralStore

logger = logging.getLogger(__name__)

# Rate limiting constants
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_BLOCK_SECONDS = 300


@dataclass(frozen=True)
class AgentIdentity:
    """Authenticated agent identity with permissions."""

    agent_id: str
    display_name: str
    permissions: set[str]
    councils: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class PermissionCheckResult:
    """Result of a permission check."""

    allowed: bool
    missing: set[PermissionScope]
    agent_id: str


class AgentAuthenticator:
    """
    Authenticates agents and checks permissions via Redis.

    Token verification uses constant-time comparison on the hash
    to prevent timing attacks.
    """

    def __init__(self, store: DurableStore, ephemeral: EphemeralStore | None = None):
        # `store` holds durable account state; `ephemeral` holds rate
        # counters and must be Redis-backed. RedisStore satisfies both, so a
        # single-argument construction keeps the historical behavior.
        self._store = store
        self._ephemeral: EphemeralStore = ephemeral if ephemeral is not None else store  # type: ignore[assignment]

    async def authenticate(self, token: str) -> AgentIdentity | None:
        """
        Authenticate an agent by raw token.

        Computes sha256(token), looks up the hash in Redis via
        reverse index, validates the account is enabled, and
        returns the agent identity with permissions.

        Parameters
        ----------
        token : str
            The raw authentication token.

        Returns
        -------
        Optional[AgentIdentity]
            The authenticated agent identity, or None on failure
            (invalid token, disabled account, or rate-limited).
        """
        if not token:
            return None

        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Check rate limiting on this token hash
        rate_count = await self._ephemeral.get_rate_limit_count("auth_fail", token_hash)
        if rate_count >= RATE_LIMIT_MAX_FAILURES:
            logger.warning("Rate limited auth attempt for token hash %s...", token_hash[:8])
            return None

        # Look up agent_id by token hash (O(1) via reverse index)
        agent_id = await self._store.find_agent_by_token_hash(token_hash)
        if agent_id is None:
            await self._ephemeral.increment_rate_limit(
                "auth_fail",
                token_hash,
                RATE_LIMIT_WINDOW_SECONDS,
            )
            return None

        # Get account details
        account = await self._store.get_account(agent_id)
        if account is None:
            await self._ephemeral.increment_rate_limit(
                "auth_fail",
                token_hash,
                RATE_LIMIT_WINDOW_SECONDS,
            )
            return None

        # Verify the stored hash matches (constant-time comparison)
        stored_hash = account.get("token_hash", "")
        if not hmac.compare_digest(stored_hash, token_hash):
            await self._ephemeral.increment_rate_limit(
                "auth_fail",
                token_hash,
                RATE_LIMIT_WINDOW_SECONDS,
            )
            return None

        # Check account is enabled
        if account.get("enabled") != "1":
            logger.info("Auth rejected for disabled account %s", agent_id)
            return None

        # Get permissions
        permissions = await self._store.get_account_permissions(agent_id)
        councils = await self._store.get_account_councils(agent_id)
        if not self._store.verify_account_integrity(agent_id, account, permissions, councils):
            logger.warning(
                "Auth rejected for account %s with invalid Redis integrity envelope",
                agent_id,
            )
            return None

        return AgentIdentity(
            agent_id=agent_id,
            display_name=account.get("display_name", agent_id),
            permissions=permissions,
            councils=councils,
        )

    @staticmethod
    def can_petition_council(agent: AgentIdentity, council_id: str) -> bool:
        """Return whether an authenticated agent may petition a council."""
        return "*" in agent.councils or council_id in agent.councils

    def check_permission(
        self,
        agent: AgentIdentity,
        required: set[PermissionScope],
    ) -> PermissionCheckResult:
        """
        Check if an agent has all required permissions.

        Parameters
        ----------
        agent : AgentIdentity
            The agent identity to check permissions for.
        required : Set[PermissionScope]
            The set of required permission scopes.

        Returns
        -------
        PermissionCheckResult
            A result indicating whether the agent has the required permissions,
            including any missing scopes.
        """
        {p.value for p in required}
        missing = {p for p in required if p.value not in agent.permissions}

        return PermissionCheckResult(
            allowed=len(missing) == 0,
            missing=missing,
            agent_id=agent.agent_id,
        )
