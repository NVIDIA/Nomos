<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary

NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
property and proprietary rights in and to this material, related
documentation and any modifications thereto. Any use, reproduction,
disclosure or distribution of this material and related documentation
without an express license agreement from NVIDIA CORPORATION or
its affiliates is strictly prohibited.
-->

# Proposal 06: HA Redis and Nonce Store

## Status
**Partially implemented**

## 1. Summary

Nomos currently treats Redis as the single source of truth for accounts, auth
tokens, council membership, nonces, writ metadata, outcomes, intents, and rate
counters. That is the right data model, but the current deployment shape is a
single Redis pod with one RWO PVC. If that pod, node, or volume is unavailable,
authorization and execution fail closed. That is safe, but it is a full service
outage.

This proposal defines the Redis availability path while preserving the existing
key schema and atomic nonce claim semantics. The first implemented step keeps
Redis bundled and adds Sentinel-backed primary discovery behind one shared Nomos
Redis client configuration. External Redis remains a later step.

## 2. Goals

- Keep Redis as the single source of truth for live operational state.
- Preserve nonce replay defense as atomic `SET NX EX`.
- Make Redis unavailability or ambiguous nonce confirmation fail closed.
- Give Helm operators a supported bundled Sentinel topology where every Redis
  consumer receives the same connection settings.
- Use one shared Redis client factory for council, executor, and signup paths.
- Keep bundled standalone Redis available for local Kind and Compose
  development.
- Provide an opt-in Compose Sentinel topology that exercises the same client
  behavior outside Kubernetes.
- Leave room for a later external Redis topology without changing store
  semantics.

## 3. Non-Goals

- Do not introduce Redis Cluster in Phase 1.
- Do not change the Redis key schema.
- Do not add in-process state caches or fallback stores.
- Do not claim full infrastructure HA while Redis remains bundled in one
  application chart.
- Do not unbundle Redis or require an operator-managed Redis service in the
  first implementation step.
- Do not change writ, account, or executor authorization semantics.

Redis Cluster is deferred because the current key model includes related keys
for accounts, permissions, councils, writs, outcomes, and rate counters. Cluster
slotting and multi-key behavior need a separate design before it is safe.

## 4. Current State

- Helm deploys Redis as one `Deployment` replica.
- Redis persistence is append-only on a single RWO PVC.
- Council is configured with `COUNCIL_REDIS_HOST`, `COUNCIL_REDIS_PORT`, and
  `COUNCIL_REDIS_PASSWORD`.
- Executor and signup are configured with Redis URLs.
- Council-side `RedisStore`, executor-side `NonceStore`, and signup each create
  Redis clients independently.
- NetworkPolicy does not currently restrict Redis ingress by default.

## 5. Requirements

### R1: One Topology Contract

All Redis consumers must receive the same Redis topology settings. The chart
must not allow council, executor, and signup to silently point at different
Redis deployments.

### R2: Fail Closed

If Redis is unavailable, unreachable, read-only during failover, or the nonce
write result is ambiguous, Nomos must reject or stop the operation. It must not
authorize execution on a best-effort nonce claim.

### R3: Atomic Nonce Claim

The replay-defense primitive remains:

```text
SET nomos:nonces:{nonce_hex} {writ_id} NX EX {ttl_seconds}
```

A successful first claim returns success. An existing nonce returns replay. A
Redis error returns a fail-closed error.

### R4: No Shadow State

Nomos must not keep an in-memory fallback for accounts, nonces, writs, or rate
state. Redis remains authoritative.

### R5: Bounded Connection Behavior

Redis clients must use bounded connect and command timeouts. Startup may retry,
but request handling must not wait indefinitely for Redis.

### R6: Secure By Default For Alpha

Production Alpha values must use non-default secrets, support TLS where the
Redis provider supports it, and restrict Redis network access to the pods that
need it.

## 6. Proposed Phases

### Phase 0: Document and Guard the Current Invariants

- Restore this proposal as the canonical reference for Redis HA work.
- Keep `nomos/store/SCHEMA.md` as the key schema contract.
- Add or confirm tests for `SET NX EX` nonce behavior and Redis unavailable
  fail-closed behavior.
- Add Helm template checks proving the current bundled Redis path still renders
  for Kind.

### Phase 1A: Bundled Sentinel Support

Add a shared HA-aware Redis factory under `nomos_runtime/`.

Supported topology modes:

| Mode | Purpose | Notes |
| --- | --- | --- |
| `sentinel` | Default bundled Redis topology | Uses chart-managed Redis replicas and Sentinel pods. |
| `standalone` | Explicit compatibility override | Uses the chart-managed Redis service with one pod. |

Consumers:

- Council `RedisStore`.
- Executor `NonceStore`.
- Agent signup Redis client.
- Account/seed scripts can follow after the service paths are stable.

Helm changes:

- Add a single Redis topology values block.
- Render the same topology settings into council, executor, and signup.
- Make Sentinel the default Helm topology.
- Keep standalone mode as an explicit override for constrained local or
  compatibility environments.
- Render three Redis pods, three Sentinel pods, per-pod PVCs, and disruption
  budgets when Sentinel mode is enabled.
- Add an optional Compose override with three Redis containers and three
  Sentinel containers so local deployments can test Sentinel master discovery
  and failover behavior.

### Phase 1B: External Redis Support

Add an operator-owned or managed Redis topology after the bundled Sentinel path
is stable. At that point, `redis.enabled=false` can be supported only when every
Redis consumer receives a complete external topology.

### Phase 2: Durability Confirmation Option

Add an optional strict mode for critical nonce writes. After a successful nonce
claim, the client may require replica acknowledgement with Redis durability
controls such as `WAIT` when the topology supports it.

Behavior:

- If `SET NX EX` succeeds and durability confirmation meets policy, continue.
- If `SET NX EX` reports replay, reject as replay.
- If `SET NX EX` succeeds but durability confirmation fails or is unsupported
  under strict mode, fail closed and do not execute.

Strict mode is not required for the first Alpha path, but the configuration
surface should leave room for it.

### Phase 3: Backups and Restore Drills

Define operational backup and restore requirements for Redis state:

- Managed Redis snapshots or operator-owned backup jobs.
- Restore drill documented for accounts, nonces, writ metadata, outcomes, and
  rate counters.
- Clear RPO/RTO expectations for Alpha.

## 7. Configuration Shape

The exact field names can change during implementation, but the chart should
model one Redis topology, not per-service Redis settings:

```yaml
redis:
  enabled: true
  topology:
    mode: sentinel
    db: 0
    keyPrefix: nomos:
    tls: false
    connectTimeoutMs: 1000
    commandTimeoutMs: 1000
    sentinel:
      serviceName: nomos-master
      nodes:
        - nomos-redis-sentinel:26379
```

Secrets remain separate from topology:

```yaml
secrets:
  keys:
    redisPassword: REDIS_PASSWORD
```

For `external` and `sentinel`, operators may provide the secret through native
Kubernetes Secrets or External Secrets.

## 8. Client Design

The shared factory should expose one small API:

- Build a Redis client from structured topology settings.
- Support standalone URL/host-port and Sentinel primary discovery.
- Apply TLS, database, password, decode mode, and bounded timeouts consistently.
- Return an async Redis-compatible client used by existing store classes.

The existing store surfaces should remain stable:

- `RedisStore.claim_nonce(...)`
- `NonceStore.claim_nonce(...)`
- account CRUD methods
- writ and outcome methods
- rate-limit methods

## 9. Network Policy

When NetworkPolicy is enabled, Redis ingress should be allowed only from:

- council
- executor-openshell
- agent-signup
- explicitly named maintenance jobs, if any

Redis egress should be empty unless Sentinel or managed-provider requirements
force specific egress rules.

## 10. Verification

Implementation is complete when these checks pass:

- Unit tests cover topology parsing and client factory selection.
- Council and executor tests prove Redis errors fail closed.
- Nonce tests prove `SET NX EX` remains atomic.
- Signup health and account writes use the shared Redis configuration.
- Helm template tests prove bundled, external, and Sentinel modes render the
  expected env vars and that all consumers receive the same topology.
- A local failover drill, using Sentinel or a controlled fake, proves an
  ambiguous nonce claim does not execute.
- Documentation explains that bundled Redis is for development, while Alpha
  production uses external or Sentinel-backed Redis.

## 11. Acceptance Criteria

- A Kind install still works with bundled Redis.
- A production-style Helm render can disable bundled Redis.
- Council, executor, and signup all consume the same Redis topology.
- Redis downtime denies authorization and execution rather than bypassing
  nonce or auth checks.
- No Redis Cluster support is shipped under this proposal.
