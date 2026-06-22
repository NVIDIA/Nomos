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
PostgreSQL-backed durable store.

Implements the DurableStore protocol with the exact return shapes of
RedisStore — "1"/"0" enabled flags, str(time.time()) timestamp strings, and
the "_integrity_hmac" key inside account dicts — so callers (notably
AgentAuthenticator) behave identically against either backend.

Improvements over the Redis layout that don't change shapes:
- account integrity HMAC refresh happens in the same transaction as the
  mutation it covers
- intents are retained beyond the last 30 (get_agent_intents simply orders
  by recency), supporting indexed audit queries
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record

if TYPE_CHECKING:
    import psycopg
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [1, 2, 4]


class PostgresStore:
    """Durable store on an async psycopg connection pool."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        connect_timeout_s: float = 5.0,
        account_integrity_key: str = "",
    ):
        self._dsn = dsn
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._connect_timeout_s = connect_timeout_s
        self._account_integrity_key = account_integrity_key
        self._pool: AsyncConnectionPool | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        if self._pool is not None:
            return
        pool = AsyncConnectionPool(
            self._dsn,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
            timeout=self._connect_timeout_s,
            open=False,
        )
        await pool.open(wait=True, timeout=self._connect_timeout_s)
        self._pool = pool

    async def connect_with_retry(self) -> bool:
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                await self.connect()
                return True
            except Exception as e:
                logger.warning(
                    "Postgres connect attempt %d/%d failed: %s",
                    attempt + 1,
                    MAX_RETRY_ATTEMPTS,
                    e,
                )
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
        return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health_check(self) -> bool:
        if self._pool is None:
            return False
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as e:
            logger.warning("Postgres health check failed: %s", e)
            return False

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("PostgresStore not connected. Call connect() first.")
        return self._pool

    # ── accounts ──────────────────────────────────────────────────────

    async def set_account(
        self,
        agent_id: str,
        token_hash: str,
        display_name: str = "",
        enabled: bool = True,
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO accounts (agent_id, token_hash, display_name, enabled, created_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (agent_id) DO UPDATE SET token_hash = EXCLUDED.token_hash, "
                "display_name = EXCLUDED.display_name, enabled = EXCLUDED.enabled, "
                "updated_at = %s",
                (
                    agent_id,
                    token_hash,
                    display_name or agent_id,
                    enabled,
                    str(time.time()),
                    str(time.time()),
                ),
            )
            await self._refresh_integrity_in(conn, agent_id)

    async def get_account(self, agent_id: str) -> dict[str, str] | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT token_hash, display_name, enabled, role, created_at, updated_at, "
                    "integrity_hmac, extra FROM accounts WHERE agent_id = %s",
                    (agent_id,),
                )
            ).fetchone()
        if row is None:
            return None
        token_hash, display_name, enabled, role, created_at, updated_at, integrity, extra = row
        account: dict[str, str] = {
            "token_hash": token_hash,
            "display_name": display_name,
            "created_at": created_at,
            "enabled": "1" if enabled else "0",
        }
        if role is not None:
            account["role"] = role
        if updated_at is not None:
            account["updated_at"] = updated_at
        for key, value in (extra or {}).items():
            account[str(key)] = str(value)
        if integrity:
            account[ACCOUNT_INTEGRITY_FIELD] = integrity
        return account

    async def set_account_permissions(self, agent_id: str, permissions: set[str]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM account_permissions WHERE agent_id = %s", (agent_id,))
            for permission in sorted(permissions):
                await conn.execute(
                    "INSERT INTO account_permissions (agent_id, permission) VALUES (%s, %s)",
                    (agent_id, permission),
                )
            await self._refresh_integrity_in(conn, agent_id)

    async def get_account_permissions(self, agent_id: str) -> set[str]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT permission FROM account_permissions WHERE agent_id = %s",
                    (agent_id,),
                )
            ).fetchall()
        return {row[0] for row in rows}

    async def set_account_councils(self, agent_id: str, councils: set[str]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM account_councils WHERE agent_id = %s", (agent_id,))
            for council_id in sorted(councils):
                await conn.execute(
                    "INSERT INTO account_councils (agent_id, council_id) VALUES (%s, %s)",
                    (agent_id, council_id),
                )
            await self._refresh_integrity_in(conn, agent_id)

    async def grant_account_council(self, agent_id: str, council_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO account_councils (agent_id, council_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (agent_id, council_id),
            )
            await self._refresh_integrity_in(conn, agent_id)

    async def revoke_account_council(self, agent_id: str, council_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM account_councils WHERE agent_id = %s AND council_id = %s",
                (agent_id, council_id),
            )
            await self._refresh_integrity_in(conn, agent_id)

    async def get_account_councils(self, agent_id: str) -> set[str]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT council_id FROM account_councils WHERE agent_id = %s",
                    (agent_id,),
                )
            ).fetchall()
        return {row[0] for row in rows}

    async def get_council_agents(self, council_id: str) -> set[str]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT agent_id FROM account_councils WHERE council_id = %s",
                    (council_id,),
                )
            ).fetchall()
        return {row[0] for row in rows}

    async def find_agent_by_token_hash(self, token_hash: str) -> str | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT agent_id FROM token_index WHERE token_hash = %s", (token_hash,)
                )
            ).fetchone()
        return row[0] if row else None

    async def set_token_index(self, token_hash: str, agent_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO token_index (token_hash, agent_id) VALUES (%s, %s) "
                "ON CONFLICT (token_hash) DO UPDATE SET agent_id = EXCLUDED.agent_id",
                (token_hash, agent_id),
            )

    async def update_account_fields(self, agent_id: str, fields: dict[str, str]) -> None:
        """Partially update account fields; unknown keys land in extra jsonb."""
        from psycopg.types.json import Json

        column_names = {"token_hash", "display_name", "role", "created_at", "updated_at"}
        async with self.pool.connection() as conn:
            extras: dict[str, str] = {}
            for key, value in fields.items():
                if key == "enabled":
                    await conn.execute(
                        "UPDATE accounts SET enabled = %s WHERE agent_id = %s",
                        (value == "1", agent_id),
                    )
                elif key in column_names:
                    await conn.execute(
                        f"UPDATE accounts SET {key} = %s WHERE agent_id = %s",  # noqa: S608 # nosec B608 - column from allowlist
                        (value, agent_id),
                    )
                else:
                    extras[key] = value
            if extras:
                await conn.execute(
                    "UPDATE accounts SET extra = extra || %s::jsonb WHERE agent_id = %s",
                    (Json(extras), agent_id),
                )
            await self._refresh_integrity_in(conn, agent_id)

    async def delete_account(self, agent_id: str) -> None:
        """Delete an account; child rows and token index cascade via FKs."""
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM accounts WHERE agent_id = %s", (agent_id,))

    async def delete_token_index(self, token_hash: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM token_index WHERE token_hash = %s", (token_hash,))

    async def list_account_ids(self) -> list[str]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT agent_id FROM accounts ORDER BY agent_id")
            ).fetchall()
        return [row[0] for row in rows]

    # ── account integrity ─────────────────────────────────────────────

    async def refresh_account_integrity(self, agent_id: str) -> None:
        if not self._account_integrity_key:
            return
        async with self.pool.connection() as conn:
            await self._refresh_integrity_in(conn, agent_id)

    async def _refresh_integrity_in(self, conn: psycopg.AsyncConnection, agent_id: str) -> None:
        """Recompute the HMAC inside the caller's transaction."""
        if not self._account_integrity_key:
            return
        row = await (
            await conn.execute(
                "SELECT token_hash, display_name, enabled, role, created_at, updated_at, extra "
                "FROM accounts WHERE agent_id = %s",
                (agent_id,),
            )
        ).fetchone()
        if row is None:
            return
        token_hash, display_name, enabled, role, created_at, updated_at, extra = row
        account: dict[str, str] = {
            "token_hash": token_hash,
            "display_name": display_name,
            "created_at": created_at,
            "enabled": "1" if enabled else "0",
        }
        if role is not None:
            account["role"] = role
        if updated_at is not None:
            account["updated_at"] = updated_at
        for key, value in (extra or {}).items():
            account[str(key)] = str(value)

        perm_rows = await (
            await conn.execute(
                "SELECT permission FROM account_permissions WHERE agent_id = %s", (agent_id,)
            )
        ).fetchall()
        council_rows = await (
            await conn.execute(
                "SELECT council_id FROM account_councils WHERE agent_id = %s", (agent_id,)
            )
        ).fetchall()
        signature = sign_account_record(
            key=self._account_integrity_key,
            agent_id=agent_id,
            account=account,
            permissions={r[0] for r in perm_rows},
            councils={r[0] for r in council_rows},
        )
        await conn.execute(
            "UPDATE accounts SET integrity_hmac = %s WHERE agent_id = %s",
            (signature, agent_id),
        )

    def verify_account_integrity(
        self,
        agent_id: str,
        account: dict[str, str],
        permissions: set[str],
        councils: set[str],
    ) -> bool:
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

    # ── trusted executors ─────────────────────────────────────────────

    async def register_trusted_executor(
        self,
        *,
        executor_id: str,
        public_key_hex: str,
        key_fingerprint: str,
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        from psycopg.types.json import Json

        now = str(time.time())
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trusted_executors (key_fingerprint, executor_id, public_key_hex, "
                "enabled, registered_at, updated_at, metadata_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (key_fingerprint) DO UPDATE SET "
                "executor_id = EXCLUDED.executor_id, public_key_hex = EXCLUDED.public_key_hex, "
                "enabled = EXCLUDED.enabled, updated_at = EXCLUDED.updated_at, "
                "metadata_json = EXCLUDED.metadata_json",
                (
                    key_fingerprint,
                    executor_id,
                    public_key_hex,
                    enabled,
                    now,
                    now,
                    Json(metadata or {}),
                ),
            )

    async def get_trusted_executor(self, key_fingerprint: str) -> dict[str, str] | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT executor_id, public_key_hex, key_fingerprint, enabled, "
                    "registered_at, updated_at, metadata_json "
                    "FROM trusted_executors WHERE key_fingerprint = %s",
                    (key_fingerprint,),
                )
            ).fetchone()
        if row is None:
            return None
        return self._executor_row_to_dict(row)

    async def list_trusted_executors(self) -> list[dict[str, str]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT executor_id, public_key_hex, key_fingerprint, enabled, "
                    "registered_at, updated_at, metadata_json "
                    "FROM trusted_executors ORDER BY key_fingerprint"
                )
            ).fetchall()
        return [self._executor_row_to_dict(row) for row in rows]

    @staticmethod
    def _executor_row_to_dict(row: tuple[Any, ...]) -> dict[str, str]:
        executor_id, public_key_hex, fingerprint, enabled, registered_at, updated_at, meta = row
        return {
            "executor_id": executor_id,
            "public_key_hex": public_key_hex,
            "key_fingerprint": fingerprint,
            "enabled": "1" if enabled else "0",
            "registered_at": registered_at,
            "updated_at": updated_at,
            "metadata_json": json.dumps(meta or {}, sort_keys=True),
        }

    # ── writs / outcomes / intents ────────────────────────────────────

    async def store_writ(self, writ_id: str, writ_data: dict[str, Any]) -> None:
        from psycopg.types.json import Json

        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO writs (writ_id, writ_json) VALUES (%s, %s) "
                "ON CONFLICT (writ_id) DO UPDATE SET writ_json = EXCLUDED.writ_json",
                (writ_id, Json(writ_data)),
            )

    async def get_writ(self, writ_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT writ_json FROM writs WHERE writ_id = %s", (writ_id,))
            ).fetchone()
        return row[0] if row else None

    async def store_writ_outcome(
        self,
        writ_id: str,
        exit_code: int,
        stdout_hash: str,
        stderr_hash: str,
        executor_id: str = "",
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO writ_outcomes (writ_id, exit_code, stdout_hash, stderr_hash, "
                "executor_id, executed_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (writ_id) DO UPDATE SET exit_code = EXCLUDED.exit_code, "
                "stdout_hash = EXCLUDED.stdout_hash, stderr_hash = EXCLUDED.stderr_hash, "
                "executor_id = EXCLUDED.executor_id, executed_at = EXCLUDED.executed_at",
                (writ_id, exit_code, stdout_hash, stderr_hash, executor_id, str(time.time())),
            )

    async def get_writ_outcome(self, writ_id: str) -> dict[str, str] | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT exit_code, stdout_hash, stderr_hash, executed_at, executor_id "
                    "FROM writ_outcomes WHERE writ_id = %s",
                    (writ_id,),
                )
            ).fetchone()
        if row is None:
            return None
        exit_code, stdout_hash, stderr_hash, executed_at, executor_id = row
        return {
            "exit_code": str(exit_code),
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "executed_at": executed_at,
            "executor_id": executor_id,
        }

    async def store_intent(self, intent_id: str, intent_data: dict[str, Any]) -> None:
        from psycopg.types.json import Json

        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO intents (intent_id, agent_id, intent_json) VALUES (%s, %s, %s) "
                "ON CONFLICT (intent_id) DO UPDATE SET intent_json = EXCLUDED.intent_json, "
                "agent_id = EXCLUDED.agent_id",
                (intent_id, intent_data.get("agent_id"), Json(intent_data)),
            )

    async def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT intent_json FROM intents WHERE intent_id = %s", (intent_id,)
                )
            ).fetchone()
        return row[0] if row else None

    async def get_agent_intents(self, agent_id: str, limit: int = 30) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT intent_json FROM intents WHERE agent_id = %s "
                    "ORDER BY created_at DESC, intent_id DESC LIMIT %s",
                    (agent_id, limit),
                )
            ).fetchall()
        return [row[0] for row in rows]
