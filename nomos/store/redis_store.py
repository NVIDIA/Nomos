# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Redis integration layer for Nomos.

Provides async access to Redis with namespaced keys, health checks,
and typed operations for each key schema.

Key schemas (all prefixed with configurable namespace, default 'nomos:'):

    nomos:accounts:{agent_id}            → Hash: token_hash, display_name, created_at, enabled
    nomos:accounts:{agent_id}:perms      → Set of permission strings
    nomos:accounts:{agent_id}:councils   → Set of council_ids the agent may petition
    nomos:councils:{council_id}:agents   → Set of agent_ids allowed to petition this council
    nomos:nonces:{nonce_hex}             → String: writ_id (SET NX + EX for atomic claim + TTL)
    nomos:writs:{writ_id}                → Hash: full writ metadata (JSON)
    nomos:writs:{writ_id}:outcome        → Hash: exit_code, stdout_hash,
                                        stderr_hash, executed_at, executor_id
    nomos:ledger:{council_id}:entries    → Sorted set by timestamp: ledger entry hashes
    nomos:intents:{intent_id}            → Hash: intent request + decision + timestamps
"""

import asyncio
import hmac
import json
import logging
import os
import time
from typing import Any, cast

import redis.asyncio as aioredis

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record
from nomos.config.settings import RedisConfig
from nomos_runtime import ClientTLSConfig, require_strict_client, strict_transport_profile
from nomos_runtime.redis_client import RedisConnectionConfig, create_redis_client

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [1, 2, 4]  # exponential backoff


class RedisStore:
    """
    Async Redis client with namespaced keys for Nomos.

    All keys are prefixed with a configurable namespace (default: 'nomos:')
    to avoid collisions with other applications sharing the same Redis.
    """

    def __init__(self, config: RedisConfig):
        self._config = config
        require_strict_client(
            ClientTLSConfig(
                enabled=config.tls,
                ca_file=config.tls_ca_file,
                cert_file=config.tls_cert_file,
                key_file=config.tls_key_file,
                verify_hostname=config.tls_verify_hostname,
            ),
            label="Redis",
            strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
        )
        self._prefix = config.key_prefix
        self._account_integrity_key = config.account_integrity_key
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Establish connection to Redis."""
        self._client = create_redis_client(
            RedisConnectionConfig(
                mode=self._config.mode,
                host=self._config.host,
                port=self._config.port,
                db=self._config.db,
                password=self._config.password,
                tls=self._config.tls,
                tls_ca_file=self._config.tls_ca_file,
                tls_cert_file=self._config.tls_cert_file,
                tls_key_file=self._config.tls_key_file,
                tls_verify_hostname=self._config.tls_verify_hostname,
                tls_minimum_version=self._config.tls_minimum_version,
                sentinel_service_name=self._config.sentinel_service_name,
                sentinel_nodes=tuple(self._config.sentinel_nodes),
                sentinel_password=self._config.sentinel_password,
                connect_timeout_ms=self._config.connect_timeout_ms,
                command_timeout_ms=self._config.command_timeout_ms,
            )
        )
        if self._config.mode == "sentinel":
            logger.info(
                "Connected to Redis via Sentinel master %s (db=%d)",
                self._config.sentinel_service_name,
                self._config.db,
            )
            return
        logger.info(
            "Connected to Redis at %s:%d (db=%d)",
            self._config.host,
            self._config.port,
            self._config.db,
        )

    async def connect_with_retry(self) -> bool:
        """
        Connect with exponential backoff.

        Returns
        -------
        bool
            True if connected successfully, False if failed after retries.
        """
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                await self.connect()
                if await self.health_check():
                    return True
            except Exception as e:
                delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "Redis connection attempt %d/%d failed: %s — retrying in %ds",
                    attempt + 1,
                    MAX_RETRY_ATTEMPTS,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        logger.error("Redis connection failed after %d attempts — fail-closed", MAX_RETRY_ATTEMPTS)
        return False

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.aclose()  # type: ignore[union-attr, attr-defined]
            self._client = None

    async def health_check(self) -> bool:
        """
        Check Redis connection health.

        Returns
        -------
        bool
            True if connection is healthy, False otherwise.
        """
        try:
            return await self._client.ping()  # type: ignore[union-attr, misc, no-any-return]
        except Exception as e:
            logger.warning("Redis health check failed: %s", e)
            return False

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisStore not connected. Call connect() first.")
        return self._client

    def _key(self, *parts: str) -> str:
        """Build a namespaced key."""
        return self._prefix + ":".join(parts)

    # ── Account operations ────────────────────────────────────────────

    async def set_account(
        self,
        agent_id: str,
        token_hash: str,
        display_name: str = "",
        enabled: bool = True,
    ) -> None:
        """Create or update an agent account."""
        key = self._key("accounts", agent_id)
        await self.client.hset(  # type: ignore[misc]
            key,
            mapping={
                "token_hash": token_hash,
                "display_name": display_name or agent_id,
                "created_at": str(time.time()),
                "enabled": "1" if enabled else "0",
            },
        )
        await self.refresh_account_integrity(agent_id)

    async def get_account(self, agent_id: str) -> dict[str, str] | None:
        """
        Get account info.

        Parameters
        ----------
        agent_id : str
            The ID of the agent account to retrieve.

        Returns
        -------
        Optional[Dict[str, str]]
            The account information as a dictionary, or None if not found.
        """
        key = self._key("accounts", agent_id)
        data = await self.client.hgetall(key)  # type: ignore[misc]
        return data if data else None

    async def set_account_permissions(self, agent_id: str, permissions: set[str]) -> None:
        """Set permissions for an agent (replaces existing)."""
        key = self._key("accounts", agent_id, "perms")
        pipe = self.client.pipeline()
        pipe.delete(key)
        if permissions:
            pipe.sadd(key, *permissions)
        await pipe.execute()
        await self.refresh_account_integrity(agent_id)

    async def get_account_permissions(self, agent_id: str) -> set[str]:
        """Get all permissions for an agent."""
        key = self._key("accounts", agent_id, "perms")
        return await self.client.smembers(key)  # type: ignore[misc, no-any-return]

    async def set_account_councils(self, agent_id: str, councils: set[str]) -> None:
        """Replace the set of councils an agent may petition."""
        old_councils = await self.get_account_councils(agent_id)
        agent_key = self._key("accounts", agent_id, "councils")
        pipe = self.client.pipeline()
        pipe.delete(agent_key)
        if councils:
            pipe.sadd(agent_key, *councils)
        for council_id in old_councils - councils:
            pipe.srem(self._key("councils", council_id, "agents"), agent_id)
        for council_id in councils:
            pipe.sadd(self._key("councils", council_id, "agents"), agent_id)
        await pipe.execute()
        await self.refresh_account_integrity(agent_id)

    async def grant_account_council(self, agent_id: str, council_id: str) -> None:
        """Allow an agent to petition a council."""
        pipe = self.client.pipeline()
        pipe.sadd(self._key("accounts", agent_id, "councils"), council_id)
        pipe.sadd(self._key("councils", council_id, "agents"), agent_id)
        await pipe.execute()
        await self.refresh_account_integrity(agent_id)

    async def revoke_account_council(self, agent_id: str, council_id: str) -> None:
        """Remove an agent's membership in a council."""
        pipe = self.client.pipeline()
        pipe.srem(self._key("accounts", agent_id, "councils"), council_id)
        pipe.srem(self._key("councils", council_id, "agents"), agent_id)
        await pipe.execute()
        await self.refresh_account_integrity(agent_id)

    async def get_account_councils(self, agent_id: str) -> set[str]:
        """Get council ids an agent may petition."""
        key = self._key("accounts", agent_id, "councils")
        return await self.client.smembers(key)  # type: ignore[misc, no-any-return]

    async def get_council_agents(self, council_id: str) -> set[str]:
        """Get agent ids allowed to petition a council."""
        key = self._key("councils", council_id, "agents")
        return await self.client.smembers(key)  # type: ignore[misc, no-any-return]

    async def find_agent_by_token_hash(self, token_hash: str) -> str | None:
        """
        Find agent_id by token hash.

        Uses a reverse index for O(1) lookup instead of scanning all accounts.
        """
        key = self._key("token_index", token_hash)
        return await self.client.get(key)  # type: ignore[no-any-return]

    async def set_token_index(self, token_hash: str, agent_id: str) -> None:
        """Set the reverse index from token_hash → agent_id."""
        key = self._key("token_index", token_hash)
        await self.client.set(key, agent_id)

    async def update_account_fields(self, agent_id: str, fields: dict[str, str]) -> None:
        """Partially update account hash fields and refresh integrity."""
        if fields:
            # cast() satisfies the redis-py stub's invariant Mapping; values
            # here are all str.
            mapping = cast("dict[str | bytes, bytes | float | int | str]", dict(fields))
            await self.client.hset(  # type: ignore[misc]
                self._key("accounts", agent_id), mapping=mapping
            )
        await self.refresh_account_integrity(agent_id)

    async def delete_account(self, agent_id: str) -> None:
        """Delete an account, its permissions/councils, and its token index."""
        account = await self.get_account(agent_id)
        councils = await self.get_account_councils(agent_id)
        pipe = self.client.pipeline()
        pipe.delete(self._key("accounts", agent_id))
        pipe.delete(self._key("accounts", agent_id, "perms"))
        pipe.delete(self._key("accounts", agent_id, "councils"))
        for council_id in councils:
            pipe.srem(self._key("councils", council_id, "agents"), agent_id)
        token_hash = (account or {}).get("token_hash")
        if token_hash:
            pipe.delete(self._key("token_index", token_hash))
        await pipe.execute()

    async def delete_token_index(self, token_hash: str) -> None:
        """Remove a token_hash → agent_id reverse-index entry."""
        await self.client.delete(self._key("token_index", token_hash))

    async def list_account_ids(self) -> list[str]:
        """List agent ids that have account records."""
        prefix = self._key("accounts", "")
        ids = []
        async for key in self.client.scan_iter(match=f"{prefix}*"):
            suffix = key.removeprefix(prefix)
            if ":" not in suffix:
                ids.append(suffix)
        return sorted(ids)

    # ── Trusted executor registry ────────────────────────────────────

    async def register_trusted_executor(
        self,
        *,
        executor_id: str,
        public_key_hex: str,
        key_fingerprint: str,
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        """Register or refresh a trusted executor evidence key."""
        now = str(time.time())
        key = self._key("trusted_executors", key_fingerprint)
        existing = await self.client.hgetall(key)  # type: ignore[misc]
        await self.client.hset(  # type: ignore[misc]
            key,
            mapping={
                "executor_id": executor_id,
                "public_key_hex": public_key_hex,
                "key_fingerprint": key_fingerprint,
                "enabled": "1" if enabled else "0",
                "registered_at": existing.get("registered_at", now),
                "updated_at": now,
                "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            },
        )
        await self.client.sadd(self._key("trusted_executors"), key_fingerprint)  # type: ignore[misc]

    async def get_trusted_executor(self, key_fingerprint: str) -> dict[str, str] | None:
        """Return a trusted executor record by public-key fingerprint."""
        data = await self.client.hgetall(self._key("trusted_executors", key_fingerprint))  # type: ignore[misc]
        return data if data else None

    async def list_trusted_executors(self) -> list[dict[str, str]]:
        """List registered trusted executor records."""
        fingerprints = await self.client.smembers(self._key("trusted_executors"))  # type: ignore[misc]
        records = []
        for fingerprint in sorted(fingerprints):
            record = await self.get_trusted_executor(fingerprint)
            if record:
                records.append(record)
        return records

    async def refresh_account_integrity(self, agent_id: str) -> None:
        """Refresh the account HMAC when account-critical state changes."""
        if not self._account_integrity_key:
            return
        account = await self.get_account(agent_id)
        if not account:
            return
        permissions = await self.get_account_permissions(agent_id)
        councils = await self.get_account_councils(agent_id)
        signature = sign_account_record(
            key=self._account_integrity_key,
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        )
        await self.client.hset(  # type: ignore[misc]
            self._key("accounts", agent_id),
            mapping={ACCOUNT_INTEGRITY_FIELD: signature},
        )

    def verify_account_integrity(
        self,
        agent_id: str,
        account: dict[str, str],
        permissions: set[str],
        councils: set[str],
    ) -> bool:
        """Verify account-critical state when integrity is configured."""
        if not self._account_integrity_key:
            return True
        expected = sign_account_record(
            key=self._account_integrity_key,
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        )
        return hmac.compare_digest(account.get(ACCOUNT_INTEGRITY_FIELD, ""), expected)

    # ── Nonce operations ──────────────────────────────────────────────

    async def claim_nonce(self, nonce: str, writ_id: str, ttl_seconds: int) -> bool:
        """
        Atomically claim a nonce.

        Uses SET NX EX for atomic claim + TTL.

        Parameters
        ----------
        nonce : str
            The nonce string to claim.
        writ_id : str
            The ID of the writ claiming the nonce.
        ttl_seconds : int
            Time-to-live for the nonce in seconds.

        Returns
        -------
        bool
            True if claimed (first use), False if already claimed (replay).
        """
        key = self._key("nonces", nonce)
        result = await self.client.set(key, writ_id, nx=True, ex=ttl_seconds)
        return result is not None

    async def is_nonce_used(self, nonce: str) -> bool:
        """Check if a nonce has been used."""
        key = self._key("nonces", nonce)
        return await self.client.exists(key) > 0  # type: ignore[no-any-return]

    async def release_nonce(self, nonce: str) -> bool:
        """Release a nonce so it can be used again (mainly for tests)."""
        key = self._key("nonces", nonce)
        result = await self.client.delete(key)
        return result > 0  # type: ignore[no-any-return]

    # ── Writ operations ───────────────────────────────────────────────

    async def store_writ(self, writ_id: str, writ_data: dict[str, Any]) -> None:
        """Store writ metadata as a JSON hash."""
        key = self._key("writs", writ_id)
        await self.client.set(key, json.dumps(writ_data))

    async def get_writ(self, writ_id: str) -> dict[str, Any] | None:
        """Retrieve writ metadata."""
        key = self._key("writs", writ_id)
        data = await self.client.get(key)
        if data:
            return json.loads(data)  # type: ignore[no-any-return]
        return None

    async def store_writ_outcome(
        self,
        writ_id: str,
        exit_code: int,
        stdout_hash: str,
        stderr_hash: str,
        executor_id: str = "",
    ) -> None:
        """Store execution outcome for a writ."""
        key = self._key("writs", writ_id, "outcome")
        await self.client.hset(  # type: ignore[misc]
            key,
            mapping={
                "exit_code": str(exit_code),
                "stdout_hash": stdout_hash,
                "stderr_hash": stderr_hash,
                "executed_at": str(time.time()),
                "executor_id": executor_id,
            },
        )

    async def get_writ_outcome(self, writ_id: str) -> dict[str, str] | None:
        """Get execution outcome for a writ."""
        key = self._key("writs", writ_id, "outcome")
        data = await self.client.hgetall(key)  # type: ignore[misc]
        return data if data else None

    # ── Intent operations ─────────────────────────────────────────────

    async def store_intent(self, intent_id: str, intent_data: dict[str, Any]) -> None:
        """Store intent request and decision, and add to agent's LIFO queue."""
        key = self._key("intents", intent_id)

        # Store the intent data
        await self.client.set(key, json.dumps(intent_data))

        # Add to agent's LIFO queue (max 30 items)
        agent_id = intent_data.get("agent_id")
        if agent_id:
            queue_key = self._key("agent_intents", agent_id)
            pipe = self.client.pipeline()
            pipe.lpush(queue_key, intent_id)
            pipe.ltrim(queue_key, 0, 29)  # Keep only the last 30 items (0-indexed)
            await pipe.execute()

    async def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        """Retrieve intent data."""
        key = self._key("intents", intent_id)
        data = await self.client.get(key)
        if data:
            return json.loads(data)  # type: ignore[no-any-return]
        return None

    async def get_agent_intents(self, agent_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """
        Retrieve the most recent intents for an agent from their LIFO queue.

        Parameters
        ----------
        agent_id : str
            The ID of the agent to retrieve intents for.
        limit : int, optional
            Maximum number of intents to retrieve (max 30), by default 30.

        Returns
        -------
        List[Dict[str, Any]]
            A list of intent dictionaries, ordered newest first.
        """
        queue_key = self._key("agent_intents", agent_id)

        # Get the intent IDs from the queue
        # limit - 1 because lrange is inclusive on both ends
        intent_ids = await self.client.lrange(queue_key, 0, limit - 1)  # type: ignore[misc]

        if not intent_ids:
            return []

        # Fetch the actual intent data
        intents = []
        for intent_id in intent_ids:
            intent = await self.get_intent(intent_id)
            if intent:
                intents.append(intent)

        return intents

    # ── Ledger operations ─────────────────────────────────────────────

    async def append_ledger_entry(
        self,
        council_id: str,
        entry_hash: str,
        timestamp: float,
    ) -> None:
        """Append a ledger entry hash to the council's sorted set."""
        key = self._key("ledger", council_id, "entries")
        await self.client.zadd(key, {entry_hash: timestamp})

    async def get_ledger_entries(
        self,
        council_id: str,
        start: float = 0,
        end: float = float("inf"),
        limit: int = 100,
    ) -> list[str]:
        """Get ledger entry hashes in time range."""
        key = self._key("ledger", council_id, "entries")
        entries = await self.client.zrangebyscore(
            key,
            min=start,
            max=end,
            start=0,
            num=limit,
        )
        return entries  # type: ignore[no-any-return]

    # ── Rate limiting ─────────────────────────────────────────────────

    async def increment_rate_limit(
        self,
        category: str,
        identifier: str,
        window_seconds: int = 60,
    ) -> int:
        """
        Increment a rate limit counter.

        The counter auto-expires after window_seconds.

        Parameters
        ----------
        category : str
            The rate limit category (e.g., 'auth_fail').
        identifier : str
            The specific identifier to limit (e.g., a token hash).
        window_seconds : int, optional
            The time window for the rate limit in seconds, by default 60.

        Returns
        -------
        int
            The new counter value.
        """
        key = self._key("ratelimit", category, identifier)
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        results = await pipe.execute()
        return results[0]  # type: ignore[no-any-return]

    async def get_rate_limit_count(self, category: str, identifier: str) -> int:
        """Get current rate limit count."""
        key = self._key("ratelimit", category, identifier)
        count = await self.client.get(key)
        return int(count) if count else 0
