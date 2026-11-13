"""Partial store initialization owns its resources through failure/cancellation."""

import asyncio

import pytest

from nomos.config.settings import RedisConfig
from nomos.store.postgres_store import PostgresStore
from nomos.store.redis_store import RedisStore


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_open", [False, True])
async def test_postgres_failed_open_closes_actual_local_pool(monkeypatch, cancel_open):
    entered = asyncio.Event()
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    instances = []

    class Pool:
        def __init__(self, *args, **kwargs):
            self.closed = False
            instances.append(self)

        async def open(self, **kwargs):
            entered.set()
            if cancel_open:
                await asyncio.Event().wait()
            raise RuntimeError("open failed")

        async def close(self):
            close_entered.set()
            await release_close.wait()
            self.closed = True

    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", Pool)
    store = PostgresStore("unused")
    opening = asyncio.create_task(store.connect())
    await entered.wait()
    if cancel_open:
        opening.cancel()
    await close_entered.wait()
    opening.cancel()
    await asyncio.sleep(0)
    opening.cancel()
    await asyncio.sleep(0)
    assert not opening.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError if cancel_open else RuntimeError):
        await opening
    assert len(instances) == 1 and instances[0].closed
    assert store._pool is None
    await store.close()


@pytest.mark.asyncio
async def test_redis_retry_closes_every_failed_client(monkeypatch):
    instances = []

    class Client:
        closed = False

        async def ping(self):
            return False

        async def aclose(self):
            self.closed = True

    def create(config):
        result = Client()
        instances.append(result)
        return result

    monkeypatch.setattr("nomos.store.redis_store.create_redis_client", create)
    monkeypatch.setattr("nomos.store.redis_store.RETRY_BACKOFF_SECONDS", [0, 0, 0])
    store = RedisStore(RedisConfig())
    assert not await store.connect_with_retry()
    assert len(instances) == 3 and all(client.closed for client in instances)
    assert store._client is None


@pytest.mark.asyncio
async def test_redis_cancel_during_health_check_drains_client_close(monkeypatch):
    entered = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class Client:
        async def ping(self):
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    monkeypatch.setattr("nomos.store.redis_store.create_redis_client", lambda config: Client())
    store = RedisStore(RedisConfig())
    opening = asyncio.create_task(store.connect_with_retry())
    await entered.wait()
    opening.cancel()
    await closing.wait()
    opening.cancel()
    await asyncio.sleep(0)
    assert not opening.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert closed.is_set() and store._client is None
