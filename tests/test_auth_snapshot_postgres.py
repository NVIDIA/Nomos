"""Real statement-snapshot checks against an explicitly supplied disposable DB."""

import asyncio
import hashlib
import os

import pytest
import pytest_asyncio

from nomos.auth.authenticator import AgentAuthenticator
from nomos.auth.provisioning import rotate_token
from nomos.store.postgres_store import PostgresStore
from tests.postgres_test_support import ensure_migrated_and_clean
from tests.test_auth_snapshot import Ephemeral

TOKEN = "postgres-snapshot-token"
HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest_asyncio.fixture
async def pg():
    dsn = os.environ.get("NOMOS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("explicit disposable NOMOS_TEST_DATABASE_URL required")
    ensure_migrated_and_clean(dsn)
    store = PostgresStore(dsn, account_integrity_key="snapshot-integrity-test-key")
    await store.connect()
    try:
        await store.set_account("agent", HASH, display_name="Agent")
        await store.set_account_permissions("agent", {"p1", "p2"})
        await store.set_account_councils("agent", {"c1", "c2", "c3"})
        await store.set_token_index(HASH, "agent")
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_real_auth_rotation_disable_and_revocation(pg):
    ep = Ephemeral()
    auth = AgentAuthenticator(pg, ep)
    identity = await auth.authenticate(TOKEN)
    assert identity.permissions == {"p1", "p2"} and identity.councils == {"c1", "c2", "c3"}
    await pg.revoke_account_council("agent", "c2")
    assert (await auth.authenticate(TOKEN)).councils == {"c1", "c3"}
    rotated = await rotate_token(pg, agent_id="agent")
    assert await auth.authenticate(TOKEN) is None and ep.increments == 1
    assert await auth.authenticate(rotated["token"]) is not None
    await pg.update_account_fields("agent", {"enabled": "0"})
    assert await auth.authenticate(rotated["token"]) is None and ep.increments == 1
    await pg.delete_account("agent")
    assert await auth.authenticate(rotated["token"]) is None and ep.increments == 2


@pytest.mark.asyncio
async def test_real_stale_index_hash_mismatch_and_invalid_hmac(pg):
    ep = Ephemeral()
    auth = AgentAuthenticator(pg, ep)
    await pg.set_token_index(hashlib.sha256(b"stale-token").hexdigest(), "agent")
    assert await auth.authenticate("stale-token") is None and ep.increments == 1
    async with pg.pool.connection() as conn:
        await conn.execute("UPDATE accounts SET integrity_hmac='tampered' WHERE agent_id='agent'")
    assert await auth.authenticate(TOKEN) is None and ep.increments == 1


@pytest.mark.asyncio
async def test_real_empty_memberships_and_decoder_parity(pg):
    await pg.set_account_permissions("agent", set())
    await pg.set_account_councils("agent", set())
    await pg.update_account_fields("agent", {"custom": "value", "role": "test-role"})
    snap = await pg.get_auth_snapshot(HASH)
    assert dict(snap.account) == await pg.get_account("agent")
    assert snap.permissions == snap.councils == frozenset()
    identity = await AgentAuthenticator(pg, Ephemeral()).authenticate(TOKEN)
    assert identity.permissions == identity.councils == set()


@pytest.mark.asyncio
async def test_statement_snapshot_coherent_during_atomic_membership_changes(pg):
    async def change(generation):
        tag = str(generation)
        async with pg.pool.connection() as conn:
            await conn.execute(
                "UPDATE accounts SET display_name=%s WHERE agent_id=%s", (tag, "agent")
            )
            await conn.execute("DELETE FROM account_permissions WHERE agent_id=%s", ("agent",))
            await conn.execute(
                "INSERT INTO account_permissions VALUES (%s,%s)", ("agent", "p:" + tag)
            )
            await conn.execute("DELETE FROM account_councils WHERE agent_id=%s", ("agent",))
            await conn.execute("INSERT INTO account_councils VALUES (%s,%s)", ("agent", "c:" + tag))
            await pg._refresh_integrity_in(conn, "agent")

    await change(0)
    seen = set()

    async def writer():
        for i in range(1, 26):
            await change(i)

    async def reader():
        for _ in range(90):
            snapshot = await pg.get_auth_snapshot(HASH)
            tag = snapshot.account["display_name"]
            seen.add(tag)
            assert snapshot.permissions == {"p:" + tag}
            assert snapshot.councils == {"c:" + tag}
            assert pg.verify_account_integrity(
                snapshot.agent_id,
                dict(snapshot.account),
                set(snapshot.permissions),
                set(snapshot.councils),
            )

    await asyncio.gather(writer(), reader(), reader())
    assert len(seen) > 1
