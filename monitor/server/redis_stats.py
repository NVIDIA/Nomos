# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Redis-stream stats reader for the monitor.

Post-gateway the monitor is no longer inline on /mcp, so the council is the sole
stats producer (XADD to a capped stream — see nomos/nat/telemetry_sink.py). This
reader rebuilds the metrics Snapshot from that stream on each request by replaying
the windowed entries into a FRESH MetricsRegistry. Being stateless per request, N
monitor replicas all render an identical view from the shared stream.

See docs/proposals/13_council_stats_via_redis.md.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from monitor.server.metrics import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_WINDOW_SECONDS,
    MetricsRegistry,
)

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


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

    async def snapshot(self) -> Any:
        """Replay the most-recent stream entries into a fresh registry → Snapshot.

        Best-effort: on any redis/parse error returns an empty snapshot rather than
        raising, so /api/metrics degrades to zeros instead of 500-ing.
        """
        registry = MetricsRegistry(
            window_seconds=self._window_seconds, history_limit=self._history_limit
        )
        try:
            # Newest first, capped at history_limit; lifetime totals ≈ last N entries.
            entries = await self._client.xrevrange(self._stream, count=self._history_limit)
        except Exception as exc:  # degrade, never 500 the dashboard
            logger.warning("redis stats read failed: %s", exc)
            return await registry.snapshot()

        # Replay oldest→newest so history/last-seen ordering matches real time.
        for entry_id, fields in reversed(entries):
            record = _decode_record(fields)
            if record is None:
                continue
            await registry.ingest_council_record(
                record, entry_ts=_entry_ts_seconds(_as_str(entry_id))
            )
        return await registry.snapshot()


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
