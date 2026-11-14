"""Authentication snapshot contract; no Redis or database connections."""

import asyncio
import hashlib
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from nomos.auth.authenticator import AgentAuthenticator
from nomos.store.base import AccountAuthSnapshot, DurableStore
from nomos.store.postgres_store import PostgresStore
from nomos.store.redis_store import RedisStore

TOKEN = "snapshot-test-token"
HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


class Ephemeral:
    def __init__(self):
        self.count = 0
        self.increments = 0

    async def get_rate_limit_count(self, *args):
        return self.count

    async def increment_rate_limit(self, *args):
        self.increments += 1


class Legacy:
    def __init__(self):
        self.calls = []
        self.account = {"token_hash": HASH, "enabled": "1"}
        self.valid = True

    async def find_agent_by_token_hash(self, h):
        self.calls.append("index")
        return "agent"

    async def get_account(self, a):
        self.calls.append("account")
        return self.account

    async def get_account_permissions(self, a):
        self.calls.append("permissions")
        return {"read"}

    async def get_account_councils(self, a):
        self.calls.append("councils")
        return {"council"}

    def verify_account_integrity(self, *args):
        self.calls.append("integrity")
        return self.valid


class Snapshot(Legacy):
    def __init__(self):
        super().__init__()
        self.error = None
        self.missing = False

    async def get_auth_snapshot(self, h):
        self.calls.append("snapshot")
        if self.error:
            raise self.error
        if self.missing:
            return None
        return AccountAuthSnapshot(
            "agent", self.account, frozenset({"read"}), frozenset({"council"})
        )


@pytest.mark.asyncio
async def test_fast_path_and_legacy_outcomes_match():
    for store, calls in [
        (Legacy(), ["index", "account", "permissions", "councils", "integrity"]),
        (Snapshot(), ["snapshot", "integrity"]),
    ]:
        ep = Ephemeral()
        identity = await AgentAuthenticator(store, ep).authenticate(TOKEN)
        assert (
            identity.agent_id == "agent"
            and identity.permissions == {"read"}
            and identity.councils == {"council"}
        )
        assert store.calls == calls and ep.increments == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,increments", [("missing", 1), ("hash", 1), ("disabled", 0), ("integrity", 0)]
)
async def test_fast_path_denials_preserve_failure_counters(change, increments):
    store = Snapshot()
    ep = Ephemeral()
    if change == "missing":
        store.missing = True
    elif change == "hash":
        store.account["token_hash"] = "rotated"
    elif change == "disabled":
        store.account["enabled"] = "0"
    else:
        store.valid = False
    assert await AgentAuthenticator(store, ep).authenticate(TOKEN) is None
    assert ep.increments == increments
    assert all(c not in store.calls for c in ["index", "account", "permissions", "councils"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("query failed"), asyncio.CancelledError()])
async def test_snapshot_errors_never_fallback(error):
    store = Snapshot()
    store.error = error
    with pytest.raises(type(error)):
        await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    assert store.calls == ["snapshot"]


@pytest.mark.asyncio
async def test_integrity_error_never_fallback():
    store = Snapshot()

    def broken(*args):
        raise RuntimeError("integrity failed")

    store.verify_account_integrity = broken
    with pytest.raises(RuntimeError):
        await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    assert store.calls == ["snapshot"]


@pytest.mark.asyncio
async def test_rate_limit_and_empty_token_precede_durable_read():
    store = Snapshot()
    ep = Ephemeral()
    ep.count = 5
    assert await AgentAuthenticator(store, ep).authenticate(TOKEN) is None
    assert await AgentAuthenticator(store, ep).authenticate("") is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_dynamic_mock_does_not_advertise_snapshot():
    store = MagicMock()
    store.find_agent_by_token_hash = AsyncMock(return_value="agent")
    store.get_account = AsyncMock(return_value={"token_hash": HASH, "enabled": "1"})
    store.get_account_permissions = AsyncMock(return_value={"read"})
    store.get_account_councils = AsyncMock(return_value={"council"})
    store.verify_account_integrity.return_value = True
    assert inspect.getattr_static(store, "get_auth_snapshot", None) is None
    identity = await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    assert identity.agent_id == "agent"
    assert inspect.getattr_static(store, "get_auth_snapshot", None) is None


@pytest.mark.asyncio
async def test_explicit_fake_snapshot_is_used_and_bad_result_fails_closed():
    store = Legacy()
    store.get_auth_snapshot = AsyncMock(return_value={})
    with pytest.raises(TypeError):
        await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    assert store.calls == []


@pytest.mark.asyncio
async def test_raw_hash_still_uses_compare_digest(monkeypatch):
    import nomos.auth.authenticator as auth

    original = auth.hmac.compare_digest
    calls = []

    def compare(a, b):
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", compare)
    store = Snapshot()
    await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    assert calls == [(HASH, HASH)]


def test_snapshot_owns_immutable_data():
    a = {"token_hash": HASH}
    p = {"read"}
    c = {"council"}
    s = AccountAuthSnapshot("agent", a, p, c)
    a["token_hash"] = "changed"
    p.clear()
    c.clear()
    assert (
        s.account["token_hash"] == HASH and s.permissions == {"read"} and s.councils == {"council"}
    )
    with pytest.raises(TypeError):
        s.account["token_hash"] = "changed"


@pytest.mark.asyncio
async def test_identity_mutation_does_not_mutate_snapshot():
    snapshot = AccountAuthSnapshot(
        "agent", {"token_hash": HASH, "enabled": "1"}, {"read"}, {"council"}
    )
    store = Legacy()
    store.get_auth_snapshot = AsyncMock(return_value=snapshot)
    result = await AgentAuthenticator(store, Ephemeral()).authenticate(TOKEN)
    result.permissions.clear()
    result.councils.clear()
    assert snapshot.permissions == {"read"} and snapshot.councils == {"council"}


def test_existing_protocol_and_redis_contract_unchanged():
    assert "get_auth_snapshot" not in DurableStore.__dict__
    assert inspect.getattr_static(RedisStore, "get_auth_snapshot", None) is None
    assert isinstance(PostgresStore("unused"), DurableStore)


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, sql, args):
        self.calls.append((sql, args))
        return Cursor(self.row)


class Pool:
    def __init__(self, row):
        self.conn = Connection(row)
        self.checkouts = 0

    def connection(self):
        self.checkouts += 1
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_query_single_checkout_and_decoder_parity():
    account_row = (
        HASH,
        "Agent",
        True,
        "role",
        "123",
        "456",
        "signed",
        {"display_name": "override", "enabled": "0", "_integrity_hmac": "extra"},
    )
    pg = PostgresStore("unused")
    pg._pool = Pool(("agent", *account_row, ["a", "b"], ["x", "y"]))
    s = await pg.get_auth_snapshot(HASH)
    assert pg._pool.checkouts == 1 and len(pg._pool.conn.calls) == 1
    sql, args = pg._pool.conn.calls[0]
    assert args == (HASH,) and "JOIN accounts" in sql and sql.count("ARRAY(SELECT") == 2
    assert "a.token_hash = %s" not in sql
    pg._pool = Pool(account_row)
    plain = await pg.get_account("agent")
    assert dict(s.account) == plain
    assert (
        plain["display_name"] == "override"
        and plain["enabled"] == "0"
        and plain["_integrity_hmac"] == "signed"
    )
    assert s.permissions == {"a", "b"} and s.councils == {"x", "y"}


@pytest.mark.asyncio
async def test_query_missing_and_empty_memberships():
    pg = PostgresStore("unused")
    pg._pool = Pool(None)
    assert await pg.get_auth_snapshot(HASH) is None
    pg._pool = Pool(("agent", HASH, "Agent", True, None, "123", None, "", {}, [], []))
    s = await pg.get_auth_snapshot(HASH)
    assert not s.permissions and not s.councils
