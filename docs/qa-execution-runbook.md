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

# Nomos QA Execution Runbook

Companion to `docs/qa-test-plan.md` (the Master Test Plan, "MTP"). The MTP says
what will be tested and why. This runbook is the operating procedure for a single
agent-driven verification pass: Docker Compose first, then Kind, executing the
MTP's tiers against each.

Scope of one pass: MTP tiers T1, T2, T3 in full, and T4 only if the operator
authorizes LLM spend. Written for me to follow directly; every step has a command,
an expected signal, and a pass/fail rule.

---

## 0. Relationship to the MTP

| Runbook stage | Satisfies MTP |
|---|---|
| S0 Preflight | §4.3 first actions, §5.1 onboarding |
| S1 Static + T1/T2 in runner | §6.1, §8.1 entry criteria |
| S2 Compose deploy | §5 (E3) |
| S3 T3 against Compose | §6.2 |
| S4 Teardown / transition | §5 |
| S5 Kind deploy | §5 (E2) |
| S6 T3 against Kind | §6.2 |

**Target order is operator-selected.** Compose-first is the default because it is
the simpler topology, but S2–S4 may be skipped in favour of going straight to
S5–S6 when the host cannot satisfy Compose's published ports. Kind keeps Redis as
a ClusterIP service and publishes nothing, so a host port conflict on 6379 does not
apply to it. When Compose is skipped, record the coverage lost: the
container-inspecting smoke script (`scripts/test_smoke.py`) and the Compose-only
Sentinel topology checks.
| S7 T4 adjudication | §6.3 — gated on operator approval |
| S8 Performance smoke | §6.4 — optional |
| S9 Evidence + report | §10, §11 |

**A pass is not a release gate.** MTP §8.2 requires suites that do not exist yet
(scored T4, chaos, mutation). This runbook verifies what is buildable today and
records precisely what could not be verified.

---

## 0.5 Clean-room execution model (mandatory)

**No verification command runs on the host.** The host toolchain and the host
`.venv` are not the system under test, and using them produces results that are
attributable to neither. Every command in S1, S3, S6, S7, and S8 runs inside a
disposable runner container.

### Runner image

Built from `python:3.13-slim` to match the CI base, plus the binaries the suites
gate on:

| Included | Reason |
|---|---|
| `uv`, `git` | mirrors CI's `before_script` |
| `opa` | otherwise the Rego suite skips silently (G-03) |
| `helm`, `kubectl` | chart render tests; Kind interaction in S5/S6 |
| `docker` CLI | `scripts/test_smoke.py` inspects containers via `docker exec` |
| `curl` | health probes |

Dependencies install with **`uv sync --frozen --extra all`**, matching
`.gitlab-ci.yml:48`. **Not `--extra dev`** — the `dev` extra resolves to
`nomos[grpc,postgres]` plus test tooling and does *not* include `nat`, `openai`,
or `anthropic`, so a `dev`-only environment cannot import the NAT server or any
LLM backend. The README's `uv sync --extra dev` is insufficient for the suite
(see the defect log in the execution report).

### Isolation rules

1. The repo is mounted at `/work`. Writes are confined to `test-reports/`,
   `coverage.xml`, and `.coverage`.
2. The runner never mutates the host `.venv`. Container dependencies live in the
   image or a container-local venv, never the mounted `.venv`.
3. For T3 the runner reaches the deployment from inside the container, never via
   host loopback:
   - **Kind (this pass):** the runner joins the `kind` Docker network and uses
     `kind get kubeconfig --name nomos --internal`, which points at the
     control-plane container by name instead of `127.0.0.1:<random>`. The runner
     then starts its **own** `kubectl port-forward` processes against its own
     localhost. No dependency on host-side port-forwards.
   - **Compose:** the runner joins the Nomos Compose network and addresses
     services by container DNS name.

   Either way host port publishing is irrelevant to the tests, and a host port
   conflict cannot corrupt results.
4. The runner is `--rm`. Each stage starts from the image, not from accumulated
   container state.
5. Host state that the runner needs (`deploy/.env`, kubeconfig) is mounted
   explicitly and read-only where possible, never copied into the image.

### What still runs on the host

Only orchestration that cannot run elsewhere: `docker compose` up/down for the
stack itself, Kind cluster lifecycle, and the S0 preflight inventory. These are
operations *on* the environment, not measurements *of* the product.

---

## 1. Operating rules

These are the rules I break most easily under time pressure. They are not optional.

1. **Report what happened, not what should have happened.** If a suite fails,
   quote the failure. If a step is skipped, say so and why. Never describe a
   command's intended effect as its result.
2. **Never re-run to green.** A test that passes on the second identical run is a
   flaky-test defect (MTP §12 rule 4), not a pass.
3. **Never print secrets.** `deploy/.env` is mode 0600 and holds generated Redis,
   dashboard, signup, telemetry, and council-management secrets plus the agent
   token. Verify with `test -n "$VAR" && echo set`, never `echo "$VAR"` or
   `cat deploy/.env`. Same for `kubectl get secret -o jsonpath` — assign, then
   test; do not echo.
4. **Bound every command.** Use `timeout` on foreground commands. Anything that
   may exceed ~2 minutes runs with `run_in_background: true`, and I wait with an
   `until` loop, never a bare `sleep`.
5. **Two attempts, then stop.** If a step fails twice the same way, stop and
   report with the error rather than trying variations. Deployment failure modes
   here are environmental and I will not fix them by guessing.
6. **Never answer an interactive prompt by simulating a TTY.** `deploy_stack.sh`
   guards its prompts with `[ -t 0 ]`; under my tool calls stdin is not a TTY, so
   it takes the non-interactive branch and gives up on timeout. That is correct
   behavior. If something blocks on input anyway, stop — do not feed it input.
7. **Keep executed commands benign.** The executor runs real commands in a
   container. Verification commands stay read-only or write only under the
   approved working directory. No destructive probes against the host.
8. **Clean up.** Port-forwards, Kind clusters, and Compose stacks are torn down at
   the end (S10) or explicitly handed over. A left-running Kind cluster consumes
   several GB.
9. **Confirm before destructive steps.** Teardown, `AGENT_FORCE_ROTATE`, and any
   `docker system prune` need operator confirmation first — they destroy state the
   operator may want.

---

## 2. Prerequisites I need from the operator

I cannot supply these. Ask once, up front, before S0.

| Need | Why | Blocking |
|---|---|---|
| A **valid, freshly-verified** inference key | The shipped council config routes every member through NVIDIA Cloud Functions inference. Without a working key the council cannot evaluate anything. **Presence is not sufficient — see §2.1.** | S2 onward |
| Confirmation that LLM spend is acceptable | Every authorize call is 1 chairman + 3 member + optional code-review completions. A T3 pass is tens of calls; a T4 pass is thousands. | S3 (small), S7 (large) |
| Approval to create a Kind cluster | Pulls images and consumes several GB of disk. | S5 |
| Approval for teardown | Destroys the deployed stack and its ledger. | S4, S10 |
| Whether to install `opa` | Not present on this host; without it the Rego suite silently skips (MTP G-03). | S1 |
| Ports 8000, 9901, 9904, 9905, 9907, 9910 free | Compose publishes host ports; Kind uses port-forwards. | S2, S5 |

If no working key is available, S0–S1 still run and are worth running; S2 onward
cannot proceed and I say so rather than deploying a council that will fail every
decision.

### 2.1 Validate the key before deploying — mandatory S0 gate

**A present key is not a working key.** An expired credential produces a stack that
deploys perfectly, reports every pod healthy, and then fails only the tests that
exercise real adjudication — which reads as three product defects rather than one
dead credential. That is exactly how a full protocol run was wasted on 2026-08-11:
`sk-…-aug`, a rotated monthly proxy key, was still sitting in `deploy/.env`.

Probe the endpoint directly. It takes seconds, and it runs **before** the
~20-minute image build rather than after it:

```bash
set -a; . /path/to/.env; set +a
curl -s -o /tmp/nvprobe.json -w '%{http_code}\n' --max-time 30 \
  -X POST "https://inference-api.nvidia.com/v1/chat/completions" \
  -H "Authorization: Bearer ${NV_INFERENCE_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"azure/anthropic/claude-sonnet-4-6",
       "messages":[{"role":"user","content":"reply with the single word: ok"}],
       "max_tokens":8,"temperature":0}'
```

| Result | Meaning |
|---|---|
| `200` + `"ok"` | Key is live. Proceed. |
| `401` `token_not_found_in_db` | Expired, or never in the proxy's table. **Stop — request a new key. Do not deploy.** |
| Connection error | Egress/DNS/TLS problem, not a credential problem — different diagnosis. |

The 401 body echoes a `Key Hash (Token)`. Compare it with the hash in the council
logs to prove whether a "different" key is genuinely different: on 2026-08-11
`NV_INFERENCE_KEY` and `deploy/.env`'s `NVIDIA_API_KEY` turned out to be the same
credential under two names, which the matching hash settled in one command.

**Where the key lives is deployment-specific.** It has been found in the repo-root
`.env` as `NV_INFERENCE_KEY` while `deploy/.env` carried a stale `NVIDIA_API_KEY`.
Resolve which one the deploy actually consumes (`deploy_stack.sh` reads
`NVIDIA_API_KEY`) and export it explicitly rather than assuming the files agree.

---

## 3. S0 — Preflight (no deployment)

Goal: know the starting state and that the tooling is real.

```bash
# Toolchain presence — record which are missing rather than assuming
for c in docker kind kubectl helm opa uv git; do
  printf '%s: ' "$c"; command -v "$c" || echo MISSING
done
docker compose version
docker info >/dev/null 2>&1 && echo "docker daemon ok" || echo "docker daemon UNAVAILABLE"

# Port availability. 80/443 are required by deploy/kubernetes/kind-cluster.yaml,
# which maps them as extraPortMappings for the ingress controller — a Kind
# deploy fails at cluster creation if either is taken. 6379 only matters for
# the Compose target, which publishes Redis to the host.
for p in 80 443 6379 8000 9901 9904 9905 9907 9910; do
  (echo >/dev/tcp/127.0.0.1/$p) >/dev/null 2>&1 && echo "port $p IN USE" || echo "port $p free"
done

# Pre-existing Kind clusters — name collision and host-port contention
kind get clusters
docker ps --filter name=control-plane --format '{{.Names}}\t{{.Ports}}'

# Repo state — I must know whether I am testing committed or dirty state
git -C . status --short
git -C . rev-parse --short HEAD

# Host-side port conflicts against what Compose publishes
grep -nE '^\s+- "\$\{[A-Z_]+:-[0-9.]+\}:\$\{[A-Z_]+:-[0-9]+\}:' deploy/docker-compose.yml
```

**Do not run `uv sync` on the host.** Dependencies are installed inside the runner
image per §0.5. A host `uv sync --extra dev` both pollutes the host venv and
under-installs (it strips `nat`, `openai`, `anthropic`).

| Check | Pass |
|---|---|
| `docker` + daemon | present and responding |
| `kind`, `kubectl` | present (needed at S5; note if missing, continue to S3) |
| `helm` | present |
| `opa` | note presence; absence is a recorded limitation, not a blocker |
| Ports | all free, or conflicts identified and reported before deploying |
| Repo | HEAD recorded; any dirty files listed in the report |

**Record:** toolchain table, HEAD, dirty files. This goes in the final report so
results are attributable to a specific tree.

---

## 4. S1 — Static analysis and host suites (T1/T2)

Goal: satisfy MTP §8.1 entry criteria, and confirm the known blockers are still
present before blaming the deployment for them.

### 4.1 Static

```bash
timeout 300 uv run ruff check .
timeout 120 uv run ruff format --check .
timeout 300 uv run mypy nomos
timeout 300 uv run bandit -c pyproject.toml -r nomos executors trusted_executor_contract monitor demo
```

Expected on baseline `8e653f3`: **7 ruff findings, 1 mypy error** (MTP G-09). If
the counts differ, the tree has changed — re-verify MTP §4.2 before continuing.

### 4.2 Known-blocker confirmation

```bash
# G-16: expect 2 failures, ~1s
timeout 300 uv run pytest tests/test_deploy_stack_script.py -q --no-header \
  -k "not main_kind_existing_secret_only"

# G-03: expect a skip if opa is absent
timeout 120 uv run pytest tests/test_opa_policy.py -v --no-header

# G-07: expect a skip
timeout 120 uv run pytest tests/test_ledger_concurrency.py -v --no-header
```

### 4.3 Host suites

**Do not run the bare full suite.** `pytest tests` includes the two
`main_kind_existing_secret_only` tests, which take ~36 minutes each (MTP G-15).
Always exclude them:

Pass the backend credentials **explicitly**. They are not optional: without them
the suite still exits 0 while quietly skipping every Redis- and PostgreSQL-backed
test (see §14).

```bash
# Background — this takes minutes even with the exclusion
docker run --rm --network nomos-qa-net ... \
  -e NOMOS_TEST_REDIS_HOST=qa-redis -e NOMOS_TEST_REDIS_PORT=6379 \
  -e NOMOS_TEST_REDIS_PASSWORD="${QA_REDIS_PASSWORD}" \
  -e NOMOS_TEST_DATABASE_URL="postgresql://nomos:${QA_PG_PASSWORD}@qa-postgres:5432/nomos_test" \
  nomos-qa-runner:local \
  timeout 1800 uv run pytest tests -q --no-header -p no:cacheprovider \
    -k "not main_kind_existing_secret_only" \
    --durations=25 -rs > /tmp/.../t1t2.log 2>&1
```

`-rs` prints the skip reasons, which is the point: a green run with 40 skips is
not a green run (MTP §11 executed-vs-skipped metric).

**Baseline as of `5acd66e`: `1163 passed, 10 skipped`.** The 10 are the live
suites, which need a deployed target and cannot pass at S1. Any other skip count
means the run was degraded — diagnose it before reading the run as green.

Also run the profiles the CI job uses, for comparability:

```bash
timeout 1800 ./scripts/run_test_suite.py --profile unit
timeout 1800 ./scripts/run_test_suite.py --profile integration
```

| Signal | Rule |
|---|---|
| Failures beyond the 2 known | New defect — triage per MTP §12 before deploying |
| Skip count | Record the total and every reason |
| Wall clock | Record; it is a tracked metric (MTP §11) |
| Reports | `test-reports/` HTML + JUnit |

**Gate:** do not proceed to S2 with unexplained failures. Known failures (G-16)
are explained and do not block.

---

## 5. S2 — Docker Compose deployment (E3)

Compose first because it is the simpler topology and enables the container-inspecting
smoke scripts that Kind does not support.

```bash
# Background — takes several minutes
NVIDIA_API_KEY=... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

The script creates `deploy/.env` at mode 0600, generates missing secrets, deploys,
creates or verifies the `agent-developer` account, and prints service URLs.

Optional variants, one at a time, only if the operator asks:

```bash
# with the browser demo (adds :8000, HTTP basic auth)
NVIDIA_API_KEY=... DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh

# force a fresh agent token (destroys the old one — needs confirmation)
AGENT_FORCE_ROTATE=1 DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

### Verification

```bash
docker compose -f deploy/docker-compose.yml ps
curl -fsS http://127.0.0.1:9901/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:9907/v1/health | python3 -m json.tool
```

| Check | Pass |
|---|---|
| All containers | `running` / `healthy`, none restarting |
| Monitor `/healthz` | self ok **and** upstream council reachable |
| Executor `/v1/health` | reports contract version and a council key fingerprint |
| `deploy/.env` | exists, mode `600` — verify with `stat -c '%a'`, never `cat` |

**Failure handling:** on a failed rollout, collect
`docker compose -f deploy/docker-compose.yml logs --tail=100 <service>` for the
failing service and stop. Two identical failures → report, do not iterate.

---

## 6. S3 — T3 against Compose

### 6.1 Load credentials without printing them

```bash
set -a; . deploy/.env; set +a
export NOMOS_MCP_LIVE_URL="${NOMOS_MCP_LIVE_URL:-http://127.0.0.1:9901/mcp}"
export NOMOS_GRPC_SMOKE_ADDR="${NOMOS_GRPC_SMOKE_ADDR:-127.0.0.1:9904}"
export NOMOS_EXECUTOR_URL="${NOMOS_EXECUTOR_URL:-http://127.0.0.1:9907}"
test -n "$NOMOS_AGENT_ID" && echo "agent id set"
test -n "$NOMOS_AUTH_TOKEN" && echo "auth token set"
```

If the account pre-existed, the raw token is unrecoverable (Redis stores only a
hash). That requires `AGENT_FORCE_ROTATE=1`, which is destructive — ask first.

### 6.2 Deployed suites

```bash
timeout 900 ./scripts/run_test_suite.py --profile smoke
timeout 1800 ./scripts/run_test_suite.py --profile live
```

Individually, for isolation when something fails:

```bash
timeout 300 uv run pytest tests/test_grpc_live_smoke.py -q -rs
timeout 300 uv run pytest tests/test_multi_council_mcp_live.py -q -rs
timeout 600 uv run pytest tests/test_mcp_live_integration.py -q -rs
timeout 900 uv run python scripts/test_smoke.py
```

**Critical:** these suites skip when their env vars are absent. A "passing" run
that executed nothing is the exact failure mode MTP G-07 describes. Confirm the
executed count is non-zero; `-rs` shows skip reasons.

### 6.3 Manual validation ladder

Follow `docs/council-mcp-validation.md` beyond the automated suites. Then verify
by hand the boundary properties the automated suites do not yet cover:

| Property | Method | MTP |
|---|---|---|
| Monitor `/mcp` auth posture | Call `/mcp` with no `Authorization`. **On Kind this is closed:** the chart sets `MONITOR_MCP_PROXY_TOKEN` and `/mcp` answers `401 {"error":"mcp proxy authentication required"}`. Verify a bad token is also `401` and the real token gets `200`. Confirm the posture per target rather than assuming the open `dev` default. | R-04 |
| Dashboard basic auth | `/` and `/api/*` challenge; `/healthz`, `/livez` do not. **`/mcp` does challenge** wherever the proxy token is set. | §2.4 TDS-PERIM |
| Origin/Host validation | Send `Origin: http://evil.example` **with a valid proxy token** — without one the 401 fires first and the check is never reached. Expect acceptance; no such check exists. Confirmed-open. | R-04 |
| End-to-end authorize → writ → execute | One benign command through the full path; confirm exit code, and that the writ verifies. The command must satisfy **both** gates: OPA requires absolute executable paths, and the executor's own `tools.allowed` list is checked on the **basename**. `/bin/echo` passes both. | QO-1, QO-2 |
| Replay rejection | Submit the same writ blob twice; second must be rejected. Confirm the reason is `Nonce already claimed (replay detected)` — not a header or binding error, which would mask the real check. | R-06 |
| Forged writ rejection | Re-sign a copy of the *real* writ with an attacker key so structure and agent binding stay valid. Must be rejected with `Signature verification failed`. | R-06 |
| Ledger chain validity | `get_ledger_status` is **not** exposed through the proxy. Page the chain with `council_ledger_page` and verify each entry's `previous_hash` equals its predecessor's `content_hash`, and that sequences are contiguous to `head_sequence`. | QO-3, R-05 |

**Executor calls need `X-Agent-ID`** matching the writ's `target_agent_id` (README.md:681);
without it every submission fails identically and negative tests appear to pass for the
wrong reason. Always confirm the *rejection reason*, never just the rejection.

**A council approval is not an executor guarantee.** The executor applies its own
`tools.allowed` allowlist after the writ verifies, so an approved command can still be
refused (`Tool not allowed: <tool>`). That is defence in depth working, not a defect.

**`council_check_status` does not authenticate the agent** — read-only tools are exempt, so
it cannot be used to prove a token is valid. To test an account's credential without
spending an LLM call, send `council_authorize` with a non-absolute command: a valid token
reaches OPA (`Denied by OPA policy: ...`), an invalid one stops at
`Authentication failed or missing token`.

Keep the executed command benign and inside the approved working directory —
writing a file under the executor's allowed cwd is sufficient proof.

**Record for each:** the request, the decision, the verification stage on rejection,
and the ledger entry. This is the evidence bundle stub for MTP §10.

---

## 7. S4 — Teardown and transition

Needs operator confirmation — this destroys the ledger accumulated in S3.

```bash
./deploy/scripts/compose-down.sh
docker compose -f deploy/docker-compose.yml ps   # expect empty
```

Before tearing down, capture anything needed for the report: ledger export, test
reports, container logs. After teardown they are gone.

Note whether `deploy/.env` is retained — Kind's default path reuses it, and the
`KIND_USE_EXISTING_SECRET_ONLY=1` path deliberately does not.

---

## 8. S5 — Kind deployment (E2)

```bash
# Background — cluster creation plus image builds, expect many minutes
NVIDIA_API_KEY=... ./scripts/deploy_stack.sh
```

Kind is the default target. The script creates the cluster, builds and loads
images, deploys the chart, creates or verifies the account, and starts
port-forwards.

Secret-only variant, if the operator wants that path exercised:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh
```

### Verification

```bash
kubectl --context kind-nomos -n nomos get pods
kubectl --context kind-nomos -n nomos get deploy
curl -fsS http://127.0.0.1:9901/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:9907/v1/health | python3 -m json.tool
```

| Check | Pass |
|---|---|
| Pods | all `Running` and ready; no `CrashLoopBackOff` / `ImagePullBackOff` |
| Port-forwards | 9901, 9904, 9905, 9907 answering |
| Health endpoints | as S2 |

**Watch for the G-15 mechanism in the wild.** `wait_kind_rollouts` word-splits an
unquoted `$names`. If the deploy script appears to hang waiting on
oddly-named "deployments", that is the same latent bug the test suite exposed —
capture the output; it is a product defect, not a flake.

### Kind credentials

```bash
export NOMOS_MCP_LIVE_URL=http://127.0.0.1:9901/mcp
export NOMOS_GRPC_SMOKE_ADDR=127.0.0.1:9904
NOMOS_AGENT_ID="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets \
  -o jsonpath='{.data.NOMOS_AGENT_ID}' | base64 -d)"; export NOMOS_AGENT_ID
NOMOS_AUTH_TOKEN="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets \
  -o jsonpath='{.data.NOMOS_AUTH_TOKEN}' | base64 -d)"; export NOMOS_AUTH_TOKEN
test -n "$NOMOS_AGENT_ID" && test -n "$NOMOS_AUTH_TOKEN" && echo "kind creds set"
```

Assign then test. Do not echo the values.

---

## 9. S6 — T3 against Kind

Repeat §6.2 and §6.3 against the Kind deployment. Differences to expect and record:

| Difference | Expectation |
|---|---|
| Compose-only smoke scripts | `scripts/test_smoke.py` inspects containers via `docker exec` and talks to Redis on a host port — expect it to be inapplicable or partially skip. Record which parts did not run. |
| NetworkPolicy | Chart default is `enabled: false` (MTP R-14). Perimeter isolation assertions cannot pass here. Record as not-verifiable in the default profile. |
| Storage mode | **Kind defaults to PostgreSQL**, not file/Redis — `deploy/kubernetes/kind-values.yaml` sets `storage.mode: service`. Accounts land in the `accounts` table; verify with `kubectl -n nomos exec nomos-postgres-0 -- psql -U nomos -d nomos -tAc "select agent_id,enabled from accounts;"`. Compose is the Redis-backed target, so running both gives dual-backend coverage. |
| Helm render checks | `helm template` variants are testable here and are worth running: `timeout 300 uv run pytest tests/test_deploy_stack_script.py -q -k "helm"` |

**Cross-target comparison is the deliverable of this stage:** any behavior that
differs between Compose and Kind for the same request is either a defect or an
undocumented topology dependency. Both are worth reporting.

---

## 10. S7 — T4 adjudication (operator-gated)

Only with explicit approval — this is the expensive tier and the corpora are
currently unscored manual scripts (MTP G-08), so a run produces observations, not
a gate.

```bash
timeout 1800 uv run python scripts/test_jailbreak.py
timeout 1800 uv run python scripts/test_social_engineering.py
timeout 1800 uv run python scripts/test_script_obfuscation.py
timeout 1800 uv run python scripts/test_benign_bypass.py
timeout 3600 uv run python scripts/test_valid_operations.py
```

Run one at a time; note wall-clock and rough call volume for each.

**How to report this tier honestly.** These scripts print per-case results without
thresholds or repeat sampling. A single run of a stochastic system is an
observation, not a measurement. I report:

- counts by expected-vs-actual outcome,
- every case where a deny-expected request was approved, quoted in full,
- and an explicit statement that one run does not establish a rate.

I do **not** report a pass or a percentage as though it were a gate. That is what
MTP Phase 3 builds.

---

## 11. S8 — Performance smoke (optional)

```bash
uv run --with fastapi --with 'uvicorn[standard]' --with httpx --with mcp \
  uvicorn nomos_stress.app.main:app --host 127.0.0.1 --port 9910
```

Smoke profile only in a verification pass — a handful of sequential requests to
confirm the driver works against the deployment. Sustained load needs E5 sizing
and is out of scope here.

**Always end a load run by validating the ledger chain** (MTP §6.4). Throughput
numbers are void if the chain forked, which is the known R-05 defect class and is
reachable at concurrency in file-ledger mode — which is exactly what both of these
deployments use.

---

## 12. S9 — Evidence and reporting

### What I collect

| Artifact | From |
|---|---|
| Toolchain + HEAD + dirty files | S0 |
| Static analysis counts | S1 |
| Suite results, executed/skipped counts, durations | S1, S3, S6 |
| `test-reports/` HTML + JUnit | suite runner |
| Health endpoint payloads | S2, S5 |
| One full authorize → writ → execute trace | S3, S6 |
| Ledger validation result | S3, S6, S8 |
| Adjudication observations | S7 |
| Every deviation from this runbook | throughout |

### Report structure

1. **Environment** — host toolchain, commit, dirty state, which targets deployed.
2. **Results by stage** — pass/fail/skipped with counts, not adjectives.
3. **Defects found** — new ones, with severity per MTP §12 and reproduction steps.
4. **Known gaps confirmed** — which MTP §4.2 items reproduced, which did not.
5. **Not verified** — everything the pass could not reach, and why. This section
   is mandatory and is the one most likely to be quietly dropped.
6. **Cross-target differences** — Compose vs Kind.

### Reporting rules

- Counts, not impressions. "43 passed, 1 skipped" beats "mostly working."
- A skip is not a pass. Skips are listed with reasons.
- If I could not verify something, it goes in §5 of the report — never omitted
  because the rest went well.
- No claim of a security property from an in-process test alone (MTP §2.4
  environment-fidelity rule).

---

## 13. Stop conditions

Stop and report rather than continuing:

| Condition | Why |
|---|---|
| Same step fails twice identically | Environmental; guessing wastes the operator's time |
| Any command blocks on interactive input | Rule 6; never simulate a TTY |
| A secret would have to be printed to proceed | Rule 3; find another way or ask |
| A destructive step is needed without prior approval | Rule 9 |
| Disk or memory pressure during Kind bring-up | Partial cluster state is worse than none |
| Evidence of real unauthorized execution | This is a Sev-1 (MTP §12) — stop everything and report immediately |
| The deployment succeeds but the council denies every request | Almost certainly a missing or invalid API key; verify before reporting it as a product defect |

---

## 14. Repo-specific traps

Collected so I do not rediscover them mid-pass.

| Trap | Handling |
|---|---|
| `kind create cluster` fails with `could not find a log line that matches "Reached target .*Multi-User System"` | **`fs.inotify.max_user_instances` is too low.** systemd in the node needs an inotify instance and dies silently without one. Check `cat /proc/sys/fs/inotify/max_user_instances` (kind wants ≥ 512; 128 is the Ubuntu default and is not enough). Fix: `sudo sysctl -w fs.inotify.max_user_instances=8192`. Writing `/etc/sysctl.d/*.conf` alone does **not** apply it — run `sudo sysctl --system`. Not a kind, Docker, containerd, or Nomos fault; existing clusters keep working because they only need instances at creation |
| A kind node container dies at `starting init` with no systemd output | Reproduce with `-t`. Without a TTY there is no `/dev/console`, so systemd's error never reaches `docker logs` and the failure looks like a mystery. `docker run -d -t --privileged --security-opt seccomp=unconfined --security-opt apparmor=unconfined ...` |
| Deploy exits 0 but nothing works | Do not trust pod readiness. Probes here are TCP or process-level: a council answering every tool call with an error, and a gRPC proxy 401ing on every request, both report Ready. Always follow a deploy with a real end-to-end call |
| `pytest tests` takes ~72 min | Always `-k "not main_kind_existing_secret_only"` (G-15) |
| 2 tests in `test_deploy_stack_script.py` fail | Known (G-16); not caused by the deployment |
| `opa` absent → Rego suite skips silently | Record; do not read the skip as a pass (G-03) |
| Redis/PostgreSQL suites skip without services | Same; `-rs` to surface reasons (G-07) |
| **S1 run after a Compose deploy silently loses ~121 tests** | `tests/redis_test_support.py:55` resolves the Redis password as `NOMOS_TEST_REDIS_PASSWORD` → `REDIS_PASSWORD` → **`deploy/.env`** → `nomos-dev`. Once a Compose deploy has generated `deploy/.env`, that file's freshly generated password is picked up and no longer matches the QA Redis, so every Redis-backed test skips with `AuthenticationError` and the suite still exits 0. Observed as `1041 passed, 132 skipped` where the true baseline is `1163 passed, 10 skipped`. **Always set `NOMOS_TEST_REDIS_PASSWORD` and `NOMOS_TEST_DATABASE_URL` explicitly, and compare the pass/skip counts to the baseline** — an exit code of 0 does not mean the suite ran |
| Test backend env var is `NOMOS_TEST_DATABASE_URL` | Not `NOMOS_TEST_POSTGRES_DSN`. A wrong name does not error; it silently falls back and the PostgreSQL suites skip |
| `deploy/.env` holds live secrets at 0600 | Never `cat`; source and test (Rule 3) |
| Pre-existing account → token unrecoverable | Needs `AGENT_FORCE_ROTATE=1`, destructive, ask first |
| `NOMOS_DEPLOY_PROFILE` defaults to `dev` | `/mcp` may be unauthenticated by design; that is the documented default, not a new finding |
| Demo UI/API need HTTP basic auth | `DEMO_AUTH_USER` defaults to `demo`; password is generated |
| Compose defaults to Redis Sentinel topology | Not standalone; affects failover expectations |
| Kind context/namespace | `kind-nomos` / `nomos` |
| Council calls cost money and take seconds | Batch verification requests; do not loop needlessly |
| Executor runs real commands | Keep them benign and inside the allowed cwd (Rule 7) |
| Port-forwards outlive the script | Tear down in S10 |

---

## 15. S10 — Cleanup

With operator confirmation:

```bash
./deploy/kubernetes/scripts/kind-down.sh     # destroys the Kind cluster
./deploy/scripts/compose-down.sh             # if a Compose stack is still up
```

Then confirm: no stray port-forwards, no running containers from this pass, ports
released. Report what was left standing and why, if anything was.

`deploy/.env` and any generated keys are **not** removed automatically. Flag their
existence to the operator rather than deleting them — they may be wanted for a
follow-up pass, and deleting the only copy of an agent token is irreversible.
