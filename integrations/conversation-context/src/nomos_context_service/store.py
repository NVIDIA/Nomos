# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded single-process TTL store with atomic, immutable publication.

Retained canonical JSON bytes and record count are bounded, not total process
RSS. All operations share one lock, including indexed expiry. Content is lost on
restart. The client keeps a fixed not_after deadline across snapshot retries;
that deadline prevents exact expired requests from resurrecting after cleanup
or restart without keeping an unbounded tombstone database.
"""

import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from heapq import heapify, heappop, heappush
from itertools import islice
from threading import Lock

from .models import SnapshotInput, ToolRecordInput

MAX_LOOKUP_TOOL_RECORDS = 20
# Allow a small amount of stale deadline metadata before rebuilding both heaps.
_EXPIRY_HEAP_SLACK = 64
RecordKey = tuple[str, str, str]
ExpiryItem = tuple[float, int, RecordKey]


class StoreError(Exception):
    """Carry only a stable public reason, never the submitted record or secret."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def timestamp(value: float) -> str:
    """Format a server-accepted UTC timestamp consistently for client receipts."""
    return datetime.fromtimestamp(value, UTC).isoformat()


@dataclass(frozen=True)
class Entry:
    """Retain only canonical bytes and the metadata needed for lookup and expiry."""

    kind: str
    sandbox_name: str
    record_id: str
    snapshot_id: str
    raw: bytes
    revision: int
    received_at: float
    expires_at: float
    monotonic_deadline: float

    def receipt(self, *, duplicate: bool) -> dict:
        """An idempotent retry returns the original timestamps and revision."""
        return {
            "snapshot_id" if self.kind == "snapshot" else "record_id": self.record_id,
            "revision": self.revision,
            "received_at": timestamp(self.received_at),
            "expires_at": timestamp(self.expires_at),
            "duplicate": duplicate,
        }


class ContextStore:
    """In-memory store for a trusted single-user pilot, not tenant isolation."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        max_records: int = 1000,
        max_bytes: int = 16 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("TTL must be finite and positive")
        if max_records < 1 or max_bytes < 1:
            raise ValueError("store limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.clock = clock
        self.wall_clock = wall_clock
        self._entries: dict[RecordKey, Entry] = {}
        self._tools_by_sandbox: dict[str, OrderedDict[str, Entry]] = {}
        self._live_counts: dict[str, int] = {}
        self._monotonic_expiry: list[ExpiryItem] = []
        self._wall_expiry: list[ExpiryItem] = []
        self._latest: dict[str, str] = {}
        self._bytes = 0
        self._revision = 0
        self._lock = Lock()

    def _purge(self, monotonic_now: float, wall_now: float) -> None:
        """Remove due records under the lock; either clock can expire an entry."""
        for heap, now in ((self._monotonic_expiry, monotonic_now), (self._wall_expiry, wall_now)):
            while heap and heap[0][0] <= now:
                _, revision, key = heappop(heap)
                self._remove(key, revision)
        self._compact_expiry()

    def _remove(self, key: RecordKey, revision: int) -> None:
        """Remove one revision from all live indexes; ignore obsolete heap items."""
        entry = self._entries.get(key)
        if entry is None or entry.revision != revision:
            return
        del self._entries[key]
        self._bytes -= len(entry.raw)
        if entry.kind == "tool":
            tools = self._tools_by_sandbox[entry.sandbox_name]
            del tools[entry.record_id]
            if not tools:
                del self._tools_by_sandbox[entry.sandbox_name]
        remaining = self._live_counts[entry.sandbox_name] - 1
        if remaining:
            self._live_counts[entry.sandbox_name] = remaining
        else:
            del self._live_counts[entry.sandbox_name]
            self._latest.pop(entry.sandbox_name, None)
        # Keep the latest-pointer fence while older records remain live. Expiring
        # a newer short-lived snapshot must never restore an older context view.

    def _compact_expiry(self) -> None:
        """Bound stale metadata left in the other clock's heap without retaining payloads.

        Ordinary purges inspect only due items. Rebuilding is occasional O(N)
        work when either heap exceeds twice the live count plus a fixed slack.
        """
        if not self._entries:
            self._monotonic_expiry.clear()
            self._wall_expiry.clear()
            return
        limit = 2 * len(self._entries) + _EXPIRY_HEAP_SLACK
        if len(self._monotonic_expiry) <= limit and len(self._wall_expiry) <= limit:
            return
        self._monotonic_expiry = [
            (entry.monotonic_deadline, entry.revision, key) for key, entry in self._entries.items()
        ]
        self._wall_expiry = [
            (entry.expires_at, entry.revision, key) for key, entry in self._entries.items()
        ]
        heapify(self._monotonic_expiry)
        heapify(self._wall_expiry)

    def purge(self) -> None:
        """Periodically remove idle expired payloads; every operation also purges."""
        with self._lock:
            self._purge(self.clock(), self.wall_clock())

    def publish(self, record: SnapshotInput | ToolRecordInput) -> dict:
        """Commit one record or return its identical live receipt without renewal."""
        kind = "snapshot" if isinstance(record, SnapshotInput) else "tool"
        record_id = record.snapshot_id if isinstance(record, SnapshotInput) else record.record_id
        raw = json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        key = (kind, record.sandbox_name, record_id)
        with self._lock:
            monotonic_now, wall_now = self.clock(), self.wall_clock()
            self._purge(monotonic_now, wall_now)
            if isinstance(record, SnapshotInput):
                expires_at = record.not_after.timestamp()
                if expires_at <= wall_now:
                    raise StoreError(410, "publication_expired")
                if expires_at - wall_now > self.ttl_seconds:
                    raise StoreError(422, "publication_deadline_exceeds_ttl")
                deadline = monotonic_now + expires_at - wall_now
            else:
                parent = self._entries.get(("snapshot", record.sandbox_name, record.snapshot_id))
                if parent is None:
                    raise StoreError(404, "snapshot_unavailable")
                expires_at, deadline = parent.expires_at, parent.monotonic_deadline
            existing = self._entries.get(key)
            if existing is not None:
                if existing.raw != raw:
                    raise StoreError(409, "record_id_conflict")
                return existing.receipt(duplicate=True)
            if len(self._entries) >= self.max_records or self._bytes + len(raw) > self.max_bytes:
                raise StoreError(507, "store_capacity_reached")
            self._revision += 1
            entry = Entry(
                kind=kind,
                sandbox_name=record.sandbox_name,
                record_id=record_id,
                snapshot_id=record.snapshot_id,
                raw=raw,
                revision=self._revision,
                received_at=wall_now,
                expires_at=expires_at,
                monotonic_deadline=deadline,
            )
            self._entries[key] = entry
            self._live_counts[entry.sandbox_name] = self._live_counts.get(entry.sandbox_name, 0) + 1
            if kind == "snapshot":
                self._latest[record.sandbox_name] = record_id
            else:
                tools = self._tools_by_sandbox.setdefault(entry.sandbox_name, OrderedDict())
                tools[entry.record_id] = entry
            # These indexes reference the same entry or its key, never another
            # copy of the canonical payload. Retries return before this point.
            heappush(self._monotonic_expiry, (deadline, entry.revision, key))
            heappush(self._wall_expiry, (expires_at, entry.revision, key))
            self._bytes += len(raw)
            return entry.receipt(duplicate=False)

    def lookup(self, sandbox_name: str, *, include_content: bool = False) -> dict:
        """Return only live records; latest means commit order, not HTTP causality."""
        with self._lock:
            self._purge(self.clock(), self.wall_clock())
            latest_id = self._latest.get(sandbox_name)
            latest = self._entries.get(("snapshot", sandbox_name, latest_id))
            if latest is None:
                raise StoreError(404, "context_unavailable")
            tools = self._tools_by_sandbox.get(sandbox_name)
            selected = (
                list(islice(reversed(tools.values()), MAX_LOOKUP_TOOL_RECORDS)) if tools else []
            )
            result = {
                "sandbox_name": sandbox_name,
                "association": "name_based_pilot",
                "snapshot_ref": latest.record_id,
                "snapshot_revision": latest.revision,
                "snapshot_received_at": timestamp(latest.received_at),
                "snapshot_expires_at": timestamp(latest.expires_at),
                "recent_tool_record_refs": [entry.record_id for entry in selected],
                # Events may belong to earlier snapshots. Readers must recheck
                # each event's actual parent deadline after network transit.
                "tool_record_expires_at": {
                    entry.record_id: timestamp(entry.expires_at) for entry in selected
                },
                # Service-owned first acceptance times; identical retries do not renew them.
                "tool_record_received_at": {
                    entry.record_id: timestamp(entry.received_at) for entry in selected
                },
                "tool_records_truncated": tools is not None and len(tools) > len(selected),
                "exact_tool_call_id_for_http_request": None,
                "capture_coverage": "managed_relay_calls_only",
                "content_trust": "agent_reported_workload_mutable",
                "source_content_omitted": not include_content,
            }
            if include_content:
                result["snapshot"] = json.loads(latest.raw)
                result["tool_records"] = [json.loads(entry.raw) for entry in selected]
            return result
