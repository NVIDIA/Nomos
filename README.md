# Nomos

**The point.** Autonomy is what makes agents useful. It is also what lets them delete the wrong files, crash a server, leak credentials, or push a destructive change to production. Nomos is the oversight layer that catches those actions before they execute — so agents stay autonomous, and the destructive ones never run.

**Defense in depth.** Every privileged action passes through multiple independent layers before it executes. Each layer catches a different class of problem:

- **OPA policy filter** — deterministic hard rules that don't need LLM judgment: production justification requirements, blocked paths, packaged-file integrity. Optional and customer-supplied, so deployments encode their own non-negotiables.
- **LLM-backed council** — a multi-member panel that votes on whether the action matches its stated intent, scope, and blast radius. Catches semantic mismatches and adversarial framing that fixed rules can't anticipate.
- **Writ of Execution** — a cryptographically signed, time-bounded, single-use token that pins the exact command, working directory, and packaged files into bytes the executor can verify before running.
- **Sandboxed executor** — runs the command inside an isolated container with capabilities dropped, enforcing its own tool whitelist, path restrictions, and blocked patterns.

An action only runs when every layer clears it. That coordination — not any single check — is what keeps the bad ones from running.

---

## Overview

**Security Council + Writ of Execution + Sandboxed Execution.** Nomos is an authorization kernel for privileged operations with cryptographic accountability. Agents propose intent; an LLM-backed council evaluates it; approved actions are sealed in a signed **Writ of Execution** and run inside an isolated container sandbox.

Nomos solves a specific problem: **how do you let AI agents take real actions (run commands, modify infrastructure, access data) while maintaining enterprise-grade control?**

Without a governing layer, multi-agent systems trend toward opaque decisions, overbroad privileges, and irreproducible incidents. Nomos enforces a strict authorization pipeline where no command executes without cryptographic proof of approval.

### What It Does

```
Agent: "I need to run: /usr/local/bin/python3 -c 'from pathlib import Path; Path(\"/workspace/test.txt\").write_text(\"hello\\n\")'"
       ↓
OPA: deterministic front-door policy checks the normalized intent
       ↓
Code Review (optional): sidecar inspects packaged files before the voters
       ↓
Council: 3 LLM reviewers evaluate the command independently
         - Command Integrity Reviewer: checks for injection, obfuscation
         - Environment Isolation Reviewer: validates scope and targeting
         - Blast Containment Reviewer: assesses worst-case impact
       ↓
Writ Factory: mints a signed, time-bound, single-use authorization token
       ↓
Executor: verifies signature, claims nonce, runs command in sandbox
       ↓
Result: { exit_code: 0, stdout: "...", duration: 0.02s }
```

### Key Properties

- **Cryptographic accountability** — Every approved action is sealed in an Ed25519-signed Writ of Execution. The signature proves council approval; the nonce prevents replay; the expiry limits the window of use.
- **Multi-party consensus** — Council members are LLM agents routed through NVIDIA Cloud Functions inference by default, with direct Anthropic/OpenAI/NIM backends still available. They vote independently. Optional debate rounds let members reconsider after seeing each other's reasoning.
- **Defense in depth** — OPA can reject intents before LLM review; even after council approval, the executor enforces its own policy: tool whitelists, denied paths, blocked command patterns, and working directory restrictions.
- **Full audit trail** — Every step (submission, evaluation, voting, debate, minting, execution, verification) is recorded in a hash-chained, signed ledger.
- **Pluggable transports and policy** — Use MCP over Streamable HTTP, the gRPC proxy, custom OPA policies at deployment time, custom executor profiles, or in-process council tests.

### Use Cases

- **IDE agent governance** — Route Windsurf/Cursor/Copilot shell commands through the council before execution
- **CI/CD guardrails** — Gate production deployments, database migrations, and infrastructure changes behind council approval
- **Multi-agent orchestration** — Let autonomous agents request privileged operations with cryptographic proof of authorization
- **Security research** — Test how LLM reviewers handle jailbreak attempts, social engineering, and obfuscated commands

---

## Start Here

### 1. Choose a Local Target

The fastest path is `scripts/deploy_stack.sh`. It creates `deploy/.env`,
generates local secrets, deploys the stack, creates or verifies the first agent
account, and prints the URLs you should use.

| Target | Use When | Prerequisites |
|---|---|---|
| Kind, default | You want the closest local Kubernetes path. | Docker, Kind, kubectl, Helm, NVIDIA API key |
| Docker Compose | You want a simpler Docker-only startup or a remote VM install. | Docker Compose v2, NVIDIA API key |

The shipped council config uses NVIDIA Cloud Functions inference by default.
Anthropic/OpenAI keys are only needed if you change `configs/council.yaml` to
use those direct-vendor backends.

### 2. Start the Council

Run one of these from the repository root:

```bash
# Default local Kind bootstrap
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh

# Kind deployment from a pre-created nomos/nomos-secrets Secret.
# This does not create or update deploy/.env.
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh

# Docker Compose bootstrap instead of Kind
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh

# Compose plus the browser demo
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh
```

The script keeps `deploy/.env` at mode `600`. If the file does not exist, it
creates it and generates Redis, dashboard, signup, account-integrity, telemetry,
and council-management secrets. If the agent account is new, it stores
`NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` in `deploy/.env`.

Use `KIND_USE_EXISTING_SECRET_ONLY=1` only when `nomos/nomos-secrets` already
exists. That path validates required Secret keys, deploys without creating
`deploy/.env`, and patches any newly minted agent token back into the
Kubernetes Secret. If the Secret is Helm-owned, the script reuses the live
Secret data through a temporary `0600` Helm values file that is deleted after
the upgrade. If the Secret is pre-created outside Helm, it deploys with
`secrets.create=false`.

If an account already exists, the raw token cannot be recovered because Redis
stores only a hash. Rotate intentionally when you need a fresh token:

```bash
AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

### 3. Find the Running Services

The deploy script prints the active URLs at the end.

| Service | Kind default with port-forwards | Compose default |
|---|---|---|
| Monitor UI | `http://127.0.0.1:9901/` | `http://$NOMOS_PUBLIC_HOST:9901/` |
| Council MCP | `http://127.0.0.1:9901/mcp` | `http://$NOMOS_PUBLIC_HOST:9901/mcp` |
| Council gRPC | `127.0.0.1:9904` | `$NOMOS_PUBLIC_HOST:9904` |
| Agent signup | `https://127.0.0.1:9905/` | `http://$NOMOS_PUBLIC_HOST:9905/` |
| Demo UI | `https://127.0.0.1:8000/` | `http://$NOMOS_PUBLIC_HOST:8000/` when `WITH_DEMO=1` |
| Executor | `http://127.0.0.1:9907` | `http://$NOMOS_PUBLIC_HOST:9907` |

The Demo UI and Demo API require HTTP Basic auth. `deploy_stack.sh` generates
`DEMO_AUTH_PASSWORD` for normal bootstrap runs and stores it in `deploy/.env`.
With `KIND_USE_EXISTING_SECRET_ONLY=1`, the password lives in the
`nomos/nomos-secrets` Secret under `DEMO_AUTH_PASSWORD`; the username defaults
to `demo` through `DEMO_AUTH_USER`.

Compose publishes host ports by default. On a remote machine, set
`NOMOS_PUBLIC_HOST=<reachable-ip-or-dns>` before deploy and put firewall or
reverse-proxy policy around exposed ports.

### 4. Verify the Stack

Install local client/test dependencies once per checkout:

```bash
uv sync --extra dev
# or: python3 -m pip install -e ".[dev]"
```

```bash
# Monitor and upstream council are healthy
curl -s http://127.0.0.1:9901/healthz | python3 -m json.tool

# Executor is healthy and has the council's signing key
curl -s http://127.0.0.1:9907/v1/health | python3 -m json.tool

# gRPC proxy is healthy
NOMOS_GRPC_SMOKE_ADDR=127.0.0.1:9904 uv run pytest tests/test_grpc_live_smoke.py -q
```

For Kind deployments that use only `nomos-secrets`, load live-test credentials
from the Kubernetes Secret instead of sourcing `deploy/.env`:

```bash
export NOMOS_MCP_LIVE_URL=http://127.0.0.1:9901/mcp
export NOMOS_GRPC_SMOKE_ADDR=127.0.0.1:9904
export NOMOS_AGENT_ID="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_AGENT_ID}' | base64 -d)"
export NOMOS_AUTH_TOKEN="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_AUTH_TOKEN}' | base64 -d)"
```

For the full post-deploy validation ladder, use
[Council MCP Validation](docs/council-mcp-validation.md). It separates the
portable Kind/Compose MCP checks from Compose-only smoke scripts.

### 5. Manage Agent Accounts

`deploy_stack.sh` creates `agent-developer` by default unless `SKIP_ACCOUNT=1`
is set. You can create, rotate, and disable accounts later from inside the
council container:

```bash
./scripts/agent_accounts.sh create alice-agent --role developer
./scripts/agent_accounts.sh rotate-token alice-agent
./scripts/agent_accounts.sh disable alice-agent
./scripts/agent_accounts.sh list
```

Role presets:

| Role | Permissions |
|---|---|
| `readonly` | `shell:read`, `file:read` |
| `developer` | `shell:read`, `shell:write`, `file:read`, `file:write` |
| `operator` | developer permissions plus `infra:deploy` |
| `admin` | all permission scopes |

### 6. Make an End-to-End Request

This path asks the council to authorize a command, receives a signed writ, and
submits that writ to the executor.

```python
import asyncio, json, urllib.request
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

AUTH_TOKEN = "<NOMOS_AUTH_TOKEN from deploy/.env>"
AGENT_ID  = "agent-developer"

async def authorize_and_execute():
    async with streamablehttp_client("http://127.0.0.1:9901/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            result = await session.call_tool("council_authorize", arguments={
                "auth_token": AUTH_TOKEN,
                "agent_id": AGENT_ID,
                "command": "/usr/local/bin/python3 -c 'from pathlib import Path; Path(\"/workspace/test.txt\").write_text(\"hello\\\\n\")'",
                "working_directory": "/workspace",
                "description": "Create a test file in the workspace",
                "target_resource": "/workspace",
                "justification": "Testing the end-to-end authorization flow",
            })
            auth = json.loads(result.content[0].text)
            print(f"Council decision: {auth['status']}")

            if auth["status"] != "approved":
                print(f"Denied: {auth.get('guidance', auth.get('error'))}")
                return

            writ_blob = auth["writ"]  # base64-encoded signed writ

    req = urllib.request.Request(
        "http://127.0.0.1:9907/v1/writs/execute",
        data=json.dumps({"writ": writ_blob}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-ID": AGENT_ID,  # must match writ's target_agent_id
        },
    )
    with urllib.request.urlopen(req) as resp:
        exec_result = json.loads(resp.read())

    print(f"Exit code: {exec_result['exit_code']}")
    print(f"Stdout: {exec_result.get('stdout', '')}")

asyncio.run(authorize_and_execute())
```

### 7. Run Tests

```bash
# Core package tests
uv run pytest tests/ -v

# Portable deployed MCP and gRPC checks for Kind or Compose
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed

# Compose-only smoke test (requires host Redis access)
uv run python scripts/test_smoke.py

# Compose-only live E2E demo (requires the default executor container name)
uv run python scripts/test_live_e2e.py

# Standard local regression wrapper with an HTML report
uv run ./scripts/run_test_suite.py --profile integration
```

### Further Reading

- [Getting Started Walkthrough](docs/interactive/getting-started.html)
- [Workflow Walkthrough](docs/interactive/workflow.html) — current browser-based flow from agent request to signed execution
- [Deployment Setup](docs/deployment-setup.md) / [Interactive Planner](docs/interactive/deployment.html) — executor profiles, key rotation, production setup
- [Council MCP Validation](docs/council-mcp-validation.md) — post-deploy health checks and live MCP tests
- [MCP Request Builder](docs/interactive/mcp-integration.html) — build correct council tool calls
- [Multi-Council Router](docs/interactive/multi-council.html) — understand council selection and membership
- [Writ Executor Walkthrough](docs/interactive/writ-executor.html) — see executor verification and local policy gates
- [Certified Evidence Verification](docs/interactive/certified-evidence-verification.html) — follow how signed evidence becomes eligible for `certified_evidence`
- [Deployment Control Flags](docs/deployment-setup.md#deployment-control-flags) — deployment targets, env vars, profiles, and troubleshooting

<details>
<summary>Full README</summary>

## Table of Contents

- [The Problem](#the-problem)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [System Diagram](#system-diagram)
- [Writ of Execution](#writ-of-execution)
- [Deployment](#deployment)
- [Quick Start](#quick-start)
- [MCP Tools Reference](#mcp-tools-reference)
- [Kernel Internals](#kernel-internals)
- [Testing](#testing)
- [Configuration Reference](#configuration-reference)
- [Executor Profiles](#executor-profiles)
- [Executor YAML Configuration](#executor-yaml-configuration)
- [Creating a Custom Writ Executor](#creating-a-custom-writ-executor)
- [Client Integration](#client-integration)
- [Full Documentation](docs/index.md)

---

## The Problem

As "build an agent" becomes cheap and near-instant, the differentiator stops being task competence and becomes **enterprise-grade control**: who is allowed to act, under what constraints, with what proof.

Without a governing layer, multi-agent systems trend toward:
- **Opaque decisions** — No audit trail of what happened or why
- **Overbroad privileges** — Agents with god-mode access
- **Irreproducible incidents** — "It just broke" with no way to replay
- **"Trust me bro" postmortems** — No cryptographic proof of actions

---

## How It Works

Nomos enforces a strict authorization pipeline. No command executes without cryptographic proof of council approval.

| Step | What Happens |
|------|-------------|
| **1. Propose** | Agent submits a structured `Intent` (what, why, target, rollback plan) |
| **2. Authenticate** | Agent's token is verified against Redis-stored account (SHA-256, constant-time) |
| **3. Categorize** | Intent is classified into permission scopes (`SHELL_READ`, `FILE_DELETE`, `INFRA_DEPLOY`, ...) |
| **4. Chairman Pre-Auth** | Chairman agent checks agent permissions and assesses risk level (low/medium/high/critical) |
| **5. Council Vote** | LLM-backed council members evaluate the intent and vote (approve/deny with reasoning) |
| **6. Mint Writ** | If approved, a `WritOfExecution` is created — signed with the council's Ed25519 key, nonce-protected, time-bounded |
| **7. Execute** | The writ is submitted to the **Writ Executor** (separate container), which verifies the signature, checks expiration, claims the nonce, and runs the command in a sandbox |
| **8. Audit** | Every step is logged to a hash-chained, signed ledger |

---

## Architecture

For the current end-to-end workflow, including monitor ingress, OPA,
multi-council routing, Code Review, council voting, writ minting, executor
verification, and ledger browsing, see the browser-based
[Workflow Walkthrough](docs/interactive/workflow.html). The diagram below is a compact
README summary.

```
                         ┌──────────────────────────────────────────────────┐
                         │              AGENT (any LLM agent)               │
                         │                                                  │
                         │  "I need to run: mkdir /app/workspace/data"      │
                         └────────────────────────┬─────────────────────────┘
                                                  │
                                         MCP tool call
                                      (council_authorize)
                                                  │
                    ┌──────────────────────────────────────────────────────────────┐
                    │                                                              │
                    │        MONITOR :9901 → COUNCIL SERVICE  (internal :9901)     │
                    │                                                              │
                    │  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐       │
                    │  │ Authenticator│  │  Permission   │  │  Chairman    │       │
                    │  │  (Redis +    │─>│  Categorizer  │─>│  Pre-Auth    │       │
                    │  │   SHA-256)   │  │  (7 scopes)   │  │ (risk+gate)  │       │
                    │  └──────────────┘  └───────────────┘  └──────┬───────┘       │
                    │                                              │               │
                    │                                              ▼               │
                    │                ┌──────────────────────────────────┐          │
                    │                │     LLM Council Members          │          │
                    │                │  ┌──────────┐ ┌──────────┐       │          │
                    │                │  │ Command  │ │Environment│ ...  │          │
                    │                │  │Integrity │ │ Isolation │      │          │
                    │                │  │ Reviewer │ │ Reviewer  │      │          │
                    │                │  └────┬─────┘ └─────┬─────┘      │          │
                    │                │       │approve      │approve     │          │
                    │                └───────┴─────────────┴────────────┘          │
                    │                               │                              │
                    │                       threshold met                          │
                    │                               │                              │
                    │                               ▼                              │
                    │                ┌──────────────────────────┐                  │
                    │                │      WritFactory         │                  │
                    │                │  ┌────────────────────┐  │                  │
                    │                │  │ WritOfExecution    │  │                  │
                    │                │  │  - command         │  │                  │
                    │                │  │  - nonce           │  │                  │
                    │                │  │  - expires_at      │  │                  │
                    │                │  │  - Ed25519 sig     │  │                  │
                    │                │  └────────────────────┘  │                  │
                    │                └───────────┬──────────────┘                  │
                    │                            │                                 │
                    └────────────────────────────┼─────────────────────────────────┘
                                                 │
                                       signed writ blob
                                         (base64 JSON)
                                                 │
                    ┌──────────────────────────────────────────────────────────────┐
                    │                                                              │
                    │       WRIT EXECUTOR  (container, profile-selectable)       │
                    │                                                              │
                    │  Verification Pipeline (10 steps):                           │
                    │                                                              │
                    │  ┌───────────┐  ┌───────────┐  ┌───────────┐                 │
                    │  │ 1. Deser- │─>│2. Signa-  │─>│3. Tempo-  │                 │
                    │  │    ialize │  │   ture    │  │   ral     │                 │
                    │  └───────────┘  └───────────┘  └───────────┘                 │
                    │  ┌───────────┐  ┌───────────┐  ┌───────────┐                 │
                    │  │ 4. Nonce  │─>│5. Direc-  │─>│6. Blocked │                 │
                    │  │    claim  │  │   tory    │  │  patterns │                 │
                    │  └───────────┘  └───────────┘  └───────────┘                 │
                    │  ┌───────────┐  ┌───────────┐  ┌───────────┐                 │
                    │  │ 7. Tool   │─>│8. Denied  │─>│9. Denied  │                 │
                    │  │ whitelist │  │  tool args│  │   paths   │                 │
                    │  └───────────┘  └───────────┘  └───────────┘                 │
                    │                    ┌───────────┐                              │
                    │                    │10. Command│                              │
                    │                    │   execute │                              │
                    │                    └─────┬─────┘                              │
                    │                          │                                   │
                    │                     subprocess                               │
                    │               (isolated env, cwd,                            │
                    │                timeout enforced)                             │
                    │                          │                                   │
                    └──────────────────────────┼───────────────────────────────────┘
                                                            │
                                                            ▼
                                              { exit_code, stdout, stderr, duration }
                    ┌──────────────────────────────────────────────────────────────┐
                    │                                                              │
                    │        REDIS  (container, port 6379)                         │
                    │                                                              │
                    │  nomos:accounts:{id}      — agent credentials + perms           │
                    │  nomos:nonces:{hex}       — atomic nonce claims (SET NX)        │
                    │  nomos:writs:{id}         — writ metadata + outcomes            │
                    │  nomos:intents:{id}       — intent history                      │
                    │  nomos:rate:{id}:{win}    — rate limiting counters              │
                    │                                                              │
                    └──────────────────────────────────────────────────────────────┘
```

### Kernel Levelization

The kernel itself follows **Lakos-style levelization** — each level depends only on levels below it:

```
Level 4  SecurityCouncil          — orchestrates the full workflow
Level 3  SandboxExecutor          — runs actions with token enforcement
Level 2  CapabilityService        — mints/validates scoped tokens
         QuorumManager            — multi-party weighted voting
Level 1  LedgerChain              — hash-chained append-only audit log
         PolicyEngine             — explicit policy rules
Level 0  Clock, Crypto, Types     — pure utilities, no external deps
```

---

## Writ of Execution

The **Writ of Execution** is the core authorization primitive. It is a cryptographically signed, nonce-protected, time-bounded token that authorizes exactly one command execution.

### Anatomy

```json
{
  "writ_id":              "writ-9ff898532e9d4be3",
  "intent_hash":          "sha256 of the original intent",
  "command":              "/usr/local/bin/python3 -c 'from pathlib import Path; Path(\"/app/workspace/test.txt\").write_text(\"hello\\\\n\")'",
  "working_directory":    "/app/workspace",
  "allowed_env":          {"PATH": "/usr/local/bin:/usr/bin:/bin"},
  "target_agent_id":      "agent-developer",
  "issued_at":            1739226000.0,
  "expires_at":           1739226300.0,
  "max_executions":       1,
  "nonce":                "cryptographically random 32-byte hex",
  "council_id":           "prod-council",
  "threshold_used":       "unanimous",
  "chairman_risk_level":  "low",
  "member_votes":         [{"member_id": "m1", "decision": "approve"}, ...],
  "signature":            "Ed25519 signature over canonical JSON"
}
```

### Security Properties

| Property | Mechanism |
|----------|-----------|
| **Authenticity** | Ed25519 signature — only the council's private key can produce a valid writ |
| **Integrity** | Any modification to any field invalidates the signature |
| **Non-replayability** | Nonce is atomically claimed in Redis (`SET NX`); second use is rejected |
| **Time-bounding** | `expires_at` enforced by executor; TTL scales with risk (low=300s, critical=30s) |
| **Scope-limiting** | `working_directory` and `allowed_env` restrict what the command can access |

### Verification Pipeline

The trusted executor contract performs agent binding before handing the writ to
the example enforcer. Together, the adapter and enforcer run these checks before
executing the command. Failure at any step aborts immediately:

1. **Deserialize** — decode base64, parse JSON into `WritOfExecution`
2. **Agent binding** — verify `X-Agent-ID` matches the writ's `target_agent_id`
3. **Signature** — verify Ed25519 signature against council's public key
4. **Temporal** — check `issued_at <= now <= expires_at`
5. **Nonce** — atomically claim nonce in Redis (prevents replay)
6. **Directory** — verify `working_directory` is in the allowed list
7. **Blocked patterns** — reject catastrophic commands (fork bombs, sudo, pipe-to-shell, etc.)
8. **Tool whitelist** — every command segment must use an allowed tool
9. **Denied tool args** — per-tool argument restrictions (e.g., `git push --force`)
10. **Denied paths** — command must not reference forbidden paths (e.g., `/etc/shadow`)
11. **Command** — reject empty commands
12. **Packaged files** — materialize signed attachments only if hashes and paths validate

Steps 1–5 are **contract/cryptographic/nonce** checks. Steps 6–12 are **local policy and materialization** checks (executor-level). A command can be council-approved yet still blocked by local policy — defense in depth.

---

## Deployment

### Prerequisites

- Default Kind path: Docker, Kind, kubectl, Helm, and an NVIDIA API key.
- Docker Compose path: Docker Compose v2 and an NVIDIA API key.
- Anthropic/OpenAI keys are only needed if you switch members to direct-vendor backends.

### Start the Stack

```bash
# Default Kind deployment with local port-forwards
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh

# Kind deployment from an existing Kubernetes Secret, with no deploy/.env writes
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh

# Docker Compose deployment with the default OpenShell executor
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh

# Compose with a project-specific dev executor profile
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose EXECUTOR_PROFILE=executor-dev \
    EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) ./scripts/deploy_stack.sh

# Verify
curl -s http://127.0.0.1:9901/healthz | python3 -m json.tool
```

The deploy script creates `deploy/.env`, generates missing local secrets,
seeds `deploy/dynamic-config/`, creates or verifies the first agent account,
and stores `NOMOS_AGENT_ID`/`NOMOS_AUTH_TOKEN` when it mints a new token.
It also generates `DEMO_AUTH_PASSWORD` so the Demo UI is never exposed without
Basic auth on scripted bootstrap paths.
Set `KIND_USE_EXISTING_SECRET_ONLY=1` for pre-seeded Kind clusters where
runtime secrets must stay in Kubernetes and no host-local env file should be
written.

Direct Compose remains useful for debugging. Include the Sentinel override if
you want to match the scripted Compose default:

```bash
docker compose -f deploy/docker-compose.yml \
    -f deploy/docker-compose.redis-sentinel.yml \
    --profile executor-openshell up -d --build
```

This starts three Redis containers and three Sentinel containers, and wires
council, agent-signup, and the standard executor profiles to discover the
current Redis master through Sentinel. Set `COMPOSE_REDIS_TOPOLOGY=standalone`
only for local single-Redis runs that explicitly do not need failover.

### Services

| Service | Default exposure | Purpose |
|---------|------------------|---------|
| **redis** | Compose publishes `6379`; Kind keeps it in-cluster | Nonce store, accounts, rate limiting |
| **opa** | Compose publishes `8181`; Kind keeps it in-cluster | Default front-door OPA policy filter |
| **monitor** | `9901` | External MCP reverse proxy, dashboard, and metrics |
| **council** | Internal `9901` only | MCP server with LLM council members |
| **council-grpc** | `9904` | gRPC proxy that forwards to MCP through the monitor |
| **executor-openshell** | `9907` | Default trusted executor; isolated OpenShell-backed sandbox |
| **executor-default** | `9902` | Legacy restrictive Docker executor |
| **executor-dev** | `9903` | Dev executor with extended tools and host bind mounts |

Redis, OPA, council, monitor, and the gRPC proxy are no-profile Compose
services. Executors are profile-gated; choose the executor profile you want to
run. Set the `*_BIND_ADDR` and `*_PORT` variables in `deploy/.env` or the shell
when you need tighter exposure than the Compose defaults.

### Key Management

The council generates an Ed25519 key pair at first startup (`/app/keys/council.key` + `council.pub`). The executor reads the public key from the same volume to verify writ signatures.

```bash
# View the council's public key fingerprint
curl -s http://127.0.0.1:9907/v1/health | python3 -m json.tool
```

### Key Rotation

The executor supports multiple public keys for zero-downtime rotation:
1. Generate new key pair
2. Add old public key to executor's `additional_keys`
3. Restart executor
4. Switch council to sign with new key
5. After `max_writ_ttl` elapses, remove old key

---

## Quick Start

Install the local client/test dependencies before running the Python examples or helper scripts:

```bash
uv sync --extra dev
# or: python3 -m pip install -e ".[dev]"
```

### Live E2E: Council → Writ → Executor

```python
import asyncio, json, urllib.request
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def authorize_and_execute():
    agent_id = "agent-developer"

    # 1. Connect to council MCP server
    async with streamablehttp_client("http://localhost:9901/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # 2. Request authorization for a command
            result = await session.call_tool("council_authorize", arguments={
                "auth_token": "your-agent-token",  # from deploy/.env or agent_accounts.py output
                "agent_id": agent_id,
                "command": "/usr/local/bin/python3 -c 'from pathlib import Path; Path(\"/workspace/test.txt\").write_text(\"hello\\\\n\")'",
                "working_directory": "/workspace",
                "description": "Create a test file",
                "target_resource": "/workspace",
                "justification": "Testing the E2E flow",
            })
            auth = json.loads(result.content[0].text)
            print(f"Council decision: {auth['status']}")

            if auth["status"] != "approved":
                print(f"Denied: {auth.get('guidance', auth.get('error'))}")
                return

            # 3. Submit the signed writ to the executor
            writ_blob = auth["writ"]

    req = urllib.request.Request(
        "http://localhost:9907/v1/writs/execute",
        data=json.dumps({"writ": writ_blob}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-ID": agent_id,
        },
    )
    with urllib.request.urlopen(req) as resp:
        exec_result = json.loads(resp.read())

    print(f"Executed: success={exec_result['success']}, exit_code={exec_result['exit_code']}")

asyncio.run(authorize_and_execute())
```

The executor rejects execution calls without an `X-Agent-ID` header matching the writ's `target_agent_id`.

### Programmatic (Same Process)

```python
from nomos.level_0.types import Intent, CouncilMember, AgentRole
from nomos.level_4.council import SecurityCouncil, CouncilConfig
from nomos.level_3.executor import NoOpHandler

config = CouncilConfig(
    council_id="my-council",
    require_justification=True,
    default_token_ttl_seconds=300,
)
council = SecurityCouncil(config=config)
council.register_agent("my-agent", AgentRole.APPLICATION)
council.register_action_handler(NoOpHandler())

intent = Intent.create(
    agent_id="my-agent",
    action_type="noop",
    description="Test action",
    target_resource="/test",
    justification="Testing the system",
)

result = council.submit_intent(intent)
if result.requires_approval:
    council.approve_intent(intent.intent_id, "alice")
exec_result = council.execute_intent(intent.intent_id)
print(f"Success: {exec_result.success}")
```

---

## MCP Tools Reference

The council exposes sixteen MCP tools via NVIDIA NeMo Agent Toolkit: six
authorization/status operations, explicit v1/v2 aliases for authorize and
propose, and first-class dynamic council management tools. Connect with any MCP
client:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client("http://localhost:9901/mcp") as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool("council_authorize", arguments={...})
```

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `council_authorize` | **Full authorization flow** — authenticate, evaluate, mint writ | `auth_token`, `agent_id`, `command`, `working_directory`, `description`, `justification` |
| `council_propose_intent` | Submit intent for evaluation (without minting writ) | `auth_token`, `agent_id`, `command`, `description`, `target_resource`, `justification` |
| `council_v1_authorize` / `council_v1_propose_intent` | Explicit v1 aliases | Same as v1-compatible tools |
| `council_v2_authorize` / `council_v2_propose_intent` | Explicit multi-council tools | Same as above, with required `council_id` |
| `council_check_status` | Check intent/workflow status | `intent_id` |
| `council_get_public_key` | Get council's Ed25519 public key | — |
| `council_check_writ_status` | Check writ lifecycle (valid/expired/used) | `writ_id` |
| `council_pending_approvals` | List intents awaiting approval | — |
| `council_list_councils` | List configured dynamic council profiles | optional `management_token` |
| `council_show_council` | Describe one council profile | `council_id`, optional `management_token` |
| `council_create_council` | Create a generated profile from a server-side template | `management_token`, `council_id`, `template`, `enabled` |
| `council_put_council_profile` | Create or replace a full structured profile | `management_token`, `council_id`, `profile`, optional `enabled` |
| `council_set_council_enabled` | Enable or disable a dynamic council | `management_token`, `council_id`, `enabled` |
| `council_validate_catalog` | Validate the catalog and referenced profiles | optional `management_token` |

Mutating dynamic-council tools are fail-closed unless the service environment
sets `COUNCIL_MANAGEMENT_TOKEN` or `NOMOS_COUNCIL_MANAGEMENT_TOKEN`. Read-only
list/show/validate calls are unauthenticated by default and can be locked down
by setting `allow_unauthenticated_read: false` on those tool configs.

### Trusted Executor API

Executors conform to the trusted executor contract. The bundled
`executors/docker/` service is a local subprocess example, and
`executors/openshell/` is an OpenShell backend adapter. Production deployments
can replace either with any service that implements the same endpoints.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/writs/execute` | POST | Submit a signed writ for execution. Body: `{"writ": "<base64 blob>"}` plus `X-Agent-ID: <agent id>` |
| `/v1/health` | GET | Health check with contract version, capabilities, uptime, and key fingerprint |
| `/.well-known/nomos/trusted-executor.json` | GET | Machine-readable contract descriptor |
| `/execute`, `/health` | POST/GET | Legacy aliases kept for existing clients |

Nomos executors generate an executor-owned Ed25519 key and attach signed
execution evidence to responses. The evidence binds the writ id, exact approved
command, attachment hashes, stdout/stderr hashes, exit code, and verification
stage so clients can detect tampering after the fact.

---

## Kernel Internals

### Permission Scopes

The `categorize_intent` function classifies commands into 7 permission scopes:

| Scope | Example Commands |
|-------|-----------------|
| `SHELL_READ` | `ls`, `cat`, `ps`, `grep`, `date`, `echo` |
| `SHELL_WRITE` | `mkdir`, `cp`, `mv`, `chmod`, `chown` |
| `FILE_WRITE` | `>`, `>>`, `touch`, `tee` |
| `FILE_DELETE` | `rm`, `rmdir` |
| `INFRA_DEPLOY` | `kubectl apply`, `terraform apply`, `docker run`, `helm install` |
| `INFRA_DESTROY` | `kubectl delete`, `terraform destroy` |
| `FILE_READ` | `cat`, `head`, `tail`, `grep`, `find` |

### Council Members

Council members are LLM agents with configurable evaluation priorities. The shipped config uses NVIDIA Cloud Functions inference backends (`nv-opus`, `nv-gemini`, `nv-gpt`) while direct Anthropic/OpenAI/NIM backends remain available.

```yaml
# configs/council.yaml
members:
  - id: command-integrity
    display_name: "Command Integrity Reviewer"
    backend: nv-opus
    priorities:
      - "Verify command matches the stated intent"
      - "Check for injection or hidden side effects"
      - "Validate parameters are reasonable"

  - id: environment-isolation
    display_name: "Environment Isolation Reviewer"
    backend: nv-gemini
    priorities:
      - "Ensure command stays within scoped resources"
      - "Check for environment variable leaks"

  - id: blast-containment
    display_name: "Blast Containment Reviewer"
    backend: nv-gpt
    priorities:
      - "Assess worst-case impact"
      - "Verify rollback is possible"
```

### Authentication

Agent accounts are stored in Redis with SHA-256 hashed tokens:

```bash
# Create named accounts
./scripts/agent_accounts.sh create alice-agent --role developer
./scripts/agent_accounts.sh rotate-token alice-agent
./scripts/agent_accounts.sh disable alice-agent

# Optional local demo fixtures, for development machines with Python installed
python3 scripts/seed_accounts.py
```

### Redis Key Schema

All keys are prefixed with `nomos:` to avoid collisions:

| Key Pattern | Type | Purpose |
|-------------|------|---------|
| `nomos:accounts:{agent_id}` | Hash | Token hash, display name, enabled flag |
| `nomos:accounts:{agent_id}:perms` | Set | Permission scope strings |
| `nomos:nonces:{hex}` | String | Writ ID (SET NX + EX for atomic claim + TTL) |
| `nomos:writs:{writ_id}` | Hash | Full writ metadata |
| `nomos:writs:{writ_id}:outcome` | Hash | Execution result (exit code, stdout hash, etc.) |
| `nomos:intents:{intent_id}` | Hash | Intent request + decision + timestamps |
| `nomos:rate:{agent_id}:{window}` | String | Rate limiting counters |

---

## Testing

### Test Suite Overview

The core suite currently collects 700+ tests:

```bash
# Core package
uv run pytest tests/ -v

# Standard local regression wrapper and report
uv run ./scripts/run_test_suite.py --profile integration

# Deployed MCP/gRPC checks after Kind or Compose is running
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed

# By layer
uv run pytest tests/level_0/   # Clock, Crypto, Types
uv run pytest tests/level_1/   # Ledger, Policy
uv run pytest tests/level_2/   # Capability, Quorum
uv run pytest tests/level_3/   # Executor
uv run pytest tests/level_4/   # Council integration

# Writ system
uv run pytest tests/test_writ_factory.py
uv run pytest tests/test_enforcer.py
uv run pytest tests/test_e2e_writ_lifecycle.py
uv run pytest tests/test_key_rotation.py

# Auth system
uv run pytest tests/test_authenticator.py
uv run pytest tests/test_chairman_preauth.py
uv run pytest tests/test_permissions.py

# Infrastructure
uv run pytest tests/test_redis_store.py
```

### Live E2E Tests (Requires Running Stack)

```bash
# Compose-local E2E against the default OpenShell executor (port 9907)
uv run python scripts/test_live_e2e.py

# Compose-local E2E against a specific executor profile
uv run python scripts/test_live_e2e.py 9903 deploy-executor-dev-1
```

The current script tests **3 live scenarios** against Compose containers:

- **Authorized execution** — council approves, Writ is minted, executor creates a file, and the file is verified.
- **Full-path compliance** — bare `echo` is denied while `/bin/echo` is approved for the same low-risk intent.
- **Destructive command review** — council evaluates `rm -rf` and prints the decision and reasoning.

For the full post-deploy MCP validation path, including Kind-safe checks, see
[Council MCP Validation](docs/council-mcp-validation.md).

---

## Configuration Reference

### Council Config (`configs/council.yaml`)

```yaml
council_id: "prod-council"
council_key_path: "/app/keys"

redis:
  host: redis
  port: 6379
  db: 0
  key_prefix: "nomos:"

llm_backends:
  nv-opus:
    provider: nv_inference
    model: azure/anthropic/claude-opus-4-6
    api_key: "${NVIDIA_API_KEY}"

members:
  - id: command-integrity
    display_name: "Command Integrity Reviewer"
    backend: nv-opus
    priorities: [...]
```

### Executor Config

The executor loads configuration from a YAML file (preferred) or environment variables (fallback). YAML config is validated at startup via Pydantic models — bad regex, invalid ports, or missing fields produce clear error messages.

**Loading order:** `EXECUTOR_CONFIG` env var → YAML file → env var overrides for select fields.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EXECUTOR_CONFIG` | — | Path to `executor.yaml` (enables YAML mode) |
| `EXECUTOR_PUBLIC_KEY_PATH` | — | Path to council's `council.pub` (32 bytes, raw Ed25519) |
| `EXECUTOR_COUNCIL_URL` | — | Alternative: fetch key from council MCP (fallback) |
| `EXECUTOR_REDIS_URL` | `redis://localhost:6379/0` | Redis for nonce claims |
| `EXECUTOR_REDIS_PREFIX` | `nomos:` | Redis key namespace (must match council's `redis.key_prefix`) |
| `EXECUTOR_ALLOWED_DIRS` | `/workspace` | Colon-separated allowed working directories |
| `EXECUTOR_HOST` | `127.0.0.1` | Listen address (`0.0.0.0` in container mode) |
| `EXECUTOR_PORT` | `9906` in the default OpenShell container; `9902` for legacy Docker | Internal listen port |
| `EXECUTOR_OPENSHELL_PORT` | `9907` | Host port published by the default OpenShell Compose profile |
| `EXECUTOR_MAX_TIMEOUT` | `120` in the default OpenShell profile; `60` for legacy Docker | Max command execution time (seconds) |
| `EXECUTOR_LOG_LEVEL` | `INFO` | Log verbosity |
| `EXECUTOR_PRIVATE_KEY_PATH` | — | Writable executor-owned Ed25519 key for result evidence |
| `EXECUTOR_PUBLIC_KEY_EXPORT_PATH` | — | Optional raw public-key export path for evidence verification |
| `OPENSHELL_ENDPOINT` | — | OpenShell backend URL for `executor-openshell` |
| `OPENSHELL_REQUEST_TIMEOUT` | `30` | OpenShell backend request timeout |
| `EXECUTOR_UID` / `EXECUTOR_GID` | `10001` | Container process UID/GID (set via compose `user:`) |

Environment variables override the corresponding YAML values for: `EXECUTOR_PUBLIC_KEY_PATH`, `EXECUTOR_REDIS_URL`, `EXECUTOR_HOST`, `EXECUTOR_PORT`, `EXECUTOR_LOG_LEVEL`, executor identity paths, and OpenShell backend settings.

### Risk-Based TTLs

| Risk Level | Writ TTL | Set By |
|------------|----------|--------|
| `low` | 300 seconds | Chairman pre-auth |
| `medium` | 120 seconds | Chairman pre-auth |
| `high` | 60 seconds | Chairman pre-auth |
| `critical` | 30 seconds | Chairman pre-auth |

---

## Project Structure

```
Nomos/
├── nomos/
│   ├── level_0/              # Clock, Crypto, Types (WritOfExecution, Intent)
│   ├── level_1/              # LedgerChain, PolicyEngine
│   ├── level_2/              # CapabilityService, QuorumManager
│   ├── level_3/              # SandboxExecutor, Verifier
│   ├── level_4/              # SecurityCouncil orchestrator
│   ├── auth/                 # Authenticator, Permissions, WritFactory
│   ├── crypto/               # CouncilKeyManager
│   ├── config/               # CouncilSettings, RedisConfig
│   ├── llm/                  # LLM backends (Anthropic, OpenAI, NIM)
│   ├── nat/                  # NAT/MCP integration (functions, plugin)
│   ├── store/                # RedisStore
│   └── logging.py            # Structured JSON audit logging
├── trusted_executor_contract/ # Shared executor HTTP contract models
│   └── models.py             # Writ, request/response, health, descriptor, protocol
├── executors/
│   ├── common/               # Executor identity + signed evidence helpers
│   ├── docker/               # Local subprocess trusted executor
│   └── openshell/            # OpenShell trusted executor adapter
├── deploy/
│   ├── docker-compose.yml    # Redis + OPA + monitor + Council + executors
│   ├── Dockerfile            # Council container
│   ├── Dockerfile.executor   # Executor sandbox container (kubectl included)
│   ├── Dockerfile.openshell  # Local OpenShell-compatible runtime container
│   ├── dynamic-config/       # Runtime-mounted council catalog/profile copies
│   ├── executor.yaml         # Legacy Docker executor config (restrictive)
│   ├── executor-dev.yaml  # dev executor config
│   └── executor-openshell.yaml  # OpenShell executor config
├── configs/
│   ├── council.yaml          # Council configuration
│   └── council_nat_workflow.yaml  # NAT MCP workflow
├── scripts/
│   ├── agent_accounts.py     # Manage agent accounts in Redis
│   ├── seed_accounts.py      # Seed local demo accounts
│   └── test_live_e2e.py      # Live E2E test script
└── tests/                    # Core package tests
```

---

## Executor Profiles

The executor uses Docker Compose **profiles** to support multiple configurations from a single `docker-compose.yml`. Each profile has its own YAML config, host port, workspace volume, and optionally bind-mounted host directories.

### How Profiles Work

```
docker-compose.yml
├── redis              (always starts, no profile)
├── opa                (always starts, no profile)
├── council            (always starts, no profile)
├── monitor            (always starts, no profile)
├── council-grpc       (always starts, no profile)
├── executor-openshell (profile: executor-openshell, default)
├── openshell          (profile: executor-openshell, isolated runtime)
├── executor-default   (profile: executor-default)
└── executor-dev
                       (profile: executor-dev)
```

A shared YAML anchor (`x-executor-base`) defines the security baseline inherited by all executors:

| Setting | Value |
|---------|-------|
| `cap_drop` | ALL |
| `security_opt` | `no-new-privileges:true` |
| `read_only` | `true` |
| `tmpfs` | `/tmp` (noexec, nosuid, 256MB) |
| `user` | `${EXECUTOR_UID:-10001}:${EXECUTOR_GID:-10001}` |

Each profile then overrides only what it needs: port mapping, config file mount, volumes, and user identity.

### Built-in Profiles

#### `executor-default` — Legacy Restrictive Baseline

```bash
docker compose -f deploy/docker-compose.yml --profile executor-default up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9902 |
| **Config** | `deploy/executor.yaml` |
| **Timeout** | 60 seconds |
| **Allowed dirs** | `/workspace` |
| **User** | UID 10001 (executor) |
| **Workspace** | Docker volume (`executor-default-workspace`) |
| **Tools** | Safe read/write/dev set (echo, cat, ls, git, python3, curl, etc.) |

#### `executor-dev` — Development Executor

```bash
DEV_REPO_PATH=/path/to/my-project \
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose -f deploy/docker-compose.yml \
    --profile executor-dev up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9903 (direct — `network_mode: host`) |
| **Config** | `deploy/executor-dev.yaml` |
| **Network** | Host network (required for kind/k8s clusters on `127.0.0.1`) |
| **Timeout** | 300 seconds (codegen is slow) |
| **Allowed dirs** | `/workspace`, `/workspace/projects`, `/tmp`, `/workspace/projects/my-project` |
| **User** | Host user's UID/GID (for correct file ownership on bind mounts) |
| **Workspace** | Docker volume + bind-mounted host repo |
| **Extra tools** | node, npm, npx, make, cmake, cargo, kubectl |
| **Extra mounts** | `DEV_REPO_PATH` at `/workspace/projects/my-project`, `${HOME}/.kube` (read-only) |
| **kubectl safety** | `delete namespace`, `delete node`, `drain`, `cordon` all blocked |

> **Note:** `network_mode: host` means this profile shares the host's network stack. It can reach kind clusters, Redis, and the council on `localhost` directly. This trades some network isolation for k8s accessibility — appropriate for dev, not production.

#### `executor-openshell` — OpenShell Adapter

```bash
docker compose -f deploy/docker-compose.yml --profile executor-openshell up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9907 |
| **Config** | `deploy/executor-openshell.yaml` |
| **Backend** | Buildable `openshell` runtime service, internal `POST /v1/execute` |
| **Evidence** | Executor-owned Ed25519 key in `executor-openshell-keys` |
| **Runtime network** | `openshell-net`, internal-only and shared only with the trusted adapter |
| **Tools** | Bash, Python 3, and basic local file utilities |

Set `OPENSHELL_ENDPOINT` only when routing the trusted adapter to an external
OpenShell deployment instead of the bundled local runtime.

### UID/GID Passthrough

When bind-mounting host directories, the executor process must run as your host UID so that files it creates are owned by you — not by some container-internal user. The `EXECUTOR_UID` and `EXECUTOR_GID` variables control this:

```bash
# Default OpenShell profile: adapter runs as UID 10001 and runtime as UID 10002
docker compose --profile executor-openshell up -d

# Dev profile: runs as YOUR user so bind-mounted files are owned correctly
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose --profile executor-dev up -d
```

The default profile defaults to UID 10001 (no host mounts needed). Dev profiles default to UID 1000 but should always be started with explicit `EXECUTOR_UID`/`EXECUTOR_GID`.

---

## Executor YAML Configuration

Each executor profile is driven by a YAML file mounted into the container at `/app/config/executor.yaml`. The file is validated at startup by Pydantic models (`executors/docker/config_schema.py`) — invalid regex, bad ports, relative paths, or extra keys all produce clear error messages.

### Default OpenShell Profile Shape

```yaml
# ── Executor server ────────────────────────────────────
executor:
  listen_host: "0.0.0.0"           # bind address
  listen_port: 9906                 # internal OpenShell adapter port; host defaults to 9907
  log_level: "INFO"                 # DEBUG, INFO, WARNING, ERROR
  max_command_timeout: 120          # hard limit per command (seconds)
  identity_private_key_path: "/app/executor-keys/executor.key"
  identity_public_key_path: "/app/executor-keys/executor.pub"

# ── Council connection ─────────────────────────────────
council:
  public_key_path: "/app/public-keys/council.pub"  # Ed25519 public key
  url: "http://council:9901"                # fallback: fetch key via HTTP
  key_fetch_retries: 10
  key_fetch_delay: 3                        # seconds between retries

# ── Redis ──────────────────────────────────────────────
redis:
  url: "redis://redis:6379/0"
  key_prefix: "nomos:"               # must match council's prefix

# ── Filesystem access ──────────────────────────────────
filesystem:
  allowed_working_dirs:            # ONLY these dirs can be working_directory
    - /workspace
  denied_paths:                    # substring match — always rejected
    - /etc/shadow
    - .ssh
    - .aws

# ── Tool whitelist ─────────────────────────────────────
tools:
  allowed:                         # empty list = allow all (not recommended)
    - echo
    - cat
    - git
    - python3
  denied_args:                     # per-tool regex restrictions
    rm:
      - '-rf\s+/\s*$'
    git:
      - 'push\s+--force'

# ── Security ───────────────────────────────────────────
security:
  blocked_patterns:                # additional regex (on top of hardcoded defaults)
    - 'curl\s+.*--upload-file'
    - '/dev/tcp/'
```

### Security Layers (Defense in Depth)

The executor enforces **four independent policy layers**, each configurable per-profile:

| Layer | Config Key | How It Works | Hardcoded Defaults? |
|-------|-----------|--------------|---------------------|
| **Blocked patterns** | `security.blocked_patterns` | Regex match against full command string | Yes — fork bombs, sudo, su, pipe-to-shell, raw device access, mkfs, netcat, http.server, rm --no-preserve-root, chmod 777 |
| **Tool whitelist** | `tools.allowed` | Only listed binaries can execute (basename match) | Yes — safe read/write/dev set |
| **Denied tool args** | `tools.denied_args` | Per-tool regex against full command string | Yes — rm -rf /, git push --force, git reset --hard, curl PUT/DELETE, chmod 777 |
| **Denied paths** | `filesystem.denied_paths` | Substring match against full command string | Yes — /etc/shadow, /etc/passwd, /root, /proc, /sys, .ssh, .gnupg, .aws, .kube/config |

YAML values are **merged** with hardcoded defaults — you can add restrictions but cannot remove the built-in ones. This ensures defense in depth even if a config file is misconfigured.

### Blocked Patterns (Recommended Set)

Both built-in profiles include these additional patterns beyond the hardcoded defaults:

```yaml
security:
  blocked_patterns:
    # credential / secret exfiltration
    - 'env\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - 'printenv\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - 'base64.*(?:\.pem|\.key|\.crt)'
    # network exfiltration
    - 'curl\s+.*-d\s+@'
    - 'curl\s+.*--upload-file'
    - 'wget\s+--post-file'
    # reverse shells
    - '/dev/tcp/'
    - 'bash\s+-i\s+>'
    - 'exec\s+\d+<>/dev/tcp'
    # history / credential file reads
    - 'cat.*\.bash_history'
    - 'cat.*\.env'
    # package supply-chain
    - 'pip\s+install\s+--index-url'
    - 'npm\s+config\s+set\s+registry'
```

---

## Creating a Custom Writ Executor

To create an executor profile for your own project, you need three things:

1. **A YAML config file** — defines the security policy
2. **A service entry in docker-compose.yml** — wires up ports, volumes, and identity
3. **Optionally: Dockerfile changes** — if you need additional system packages

### Step 1: Create Your Config YAML

Copy the default config and customize it:

```bash
cp deploy/executor.yaml deploy/executor-my-project.yaml
```

Edit the key sections:

```yaml
executor:
  max_command_timeout: 180          # adjust for your workload

filesystem:
  allowed_working_dirs:
    - /workspace
    - /home/youruser/Development/my-project   # bind-mounted host repo
  denied_paths:
    - /etc/shadow
    - /etc/passwd
    - .ssh
    - .gnupg
    - .aws

tools:
  allowed:
    # start with the minimum set you need
    - echo
    - cat
    - ls
    - grep
    - find
    - mkdir
    - cp
    - rm
    - touch
    - python3
    - pip
    - git
    - sh
    # add project-specific tools:
    - npm
    - node
    - kubectl
  denied_args:
    rm:
      - '-rf\s+/\s*$'
      - '--no-preserve-root'
    git:
      - 'push\s+--force'
      - 'reset\s+--hard'

security:
  blocked_patterns:
    - 'env\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - '/dev/tcp/'
    - 'bash\s+-i\s+>'
    # add project-specific patterns as needed
```

### Step 2: Add a Compose Service

Add a new service to `deploy/docker-compose.yml` that inherits from the shared base:

```yaml
  # ── Executor: My Project (dev) ────────────────────────────
  executor-my-project:
    <<: *executor-base
    profiles: ["executor-my-project"]
    user: "${EXECUTOR_UID:-1000}:${EXECUTOR_GID:-1000}"   # host user for bind mounts
    ports:
      - "9905:9902"                                        # unique host port; 9904 is council-grpc
    environment:
      - EXECUTOR_PUBLIC_KEY_PATH=/app/public-keys/council.pub
      - EXECUTOR_REDIS_URL=redis://redis:6379/0
      - EXECUTOR_HOST=0.0.0.0
      - EXECUTOR_PORT=9902
      - EXECUTOR_CONTAINER_MODE=1
      - EXECUTOR_CONFIG=/app/config/executor.yaml
    volumes:
      - council-public-keys:/app/public-keys:ro
      - ./executor-my-project.yaml:/app/config/executor.yaml:ro
      - executor-my-project-workspace:/workspace
      # Bind-mount your host repo (same path inside and outside)
      - /home/youruser/Development/my-project:/home/youruser/Development/my-project
```

Add the workspace volume:

```yaml
volumes:
  # ... existing volumes ...
  executor-my-project-workspace:
    driver: local
```

### Step 3: Mount Credentials (Optional)

If your project needs kubectl, cloud CLI, or other credential-based tools:

```yaml
    environment:
      # ... existing env vars ...
      - KUBECONFIG=/home/executor/.kube/config
      - AWS_CONFIG_FILE=/home/executor/.aws/config
    volumes:
      # ... existing volumes ...
      - ${HOME}/.kube:/home/executor/.kube:ro          # always read-only!
      - ${HOME}/.aws:/home/executor/.aws:ro
```

**Important:** Always mount credentials as `:ro` (read-only). Remove the corresponding entry from `denied_paths` in your YAML config if you intentionally mount it (e.g., remove `.kube/config` if you mount `~/.kube`).

### Step 4: Add System Packages (Optional)

If your project needs packages not in the base image, you have two options:

**Option A: Modify `Dockerfile.executor`** (shared by all profiles)

```dockerfile
# Add to the apt-get install block:
RUN apt-get update && apt-get install -y --no-install-recommends \
    your-package \
    && rm -rf /var/lib/apt/lists/*
```

**Option B: Use a separate Dockerfile** (profile-specific)

```yaml
  executor-my-project:
    build:
      context: ..
      dockerfile: deploy/Dockerfile.executor.my-project   # custom Dockerfile
    # ... rest of config ...
```

### Step 5: Start and Test

```bash
# Start your profile
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose -f deploy/docker-compose.yml \
    --profile executor-my-project up -d --build

# Verify health
curl -s http://127.0.0.1:9905/v1/health | python3 -m json.tool

# Run E2E tests against your profile
uv run python scripts/test_live_e2e.py 9905 deploy-executor-my-project-1
```

### Path Matching Strategy

When agents formulate commands, the `working_directory` in the writ must match an entry in `allowed_working_dirs`. For bind-mounted host directories, **use the same path inside and outside the container** so agents don't need to translate paths:

```yaml
# In your executor YAML:
filesystem:
  allowed_working_dirs:
    - /home/youruser/Development/my-project

# In docker-compose.yml volumes:
volumes:
  - /home/youruser/Development/my-project:/home/youruser/Development/my-project
```

This way, when the agent says `working_directory: /home/youruser/Development/my-project`, the executor sees the same path and the files are the same.

### Checklist for New Profiles

- [ ] YAML config file created in `deploy/`
- [ ] `allowed_working_dirs` includes all directories the agent needs
- [ ] `tools.allowed` is minimal — only what the project requires
- [ ] `denied_paths` excludes sensitive files (credentials, system config)
- [ ] `security.blocked_patterns` is populated (not empty)
- [ ] Compose service added with unique host port
- [ ] `user:` set to `${EXECUTOR_UID}:${EXECUTOR_GID}` if using bind mounts
- [ ] Credential mounts are `:ro`
- [ ] Workspace volume declared in `volumes:` section
- [ ] Health check verified: `curl http://localhost:<port>/v1/health`

---

## Client Integration

Nomos is designed to be consumed by any LLM-based IDE agent (Cascade/Windsurf, Cursor, Copilot, etc.) or autonomous agent that can make HTTP/MCP calls. The reference integration is with **Windsurf Cascade**.

### Integration Pattern

The recommended integration uses a thin CLI wrapper script that the IDE agent calls via its `run_command` facility:

```
IDE Agent  →  run_command("scripts/nomos_execute.py --command '...' ...")
                    │
                    ├─ 1. Self-check: reads /proc/{ppid}/cmdline, rejects shell injection
                    ├─ 2. Calls council_authorize via MCP → gets signed writ
                    └─ 3. POSTs writ to executor /v1/writs/execute → returns {stdout, stderr, exit_code}
```

The CLI wrapper (`nomos_execute.py`) lives in the client project, not in Nomos. It reads connection details from `~/.config/nomos/<agent>.json`:

```json
{
  "auth_token": "<agent token from agent_accounts.py>",
  "agent_id": "cascade",
  "council_url": "http://localhost:9901/mcp",
  "executor_url": "http://localhost:9903"
}
```

### IDE Safety Layers

When integrated with an IDE agent, the full protection stack is:

| Layer | Where | What It Does |
|-------|-------|-------------|
| **IDE rule** | Agent config | Instructs the agent to route all commands through the CLI wrapper |
| **Allowlist/Denylist** | IDE settings | Auto-runs wrapper calls; denies shell operators (`&&`, `\|`, `;`) |
| **Script self-check** | CLI wrapper | Reads parent process cmdline, rejects shell injection (exit 99) |
| **Council** | Nomos | LLM-backed council evaluates and votes on the command |
| **Writ executor** | Nomos | Cryptographic verification + sandboxed execution |
| **Executor profile** | Nomos | Tool whitelist, denied args, denied paths, blocked patterns |

### Shell Injection Prevention

The CLI wrapper supports two command input modes to prevent shell metacharacter injection through the IDE's allowlist:

- `--command "kubectl get pods"` — direct text, safe for commands without shell operators
- `--command-b64 "a3ViZWN0bC..."` — base64-encoded, required when the command contains `>`, `|`, `&&`, `;`
- `--encode "/bin/echo hello > /tmp/test"` — utility mode that prints the base64 encoding and exits

The wrapper also performs a runtime self-check by reading `/proc/{ppid}/cmdline` to detect if it was invoked with chained shell operators (e.g., `scripts/nomos_execute.py ... && rm -rf /`). If detected, it exits with code 99 before doing anything.

---

## Contributing

This project is currently not accepting contributions.

---

## License

Apache License 2.0

This project will download and install additional third-party open source software projects. Review the license terms of these open source projects before use.

---

## Acknowledgments

- Architecture inspired by John Lakos's work on large-scale C++ software design
- Built for integration with NVIDIA NeMo Agent Toolkit
- MCP protocol support for universal agent compatibility

</details>
