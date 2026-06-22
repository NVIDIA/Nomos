# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""DurableStore parity tests: PostgresStore must mirror RedisStore shapes."""

from __future__ import annotations

import pytest
import pytest_asyncio

from nomos.store.base import DurableStore, EphemeralStore
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)
from tests.redis_test_support import redis_config_for_tests, redis_unavailable_reason

_PG_SKIP = postgres_unavailable_reason()
_REDIS_SKIP = redis_unavailable_reason()

BACKENDS = [
    pytest.param("postgres", marks=pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")),
    pytest.param(
        "redis", marks=pytest.mark.skipif(_REDIS_SKIP is not None, reason=_REDIS_SKIP or "")
    ),
]

INTEGRITY_KEY = "parity-test-integrity-key"


@pytest_asyncio.fixture
async def store(request):
    backend = request.param
    if backend == "postgres":
        from nomos.store.postgres_store import PostgresStore

        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)
        pg = PostgresStore(dsn, account_integrity_key=INTEGRITY_KEY)
        await pg.connect()
        yield pg
        await pg.close()
    else:
        from nomos.store.redis_store import RedisStore

        config = redis_config_for_tests()
        config = config.model_copy(update={"account_integrity_key": INTEGRITY_KEY})
        rs = RedisStore(config)
        await rs.connect()
        await rs.client.flushdb()
        yield rs
        await rs.client.flushdb()
        await rs.close()


def test_redis_store_conforms_to_protocols():
    from nomos.store.redis_store import RedisStore

    config = redis_config_for_tests()
    rs = RedisStore(config)
    assert isinstance(rs, DurableStore)
    assert isinstance(rs, EphemeralStore)


def test_postgres_store_conforms_to_durable_protocol():
    from nomos.store.postgres_store import PostgresStore

    pg = PostgresStore("postgresql://ignored/ignored")
    assert isinstance(pg, DurableStore)


@pytest.mark.parametrize("store", BACKENDS, indirect=True)
class TestDurableStoreParity:
    @pytest.mark.asyncio
    async def test_account_lifecycle_shapes(self, store):
        await store.set_account("agent-1", "hash-1", display_name="Agent One", enabled=True)
        account = await store.get_account("agent-1")
        assert account is not None
        assert account["token_hash"] == "hash-1"
        assert account["display_name"] == "Agent One"
        assert account["enabled"] == "1"
        assert isinstance(account["created_at"], str)
        assert float(account["created_at"]) > 0
        assert await store.get_account("missing") is None

        await store.set_account("agent-1", "hash-1", display_name="Agent One", enabled=False)
        assert (await store.get_account("agent-1"))["enabled"] == "0"

    @pytest.mark.asyncio
    async def test_permissions_and_councils(self, store):
        await store.set_account("agent-2", "hash-2")
        await store.set_account_permissions("agent-2", {"shell:read", "file:write"})
        assert await store.get_account_permissions("agent-2") == {"shell:read", "file:write"}
        await store.set_account_permissions("agent-2", {"shell:read"})
        assert await store.get_account_permissions("agent-2") == {"shell:read"}

        await store.set_account_councils("agent-2", {"council-a", "council-b"})
        assert await store.get_account_councils("agent-2") == {"council-a", "council-b"}
        assert "agent-2" in await store.get_council_agents("council-a")

        await store.revoke_account_council("agent-2", "council-a")
        assert await store.get_account_councils("agent-2") == {"council-b"}
        assert "agent-2" not in await store.get_council_agents("council-a")

        await store.grant_account_council("agent-2", "council-c")
        assert "council-c" in await store.get_account_councils("agent-2")

    @pytest.mark.asyncio
    async def test_token_index(self, store):
        await store.set_account("agent-3", "hash-3")
        await store.set_token_index("hash-3", "agent-3")
        assert await store.find_agent_by_token_hash("hash-3") == "agent-3"
        assert await store.find_agent_by_token_hash("nope") is None

    @pytest.mark.asyncio
    async def test_integrity_envelope_round_trip(self, store):
        await store.set_account("agent-4", "hash-4")
        await store.set_account_permissions("agent-4", {"shell:read"})
        await store.set_account_councils("agent-4", {"council-a"})

        account = await store.get_account("agent-4")
        permissions = await store.get_account_permissions("agent-4")
        councils = await store.get_account_councils("agent-4")
        assert account.get("_integrity_hmac")
        assert store.verify_account_integrity("agent-4", account, permissions, councils)
        # tampering is detected
        tampered = dict(account)
        tampered["enabled"] = "0" if account["enabled"] == "1" else "1"
        assert not store.verify_account_integrity("agent-4", tampered, permissions, councils)

    @pytest.mark.asyncio
    async def test_writs_outcomes_intents(self, store):
        await store.store_writ("writ-1", {"writ_id": "writ-1", "command": "ls"})
        writ = await store.get_writ("writ-1")
        assert writ == {"writ_id": "writ-1", "command": "ls"}
        assert await store.get_writ("missing") is None

        await store.store_writ_outcome("writ-1", 0, "out-hash", "err-hash", executor_id="ex-1")
        outcome = await store.get_writ_outcome("writ-1")
        assert outcome["exit_code"] == "0"
        assert outcome["stdout_hash"] == "out-hash"
        assert outcome["executor_id"] == "ex-1"
        assert float(outcome["executed_at"]) > 0

        for n in range(3):
            await store.store_intent(
                f"intent-{n}", {"intent_id": f"intent-{n}", "agent_id": "agent-5"}
            )
        intent = await store.get_intent("intent-1")
        assert intent["intent_id"] == "intent-1"
        recent = await store.get_agent_intents("agent-5", limit=2)
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_trusted_executors(self, store):
        await store.register_trusted_executor(
            executor_id="openshell:abc",
            public_key_hex="aa" * 32,
            key_fingerprint="ff" * 32,
            metadata={"pool": "default"},
        )
        record = await store.get_trusted_executor("ff" * 32)
        assert record["executor_id"] == "openshell:abc"
        assert record["enabled"] == "1"
        registered_at = record["registered_at"]

        # re-registration preserves registered_at, updates the rest
        await store.register_trusted_executor(
            executor_id="openshell:abc-v2",
            public_key_hex="bb" * 32,
            key_fingerprint="ff" * 32,
            enabled=False,
        )
        updated = await store.get_trusted_executor("ff" * 32)
        assert updated["executor_id"] == "openshell:abc-v2"
        assert updated["enabled"] == "0"
        assert updated["registered_at"] == registered_at

        executors = await store.list_trusted_executors()
        assert any(r["key_fingerprint"] == "ff" * 32 for r in executors)

    @pytest.mark.asyncio
    async def test_health_check(self, store):
        assert await store.health_check() is True


@pytest.mark.parametrize("store", BACKENDS, indirect=True)
class TestAccountAdminParity:
    @pytest.mark.asyncio
    async def test_update_account_fields(self, store):
        await store.set_account("agent-6", "hash-6")
        await store.update_account_fields(
            "agent-6", {"role": "operator", "enabled": "0", "invited_by": "ops"}
        )
        account = await store.get_account("agent-6")
        assert account["role"] == "operator"
        assert account["enabled"] == "0"
        assert account["invited_by"] == "ops"
        # integrity still verifies after partial update
        permissions = await store.get_account_permissions("agent-6")
        councils = await store.get_account_councils("agent-6")
        assert store.verify_account_integrity("agent-6", account, permissions, councils)

    @pytest.mark.asyncio
    async def test_delete_account_cascades(self, store):
        await store.set_account("agent-7", "hash-7")
        await store.set_token_index("hash-7", "agent-7")
        await store.set_account_permissions("agent-7", {"shell:read"})
        await store.set_account_councils("agent-7", {"council-x"})

        await store.delete_account("agent-7")

        assert await store.get_account("agent-7") is None
        assert await store.find_agent_by_token_hash("hash-7") is None
        assert await store.get_account_permissions("agent-7") == set()
        assert "agent-7" not in await store.get_council_agents("council-x")

    @pytest.mark.asyncio
    async def test_delete_token_index_and_list_ids(self, store):
        await store.set_account("agent-8", "hash-8")
        await store.set_token_index("hash-8", "agent-8")
        await store.delete_token_index("hash-8")
        assert await store.find_agent_by_token_hash("hash-8") is None
        assert "agent-8" in await store.list_account_ids()
