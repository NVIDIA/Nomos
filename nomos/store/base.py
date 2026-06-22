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
Store protocols: durable vs ephemeral state.

`DurableStore` is the contract for state that must survive restarts and be
shared across replicas (accounts, writs, outcomes, intents, trusted
executors). `RedisStore` satisfies it as-is (status quo); `PostgresStore`
satisfies it for HA deployments.

`EphemeralStore` is the Redis-only contract that never moves: atomic nonce
claims and rate counters depend on Redis semantics (SET NX EX, INCR+EXPIRE)
and must keep failing closed when Redis is down.

Method names and return shapes are verbatim from RedisStore — including its
stringly conventions ("1"/"0" for enabled, str(time.time()) timestamps, the
"_integrity_hmac" account field) — so AgentAuthenticator and friends run
byte-identical against either backend.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DurableStore(Protocol):
    """Durable source of truth for accounts, writs, intents, and executors."""

    # ── lifecycle ─────────────────────────────────────────────────────
    async def connect(self) -> None: ...

    async def connect_with_retry(self) -> bool: ...

    async def close(self) -> None: ...

    async def health_check(self) -> bool: ...

    # ── accounts ──────────────────────────────────────────────────────
    async def set_account(
        self,
        agent_id: str,
        token_hash: str,
        display_name: str = "",
        enabled: bool = True,
    ) -> None: ...

    async def get_account(self, agent_id: str) -> dict[str, str] | None: ...

    async def set_account_permissions(self, agent_id: str, permissions: set[str]) -> None: ...

    async def get_account_permissions(self, agent_id: str) -> set[str]: ...

    async def set_account_councils(self, agent_id: str, councils: set[str]) -> None: ...

    async def grant_account_council(self, agent_id: str, council_id: str) -> None: ...

    async def revoke_account_council(self, agent_id: str, council_id: str) -> None: ...

    async def get_account_councils(self, agent_id: str) -> set[str]: ...

    async def get_council_agents(self, council_id: str) -> set[str]: ...

    async def find_agent_by_token_hash(self, token_hash: str) -> str | None: ...

    async def set_token_index(self, token_hash: str, agent_id: str) -> None: ...

    async def update_account_fields(self, agent_id: str, fields: dict[str, str]) -> None: ...

    async def delete_account(self, agent_id: str) -> None: ...

    async def delete_token_index(self, token_hash: str) -> None: ...

    async def list_account_ids(self) -> list[str]: ...

    async def refresh_account_integrity(self, agent_id: str) -> None: ...

    def verify_account_integrity(
        self,
        agent_id: str,
        account: dict[str, str],
        permissions: set[str],
        councils: set[str],
    ) -> bool: ...

    # ── trusted executors ─────────────────────────────────────────────
    async def register_trusted_executor(
        self,
        *,
        executor_id: str,
        public_key_hex: str,
        key_fingerprint: str,
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None: ...

    async def get_trusted_executor(self, key_fingerprint: str) -> dict[str, str] | None: ...

    async def list_trusted_executors(self) -> list[dict[str, str]]: ...

    # ── writs / outcomes / intents ────────────────────────────────────
    async def store_writ(self, writ_id: str, writ_data: dict[str, Any]) -> None: ...

    async def get_writ(self, writ_id: str) -> dict[str, Any] | None: ...

    async def store_writ_outcome(
        self,
        writ_id: str,
        exit_code: int,
        stdout_hash: str,
        stderr_hash: str,
        executor_id: str = "",
    ) -> None: ...

    async def get_writ_outcome(self, writ_id: str) -> dict[str, str] | None: ...

    async def store_intent(self, intent_id: str, intent_data: dict[str, Any]) -> None: ...

    async def get_intent(self, intent_id: str) -> dict[str, Any] | None: ...

    async def get_agent_intents(self, agent_id: str, limit: int = 30) -> list[dict[str, Any]]: ...


@runtime_checkable
class EphemeralStore(Protocol):
    """Redis-only operational state: nonces and rate counters.

    These stay in Redis in every storage mode; their atomicity and
    fail-closed behavior are part of the security contract.
    """

    async def claim_nonce(self, nonce: str, writ_id: str, ttl_seconds: int) -> bool: ...

    async def is_nonce_used(self, nonce: str) -> bool: ...

    async def release_nonce(self, nonce: str) -> bool: ...

    async def increment_rate_limit(
        self, category: str, identifier: str, window_seconds: int = 60
    ) -> int: ...

    async def get_rate_limit_count(self, category: str, identifier: str) -> int: ...

    async def health_check(self) -> bool: ...
