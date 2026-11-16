# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Waits that outlive caller cancellation must block, not poll.

MCP request handlers and server lifespans run inside anyio cancel scopes. A
cancelled scope raises ``CancelledError`` again at every await, so a loop that
re-awaits unfinished work without shielding the wait runs once per event-loop
turn and holds a core until the work completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest

from nomos.auth.signing_executor import drain_signing
from nomos.nat.workflow_runtime import drain
from nomos.opa.client import OPAHttpClient
from nomos.store.postgres_store import PostgresStore
from nomos.store.redis_store import RedisStore


async def _run_cancelled(wait: Callable[[], Awaitable[Any]]) -> tuple[asyncio.Task[Any], list[int]]:
    """Start ``wait`` inside an already-cancelled anyio scope and count its wake-ups."""
    turns = [0]
    original = asyncio.shield

    def counting_shield(awaitable):
        turns[0] += 1
        return original(awaitable)

    async def handler():
        with anyio.CancelScope() as scope:
            scope.cancel()
            return await wait()

    asyncio.shield = counting_shield  # type: ignore[assignment]
    try:
        task = asyncio.create_task(handler())
        await asyncio.sleep(0.2)
    finally:
        asyncio.shield = original  # type: ignore[assignment]
    return task, turns


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for", [drain, drain_signing])
async def test_request_path_drains_do_not_spin_in_a_cancelled_scope(wait_for):
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    task, turns = await _run_cancelled(lambda: wait_for(work))

    assert not task.done()
    # One wait before the cancellation is seen and one after, not one per loop turn.
    assert turns[0] <= 3
    work.set_result("done")
    # The drain re-raises the scope's own cancellation, which the scope absorbs:
    # the handler ends cancelled rather than returning the result.
    assert await task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for", [drain, drain_signing])
async def test_request_path_drains_still_survive_repeated_task_cancellation(wait_for):
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(wait_for(work))
    for _ in range(3):
        await asyncio.sleep(0.01)
        task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()

    work.set_result("done")
    with pytest.raises(asyncio.CancelledError):
        await task


class _SlowClose:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def aclose(self) -> None:
        await self.release.wait()
        self.closed = True

    close = aclose


@pytest.mark.asyncio
async def test_redis_close_does_not_spin_in_a_cancelled_scope():
    store = RedisStore.__new__(RedisStore)
    client = _SlowClose()
    store._client = client  # type: ignore[assignment]

    task, turns = await _run_cancelled(store.close)

    assert not task.done()
    assert turns[0] <= 3
    client.release.set()
    # The close re-raises the scope's own cancellation, which the scope absorbs.
    assert await asyncio.wait_for(task, 2) is None
    assert client.closed and store._client is None


@pytest.mark.asyncio
async def test_opa_close_does_not_spin_in_a_cancelled_scope():
    opa = OPAHttpClient.__new__(OPAHttpClient)
    client = _SlowClose()
    opa._client = client  # type: ignore[assignment]
    opa._close_task = None
    opa._idle = asyncio.Event()
    opa._idle.set()

    task, turns = await _run_cancelled(opa.aclose)

    assert not task.done()
    assert turns[0] <= 3
    client.release.set()
    assert await asyncio.wait_for(task, 2) is None
    assert client.closed


@pytest.mark.asyncio
async def test_registry_close_does_not_spin_in_a_cancelled_scope(tmp_path):
    from nomos.config.settings import load_council_catalog
    from nomos.nat.registry import CouncilRegistry
    from tests.test_council_registry import _write_catalog

    path = _write_catalog(tmp_path)
    registry = CouncilRegistry(load_council_catalog(str(path)), path)
    registry._initializing.add("held")  # the drain waits while anything is initialising

    task, turns = await _run_cancelled(registry.aclose)

    assert not task.done()
    assert turns[0] <= 3
    registry._initializing.discard("held")
    registry._changed.set()
    assert await asyncio.wait_for(task, 2) is None


class _FailingPool:
    """Pool whose open fails and whose close can be held open by the test."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.release = asyncio.Event()
        self.closed = False
        _FailingPool.last = self

    async def open(self, *args: Any, **kwargs: Any) -> None:
        raise OSError("database unreachable")

    async def close(self) -> None:
        await self.release.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_cancellation_during_pool_cleanup_stops_the_connect_retries(monkeypatch):
    """An open failure followed by cancellation mid-cleanup must end as cancelled.

    Re-raising the open failure instead would let ``connect_with_retry`` carry
    on connecting after its caller was told to stop.
    """
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FailingPool)
    store = PostgresStore("postgresql://unused")

    task = asyncio.create_task(store.connect_with_retry())
    await asyncio.sleep(0.05)
    pool = _FailingPool.last
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # cleanup still owns the pool's workers

    pool.release.set()
    # Bounded: without the fix the retry opens a second pool and never returns.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert pool.closed and store._pool is None


@pytest.mark.asyncio
async def test_open_failure_without_cancellation_is_still_reported(monkeypatch):
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FailingPool)
    store = PostgresStore("postgresql://unused")

    task = asyncio.create_task(store.connect())
    await asyncio.sleep(0.02)
    _FailingPool.last.release.set()
    with pytest.raises(OSError, match="database unreachable"):
        await task
    assert _FailingPool.last.closed and store._pool is None


@pytest.mark.asyncio
async def test_pool_cleanup_does_not_spin_in_a_cancelled_scope(monkeypatch):
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FailingPool)
    store = PostgresStore("postgresql://unused")

    # The open failure is raised first; the scope is cancelled during cleanup.
    async def connect_then_cancel():
        with anyio.CancelScope() as scope:
            asyncio.get_running_loop().call_later(0.02, scope.cancel)
            await store.connect()

    turns = [0]
    original = asyncio.shield

    def counting_shield(awaitable):
        turns[0] += 1
        return original(awaitable)

    monkeypatch.setattr(asyncio, "shield", counting_shield)
    task = asyncio.create_task(connect_then_cancel())
    await asyncio.sleep(0.2)
    assert not task.done()
    assert turns[0] <= 3

    _FailingPool.last.release.set()
    await task  # the anyio scope absorbs its own cancellation
    assert _FailingPool.last.closed
