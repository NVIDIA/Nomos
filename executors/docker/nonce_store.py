# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Redis-backed nonce store for replay prevention.

Atomic SET NX EX ensures only one executor can claim a nonce.
"""

import logging
import os

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from nomos_runtime import ClientTLSConfig, strict_transport_profile
from nomos_runtime.redis_client import RedisConnectionConfig, create_redis_client

logger = logging.getLogger(__name__)


class NonceStore:
    """Atomic nonce tracking via Redis."""

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "nomos:",
        *,
        redis_mode: str = "standalone",
        sentinel_service_name: str = "",
        sentinel_nodes: list[str] | tuple[str, ...] = (),
        sentinel_password: str = "",
        connect_timeout_ms: int = 1000,
        command_timeout_ms: int = 1000,
    ):
        self._redis_url = redis_url
        self._prefix = key_prefix
        self._redis_mode = redis_mode
        self._sentinel_service_name = sentinel_service_name
        self._sentinel_nodes = tuple(sentinel_nodes)
        self._sentinel_password = sentinel_password
        self._connect_timeout_ms = connect_timeout_ms
        self._command_timeout_ms = command_timeout_ms
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        tls = ClientTLSConfig.from_env("EXECUTOR_REDIS")
        if strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")) and (
            not tls.mutual or not tls.verify_hostname
        ):
            raise ValueError(
                "executor Redis mTLS with hostname verification is required "
                "in this deployment profile"
            )
        self._client = create_redis_client(
            RedisConnectionConfig(
                mode=self._redis_mode,
                url=self._redis_url,
                sentinel_service_name=self._sentinel_service_name,
                sentinel_nodes=self._sentinel_nodes,
                sentinel_password=self._sentinel_password,
                connect_timeout_ms=self._connect_timeout_ms,
                command_timeout_ms=self._command_timeout_ms,
                tls=tls.enabled,
                tls_ca_file=tls.ca_file,
                tls_cert_file=tls.cert_file,
                tls_key_file=tls.key_file,
                tls_verify_hostname=tls.verify_hostname,
            )
        )
        await self._client.ping()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("NonceStore not connected")
        return self._client

    async def claim_nonce(self, nonce: str, writ_id: str, ttl_seconds: int) -> bool:
        """Atomically claim a nonce. Returns True if first claim."""
        key = f"{self._prefix}nonces:{nonce}"
        try:
            result = await self.client.set(key, writ_id, nx=True, ex=ttl_seconds)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.warning("Redis nonce claim connection failed; reconnecting once: %s", exc)
            await self.close()
            await self.connect()
            result = await self.client.set(key, writ_id, nx=True, ex=ttl_seconds)
        return result is not None

    async def release_nonce(self, nonce: str) -> bool:
        """Release a claimed nonce (on post-claim verification failure)."""
        key = f"{self._prefix}nonces:{nonce}"
        result = await self.client.delete(key)
        return result > 0

    async def report_outcome(
        self,
        writ_id: str,
        exit_code: int,
        stdout_hash: str,
        stderr_hash: str,
        executor_id: str = "",
    ) -> None:
        """Best-effort outcome reporting to Redis."""
        import time

        key = f"{self._prefix}writs:{writ_id}:outcome"
        try:
            await self.client.hset(
                key,
                mapping={
                    "exit_code": str(exit_code),
                    "stdout_hash": stdout_hash,
                    "stderr_hash": stderr_hash,
                    "executed_at": str(time.time()),
                    "executor_id": executor_id,
                },
            )
        except Exception as e:
            logger.warning("Failed to report outcome for %s: %s", writ_id, e)

    async def register_trusted_executor(
        self,
        *,
        executor_id: str,
        public_key_hex: str,
        key_fingerprint: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Register this executor's evidence public key for council verification."""
        import json
        import time

        key = f"{self._prefix}trusted_executors:{key_fingerprint}"
        index_key = f"{self._prefix}trusted_executors"
        existing = await self.client.hgetall(key)
        now = str(time.time())
        await self.client.hset(
            key,
            mapping={
                "executor_id": executor_id,
                "public_key_hex": public_key_hex,
                "key_fingerprint": key_fingerprint,
                "enabled": "1",
                "registered_at": existing.get("registered_at", now),
                "updated_at": now,
                "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            },
        )
        await self.client.sadd(index_key, key_fingerprint)
