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

# Nomos QA Execution Report

Verification pass executed against `docs/qa-test-plan.md` (MTP) following
`docs/qa-execution-runbook.md`. Both deployment targets were taken from a cold
start through the full protocol.

| | |
|---|---|
| Branch | `feat/bespoke-council-tools` |
| Final commit | `7d3eece` |
| Targets | Kind (E2, PostgreSQL) and Docker Compose (E3, Redis + Sentinel) |
| Execution | Clean-room: every measurement inside a disposable `nomos-qa-runner:local` container |
| Result | **Both targets green end to end.** 11 product defects found and fixed, each with a regression test that fails on its parent commit |

---

## 1. Final results

### Kind (E2) — commit `06cf686`, protocol run P4

| Stage | Result |
|---|---|
| S0 preflight + inference key gate | PASS — `HTTP 200 -> 'ok'`, clean tree |
| S1 static | ruff clean · 231 files formatted · mypy clean (89 files) · bandit "No issues identified" · OPA `PASS: 18/18` |
| S1 host suites | **1162 passed, 10 skipped** |
| S5 deploy | `EXIT=0`, all 10 deployments ready |
| S6.1 credentials | account provisioned and enabled in the `accounts` table |
| S6.2 unauthenticated surface | 6/6 correct · **0 council 401s** |
| S6.3 live suites | **9 passed, 1 skipped** |
| S6 validation ladder | **5/5** |
| S6.4 register wrapper | `EXIT=0`, `AGENT_TOKEN=` contract preserved, account authenticates |
| Helm render checks | 7 passed |

### Docker Compose (E3) — commit `7d3eece`, protocol run P7

| Stage | Result |
|---|---|
| S0 preflight + key gate | PASS — clean tree at `7d3eece` |
| S1 static + host suites | ruff/mypy/OPA clean · **1170 passed, 10 skipped** |
| S2 deploy | `EXIT=0`, 13/13 containers healthy, `.env` mode `600` |
| S2 health | monitor self **and** upstream council ok; executor reports contract `v1` + key fingerprint |
| S3.1 credentials | `agent-developer` present in Redis under `nomos:` |
| S3.2 unauthenticated surface | 6/6 correct · **0 council 401s** |
| S3.3 live suites | **9 passed, 1 skipped** — identical to Kind |
| S3 validation ladder | **5/5** |
| S3 register wrapper | `EXIT=0`, contract preserved, account authenticates |

The 10 S1 skips are the live suites, which require a deployed target and cannot
run at S1. The 1 live-suite skip is a multi-council case gated on a second
council profile.

### The ladder, on both targets

| Rung | Kind | Compose | Evidence |
|---|---|---|---|
| L5a authorize mints writ | PASS | PASS | `status=approved` |
| L5b writ verifies and executes | PASS | PASS | `exit_code=0` |
| L6 replay rejected | PASS | PASS | `Nonce already claimed (replay detected)` |
| L7 forged writ rejected | PASS | PASS | `Signature verification failed` |
| L8 ledger chain valid | PASS | PASS | contiguous to head, no broken links |

The end-to-end leg is proven by a real side effect, not a status code: a
25-byte marker file with the expected contents inside the openshell sandbox on
each target (`nomos-qa-ladder-dc209456.txt` on Kind,
`nomos-qa-ladder-ccacf1a2.txt` on Compose) — council → OPA → signed writ →
sandboxed execution.

---

## 2. Defects found and fixed

All 11 were found by executing the protocol, not by reading code. Each has a
regression test that fails on its parent commit.

| Sev | Defect | Commit |
|---|---|---|
| 1 | Council image missing the `openai` extra → every tool call failed | `02f0b8e` |
| 2 | Post-deploy sweep restarted Vault, wiping its in-memory bootstrap | `ce2ac59` |
| 2 | `kubectl exec` missing `-i` → the account heredoc had **never** run | `558dadb` |
| 2 | NOMOS-2: accounts CLI unusable under PostgreSQL | `b34cdcc` |
| 2 | Chart never gave `council-grpc` its proxy token → every gRPC call 401'd | `4db09fa` |
| 2 | Deploy exited 0 with an empty accounts table | `cbba708` |
| 2 | `seed_accounts.py` wrote no integrity HMAC → every seeded account failed auth | `aa4f043` |
| 2 | Compose cold start killed by its own healthcheck budget | `5acd66e` |
| 2 | Compose agent registration could never succeed (three stacked defects) | `7d3eece` |
| 3 | MCP auth reported internal faults as bad credentials | `6e0ca18` |
| 3 | Demo port-forward evaluated before `WITH_DEMO` was defined | `f51c8c2` |

### The two most instructive

**Compose cold start (`5acd66e`).** A fresh Compose deploy failed
deterministically with `dependency failed to start: container deploy-redis-1 is
unhealthy`. Nothing was wrong with Redis. Each redis node probes all three
sentinels before exec'ing `redis-server`, but on a cold start those sentinels
cannot be up — they depend on these very nodes reporting healthy. Every hostname
therefore failed to resolve at ~20s per lookup:

```
container started      19:34:03.8
redis-server started   19:35:04.0     (+60.2s, all three nodes within 32ms)
```

against a healthcheck budget of `start_period 5s + retries 5 × interval 10s =
55s`. Docker marked all three unhealthy at ~45s and compose aborted before
`redis-server` had started at all. Bounding each probe at 5s takes cold start
from 60s to 17s.

**Compose registration (`7d3eece`).** Three independent defects, each hidden by
the next:

1. `FALLBACK_REDIS_URL` embedded the dev password, so the URL password was never
   empty and `COUNCIL_REDIS_PASSWORD` was never consulted — every invocation
   against a real deployment authenticated with the dev password. Under the
   default sentinel topology this surfaced only as `MasterNotFoundError`, which
   names master discovery and never mentions credentials.
2. `dc()` hard-coded the postgres overlay and project `nomos`, while
   `deploy_stack.sh` deploys the sentinel topology under project `deploy` — so
   the helper addressed a backend that was not running in a project that did not
   exist.
3. The command substitution captured output with `2>/dev/null`, so all three
   failures produced an identical bare exit 1 with **no output whatsoever**.

Fixing (3) is what made (1) and (2) findable. A wrapper that hides its stderr
converts every distinct failure into the same silent one.

---

## 3. Corrections to the test procedure itself

Executing the runbook exposed defects **in the runbook**. These matter as much
as the product defects: each one would have produced a false pass.

| Correction | Consequence if uncorrected |
|---|---|
| Executor calls require `X-Agent-ID` matching the writ's `target_agent_id` | Without it every submission fails identically, so **replay and forgery tests report PASS for entirely the wrong reason**. The runbook now requires asserting the rejection *reason*, never just the rejection |
| S1 after a Compose deploy silently loses ~121 tests | `redis_test_support` falls back to `deploy/.env`, whose generated password no longer matches the QA Redis. Observed `1041 passed, 132 skipped` against a true baseline of `1163, 10` — **exit code still 0** |
| Monitor `/mcp` is closed on Kind, not open | The runbook told the tester to expect success and record an open state that does not exist there |
| The Origin/Host check was unreachable as written | The 401 fires first, so the property under test never executed. Must be sent with a valid proxy token |
| `get_ledger_status` is not proxied | Chain validity is verified by paging `council_ledger_page` and walking `previous_hash` against the predecessor's `content_hash` |
| Kind defaults to PostgreSQL, not file/Redis | `kind-values.yaml` sets `storage.mode: service` |

`scripts/qa/ladder_probe.py` makes the ladder executable rather than prose, so
these failure modes cannot silently return.

Two behaviours are now documented as expected, so a tester does not log them as
defects: the executor's `tools.allowed` list is a **second** gate applied after
council approval (basename-matched, so it composes correctly with OPA's
absolute-path rule), and `council_check_status` does not authenticate the agent.

---

## 4. Cross-target comparison

The runbook calls this the deliverable of the T3 stage.

| Property | Kind | Compose |
|---|---|---|
| Storage backend | PostgreSQL (`storage.mode: service`) | Redis + Sentinel (3 nodes, 3 sentinels) |
| Monitor `/mcp` | **Closed** — 401 without a bearer token | **Open** — 200 unauthenticated |
| Live suites | 9 passed, 1 skipped | 9 passed, 1 skipped |
| Validation ladder | 5/5 | 5/5 |
| Origin/Host validation | Absent (confirmed-open) | Absent (confirmed-open) |

Running both gives genuine dual-backend coverage of the same provisioning code
path, which is what caught NOMOS-2 and the sentinel password defect.

### Open item for the owner: divergent `/mcp` auth posture

The two supported targets ship **different authentication postures on the same
endpoint by default**. On Compose an unauthenticated caller can enumerate all 18
tools and read the ledger. Bounded precisely — it **cannot** authorize commands:

```
anon can enumerate 18 tools
anon ledger read: entries=4 head=3
anon authorize: status=error err=Authentication failed or missing token (require_auth is enabled)
```

So this is information disclosure (audit history, council topology, tool
surface), not privilege escalation. Compose does emit the documented warning —

```
WARNING council-monitor: monitor /mcp proxy is UNAUTHENTICATED;
set MONITOR_MCP_PROXY_TOKEN to require Bearer auth on the proxy
```

— which is exactly the "documented-but-open" dev posture MTP R-04 describes, so
it is **not filed as a defect**. It is raised because the divergence is
undocumented: Kind is stricter than the plan records, and an operator who
hardens Kind may reasonably assume Compose matches. Setting
`MONITOR_MCP_PROXY_TOKEN` in `deploy/.env` closes it.

---

## 5. What was not verified

Stated plainly, since an unqualified "green" would overclaim.

| Not verified | Why |
|---|---|
| T4 adjudication (MTP §6.3) | Operator-gated on LLM spend; never authorized. The statistical quality of council decisions over a corpus is **unmeasured** |
| S8 performance smoke | Optional; not run. No throughput or latency baseline |
| NetworkPolicy perimeter isolation (R-14) | Chart default `enabled: false`; not verifiable in the default profile |
| Chaos and mutation suites (MTP §8.2) | Do not exist yet |
| MTP G-01 / G-02 / G-03 | CI still runs only the `unit` profile; orphaned test files remain; the Rego suite still does not execute in CI. **Untouched by this pass** |
| Failover behaviour | Sentinel topology was deployed and healthy, but no master failover was induced |

G-01 is the one worth acting on. Every defect in §2 was reachable by a test that
existed but never ran in CI. The fixes are committed; the reason they were
needed is not yet fixed.

---

## 6. Evidence

| Artifact | Location |
|---|---|
| Kind bundle (container logs, pods, accounts, stage logs) | `nomos-qa-artifacts/evidence-kind-p4/` (17 files) |
| Compose bundle | `nomos-qa-artifacts/evidence-compose-p7/` (17 files) |
| Per-stage logs | `nomos-qa-artifacts/logs/p{4,6,7}_*.log` |
| Ladder harness | `scripts/qa/ladder_probe.py` |

Both bundles pin the commit under test. No secrets were printed at any point;
credentials were sourced and tested for presence, never echoed.
