<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# 09 — High-Availability Capability Plan

## Goal
Survive the loss of any single **pod, node, or backend instance** without losing
committed authorizations or taking sustained downtime.
- **RTO:** automatic recovery per tier in ≲ 1 minute.
- **RPO = 0** for the ledger and account/writ state (no lost committed decisions).

## Where we are (post PG-HA cutover)
- **Council:** 1 replica. Durable state (ledger, accounts, writs, intents) in
  PostgreSQL; signing via Vault transit; nonces + rate-limits in redis — all shared.
  **No PVCs on the council** (RWO Multi-Attach pin removed). ✅ storage barrier gone.
- **Backends:** `nomos-pg` (1 node, block), `nomos-vault` (1 node, file storage,
  manual unseal), `redis` (1 node, standalone). All single-node SPOFs.
- **Net:** no HA today (1 replica + single-node backends), but the cutover removed
  the structural blocker that made HA impossible.

HA spans **two independent tiers; both are required**.

---

## Tier 1 — Council app-tier HA (run N replicas) — gated on NOMOS-5b  *[Nomos]*

**Blocker:** in-flight workflow state (`SecurityCouncil._workflows`, council.py) and
quorum vote records (`QuorumManager._records`, manager.py) are **in-memory per
replica** with no store fallback, so a status/vote request that load-balances to
another replica fails. Single-call auto-decided `council_authorize` is replica-safe;
**deferred/quorum approval is not.** Durable serialization exists
(`IntentWorkflow.to_durable_dict/from_durable_dict`) but is **never wired** (NOMOS-5b
is "foundation only").

**Work:**
1. PG tables `council_workflows` + `vote_records` (+ a `version`/`updated_at` column).
2. `DurableStore` methods: store/get/list workflows + vote records; serialize
   concurrent transitions across replicas via `SELECT … FOR UPDATE` (or optimistic
   version check) — the ledger already atomically sequences; the workflow/vote state
   machine needs the same guard against lost updates.
3. Wire it: persist on every transition (submit → vote → finalize → decide → mint);
   `get_workflow`/`cast_vote`/`finalize_vote` fall back to the store + rehydrate on a
   local miss; evict local cache only for terminal workflows.
4. (Soft) make load-shed admission cluster-aware — move the intent-latency EWMA /
   queue depth to a shared signal (redis), else shedding is per-replica/uneven.
5. Deploy: council `autoscaling.maxReplicas > 1` (HPA on CPU/inflight),
   `PodDisruptionBudget(minAvailable)`, `topologySpreadConstraints` + anti-affinity.

**Validation:** kill a council pod mid-approval → another replica resumes the
workflow and completes quorum; N-replica 32-burst → zero lost votes / sequence errors.

**Outcome:** delivers **both** horizontal scale-out **and** survival of council
pod/node loss. Works on today's single-node backends (partial HA), so it can land
before Tier 2.

### Tier 1a — what shipped (NOMOS-5b) vs deferred (NOMOS-5c)  *(operator decision 2026-06-15)*

Tracing the **deployed** MCP surface (`nomos/nat/plugin.py`) showed the council
runs every `council_propose`/`council_authorize` review — submit → in-process LLM
votes → debate → decide → mint — **atomically inside one request on one replica**.
There is **no external approve/deny/vote MCP tool**, so the live vote state machine
never crosses a replica boundary. The only multi-replica gap is **reads** of an
already-decided workflow (`check_intent_status`/`get_pending_approvals`).

- **NOMOS-5b (shipped):** durable workflow **status cache** — `council_workflows`
  table; write-through on terminal/decision transitions; read-through/rehydrate on
  a `get_workflow` local miss; `get_pending_approvals` unions the store; expired-
  terminal reaper. Delivers deployed **scale-out** (N replicas share status + the PG
  atomic ledger) and **pod/node-loss survival** (an in-flight request on a dead pod
  is client-retried; no *committed* decision is lost — commit is atomic at
  `_close_vote`'s ledger append). `None`-store (file/redis) behavior is byte-identical.
- **NOMOS-5c (deferred, GATED on exposing a deferred/external-approval MCP tool):**
  persisted **+ locked** `VoteRecord` (`SELECT … FOR UPDATE`/optimistic version),
  `AWAITING_APPROVAL` persistence after `open_vote`, store-backed rehydration on **all**
  mutating paths (`approve`/`deny`/`cast`/`revise`/`finalize`/`mint`/`execute`),
  persisted expiry transitions, and tests for cross-replica pending/status,
  cross-replica approve/finalize after a local miss, concurrent-vote no-lost-update,
  and pod-loss mid-approval resume. **Why coupled, not incremental:** adding workflow
  store-fallback to the mutating paths *without* shared vote-records is a **silent
  lost vote** — `QuorumManager.cast_vote` returns "No vote open" on a replica with no
  local record, yet `cast_member_vote` reports `success=True` with the workflow still
  `AWAITING` (verified `nomos/level_2/quorum/manager.py:133`). So the vote-sharing
  pieces ship together or not at all. Until a deferred-approval tool exists this is
  dead scope.

---

## Tier 2 — Backend HA (remove PG/Vault/redis SPOFs) — Track B  *[Infra]*

### 2a. Redis HA — lowest risk, do first
The chart already defaults to **sentinel** topology (stg trimmed it to standalone).
Flip stg `redis.topology.mode=sentinel` (1 primary + 2 replicas + 3 sentinels); apps
are sentinel-aware (`*_REDIS_MODE`). Redis is **fail-soft** now (ephemeral nonces/rate
only), so losing it degrades gracefully — the safest backend to make HA. Spread across
nodes + PDB.

### 2b. PostgreSQL HA — highest value (holds all durable state)
No PG operator in-cluster today (no CNPG/Crunchy/Zalando CRDs). Two paths:
- **Managed HA Postgres / DBaaS** (preferred long-term) — out-of-band platform
  request; lead time, may not exist for Astra.
- **Self-service in-cluster operator — CloudNativePG** (recommended now): 3-node
  cluster (primary + 2 replicas), automated failover, `-rw`/`-ro` services, optional
  PgBouncer pooling. Council uses the `-rw` service (writes need primary).
  **Synchronous replication** → RPO 0 for the ledger/accounts. Block storage per
  replica + Portworx replication factor ≥ 2.
- **Council connection:** verify the psycopg pool reconnects when the `-rw` endpoint
  fails over. Ledger atomic-sequencing + workflow row-locks carry over (committed
  state is on the promoted primary).

### 2c. Vault HA
Single-node file-storage Vault with **manual unseal** is not HA (a restart needs
unseal). Two paths:
- **Migrate transit to the central it-fusion Vault** (preferred — it is already HA):
  move the `transit` mount + keys (`nomos-council`, `nomos-executor`) + the
  kubernetes-auth role; removes in-cluster Vault entirely. Needs a Vault-admin request.
- **3-node in-cluster Vault with Raft** integrated storage (consensus + auto-failover)
  + **auto-unseal** (transit-auto-unseal from another Vault, or cloud KMS) so node
  restarts don't require manual unseal. Transit keys replicate across the Raft cluster.

---

## Sequencing & dependencies
1. **NOMOS-5b** (Tier 1) — unblocks multi-replica council → app-tier HA + scale-out.
   Highest leverage; runs on today's backends. **Do first.**
2. **Redis HA** (2a) — quick, low-risk, chart-supported.
3. **PostgreSQL HA** (2b) — biggest lift, highest value; the DBaaS-vs-operator decision
   gates the timeline.
4. **Vault HA** (2c) — central-Vault migration or Raft + auto-unseal.

**Full HA = Tier 1 (N replicas) + all of Tier 2 + node/zone spread + PDBs.**

## Cross-cutting
- Node/zone anti-affinity + `topologySpreadConstraints` for the council and every
  backend StatefulSet (a node loss must not remove a quorum).
- `PodDisruptionBudget` on all components (survive drains/upgrades).
- Portworx replication factor ≥ 2 on the block PVCs (per-volume storage HA).
- Confirm the route/monitor load-balances across council replicas and that MCP session
  handling tolerates it once NOMOS-5b makes session state restorable (else add session
  affinity by `mcp-session-id`/`intent_id`).
- All changes are GitOps via ArgoCD; note the council Deployment apply-skip quirk
  (template changes need a force-resync).

## Key decisions
1. PG HA: managed DBaaS (platform request) vs self-hosted CloudNativePG?
2. PG replication: synchronous (RPO 0, higher latency) vs async (small RPO)?
3. Vault HA: migrate to central it-fusion Vault (cleanest; needs Vault-admin) vs
   in-cluster Raft + auto-unseal?

## Validation (chaos drills, per tier)
- Kill a council pod mid-authorization → workflow resumes elsewhere, quorum completes,
  ledger contiguous.
- `oc delete` the PG primary → failover < RTO, council reconnects, no lost committed
  entries (RPO 0 with sync replication).
- Kill a redis node → sentinel promotes; nonces/rate degrade gracefully; no durable loss.
- Kill a Vault node → Raft re-elects (or central Vault unaffected); signing continues.
- Cordon + drain a node → PDBs hold, spread keeps quorum, no outage.
