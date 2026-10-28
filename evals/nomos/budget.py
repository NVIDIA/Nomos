# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Crash-safe shared provider request reservations for a whole pilot."""

from __future__ import annotations

import fcntl
import time
import uuid
from pathlib import Path

from evals.nomos.workflow import read, write


class RequestBudget:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, path: Path, *, calls: int = 48, seconds: int = 2700):
        if path.exists() or not 1 <= calls <= 10000 or not 1 <= seconds <= 86400:
            raise ValueError("invalid or existing request budget")
        write(path, {"max_calls": calls, "deadline": time.time() + seconds, "requests": []})
        return cls(path)

    def reserve(self, purpose: str) -> str:
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read(self.path)
            if len(data["requests"]) >= data["max_calls"] or time.time() >= data["deadline"]:
                raise RuntimeError("shared provider budget exhausted")
            identity = uuid.uuid4().hex
            data["requests"].append(
                {
                    "id": identity,
                    "purpose": purpose,
                    "reserved_at": time.time(),
                    "status": "reserved",
                }
            )
            write(self.path, data)
            return identity

    def finish(self, identity: str, *, status: str, usage=None):
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read(self.path)
            row = next(r for r in data["requests"] if r["id"] == identity)
            row.update(status=status, usage=usage, finished_at=time.time())
            write(self.path, data)


class SharedCounter(dict):
    """The pinned Compass client increments calls immediately before each dispatch."""

    def __init__(self, budget: RequestBudget):
        super().__init__(calls=0)
        self.shared = budget

    def __setitem__(self, key, value):
        if key == "calls" and value > self.get("calls", 0):
            self.shared.reserve("compass-diagnosis")
        super().__setitem__(key, value)
