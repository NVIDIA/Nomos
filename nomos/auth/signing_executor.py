# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded service-owned writ signing; never owns or closes the signer."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from nomos.crypto.signer_provider import SignerProvider
from nomos.level_0.crypto.signing import Signature


class SigningCapacityError(RuntimeError):
    """Temporary signing admission failure; no signature was requested."""


async def drain_signing(future):
    """Actual work retains its ownership until completion despite cancellation."""
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
                if not future.cancelled():
                    future.exception()
                raise


class WritSigningExecutor:
    def __init__(self, *, workers: int, capacity: int):
        if workers < 1 or capacity < 1:
            raise ValueError("Signing worker and capacity bounds must be positive")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="writ-sign")
        self._slots = asyncio.Semaphore(workers)
        self._capacity = capacity
        self._admitted = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._close_task = None

    async def sign(self, signer: SignerProvider, payload: bytes) -> Signature:
        if self._closed:
            raise SigningCapacityError("Writ signing service is closed")
        if self._admitted >= self._capacity:
            raise SigningCapacityError("Writ signing temporarily at capacity; no writ issued")
        self._admitted += 1
        self._idle.clear()
        try:
            async with self._slots:
                future = asyncio.get_running_loop().run_in_executor(
                    self._executor, signer.sign, payload
                )
                return await drain_signing(future)
        finally:
            self._admitted -= 1
            if not self._admitted:
                self._idle.set()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await drain_signing(self._close_task)

    async def _close(self) -> None:
        await self._idle.wait()
        # _idle is set only after every admitted actual worker future finishes.
        # No pending signer work remains; stopping idle threads needs neither a
        # blocking join on the loop nor the unrelated default executor.
        self._executor.shutdown(wait=False)
