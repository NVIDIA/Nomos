<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# 13 — Council stats via Redis (monitor as thin reader)

**Status:** in progress (2026-06-22). Branch `feat/council-stats-redis` (off
`feat/council-stateless-mcp`). Companion to the deploy-repo epic
`docs/proposals/12_council_horizontal_scaling.md` (Phases 4–5).

## Problem
The monitor's metrics had two sources: (1) **proxy-inline** recording
(`MetricsRegistry.start/complete` in `monitor/server/proxy.py`) — request counts,
throughput, by-tool/agent/outcome, decisions — captured because the monitor sat
inline on `/mcp`; and (2) a **telemetry POST** to `/internal/council-latency`
(`record_council_latency`) — stage/actor/limiter latencies, pushed by the council.

The Phase-3 nginx gateway took over the `/mcp` route, so the monitor is no longer
inline → **source (1) is dead** (no request counts/throughput/decisions). Source (2)
still works. Net: the dashboard's core throughput/decision stats are broken.

Also: with multiple monitor replicas, in-memory metrics give each replica only its
own slice — no consistent view.

## Design
Make the **council** the single producer (it sees every request natively, and is the
only component that does post-gateway) and write stats to **Redis**; the **monitor**
becomes a stateless **reader**. Redis is the shared aggregate → every monitor replica
renders an identical view.

**Transport: a capped Redis Stream** `nomos:<prefix>council:telemetry`.
- `XADD … MAXLEN ~ <N>` — append-only, time-ordered, memory-bounded.
- Readers use `XREVRANGE` over the rolling window → independent, consistent reads.
- Fail-soft (matches redis's service-mode role: ephemeral, best-effort). A redis
  outage degrades stats only; it never blocks a council decision.

### Phase 4 — council → Redis  *(this branch, first)*
0. **No call-site changes needed for the MVP.** The Redis Stream entry ID encodes the
   timestamp (free `ts`), and the existing payload already has `tool` / `outcome`
   (→ status) / `stages.total` (→ duration) / `actors` / `limiters`. That's enough to
   rebuild throughput, by-tool, by-outcome, and latency percentiles. `agent_id`
   (by-agent breakdown, needs threading through ~8 call sites) is **deferred**.
1. **`RedisTelemetrySink`** (new, `nomos/nat/telemetry_sink.py`): given the council's
   already-connected `RedisStore` client, `XADD` the JSON record to the stream with a
   MAXLEN cap. Async, best-effort (swallow+log on error), never raises into the request.
3. **Wire** into `CouncilTelemetryPublisher.publish_later` — fire the redis XADD task
   alongside the existing HTTP POST (both are fire-and-forget). Inject the redis client
   after `init_stores()` connects it.
4. **Config** (`settings.py` `TelemetryConfig`): `redis_enabled` (default true when a
   redis client exists), `redis_stream` key, `redis_maxlen`.
5. **Tests**: payload enrichment; sink XADDs the expected fields + respects MAXLEN;
   sink is a no-op (no raise) when redis is unavailable; publisher fires both sinks.

### Phase 5 — monitor reads Redis  *(after Phase 4 verified)*
1. Monitor gains a redis client (reuse `*_REDIS_*` config like council/executor).
2. Build the snapshot from `XREVRANGE` over the window instead of proxy-inline data;
   keep stage/actor latency (now sourced from the stream records too).
3. `MONITOR_STATS_SOURCE = redis|inline` toggle; keep `/internal/council-latency` as a
   no-op/deprecated during transition. Multi-replica-safe (each reads the same stream).
4. Tests: snapshot rebuilt from a synthetic stream; window math; empty-stream safe.

## Deploy (after both phases, in the deploy repo)
- Council: `COUNCIL_TELEMETRY_REDIS_*` env (stream key/maxlen) — redis client already wired.
- Monitor: add `MONITOR_REDIS_*` (same topology contract) + `MONITOR_STATS_SOURCE=redis`;
  NetworkPolicy monitor→redis egress.
- Build via nomos-release off this branch (carries stateless worker + stats). Then the
  monitor's dashboard works again with N replicas.

## Non-goals (for now)
- Full DecisionSummary parity (votes/risk/missing-perms/writ facts the monitor used to
  parse from responses). The MVP restores counts/throughput/outcome/latency; richer
  decision facts can be added to the payload later if needed.
- Retiring the HTTP telemetry sink (kept during transition; remove once redis-read ships).

## Log
- 2026-06-22: Mapped current producer/consumer. Branch cut off the stateless branch
  (so one image carries stateless transport + stats — no council regression). Starting
  Phase 4 (council Redis sink).
- 2026-06-22: **Phase 4 done** (`55e46c9`) — RedisTelemetrySink + publisher fan-out + config;
  5 tests; 175 nat/config green.
- 2026-06-22: **Phase 5 done** (this commit) — monitor reads the stream:
  `MetricsRegistry.ingest_council_record(record, entry_ts=…)` replays a telemetry record
  into a completion (+ stage/actor/limiter samples), reusing `snapshot()` for exact shape;
  `record_council_latency` refactored to a ts-injectable `_record_latency_samples_unlocked`.
  `RedisStatsReader` (monitor/server/redis_stats.py) XREVRANGEs the stream and replays into a
  FRESH registry per request → stateless, identical across N monitor replicas; degrades to an
  empty snapshot on redis error (never 500s). `/api/metrics` uses it when
  `MONITOR_STATS_SOURCE=redis` (sentinel-aware `MONITOR_REDIS_*`; stream `MONITOR_STATS_STREAM`);
  defaults to the in-memory registry otherwise. 10 new tests; 62 monitor tests green.
  **NEXT: deploy wiring** (deploy repo) — council `COUNCIL_TELEMETRY_REDIS_*`, monitor
  `MONITOR_STATS_SOURCE=redis` + `MONITOR_REDIS_*` + NetworkPolicy monitor→redis; build off
  this branch via nomos-release.
