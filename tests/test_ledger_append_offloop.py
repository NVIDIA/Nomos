# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The consolidated review append must not run on the event loop."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nomos.level_0.types import Intent
from nomos.level_1.ledger import LedgerEntryType
from tests.test_one_to_one_ledger import _entries, _propose_input, _registry


def _intent() -> Intent:
    return Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="off-loop append test",
        target_resource="/tmp/test",
        justification="test",
    )


async def _service(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    service, _ = await registry.service_for_propose(_propose_input())
    return service


def _slow_append(service, monkeypatch, delay_s: float, threads: list[str]):
    original = service.council.ledger.append

    def slow(*args, **kwargs):
        threads.append(threading.current_thread().name)
        time.sleep(delay_s)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.council.ledger, "append", slow)


@pytest.mark.asyncio
async def test_slow_append_does_not_block_the_event_loop(tmp_path, monkeypatch):
    service = await _service(tmp_path, monkeypatch)
    threads: list[str] = []
    _slow_append(service, monkeypatch, 0.3, threads)

    worst_gap = 0.0

    async def heartbeat() -> None:
        nonlocal worst_gap
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            worst_gap = max(worst_gap, time.monotonic() - before)

    beat = asyncio.create_task(heartbeat())
    await service._record_consolidated_review(
        _intent(), request_type="propose", final_state="denied", decision="deny"
    )
    beat.cancel()

    assert threads and threads[0] != threading.main_thread().name
    assert threads[0].startswith("ledger")
    assert worst_gap < 0.15, f"event loop stalled for {worst_gap:.3f}s during the append"


@pytest.mark.asyncio
async def test_append_is_committed_before_the_caller_resumes(tmp_path, monkeypatch):
    service = await _service(tmp_path, monkeypatch)
    _slow_append(service, monkeypatch, 0.1, [])
    intent = _intent()

    def reviews() -> int:
        return sum(
            1
            for e in _entries(service.council)
            if e.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED
            and e.payload.get("intent_id") == intent.intent_id
        )

    assert reviews() == 0
    await service._record_consolidated_review(
        intent, request_type="authorize", final_state="approved", decision="approve"
    )
    assert reviews() == 1


@pytest.mark.asyncio
async def test_cancelled_caller_waits_for_the_append_then_is_cancelled(tmp_path, monkeypatch):
    service = await _service(tmp_path, monkeypatch)
    _slow_append(service, monkeypatch, 0.3, [])
    before = len(_entries(service.council))

    task = asyncio.create_task(
        service._record_consolidated_review(
            _intent(), request_type="authorize", final_state="approved", decision="approve"
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(_entries(service.council)) == before + 1


@pytest.mark.asyncio
async def test_append_failure_propagates(tmp_path, monkeypatch):
    service = await _service(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(service.council.ledger, "append", boom)
    with pytest.raises(RuntimeError, match="ledger down"):
        await service._record_consolidated_review(
            _intent(), request_type="authorize", final_state="approved", decision="approve"
        )


@pytest.mark.asyncio
async def test_appends_from_one_process_run_one_at_a_time(tmp_path, monkeypatch):
    service = await _service(tmp_path, monkeypatch)
    active = peak = 0
    original = service.council.ledger.append

    def tracked(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.02)
        try:
            return original(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(service.council.ledger, "append", tracked)
    await asyncio.gather(
        *(
            service._record_consolidated_review(
                _intent(), request_type="propose", final_state="denied", decision="deny"
            )
            for _ in range(8)
        )
    )
    assert peak == 1
