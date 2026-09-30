# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded correlation state between Hermes pre- and post-tool hooks."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingAuthorization:
    """Bounded correlation metadata retained until Hermes reports completion."""

    writ_id: str
    decision_id: str
    command: str
    working_directory: str
    created_at: float


class PendingAuthorizations:
    """Thread-safe TTL/LRU map; it is correlation state, never replay state."""

    def __init__(self, *, max_entries: int = 2048, ttl_seconds: float = 900.0) -> None:
        if max_entries <= 0 or ttl_seconds <= 0:
            raise ValueError("pending authorization bounds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._items: OrderedDict[tuple[str, str], PendingAuthorization] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._items:
            _, oldest = next(iter(self._items.items()))
            if now - oldest.created_at <= self._ttl_seconds:
                break
            self._items.popitem(last=False)

    def put(self, key: tuple[str, str], value: PendingAuthorization) -> None:
        """Store one authorization while enforcing TTL and entry bounds."""
        with self._lock:
            self._prune(value.created_at)
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    def pop(self, key: tuple[str, str]) -> PendingAuthorization | None:
        """Remove and return authorization metadata for a completed call."""
        with self._lock:
            self._prune(time.time())
            return self._items.pop(key, None)
