# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Parity tests for nomos.auth.provisioning across both durable backends.

The decisive test here is `TestEndToEndAuthentication` — it provisions an account
and then authenticates the returned token through the real `AgentAuthenticator`.
That is what catches a store constructed without an account integrity key, which
writes accounts that store fine and then fail login.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from nomos.auth.authenticator import AgentAuthenticator
from nomos.auth.provisioning import (
    ProvisioningError,
    account_councils,
    council_members,
    create_account,
    delete_account,
    grant_council,
    list_accounts,
    load_record,
    require_signed_store,
    revoke_council,
    rotate_token,
    set_enabled,
    set_permissions,
    token_hash,
)
from nomos.store.postgres_store import PostgresStore
from nomos.store.redis_store import RedisStore
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

INTEGRITY_KEY = "provisioning-parity-integrity-key"


@pytest_asyncio.fixture
async def store(request):
    """Yield a clean, integrity-keyed store for the requested backend."""
    backend = request.param
    if backend == "postgres":
        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)
        pg = PostgresStore(dsn, account_integrity_key=INTEGRITY_KEY)
        await pg.connect()
        try:
            yield pg
        finally:
            await pg.close()
    else:
        config = redis_config_for_tests().model_copy(
            update={"account_integrity_key": INTEGRITY_KEY}
        )
        rs = RedisStore(config)
        await rs.connect()
        await rs.client.flushdb()
        try:
            yield rs
        finally:
            await rs.close()


@pytest_asyncio.fixture
async def ephemeral():
    """Redis for rate-limit counters — AgentAuthenticator needs one even when the
    durable store is PostgreSQL (`authenticator.py` splits durable vs ephemeral)."""
    if _REDIS_SKIP is not None:
        pytest.skip(_REDIS_SKIP)
    config = redis_config_for_tests().model_copy(
        update={"account_integrity_key": INTEGRITY_KEY, "key_prefix": "nomos_test_eph:"}
    )
    rs = RedisStore(config)
    await rs.connect()
    await rs.client.flushdb()
    try:
        yield rs
    finally:
        await rs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", BACKENDS, indirect=True)
class TestProvisioningParity:
    async def test_create_returns_token_and_record(self, store):
        result = await create_account(
            store, agent_id="agent-a", role="developer", councils=["council-prod"]
        )

        assert result["token"], "create must return a raw token"
        assert result["agent_id"] == "agent-a"
        assert result["enabled"] is True
        assert result["role"] == "developer"
        assert set(result["permissions"]) == {
            "shell:read",
            "shell:write",
            "file:read",
            "file:write",
        }
        assert result["councils"] == ["council-prod"]

    async def test_create_rejects_duplicate_without_force(self, store):
        await create_account(store, agent_id="agent-a", role="readonly")
        with pytest.raises(ProvisioningError, match="already exists"):
            await create_account(store, agent_id="agent-a", role="readonly")

    async def test_force_replaces_and_preserves_created_at(self, store):
        first = await create_account(store, agent_id="agent-a", role="readonly")
        second = await create_account(store, agent_id="agent-a", role="operator", force=True)

        assert second["created_at"] == first["created_at"], "created_at must survive replace"
        assert second["token"] != first["token"]
        assert "infra:deploy" in second["permissions"]

    async def test_force_revokes_the_previous_token(self, store):
        first = await create_account(store, agent_id="agent-a", role="readonly")
        await create_account(store, agent_id="agent-a", role="readonly", force=True)

        assert await store.find_agent_by_token_hash(token_hash(first["token"])) is None

    async def test_rotate_token_replaces_index(self, store):
        created = await create_account(store, agent_id="agent-a", role="developer")
        rotated = await rotate_token(store, agent_id="agent-a")

        assert rotated["token"] != created["token"]
        assert await store.find_agent_by_token_hash(token_hash(rotated["token"])) == "agent-a"
        assert await store.find_agent_by_token_hash(token_hash(created["token"])) is None

    async def test_rotate_missing_account_raises(self, store):
        with pytest.raises(ProvisioningError, match="does not exist"):
            await rotate_token(store, agent_id="nope")

    async def test_enable_disable_round_trip(self, store):
        await create_account(store, agent_id="agent-a", role="developer")

        disabled = await set_enabled(store, agent_id="agent-a", enabled=False)
        assert disabled["enabled"] is False

        enabled = await set_enabled(store, agent_id="agent-a", enabled=True)
        assert enabled["enabled"] is True

    async def test_set_permissions_explicit(self, store):
        await create_account(store, agent_id="agent-a", role="readonly")
        updated = await set_permissions(
            store, agent_id="agent-a", permissions=["file:read", "shell:read"]
        )
        assert updated["permissions"] == ["file:read", "shell:read"]

    async def test_set_permissions_rejects_unknown_scope(self, store):
        await create_account(store, agent_id="agent-a", role="readonly")
        with pytest.raises(Exception, match="unknown permission"):
            await set_permissions(store, agent_id="agent-a", permissions=["not:a:scope"])

    async def test_grant_and_revoke_council(self, store):
        await create_account(store, agent_id="agent-a", role="developer", councils=["c1"])

        await grant_council(store, agent_id="agent-a", council_id="c2")
        assert (await account_councils(store, agent_id="agent-a"))["councils"] == ["c1", "c2"]
        assert "agent-a" in (await council_members(store, council_id="c2"))["agents"]

        await revoke_council(store, agent_id="agent-a", council_id="c1")
        assert (await account_councils(store, agent_id="agent-a"))["councils"] == ["c2"]
        assert "agent-a" not in (await council_members(store, council_id="c1"))["agents"]

    async def test_delete_removes_account_and_token(self, store):
        created = await create_account(store, agent_id="agent-a", role="developer")

        result = await delete_account(store, agent_id="agent-a")

        assert result == {"deleted": True, "agent_id": "agent-a"}
        assert await load_record(store, "agent-a") is None
        assert await store.find_agent_by_token_hash(token_hash(created["token"])) is None

    async def test_list_includes_accounts_with_councils(self, store):
        # Regression: the previous Redis-only implementation scanned `accounts:*`
        # and hit the `:councils` SET key with HGETALL, raising WRONGTYPE. Every
        # account created by the CLI has a council, so listing was always broken.
        await create_account(store, agent_id="agent-a", role="developer", councils=["council-prod"])
        await create_account(store, agent_id="agent-b", role="readonly", councils=["council-prod"])

        listed = await list_accounts(store)

        assert [r.agent_id for r in listed] == ["agent-a", "agent-b"]

    async def test_integrity_envelope_verifies_after_every_mutation(self, store):
        # Each of these calls _assert_integrity internally; reaching the end without
        # a ProvisioningError is the assertion.
        await create_account(store, agent_id="agent-a", role="developer", councils=["c1"])
        await rotate_token(store, agent_id="agent-a")
        await set_enabled(store, agent_id="agent-a", enabled=False)
        await set_enabled(store, agent_id="agent-a", enabled=True)
        await set_permissions(store, agent_id="agent-a", role="operator")
        await grant_council(store, agent_id="agent-a", council_id="c2")
        await revoke_council(store, agent_id="agent-a", council_id="c1")

        account = await store.get_account("agent-a")
        assert account is not None
        assert account.get("_integrity_hmac"), "integrity envelope must be present"


class _RecordingStore:
    """Wraps a store and records the order of mutating calls."""

    _MUTATORS = frozenset(
        {
            "set_account",
            "update_account_fields",
            "set_account_permissions",
            "set_account_councils",
            "grant_account_council",
            "revoke_account_council",
            "set_token_index",
            "delete_token_index",
            "delete_account",
        }
    )

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self._MUTATORS:
            return attr

        async def recorded(*args, **kwargs):
            self.calls.append(name)
            return await attr(*args, **kwargs)

        return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("store", BACKENDS, indirect=True)
class TestFailClosedOrdering:
    """`DurableStore` has no transactions, so ordering is what makes a torn
    write safe: the token index is written last and removed first, so any
    partial state is unfindable rather than half-authenticating."""

    async def test_create_publishes_token_index_last(self, store):
        rec = _RecordingStore(store)
        await create_account(rec, agent_id="agent-a", role="developer", councils=["c1"])

        assert rec.calls[-1] == "set_token_index", (
            f"token index must be the final write; got order {rec.calls}"
        )

    async def test_force_replace_unpublishes_before_mutating(self, store):
        await create_account(store, agent_id="agent-a", role="readonly")
        rec = _RecordingStore(store)
        await create_account(rec, agent_id="agent-a", role="operator", force=True)

        assert rec.calls[0] == "delete_token_index", (
            f"a replace must revoke the old token before mutating; got {rec.calls}"
        )
        assert rec.calls[-1] == "set_token_index"

    async def test_delete_unpublishes_before_removing_account(self, store):
        await create_account(store, agent_id="agent-a", role="developer")
        rec = _RecordingStore(store)
        await delete_account(rec, agent_id="agent-a")

        assert rec.calls.index("delete_token_index") < rec.calls.index("delete_account"), (
            f"token must be revoked before the record is removed; got {rec.calls}"
        )

    async def test_failed_integrity_does_not_publish_the_token(self, store):
        # A store whose verification fails must not leave a usable token behind.
        class _Failing(_RecordingStore):
            def verify_account_integrity(self, *args, **kwargs):
                return False

        rec = _Failing(store)
        with pytest.raises(ProvisioningError, match="integrity"):
            await create_account(rec, agent_id="agent-bad", role="developer")

        assert "set_token_index" not in rec.calls, (
            "an account that failed verification must never become reachable"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("store", BACKENDS, indirect=True)
class TestEndToEndAuthentication:
    """The real gate: a provisioned account must actually authenticate."""

    async def test_created_account_authenticates(self, store, ephemeral):
        created = await create_account(
            store, agent_id="agent-a", role="developer", councils=["council-prod"]
        )

        auth = AgentAuthenticator(store, ephemeral=ephemeral)
        identity = await auth.authenticate(created["token"])

        assert identity is not None, "a freshly provisioned token must authenticate"
        assert identity.agent_id == "agent-a"
        assert "shell:write" in identity.permissions
        assert identity.councils == {"council-prod"}

    async def test_rotated_token_authenticates_and_old_one_does_not(self, store, ephemeral):
        created = await create_account(store, agent_id="agent-a", role="developer")
        rotated = await rotate_token(store, agent_id="agent-a")

        auth = AgentAuthenticator(store, ephemeral=ephemeral)
        assert (await auth.authenticate(rotated["token"])) is not None
        assert (await auth.authenticate(created["token"])) is None

    async def test_disabled_account_does_not_authenticate(self, store, ephemeral):
        created = await create_account(store, agent_id="agent-a", role="developer")
        await set_enabled(store, agent_id="agent-a", enabled=False)

        auth = AgentAuthenticator(store, ephemeral=ephemeral)
        assert (await auth.authenticate(created["token"])) is None

    async def test_deleted_account_does_not_authenticate(self, store, ephemeral):
        created = await create_account(store, agent_id="agent-a", role="developer")
        await delete_account(store, agent_id="agent-a")

        auth = AgentAuthenticator(store, ephemeral=ephemeral)
        assert (await auth.authenticate(created["token"])) is None


@pytest.mark.asyncio
class TestUnsignedStoreIsRefused:
    """An unkeyed store writes unsigned accounts that a keyed council rejects.

    `verify_account_integrity` returns True unconditionally when the store has no
    key, so this cannot be caught by verifying after write — it needs an explicit
    check at provisioning time.
    """

    @pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
    async def test_postgres_store_without_key_is_refused(self):
        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)
        unkeyed = PostgresStore(dsn)  # no account_integrity_key — the historical bug
        await unkeyed.connect()
        try:
            with pytest.raises(ProvisioningError, match="integrity key"):
                require_signed_store(unkeyed, storage_mode="postgres")
            # An explicit opt-out downgrades it to a warning rather than a refusal.
            assert require_signed_store(unkeyed, storage_mode="postgres", allow_unsigned=True)
        finally:
            await unkeyed.close()

    @pytest.mark.skipif(_REDIS_SKIP is not None, reason=_REDIS_SKIP or "")
    async def test_unkeyed_redis_warns_but_is_allowed(self):
        # An empty key is still a legitimate local Redis config, so this path
        # must warn rather than refuse — hard-failing would break dev flows.
        config = redis_config_for_tests().model_copy(update={"account_integrity_key": ""})
        rs = RedisStore(config)
        await rs.connect()
        try:
            warning = require_signed_store(rs, storage_mode="redis")
            assert "unsigned" in warning
        finally:
            await rs.close()

    @pytest.mark.skipif(_REDIS_SKIP is not None, reason=_REDIS_SKIP or "")
    async def test_keyed_store_reports_no_warning(self):
        config = redis_config_for_tests().model_copy(
            update={"account_integrity_key": INTEGRITY_KEY}
        )
        rs = RedisStore(config)
        await rs.connect()
        try:
            assert require_signed_store(rs, storage_mode="redis") == ""
        finally:
            await rs.close()
