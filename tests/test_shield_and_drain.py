# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Waiting out critical work after the caller is cancelled must not spin."""

from __future__ import annotations

import asyncio
import threading
import time

import anyio
import pytest

from nomos.nat.functions import CouncilService


@pytest.mark.asyncio
async def test_cancelled_anyio_scope_waits_without_spinning(monkeypatch):
    """A cancelled anyio scope raises at every await; the wait must still block."""
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    waits = 0
    original = asyncio.shield

    def counting_shield(awaitable):
        nonlocal waits
        waits += 1
        return original(awaitable)

    monkeypatch.setattr(asyncio, "shield", counting_shield)

    async def handler():
        with anyio.CancelScope() as scope:
            scope.cancel()  # what an MCP request scope does when its client goes away
            return await CouncilService._shield_and_drain(work)

    task = asyncio.create_task(handler())
    await asyncio.sleep(0.2)
    assert not task.done()
    # One wait before the cancellation is seen and one after; not one per loop turn.
    assert waits <= 3

    work.set_result("committed")
    assert await task == ("committed", True)


@pytest.mark.asyncio
async def test_repeated_task_cancellation_still_returns_the_result():
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(CouncilService._shield_and_drain(work))
    for _ in range(3):
        await asyncio.sleep(0.01)
        task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()

    work.set_result("committed")
    assert await task == ("committed", True)


@pytest.mark.asyncio
async def test_uncancelled_wait_reports_no_cancellation():
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_later(0.01, work.set_result, "done")
    assert await CouncilService._shield_and_drain(work) == ("done", False)


@pytest.mark.asyncio
async def test_failure_of_the_work_reaches_the_cancelled_caller():
    work: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(CouncilService._shield_and_drain(work))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    work.set_exception(RuntimeError("ledger down"))
    with pytest.raises(RuntimeError, match="ledger down"):
        await task


@pytest.mark.asyncio
async def test_a_deadline_over_a_ledger_read_ends_as_a_timeout():
    """The scope that cancelled the read must get its own cancellation back."""
    from nomos.nat.plugin import _read_in_thread

    finished = threading.Event()

    def read():
        time.sleep(0.2)
        finished.set()
        return "page"

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await _read_in_thread(read)
    # The lease is still held until the read itself has finished.
    assert finished.is_set()
