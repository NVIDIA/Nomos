# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redis-stream stats reader for the monitor.

Post-gateway the monitor is no longer inline on /mcp, so the council is the sole
stats producer (XADD to a capped stream — see nomos/nat/telemetry_sink.py). This
reader rebuilds the metrics Snapshot from that stream on each request by replaying
the windowed entries into a FRESH MetricsRegistry. Being stateless per request, N
monitor replicas all render an identical view from the shared stream.

See docs/proposals/13_council_stats_via_redis.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from monitor.server.metrics import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_WINDOW_SECONDS,
    MetricsRegistry,
)

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# The dashboard polls three endpoints at 1 Hz per open tab. One replay per
# second serves all of them instead of re-parsing the stream for each call.
REPLAY_TTL_SECONDS = 1.0


def _entry_ts_seconds(entry_id: str) -> float:
    """Redis stream IDs are '<ms>-<seq>'; return the millisecond part as seconds."""
    try:
        return int(entry_id.split("-", 1)[0]) / 1000.0
    except (ValueError, AttributeError, IndexError):
        return 0.0


class RedisStatsReader:
    """Builds a metrics Snapshot by replaying a council telemetry Redis stream."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        stream: str,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ):
        self._client = client
        self._stream = stream
        self._window_seconds = window_seconds
        self._history_limit = history_limit
        self._cached: MetricsRegistry | None = None
        self._cached_at = 0.0
        self._replay_lock = asyncio.Lock()

    async def snapshot(self) -> Any:
        """Snapshot of the replayed stream (see `registry`)."""
        return await (await self.registry()).snapshot()

    async def registry(self) -> MetricsRegistry:
        """Replay the most-recent stream entries into a fresh registry.

        Every dashboard endpoint (metrics, recent requests, CSV export) reads this
        one registry so they agree with each other. Replays are shared for
        REPLAY_TTL_SECONDS. Best-effort: on any redis/parse error returns an empty
        registry rather than raising, so the dashboard degrades to zeros instead
        of 500-ing.
        """
        async with self._replay_lock:
            if self._cached is None or time.monotonic() - self._cached_at >= REPLAY_TTL_SECONDS:
                self._cached = await self._replay()
                self._cached_at = time.monotonic()
            return self._cached

    async def _replay(self) -> MetricsRegistry:
        registry = MetricsRegistry(
            window_seconds=self._window_seconds,
            history_limit=self._history_limit,
            stats_source="redis",
        )
        try:
            # Newest first, capped at history_limit; "totals" ≈ last N entries.
            entries = await self._client.xrevrange(self._stream, count=self._history_limit)
        except Exception as exc:  # degrade, never 500 the dashboard
            logger.warning("redis stats read failed: %s", exc)
            return registry

        # Replay oldest→newest so history/last-seen ordering matches real time.
        for entry_id, fields in reversed(entries):
            record = _decode_record(fields)
            if record is None:
                continue
            await registry.ingest_council_record(
                record, entry_ts=_entry_ts_seconds(_as_str(entry_id))
            )
        return registry


def _as_str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode_record(fields: Any) -> dict[str, Any] | None:
    """Extract the JSON payload from a stream entry's fields ({'data': '<json>'})."""
    if not isinstance(fields, dict):
        return None
    raw = fields.get("data") or fields.get(b"data")
    if raw is None:
        return None
    try:
        record = json.loads(_as_str(raw))
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None
