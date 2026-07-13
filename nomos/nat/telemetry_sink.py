# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redis-stream telemetry sink for council stats.

Post-gateway, the monitor no longer sits inline on /mcp, so it can't observe
requests directly. The council is the only component that sees every request, so
it becomes the sole stats producer: each per-decision telemetry record is appended
to a capped Redis stream, which the monitor (one or many replicas) reads to render
a consistent view. See docs/proposals/13_council_stats_via_redis.md.

Best-effort by design — redis is fail-soft here (ephemeral stats only). A redis
error degrades the dashboard, never a council decision: publish swallows and logs.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RedisTelemetrySink:
    """Appends council telemetry records to a capped Redis stream (XADD)."""

    def __init__(self, *, stream: str, maxlen: int, enabled: bool = True):
        self._stream = stream
        self._maxlen = maxlen
        self._enabled = enabled
        self._client: aioredis.Redis | None = None

    def attach(self, client: aioredis.Redis | None) -> None:
        """Wire the council's already-connected redis client (after init_stores)."""
        self._client = client

    @property
    def active(self) -> bool:
        return self._enabled and self._client is not None

    async def publish(self, payload: dict[str, Any]) -> None:
        """XADD the record to the stream. Never raises (best-effort)."""
        if not self.active:
            return
        try:
            # One JSON field keeps nested stages/actors/limiters lossless; the
            # stream entry ID supplies the timestamp, so the record needs no ts.
            await self._client.xadd(  # type: ignore[union-attr]
                self._stream,
                {"data": json.dumps(payload, separators=(",", ":"))},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:  # stats must never break a decision
            logger.debug("redis telemetry XADD failed: %s", exc)
