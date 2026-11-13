# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Loop-owned, bounded immutable workflow outbox. No worker sees live state."""

import asyncio
import json
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from nomos.level_0.types.workflow_snapshot import WorkflowSnapshot

logger = logging.getLogger(__name__)


async def drain(future):
    """Cancellation never releases a store lease before its worker finishes."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if future.done():
                # Consume errors, but cancellation remains the caller's outcome.
                if not future.cancelled():
                    future.exception()
                raise


class WorkflowRuntime:
    """One write lane, one read lane; coalesce only snapshots not yet started.

    enqueue is synchronous and must run on the owning loop. flush waits for the
    requested version's attempt, not unrelated intents. A false receipt is never
    sufficient for eviction: a rejected CAS is reconciled against exact content.
    """

    def __init__(self, store: Any, *, capacity: int):
        if capacity < 1:
            raise ValueError("workflow capacity must be positive")
        self.store = store
        self.capacity = capacity
        self._pending: dict[str, WorkflowSnapshot] = {}
        self._scheduled: set[str] = set()
        self._queue: deque[str] = deque()
        self._events: dict[str, asyncio.Event] = {}
        self._latest: dict[str, int] = {}
        self._attempted: dict[str, int] = {}
        self._durable: dict[str, int] = {}
        self._writer: asyncio.Task | None = None
        self._write_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="workflow-write"
        )
        self._read_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow-read")
        self._read_gate = asyncio.Semaphore(1)
        self._closed = False
        self._close_task: asyncio.Task | None = None

    def enqueue(self, snapshot: WorkflowSnapshot) -> None:
        if self._closed:
            raise RuntimeError("workflow runtime closed")
        key = snapshot.intent_id
        if snapshot.logical_version <= self._latest.get(key, 0):
            raise ValueError("workflow snapshot versions must increase")
        if key not in self._scheduled:
            if len(self._scheduled) >= self.capacity:
                raise RuntimeError("workflow outbox capacity exceeded")
            self._scheduled.add(key)
            self._queue.append(key)
        self._latest[key] = snapshot.logical_version
        previous = self._pending.get(key)
        if previous is None or snapshot.logical_version > previous.logical_version:
            self._pending[key] = snapshot
        self._events.setdefault(key, asyncio.Event()).clear()
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._write_loop(), name="workflow-outbox")

    def _write(self, snapshot: WorkflowSnapshot) -> bool:
        if self.store is None:
            return False
        if self.store.upsert_snapshot(snapshot):
            return True
        # A lost acknowledgement/retry may see the exact same version. Never
        # count a stale, conflicting, or terminal-preserving rejection as saved.
        return (
            json.dumps(
                self.store.get_workflow(snapshot.intent_id), sort_keys=True, separators=(",", ":")
            )
            == snapshot.payload_json
        )

    async def _write_loop(self) -> None:
        while self._queue:
            key = self._queue.popleft()
            snapshot = self._pending.pop(key)
            try:
                saved = await drain(
                    asyncio.get_running_loop().run_in_executor(
                        self._write_executor, self._write, snapshot
                    )
                )
            except Exception:
                logger.warning("workflow snapshot persistence failed")
                saved = False
            self._attempted[key] = snapshot.logical_version
            if saved:
                self._durable[key] = snapshot.logical_version
            self._events[key].set()
            if key in self._pending:
                self._queue.append(key)
            else:
                self._scheduled.remove(key)

    async def flush(self, intent_id: str, version: int) -> bool:
        while self._attempted.get(intent_id, 0) < version:
            event = self._events.get(intent_id)
            if event is None or intent_id not in self._scheduled:
                return False
            event.clear()
            await event.wait()
        return self.is_durable(intent_id, version)

    def is_durable(self, intent_id: str, version: int) -> bool:
        return self._durable.get(intent_id, 0) >= version

    def forget(self, intent_id: str) -> None:
        if intent_id in self._scheduled:
            raise RuntimeError("cannot forget a pending workflow snapshot")
        self._latest.pop(intent_id, None)
        self._attempted.pop(intent_id, None)
        self._durable.pop(intent_id, None)
        self._events.pop(intent_id, None)

    def pending(self, intent_id: str) -> bool:
        return intent_id in self._scheduled

    async def read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("workflow runtime closed")
        if self.store is None:
            return None
        async with self._read_gate:
            if self._closed:
                raise RuntimeError("workflow runtime closed")
            future = asyncio.get_running_loop().run_in_executor(
                self._read_executor, partial(getattr(self.store, method), *args, **kwargs)
            )
            return await drain(future)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await drain(self._close_task)

    async def _close(self) -> None:
        if self._writer is not None:
            await self._writer
        # Requests queued before close recheck _closed after acquiring the gate.
        async with self._read_gate:
            pass
        self._write_executor.shutdown(wait=True)
        self._read_executor.shutdown(wait=True)
