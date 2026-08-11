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

# Nomos Master Test Plan

| Field | Value |
|---|---|
| Document | Master Test Plan (MTP) |
| Version | 1.0 — draft for review |
| Structure | ISO/IEC/IEEE 29119-3 §7 (Test Plan) |
| System under test | Nomos authorization kernel |
| Baseline | `8e653f3`, branch `feat/bespoke-council-tools` |
| Scale | ~19.6 kLOC product Python (88 modules); ~21.4 kLOC test Python (78 files, 1056 tests) |

This plan defines **how testing is organized, resourced, and gated**. It does not
enumerate test cases. Case-level detail belongs in the Test Design Specifications
listed in §10, which the QA team writes per area against this plan.

### How the QA team uses this document

| If you are… | Start at |
|---|---|
| Joining the team | §5.1 onboarding checklist, then §6 execution procedures |
| Deciding what to test and how deeply | §2.4 coverage commitments, §3 risk register |
| Writing a Test Design Specification | §2.4 for your area's depth and scope, §10 for the deliverable format |
| Running a test cycle | §6 procedures, §8 criteria |
| Deciding whether to ship | §8.2 exit gate |
| Triaging a defect | §12 |
| Planning the next quarter | §13 phasing |

**Before executing anything, complete §4.3.** The plan assumes a working pipeline;
at the time of writing there is not one, and §13 Phase 0 must land first.

---

## 1. Scope and objectives

### 1.1 Testing mission

Nomos exists so that privileged agent actions cannot execute without cryptographic
proof of approval. Testing is therefore organized around one question:

> Can any path cause an unauthorized command to execute, or an authorized command
> to execute with different bytes, in a different place, more than once, or after
> its authorization expired?

Feature correctness, throughput, and dashboards are tested, but they are subordinate
to that question and are gated less strictly.

### 1.2 Quality objectives

| ID | Objective | How measured |
|---|---|---|
| QO-1 | No unauthorized execution | Zero P1 escapes across adversarial corpora; deterministic bypass suites at 100% |
| QO-2 | Writ integrity end to end | Tamper/replay/expiry matrix at 100% |
| QO-3 | Audit completeness | Every decision yields a chain-valid ledger entry; replay reproduces the record |
| QO-4 | Legitimate work is not blocked | False-deny rate within gate on the valid-operations corpus |
| QO-5 | Deterministic layers are deterministic | Identical input → identical OPA/policy/permission outcome |
| QO-6 | Availability under load | Published latency and error-budget targets met at declared concurrency |
| QO-7 | Safe by default | Non-dev deployment profiles refuse to start insecure; all failure modes fail closed |
| QO-8 | Recoverability | Ledger migration and key rotation provably lossless |

### 1.3 In scope

Core kernel (`nomos/level_0..4`, `auth`, `crypto`); decision surface (`llm`, `opa`,
`policies/`); transports (`mcp`, `nat`, `grpc`); execution (`executors/`,
`trusted_executor_contract`); storage (`store/`, `level_1/ledger`); services
(`monitor/`, `agent_signup/`, `demo/`); deployment (`deploy/`, `scripts/deploy_stack.sh`,
`nomos_runtime` preflight); tooling (`nomos_stress`, `scripts/run_test_suite.py`).

### 1.4 Out of scope

- Third-party LLM provider correctness and availability — tested only at the
  boundary contract; provider SLA is an operational concern.
- `agent_signup/` production-hardening assertions — a development/onboarding
  surface, chart-gated off in service mode.
- Customer-supplied OPA policy content — Nomos tests the evaluation contract,
  fail-closed behavior, and the shipped reference policy only.

---

## 2. Test approach

### 2.1 Test levels

| Tier | Level | Character | Where it runs |
|---|---|---|---|
| T1 | Unit / component | Pure, deterministic, fast | Developer, CI |
| T2 | Integration / contract | In-process services, mocked LLM, real Redis/PostgreSQL | Developer, CI |
| T3 | System / end-to-end | Deployed stack, real transports, real executor | Kind and Compose |
| T4 | Adjudication quality | Real LLM backends, statistical | Adjudication lab |

**T4 requires a different method and is the main methodological commitment of this
plan.** Council decisions are stochastic. A per-case boolean assertion on an LLM
verdict is a flaky test that will be muted within a month. T4 assertions are
therefore *rates over versioned corpora*, sampled with repeat runs, gated on
thresholds, and compared against a stored baseline to detect drift. Individual
corpus cases are data, not tests.

### 2.2 Test types and where each is applied

| Type | Applied to | Rationale |
|---|---|---|
| Functional | All levels | Baseline correctness |
| Contract | MCP tools, gRPC, trusted-executor contract, OPA decision shape | These are versioned interfaces with external consumers |
| Property-based | Canonical serialization, path normalization, shell tokenization, hash chain | Invariants matter more than examples on these surfaces |
| Fuzz | Command strings, writ blobs, JSON-RPC bodies, packaged-file paths | This is where enforcement bypasses actually live |
| Concurrency | Ledger append, nonce claim, workflow store, catalog reload | Known defect class; see §4 |
| Security | Per trust boundary (STRIDE), adversarial corpora, SAST, dependency and image scanning | Product is a security control |
| Performance | Council throughput, latency, saturation, soak | Published targets |
| Resilience | Redis/PostgreSQL/OPA/LLM outage, pod kill, partition, clock skew | Fail-closed is a core claim |
| Deployment | Compose and Helm render, preflight, bring-up | Insecure defaults are a live risk |
| Migration | JSONL→PostgreSQL ledger, key rotation, chart/image skew | Irreversible if wrong |
| Regression | Every fixed defect, mandatory | See §12 |

### 2.3 Techniques not currently used that this plan adopts

Property-based testing, fuzzing, mutation testing on security-critical modules, and
statistical scoring of LLM adjudication. These are adopted because example-based
tests have a poor record against tokenizer evasion and prompt manipulation, which
are two of the three highest-scored risks in §3.

### 2.4 Coverage commitments by area

Depth is assigned by risk, not uniformly. Each area's cases are specified in its
Test Design Specification (§10).

| Area | Risk | Levels | Depth | TDS |
|---|---|---|---|---|
| Executor enforcement | R-01 | T1, T2, T3 | Exhaustive + property + fuzz + mutation | TDS-EXEC |
| LLM adjudication | R-02 | T4 | Scored corpora, repeat-sampled, drift-gated | TDS-ADJ |
| Writ lifecycle and crypto | R-02, R-06, R-10 | T1, T2, T3 | Exhaustive; table-driven over every signed field | TDS-WRIT |
| Ledger integrity and concurrency | R-05, R-11 | T1, T2, T3 | Exhaustive + concurrency fan-out, both backends | TDS-LDG |
| Authentication and permissions | R-07, R-13 | T1, T2 | Exhaustive + timing analysis | TDS-AUTH |
| Perimeter (monitor, proxy, origin) | R-04 | T2, T3 | Exhaustive; deployed-only for the security assertions | TDS-PERIM |
| Keys, signing, evidence | R-03 | T1, T2, T3 | Exhaustive incl. rotation and Vault path | TDS-KEY |
| OPA filter | R-08 | T1, T2 | Exhaustive incl. Rego unit tests | TDS-OPA |
| Multi-council and workflow HA | R-09 | T2, T3 | Representative + multi-replica | TDS-MC |
| Transports (MCP, gRPC) | — | T2, T3 | Contract + schema-drift detection | TDS-XPORT |
| Storage and migration | R-15 | T2, T3 | Exhaustive on migration; representative elsewhere | TDS-STOR |
| Deployment and config | R-14 | T2, T3 | Render matrix + clean-machine bring-up | TDS-DEP |
| Performance and resilience | R-16 | T3 | Profile-driven, trend-gated | TDS-PERF |
| Audit and evidence bundles | — | T3 | Representative, offline-verifiable | TDS-AUD |

**Environment fidelity rule:** any assertion of a *security* property must be
demonstrated at T3 against real process boundaries. In-process tests prove logic;
they do not prove the deployed topology enforces it.

---

## 3. Risk-based prioritization

Score = Impact × Likelihood (1–5 each). Score ≥ 15 receives exhaustive coverage,
independent verification, and blocks release.

| ID | Risk | I | L | Score |
|---|---|---|---|---|
| R-01 | Executor enforcement bypass via shell parsing | 5 | 4 | **20** |
| R-02 | Council approves a genuinely destructive action | 5 | 4 | **20** |
| R-05 | Ledger append race / chain fork under concurrency (known defect) | 4 | 4 | **16** |
| R-03 | Signing authority compromise or misuse | 5 | 3 | **15** |
| R-04 | Unauthenticated `/mcp` proxy, or DNS rebinding | 5 | 3 | **15** |
| R-06 | Writ replay across executors or nonce-store failure | 5 | 3 | **15** |
| R-07 | Permission categorizer under-classifies a destructive command | 5 | 3 | **15** |
| R-09 | Multi-replica workflow loss (in-memory workflow map) | 3 | 4 | 12 |
| R-10 | Clock skew voids or extends writ validity | 4 | 3 | 12 |
| R-11 | Ledger deletion or rollback by a storage-level actor | 4 | 3 | 12 |
| R-13 | Auth brute force (rate limit keyed per token hash) | 4 | 3 | 12 |
| R-14 | Deployment ships insecure defaults | 4 | 3 | 12 |
| R-16 | Council overload / denial of service | 3 | 4 | 12 |
| R-17 | Legitimate operations falsely denied; operators route around Nomos | 4 | 3 | 12 |
| R-08 | OPA fails open | 5 | 2 | 10 |
| R-12 | Packaged-file path escape or hash-mismatch handling | 5 | 2 | 10 |
| R-15 | Storage cutover loses or forks the live ledger | 5 | 2 | 10 |
| R-18 | Vulnerable dependency or base image reaches production | 4 | 3 | 12 |

---

## 4. Conditions at handover

This section describes the state the QA team inherits. It is the evidence base for
the phasing in §13. **Re-verify §4.2 against the current commit before planning a
cycle** — some items may have been fixed since this plan was written, and the
phasing changes if so.

### 4.1 What already exists and can be built on

Levelized code with matching test layout; a real cross-boundary tamper suite; a
substantial adversarial corpus already drafted as scripts; a profile-based suite
runner with HTML/JUnit reporting; a load console; a written deployment validation
ladder; a security review with an explicit residual-risk list; and a frank
engineering record of known HA and storage defects.

### 4.2 Gaps verified on the baseline commit

Each row was reproduced by running the suite, not inferred from reading. The
"reproduce with" column tells the QA team how to confirm the item is still open.

| # | Finding | Sev | Reproduce with |
|---|---|---|---|
| G-15 | **CI's unit profile takes ~72 min.** Two tests in `test_deploy_stack_script.py` stub `kubectl_kind` as a `printf`; `wait_kind_rollouts` word-splits the stub's echoed arguments into 9 phantom deployments, each waiting the full 240 s `WAIT_TIMEOUT_SECONDS`. No `timeout=` bounds the subprocess. The file's other 27 tests run in 0.99 s. Also exposes a latent unquoted-expansion bug in the deploy script. | Critical | `pytest tests/test_deploy_stack_script.py -k main_kind_existing_secret_only` — does not complete |
| G-16 | **CI's unit profile is red** on committed state: two stale string assertions against `kind-values.yaml` and `deploy_stack.sh` invalidated by the rollout-wait rewrite. | Critical | `pytest tests/test_deploy_stack_script.py -k "not main_kind_existing_secret_only"` |
| G-01 | CI runs only the `unit` profile. Integration, Redis, and all deployed suites never execute in CI. | High | Read `.gitlab-ci.yml` `unit_tests:` |
| G-02 | 27 of 78 test files belong to no suite profile — incl. ledger concurrency, PostgreSQL ledger and store, workflow durability, chairman pre-auth, bespoke tools, signer provider. | High | Diff `tests/test_*.py` against the paths in `scripts/run_test_suite.py` |
| G-03 | Rego policy tests never execute in CI — they skip without the `opa` binary, which the CI image lacks. | High | `pytest tests/test_opa_policy.py -v` on a host without `opa` |
| G-07 | Skips are silent and unbudgeted. The one test covering the known ledger race skipped entirely. A green run can mean nothing ran. | High | `pytest tests/test_ledger_concurrency.py -v` |
| G-08 | Adversarial corpora are unscored manual scripts — no thresholds, no repeat sampling, no drift baseline, in no profile. | High | Read `scripts/test_jailbreak.py` and `PROFILE_SUITES` |
| G-11 | No property-based or fuzz testing of the shell tokenizer, path resolver, or writ deserializer — the R-01 surface. | High | Absence across `tests/` |
| G-04 | Image build and packaging jobs disabled (`rules: - when: never`). | High | Read `.gitlab-ci.yml` |
| G-05 | No dependency or container vulnerability scanning; SAST and secret detection only. | High | Read `.gitlab-ci.yml` `security` stage |
| G-09 | Lint gate fails on the lockfile-pinned toolchain: 7 `ruff` findings, 1 `mypy` error. | Med | `uv run ruff check . && uv run mypy nomos` |
| G-10 | Auth rate limiting keyed by token hash — each guessed token gets a fresh counter, so it throttles a repeated wrong token, not an attacker. | Med | Read `nomos/auth/authenticator.py` rate-limit path |
| G-17 | Shell scripts tested by exact substring matching — breaks on refactor without indicating defects, passes while behavior is broken. | Med | Read `tests/test_deploy_stack_script.py` |
| G-06 | No Helm chart validation in CI. | Med | Read `.gitlab-ci.yml` |
| G-12 | No clock-skew coverage, though writ validity is wall-clock across process boundaries. | Med | Absence across `tests/` |
| G-13 | No chaos or resilience suite against a real deployment. | Med | Absence across `tests/` |
| G-14 | Coverage baseline stale; floor omits the PostgreSQL modules entirely. | Med | `uv run coverage report`; read `pyproject.toml` omit list |

**Why this drives the phasing:** the pipeline is red and effectively hanging, and
the pre-commit gate is unusable for the same reason. Until that is fixed, no other
quality gate in this plan has force.

### 4.3 First actions on taking over

Do these before planning a cycle. Expect roughly one week.

1. Re-run §4.2 reproduction commands; mark each item still open, fixed, or changed.
2. Confirm Phase 0 is scheduled with an engineering owner. Do not begin building
   suites against a pipeline that cannot complete a run.
3. Stand up E0 for every team member per §5.1, and confirm each person can execute
   all four tiers in §6.
4. Request the E1 infrastructure changes (service containers, `opa`, `helm`,
   `kubeconform`, Docker-in-Docker) — these have lead time and gate Phase 1.
5. Open the E4 API budget request — it gates Phase 3.
6. Take a coverage and wall-clock baseline once the suite completes, and record it
   as the trend origin for §11.

### 4.3a Defects found by the first execution of this plan

The first Kind pass (2026-08-10, against `upstream/main` @ `fbf3ac1`) found six
defects, all in the primary `./scripts/deploy_stack.sh` path and none reachable
by any suite that runs today. Fixes and regression tests are on
`fix/mcp-auth-error-classification`.

| # | Defect | Sev | Why nothing caught it |
|---|---|---|---|
| D-1 | Post-deploy sweep restarted `nomos-vault`, discarding the in-memory bootstrap that enables its Kubernetes auth. Council and executor then blocked forever on `auth/kubernetes/login` 403. | 2 | No deployed-stack test exists |
| D-2 | Council image omitted the `openai` extra while the shipped config selects `provider: nv_inference`, which imports `openai`. **Every council tool call failed.** | 1 | Third dependency-drift instance (with NOMOS-1, `nvidia-nat-mcp`); nothing validates image deps against config |
| D-3 | Kind port-forwarded a demo the chart never deploys, failing the deploy with all deployments Ready. | 3 | Deploy exit code never asserted |
| D-4 | `agent_accounts.py` refuses all account ops under postgres (the Kind default), and the deploy reported "account already existed" — **exiting 0 with an empty accounts table and no usable credentials**. | 2 | Deploy success never verified against a working credential |
| D-5 | Helm chart never gave `council-grpc` the `COUNCIL_MCP_PROXY_TOKEN`, so **every gRPC call 401'd** while the pod reported Ready (bare TCP probe). | 2 | gRPC live suite never runs; readiness probe cannot see it |
| D-6 | Live MCP suites have no bearer-token support, so the `MONITOR_MCP_PROXY_TOKEN` remediation silently made them unable to reach the council. | 3 | Live suites are opt-in and skip silently (G-07) |

**The pattern is the point.** Every one of these sits in the documented
one-command bring-up, and every one was invisible because the deployed tier is
opt-in, skip-on-missing-env, and absent from CI. Three (D-1, D-2, D-5) produced a
stack that reported healthy while being functionally dead — readiness probes are
TCP or process-level and cannot see a council that answers every tool call with
an error. **This is the strongest available argument for MTP §8.2's requirement
that deployed smoke pass before release**, and for adding a post-deploy assertion
that one real authorize call succeeds end to end.

### 4.4 Known defects requiring regression coverage

Drawn from `docs/proposals/08_pg_ha_cutover_outstanding_issues.md` and the nSpect
response. Each is owed a regression test that fails on the pre-fix commit.

File-ledger append race under concurrency; in-memory workflow map replica-pinning;
`psycopg` absent from production images; agent-signup and accounts CLI failing under
PostgreSQL storage; executor not consuming signer environment; dynamic council
mutation disabled in PostgreSQL mode; `/mcp` bearer token blank under the default
`dev` profile; absent Host/Origin validation; signing key not KMS/HSM isolated;
ledger storage not WORM.

---

## 5. Test environments

| Env | Purpose | Composition | Owner |
|---|---|---|---|
| E0 | Developer, T1–T2 | Local Redis, PostgreSQL, `opa`, `helm` | Dev |
| E1 | CI, T1–T2 gated | Service containers for Redis and PostgreSQL; `opa`, `helm`, `kubeconform`; Docker-in-Docker | CI |
| E2 | T3 Kubernetes path | Kind via `scripts/deploy_stack.sh`, incl. a NetworkPolicy-enabled variant | QA |
| E3 | T3 Compose path | Compose target plus the `docker exec` smoke scripts | QA |
| E4 | T4 adjudication lab | Deployed council, real LLM backends, isolated keys, per-run cost budget | QA + ML |
| E5 | Performance and soak | Cluster sized to the supported profile; `nomos_stress` driver | QA |
| E6 | Release acceptance and migration rehearsal | Production-shaped: managed PostgreSQL, Vault transit signer, NetworkPolicy, ingress | Release |

**Infrastructure this plan requires and that does not exist today:** CI service
containers and binaries (E1), a NetworkPolicy-enabled Kind variant (E2), the
adjudication lab and its API budget (E4), and managed PostgreSQL plus a Vault
transit mount for E6. E6's dependencies are tracked as infrastructure blockers in
Proposal 08 and are outside QA's control — raise them early.

### 5.0 A validated inference credential is an environment prerequisite

**The QA team must hold its own inference key, and must verify it is live before
every deployed run.** This is not a formality:

- T3 and all of T4 depend on real council adjudication. An expired key does not
  degrade them gracefully — the deploy succeeds, every pod reports healthy, and
  only the adjudication-dependent tests fail. That presents as several product
  defects rather than one dead credential.
- Verification is a single API call and costs seconds. It must run **before** the
  image build, not after, or a ~20-minute build is spent to learn nothing. The
  exact probe and its result table are in the runbook (`docs/qa-execution-runbook.md`
  §2.1), which makes it a mandatory S0 gate.
- The key is proxy-issued and rotates. Treat expiry as routine, not exceptional:
  a run that has been green for weeks can fail on credential rotation alone.
- Keys can appear under several names across `.env` files. Confirm which variable
  the deploy actually consumes rather than assuming the files agree, and use the
  `Key Hash (Token)` echoed in a 401 body to prove whether two names are the same
  credential.

Observed 2026-08-11: a full protocol run reached S6.3 before the stale key
surfaced, and three failures initially looked like product defects. The gate above
exists so that cannot recur.

### 5.1 Onboarding checklist (E0)

A new QA engineer is ready when every step below succeeds.

1. `uv sync --extra all`
2. Local Redis reachable; tests use db 15 by default. Override with
   `NOMOS_TEST_REDIS_HOST` / `_PORT` / `_DB`.
3. Local PostgreSQL reachable; set `NOMOS_TEST_DATABASE_URL` to a disposable
   database. Tests truncate the `nomos` tables between runs.
4. `opa` and `helm` on `PATH` — without them, whole suites skip silently (G-03, G-07).
5. `docker` available for T3.
6. Run each tier in §6 once and confirm the expected artifacts appear.
7. Confirm you can read the exit criteria in §8.2 and say which suites feed each row.

**Verification that the environment is real, not silently degraded:** run the suite
and check the skip count. A skip total above the budget in §11 means a dependency is
missing and your results do not mean what they appear to mean.

---

## 6. Test execution procedures

Commands are the current repository interface. Where a procedure does not exist yet,
the phase that builds it is named.

### 6.1 T1/T2 — local and CI suites

```bash
# Everything, with the coverage floor (what the pre-commit hook runs)
uv run pytest tests -q --cov --cov-report=term-missing --cov-fail-under=77

# By profile, with HTML + JUnit artifacts under test-reports/
./scripts/run_test_suite.py --profile unit
./scripts/run_test_suite.py --profile integration
./scripts/run_test_suite.py --profile all --report test-reports/nomos-test-report.html

# Rego policy unit tests (requires the opa binary)
opa test policies --fail-on-empty

# Static analysis and SAST
uv run ruff check . && uv run ruff format --check . && uv run mypy nomos
uv run bandit -c pyproject.toml -r nomos executors trusted_executor_contract monitor demo
```

Until Phase 0 lands, add `-k "not main_kind_existing_secret_only"` to any run that
includes `tests/test_deploy_stack_script.py`, or the run will not complete (G-15).

**On failure:** record the failing suite and case, confirm it reproduces from a
clean state, then triage per §12. Do not re-run to green.

### 6.2 T3 — deployed system

```bash
# Kind (default target)
NVIDIA_API_KEY=... ./scripts/deploy_stack.sh

# Docker Compose, which also enables the container-inspecting smoke scripts
NVIDIA_API_KEY=... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

Then follow the validation ladder in `docs/council-mcp-validation.md` for health
checks, credential loading, and the deployed suites:

```bash
./scripts/run_test_suite.py --profile smoke
./scripts/run_test_suite.py --profile live
```

Deployed suites require `NOMOS_MCP_LIVE_URL`, `NOMOS_GRPC_SMOKE_ADDR`,
`NOMOS_AGENT_ID`, and `NOMOS_AUTH_TOKEN`. They **skip** when those are absent, so
confirm they executed rather than trusting a green result.

The NetworkPolicy-enabled Kind variant required for the perimeter assertions does
not exist yet — Phase 5.

### 6.3 T4 — adjudication

The scored runner is built in Phase 3. Until then the corpora exist only as manual
scripts and produce no gate:

```bash
uv run python scripts/test_jailbreak.py
uv run python scripts/test_social_engineering.py
uv run python scripts/test_script_obfuscation.py
uv run python scripts/test_benign_bypass.py
uv run python scripts/test_valid_operations.py
```

Target procedure after Phase 3: run each corpus against E4 at the configured repeat
count, emit a scorecard, diff against the stored baseline, and attach the diff to
the change request. Escapes are filed per §12 rule 3 and the case is promoted into
the permanent corpus.

### 6.4 Performance and soak

```bash
uv run --with fastapi --with 'uvicorn[standard]' --with httpx --with mcp \
  uvicorn nomos_stress.app.main:app --host 127.0.0.1 --port 9910
```

Drive the profiles defined in TDS-PERF against E5. **Every load run ends with a
ledger validation** — throughput results are void if the audit chain forked, which
is a known defect class (R-05).

### 6.5 Recording results

| Artifact | Location |
|---|---|
| HTML and JUnit reports | `test-reports/` |
| Coverage | `coverage.xml` (Cobertura) |
| Adjudication scorecards | per TDS-ADJ, from Phase 3 |
| Performance trends | per TDS-PERF, from Phase 5 |
| Release evidence bundle | assembled by the release manager per §10 |

---

## 7. Test data strategy

Corpora are versioned, reviewed artifacts under `tests/corpora/`, not code. Each
case carries `id`, `category`, `expected_outcome` (`approve` / `deny` / `either`),
`severity_if_wrong`, `rationale`, and `introduced_in`.

`expected_outcome: either` exists deliberately — some requests are genuine judgment
calls and must not be scored as escapes. Without it, the T4 gate becomes noise and
gets muted.

| Corpus | Purpose |
|---|---|
| Valid operations | False-deny measurement; migrated from the existing script |
| Jailbreak | Prompt injection, role override, delimiter and context attacks |
| Social engineering | Authority, urgency, blame, false prior-approval claims |
| Obfuscation | Encoding, indirection, hijacking, homoglyphs |
| Benign bypass | Legitimate-looking with a subtle catastrophic mode; quorum bait; escalation chains |
| Enforcement bypass | Executor tokenizer and path evasion; no LLM involved |
| Malformed inputs | Fuzzer-generated; seeds committed |
| Ledger fixture | ≥ 50 000-entry chain for migration and replay |

**Handling:** corpora contain no real credentials, hosts, or customer data, enforced
by secret scanning. T4 runs transmit corpus text to external providers, so corpora
are treated as publishable content — which is also the mitigation for the open
governance item on prompt egress.

---

## 8. Entry, exit, suspension, and resumption criteria

### 8.1 Entry

Static analysis clean; unit and integration suites passing locally; coverage at or
above floor; images build and pass scan; deployment manifests render.

### 8.2 Exit (release gate)

| Gate | Threshold |
|---|---|
| T1/T2 | 100% pass; zero unexplained skips |
| T3 deployed smoke | 100% on both Kind and Compose |
| T4 escapes | Zero P1; P2 within threshold across ≥ 3 independent runs |
| T4 false-deny | Within threshold on the valid-operations corpus |
| Open defects | Zero Sev-1/Sev-2; Sev-3 only with documented workaround |
| Coverage | At or above floor; full branch coverage on enforcer, writ, and auth modules |
| Security scan | Zero unfixed Critical/High in images and dependencies |
| Performance | Meets published targets at declared supported concurrency |
| Documentation | Deployment and validation docs verified by a clean-machine run |

Numeric thresholds for the T4 rows are set in TDS-ADJ once a baseline exists; they
are deliberately not fixed here before measurement.

### 8.3 Suspension

Testing suspends when: the pipeline cannot complete a run; a Sev-1 defect is open
against the item under test; or a required environment is unavailable such that
results would be misleading rather than merely incomplete.

### 8.4 Resumption

Resume when the blocking condition is cleared and the affected suites have been
re-run from a clean state — not from cached results.

---

## 9. Organization and responsibilities

| Role | Owns |
|---|---|
| QA lead | This plan, traceability, release-gate sign-off |
| Test automation engineer | Suite runner, CI stages, property/fuzz infrastructure |
| Security QA | Trust-boundary coverage, corpora curation, escape analysis |
| Adjudication QA | T4 methodology, scorecards, drift baselines |
| Performance engineer | Load profiles, trend gates |
| Development team | Unit and integration tests with the change; regression test per defect |
| Release manager | Exit-criteria verification, evidence archival |

**Independence:** T4 corpora and the enforcement-bypass corpus are owned by Security
QA, not by the engineers who implement the controls they probe.

---

## 10. Deliverables

| Deliverable | Owner | Cadence |
|---|---|---|
| This Master Test Plan | QA lead | Per release; reviewed quarterly |
| Test Design Specifications (TDS-EXEC, TDS-ADJ, TDS-WRIT, TDS-LDG, TDS-AUTH, TDS-PERIM, TDS-KEY, TDS-OPA, TDS-MC, TDS-XPORT, TDS-STOR, TDS-DEP, TDS-PERF, TDS-AUD) | Area owners | Per area, before implementation |
| Versioned test corpora | Security QA / Adjudication QA | Continuous; quarterly refresh |
| Automated suites and CI configuration | Automation engineer | Continuous |
| Test execution reports (HTML + JUnit) | Automation | Per run |
| Adjudication scorecards with baseline diff | Adjudication QA | Nightly and per council-config change |
| Performance trend reports | Performance engineer | Nightly and weekly soak |
| Traceability matrix | QA lead | Per release |
| Release evidence bundle | Release manager | Per release |

The test cases themselves are specified in the TDS documents. This plan fixes their
scope, depth, ownership, and gating; it does not enumerate them.

---

## 11. Metrics and reporting

| Metric | Purpose | Gate |
|---|---|---|
| Pass rate by level and area | Release readiness | §8.2 |
| Executed vs. skipped count | Detect silent skips (G-07) | Minimum executed count enforced per CI job |
| Line coverage | Floor | At or above configured floor, raised over time |
| Mutation score on enforcer, writ, and auth modules | Assertion quality | Target set in TDS; line coverage alone is insufficient here |
| T4 escape and false-deny rates | Adjudication quality | §8.2 |
| T4 outcome stability across repeat runs | Flakiness of the decision layer itself | Reported; unstable set published |
| Suite wall-clock | Gate usability — the root cause of G-15 | Full local suite under a published budget |
| Defect escape rate to staging/production | Plan effectiveness | Reviewed per release |
| Mean time to quarantine a flaky test | Suite trust | One working day |

Reporting: per-run HTML/JUnit artifacts; a weekly quality summary; and a
per-release evidence bundle sufficient for an external reviewer to verify the exit
criteria without access to the pipeline.

---

## 12. Defect management

| Severity | Definition | Response |
|---|---|---|
| Sev-1 | Unauthorized execution possible; writ forgery or replay; auth bypass; forgeable audit chain | Stop-ship; fix, regression test, and security review before any release |
| Sev-2 | Enforcement weakened; audit incomplete; fail-open under a plausible fault; migration data loss | Stop-ship for the affected release |
| Sev-3 | Functional defect with a workaround; false denials above gate; performance below target | Scheduled; noted in release notes |
| Sev-4 | Cosmetic, documentation, minor usability | Backlog |

Rules:

1. Every fixed defect ships with a regression test that fails on the pre-fix commit.
   No exception for Sev-1 or Sev-2.
2. Sev-1 defects require a written escape analysis: which suite should have caught
   it, and what was added so the *class* is covered, not the instance.
3. Adjudication escapes are filed against the council configuration, and the failing
   case is promoted into the permanent corpus.
4. Flaky tests are quarantined within one working day and fixed or deleted within
   five. The quarantine list is published in the test report.

---

## 13. Schedule and phasing

Sequenced so the highest-risk gaps close first and each phase is independently
shippable.

| Phase | Focus | Closes | Est. |
|---|---|---|---|
| **0** | **Restore the pipeline.** Fix the two failing assertions; bound the deploy-script subprocesses and the rollout-wait multiplication; add a global per-test timeout so hangs report as failures. | G-15, G-16 | days |
| 1 | Make the pipeline honest. Fix static analysis; put every test file in a profile and add a guard against recurrence; provision CI dependencies and run the integration and Redis profiles; budget skips; re-enable image builds; add dependency and image scanning; refresh the coverage baseline. Profile and parallelize the suite to a usable wall-clock. | G-01..G-09, G-14 | 1–2 wk |
| 2 | Lock the enforcement boundary. Enforcement-bypass corpus, property tests, fuzz targets, full writ tamper matrix, clock-skew characterization. | R-01, R-06, R-10, R-12, G-11, G-12 | 2–3 wk |
| 3 | Score the council. Migrate corpora to versioned data; build the scorecard runner; establish baselines; wire nightly and config-change gates; add indirect-injection and multi-turn escalation cases. | R-02, R-17, G-08 | 2–3 wk |
| 4 | Concurrency, HA, migration. Ledger concurrency against both backends; multi-replica workflow tests; migration rehearsal; image and signer regression tests. | R-05, R-09, R-15 | 3–4 wk |
| 5 | Perimeter, chaos, performance. Monitor auth and origin suite; NetworkPolicy-enabled variant; chaos suite; ratify performance targets and wire trend gates and soak. | R-04, R-14, R-16, G-13 | 2–3 wk |
| 6 | Audit and evidence. Evidence-bundle generation and offline verification; external ledger checkpointing; publish traceability. | R-11 | 1–2 wk |

Phase 0 is a precondition for every subsequent phase, because until the suite
completes, no gate defined in this plan can be enforced.

Ongoing cadence:

| Cadence | Activity |
|---|---|
| Per merge request | Static analysis, T1, T2, security scan, build |
| Per merge to main | Plus T3 on Kind and Compose |
| Nightly | Plus T4, performance baseline and target, fuzz |
| Weekly | Soak; scan review; flaky-test review |
| Per council-config change | Full T4 with scorecard diff attached to the request |
| Per release | Full plan, migration rehearsal on E6, evidence bundle, sign-off |
| Quarterly | Threat-model review; corpus refresh against current public techniques |

---

## 14. Assumptions, dependencies, and plan risks

**Assumptions**

- The open nSpect items are being remediated. Several planned suites are written to
  fail against current behavior and serve as acceptance gates for those fixes; that
  is intentional and should not be read as a broken suite.
- Release acceptance targets PostgreSQL storage with a Vault signer; file and Redis
  modes remain supported for local and development use.
- Supported concurrency is declared per release, and performance targets attach to
  that declaration.

**Dependencies**

- CI runners with service containers and Docker-in-Docker.
- A funded LLM API budget for T4 — the only materially expensive suite. Repeat-count
  and cadence are the cost levers.
- Managed PostgreSQL and a Vault transit mount for E6, both currently listed as
  infrastructure blockers outside QA's control.

**Risks to this plan**

| Risk | Mitigation |
|---|---|
| T4 cost or provider rate limits make nightly runs impractical | Tiered corpora — a small smoke set per request, full corpus nightly; cache by configuration fingerprint |
| Provider model updates silently change adjudication behavior | Pin model identifiers; scorecard drift detection catches unpinned change |
| Corpora become stale relative to real techniques | Quarterly refresh is a scheduled deliverable, not best-effort |
| Closing G-01 and G-02 surfaces a large batch of failures at once | Land the harness change with newly-included suites non-blocking for one week, triage, then enforce |
| E6 dependencies block Phases 4 and 6 | Use dev PostgreSQL and dev Vault as a stepping stone; do not let it block Phases 0–3 |
| Phase 0 is treated as optional | It is the plan's only hard precondition; escalate rather than proceed past it |

---

## 15. Approvals

| Role | Name | Date |
|---|---|---|
| QA lead | | |
| Engineering lead | | |
| Security reviewer | | |
| Release manager | | |
