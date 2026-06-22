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
Tests for RedisStore — Redis integration layer.

These tests require a running Redis instance. They are skipped
if Redis is not available at localhost:6379.
"""

import asyncio

import pytest
import pytest_asyncio

from tests.redis_test_support import redis_config_for_tests, redis_unavailable_reason

REDIS_UNAVAILABLE_REASON = redis_unavailable_reason()

pytestmark = [
    pytest.mark.skipif(
        REDIS_UNAVAILABLE_REASON is not None,
        reason=REDIS_UNAVAILABLE_REASON or "",
    ),
    pytest.mark.asyncio,
]


@pytest.fixture
def redis_config():
    """RedisConfig for test database (db=15 to avoid prod collisions)."""
    return redis_config_for_tests()


@pytest_asyncio.fixture
async def store(redis_config):
    """Create a connected RedisStore, flush test keys before/after."""
    from nomos.store.redis_store import RedisStore

    s = RedisStore(redis_config)
    await s.connect()
    keys = await s.client.keys("nomos_test:*")
    if keys:
        await s.client.delete(*keys)
    yield s
    keys = await s.client.keys("nomos_test:*")
    if keys:
        await s.client.delete(*keys)
    await s.close()


class TestRedisStoreHealth:
    async def test_health_check(self, store):
        assert await store.health_check() is True

    async def test_health_check_after_close(self, redis_config):
        from nomos.store.redis_store import RedisStore

        s = RedisStore(redis_config)
        await s.connect()
        await s.close()
        assert await s.health_check() is False


class TestRedisStoreAccounts:
    async def test_set_and_get_account(self, store):
        await store.set_account("agent-1", token_hash="abc123", display_name="Agent One")
        account = await store.get_account("agent-1")
        assert account is not None
        assert account["token_hash"] == "abc123"
        assert account["display_name"] == "Agent One"
        assert account["enabled"] == "1"

    async def test_get_nonexistent_account(self, store):
        result = await store.get_account("does-not-exist")
        assert result is None

    async def test_set_and_get_permissions(self, store):
        perms = {"shell:read", "shell:write", "file:read"}
        await store.set_account_permissions("agent-1", perms)
        result = await store.get_account_permissions("agent-1")
        assert result == perms

    async def test_replace_permissions(self, store):
        await store.set_account_permissions("agent-1", {"shell:read", "file:read"})
        await store.set_account_permissions("agent-1", {"infra:deploy"})
        result = await store.get_account_permissions("agent-1")
        assert result == {"infra:deploy"}

    async def test_empty_permissions(self, store):
        result = await store.get_account_permissions("agent-1")
        assert result == set()

    async def test_set_and_get_account_councils(self, store):
        await store.set_account_councils("agent-1", {"council-prod", "council-dev"})
        assert await store.get_account_councils("agent-1") == {"council-prod", "council-dev"}
        assert await store.get_council_agents("council-prod") == {"agent-1"}

    async def test_replace_account_councils_removes_reverse_index(self, store):
        await store.set_account_councils("agent-1", {"council-prod", "council-dev"})
        await store.set_account_councils("agent-1", {"council-dev"})

        assert await store.get_account_councils("agent-1") == {"council-dev"}
        assert await store.get_council_agents("council-prod") == set()
        assert await store.get_council_agents("council-dev") == {"agent-1"}

    async def test_token_index_roundtrip(self, store):
        await store.set_token_index("hash123", "agent-1")
        result = await store.find_agent_by_token_hash("hash123")
        assert result == "agent-1"

    async def test_token_index_not_found(self, store):
        result = await store.find_agent_by_token_hash("nonexistent")
        assert result is None

    async def test_disabled_account(self, store):
        await store.set_account("agent-disabled", token_hash="xyz", enabled=False)
        account = await store.get_account("agent-disabled")
        assert account["enabled"] == "0"


class TestRedisStoreNonces:
    async def test_claim_nonce_succeeds(self, store):
        result = await store.claim_nonce("nonce-1", "writ-1", ttl_seconds=60)
        assert result is True

    async def test_claim_nonce_fails_on_replay(self, store):
        await store.claim_nonce("nonce-dup", "writ-1", ttl_seconds=60)
        result = await store.claim_nonce("nonce-dup", "writ-2", ttl_seconds=60)
        assert result is False

    async def test_concurrent_nonce_claims(self, store):
        """Only one of two concurrent claims should succeed."""
        results = await asyncio.gather(
            store.claim_nonce("race-nonce", "writ-a", ttl_seconds=60),
            store.claim_nonce("race-nonce", "writ-b", ttl_seconds=60),
        )
        assert results.count(True) == 1
        assert results.count(False) == 1

    async def test_nonce_is_used(self, store):
        await store.claim_nonce("check-nonce", "writ-1", ttl_seconds=60)
        assert await store.is_nonce_used("check-nonce") is True
        assert await store.is_nonce_used("unused-nonce") is False

    async def test_nonce_ttl_expiration(self, store):
        await store.claim_nonce("expiring-nonce", "writ-1", ttl_seconds=1)
        assert await store.is_nonce_used("expiring-nonce") is True
        await asyncio.sleep(1.5)
        assert await store.is_nonce_used("expiring-nonce") is False

    async def test_release_nonce(self, store):
        await store.claim_nonce("release-me", "writ-1", ttl_seconds=60)
        assert await store.is_nonce_used("release-me") is True
        released = await store.release_nonce("release-me")
        assert released is True
        assert await store.is_nonce_used("release-me") is False
        result = await store.claim_nonce("release-me", "writ-2", ttl_seconds=60)
        assert result is True


class TestRedisStoreWrits:
    async def test_store_and_get_writ(self, store):
        writ_data = {"writ_id": "w-1", "command": "ls", "nonce": "abc"}
        await store.store_writ("w-1", writ_data)
        result = await store.get_writ("w-1")
        assert result == writ_data

    async def test_get_nonexistent_writ(self, store):
        assert await store.get_writ("nope") is None

    async def test_store_and_get_outcome(self, store):
        await store.store_writ_outcome(
            "w-1", exit_code=0, stdout_hash="aaa", stderr_hash="bbb", executor_id="exec-1"
        )
        outcome = await store.get_writ_outcome("w-1")
        assert outcome is not None
        assert outcome["exit_code"] == "0"
        assert outcome["stdout_hash"] == "aaa"
        assert outcome["executor_id"] == "exec-1"

    async def test_get_nonexistent_outcome(self, store):
        assert await store.get_writ_outcome("nope") is None


class TestRedisStoreIntents:
    async def test_store_and_get_intent(self, store):
        intent_data = {
            "intent_id": "i-1",
            "agent_id": "agent-1",
            "command": "echo hello",
            "decision": "approved",
        }
        await store.store_intent("i-1", intent_data)
        result = await store.get_intent("i-1")
        assert result == intent_data

    async def test_get_nonexistent_intent(self, store):
        assert await store.get_intent("nope") is None

    async def test_agent_intents_lifo_queue(self, store):
        # Store 35 intents for the same agent
        for i in range(35):
            intent_data = {
                "intent_id": f"i-{i}",
                "agent_id": "agent-1",
                "command": f"echo {i}",
                "decision": "approved",
            }
            await store.store_intent(f"i-{i}", intent_data)

        # Get intents for the agent
        intents = await store.get_agent_intents("agent-1")

        # Should be capped at 30 items
        assert len(intents) == 30

        # Should be LIFO (newest first, so i-34 down to i-5)
        assert intents[0]["intent_id"] == "i-34"
        assert intents[29]["intent_id"] == "i-5"

        # Test custom limit
        limited_intents = await store.get_agent_intents("agent-1", limit=5)
        assert len(limited_intents) == 5
        assert limited_intents[0]["intent_id"] == "i-34"
        assert limited_intents[4]["intent_id"] == "i-30"

    async def test_get_agent_intents_empty(self, store):
        assert await store.get_agent_intents("nonexistent-agent") == []


class TestRedisStoreLedger:
    async def test_append_and_get_entries(self, store):
        t1, t2, t3 = 1000.0, 2000.0, 3000.0
        await store.append_ledger_entry("council-1", "hash-a", t1)
        await store.append_ledger_entry("council-1", "hash-b", t2)
        await store.append_ledger_entry("council-1", "hash-c", t3)

        entries = await store.get_ledger_entries("council-1")
        assert entries == ["hash-a", "hash-b", "hash-c"]

    async def test_get_entries_with_time_range(self, store):
        await store.append_ledger_entry("council-1", "hash-a", 1000.0)
        await store.append_ledger_entry("council-1", "hash-b", 2000.0)
        await store.append_ledger_entry("council-1", "hash-c", 3000.0)

        entries = await store.get_ledger_entries("council-1", start=1500.0, end=2500.0)
        assert entries == ["hash-b"]


class TestRedisStoreTrustedExecutors:
    async def test_register_get_and_list_trusted_executor(self, store):
        await store.register_trusted_executor(
            executor_id="openshell:abc123",
            public_key_hex="ab" * 32,
            key_fingerprint="fp-abc123",
            metadata={"executor_type": "openshell"},
        )

        record = await store.get_trusted_executor("fp-abc123")
        assert record is not None
        assert record["executor_id"] == "openshell:abc123"
        assert record["public_key_hex"] == "ab" * 32
        assert record["enabled"] == "1"
        assert '"executor_type": "openshell"' in record["metadata_json"]

        records = await store.list_trusted_executors()
        assert [item["key_fingerprint"] for item in records] == ["fp-abc123"]

    async def test_register_trusted_executor_preserves_registered_at(self, store):
        await store.register_trusted_executor(
            executor_id="docker:abc123",
            public_key_hex="ab" * 32,
            key_fingerprint="fp-refresh",
        )
        first = await store.get_trusted_executor("fp-refresh")
        assert first is not None

        await store.register_trusted_executor(
            executor_id="docker:abc123",
            public_key_hex="ab" * 32,
            key_fingerprint="fp-refresh",
            enabled=False,
        )
        second = await store.get_trusted_executor("fp-refresh")
        assert second is not None
        assert second["registered_at"] == first["registered_at"]
        assert float(second["updated_at"]) >= float(first["updated_at"])
        assert second["enabled"] == "0"


class TestRedisStoreRateLimiting:
    async def test_increment_rate_limit(self, store):
        count = await store.increment_rate_limit("auth_fail", "192.168.1.1", window_seconds=60)
        assert count == 1
        count = await store.increment_rate_limit("auth_fail", "192.168.1.1", window_seconds=60)
        assert count == 2

    async def test_get_rate_limit_count(self, store):
        await store.increment_rate_limit("auth_fail", "test-ip", window_seconds=60)
        await store.increment_rate_limit("auth_fail", "test-ip", window_seconds=60)
        count = await store.get_rate_limit_count("auth_fail", "test-ip")
        assert count == 2

    async def test_rate_limit_zero_when_not_set(self, store):
        count = await store.get_rate_limit_count("auth_fail", "unknown-ip")
        assert count == 0
