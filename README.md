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

### 1. Prerequisites

- Docker and Docker Compose
- Python 3.11+
- An NVIDIA API key (the shipped council config uses NVIDIA Cloud Functions inference by default)

### 2. Configuration Map

```
 YOU EDIT THESE                        THEY CONFIGURE THESE
 ─────────────                         ────────────────────

 deploy/.env                           docker-compose.yml reads this automatically
 ┌──────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ NVIDIA_API_KEY=nvapi-...     │─────>│ council container (env var)              │
 │ ANTHROPIC_API_KEY=sk-ant-... │─────>│ optional direct-vendor backend           │
 │ OPENAI_API_KEY=sk-...        │─────>│ council container (env var)              │
 │ REDIS_PASSWORD=nomos │─┬───>│ redis --requirepass                      │
 └──────────────────────────────┘ ├───>│ council COUNCIL_REDIS_PASSWORD env var   │
                                  └───>│ executor EXECUTOR_REDIS_URL password     │
                                       └──────────────────────────────────────────┘

 configs/local_council_catalog.yaml    Which councils share one MCP service
 configs/council.yaml                  Default council profile behavior
 deploy/dynamic-config/                Runtime catalog/profile copies for Docker deploys
 ┌──────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ default_council_id:          │─────>│ v1/unversioned requests route here       │
 │   council-prod               │      │                                          │
 │ councils:                    │─────>│ Named profiles selected by council_id    │
 │   council-prod:              │      │                                          │
 │   # add more profiles here   │      │                                          │
 │                              │      │                                          │
 │ llm_backends:                │─────>│ Which LLM providers to call              │
 │   nv-opus:                   │      │ (api_key uses ${ENV_VAR} from above)     │
 │     api_key: ${NVIDIA_...}   │      │                                          │
 │                              │      │                                          │
 │ members:                     │─────>│ Who votes and with what priorities        │
 │   - id: command-integrity    │      │ Each member references a backend by name  │
 │     backend: nv-opus         │      │                                          │
 │     priorities: [...]        │      │                                          │
 │                              │      │                                          │
 │ chairman:                    │─────>│ Risk threshold decisions (unanimous by    │
 │   enabled: true              │      │ default; quorum only if explicitly enabled)│
 │ code_review:                 │─────>│ Sidecar review for packaged file content  │
 │ opa:                         │─────>│ Optional pre-council OPA intent filter    │
 │                              │      │                                          │
 │ redis:                       │─────>│ Where to store accounts, nonces, writs   │
 │   host: ${COUNCIL_REDIS_HOST}│      │ (password uses ${ENV_VAR} from above)    │
 │   password: ${COUNCIL_RED..} │      │                                          │
 │ ./scripts/councils.sh        │─────>│ Add/update/enable dynamic councils from  │
 │ create/update-from-file      │      │ inside the council container             │
 └──────────────────────────────┘      └──────────────────────────────────────────┘

 deploy/executor.yaml                  Executor sandbox policy
 ┌──────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ filesystem:                  │─────>│ Which directories commands can run in    │
 │   allowed_working_dirs: [..] │      │                                          │
 │   denied_paths: [...]        │      │                                          │
 │                              │      │                                          │
 │ tools:                       │─────>│ Which binaries are allowed (echo, cat,   │
 │   allowed: [echo, cat, ...]  │      │ git, python3, ...) — everything else     │
 │   denied_args:               │─────>│ is blocked. Per-tool arg restrictions    │
 │     rm: ['-rf /']            │      │ (e.g. git push --force)                  │
 │                              │      │                                          │
 │ security:                    │─────>│ Regex patterns that block commands       │
 │   blocked_patterns: [...]    │      │ (fork bombs, sudo, reverse shells, ...)  │
 └──────────────────────────────┘      └──────────────────────────────────────────┘

 deploy/docker-compose.yml             Service wiring
 ┌──────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ services:                    │      │                                          │
 │   redis:     (port 6379)     │─────>│ Authenticated key-value store            │
 │   opa:       (port 8181)     │─────>│ Optional OPA sidecar, mounted policies    │
 │   monitor:   (port 9901)     │─────>│ External MCP proxy + dashboard            │
 │   council:   (internal 9901) │─────>│ MCP server, reads catalog + profiles     │
 │   council-grpc: (port 9904)  │─────>│ gRPC proxy via monitor                    │
 │   executor:  (port 9907)     │─────>│ Writ verifier, reads executor-openshell  │
 └──────────────────────────────┘      └──────────────────────────────────────────┘

 scripts/agent_accounts.py             Agent accounts in Redis
 ┌──────────────────────────────┐      ┌──────────────────────────────────────────┐
 │ create alice-agent           │─────>│ Named account + one-time raw token       │
 │ --council council-dev        │─────>│ Optional non-default council membership  │
 │ rotate-token alice-agent     │─────>│ Revoke old token and issue a new one     │
 │ disable alice-agent          │─────>│ Temporarily remove access                │
 └──────────────────────────────┘      └──────────────────────────────────────────┘
```

### 3. Configure API Keys and Secrets

Set up your environment file:

```bash
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

```bash
# Required by the shipped council config
NVIDIA_API_KEY=nvapi-...

# Optional — switch member backends in configs/council.yaml to use these
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...

# OPA intent filtering is enabled by default for alpha access
COUNCIL_OPA_ENABLED=true

# Redis authentication (change this in production!)
REDIS_PASSWORD=nomos-dev

# Dynamic-council MCP mutations; deploy_stack.sh generates this when omitted
COUNCIL_MANAGEMENT_TOKEN=change-me-council-management
```

The deployment scripts read this file automatically. API keys are referenced in `configs/council.yaml` using `${ENV_VAR}` syntax and resolved at startup. To use a different LLM provider, uncomment its backend in `council.yaml` and set the key here.

### 4. Deploy the Stack

```bash
# One-command local Kind bootstrap from a clean checkout
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh

# From a fresh machine with Docker, Kind, kubectl, Helm, and Git
git clone <repo-url> Nomos
cd Nomos
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh

# Optional: use the legacy Docker Compose path explicitly
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh

# Manual Compose equivalent for Redis + OPA + Council + monitor + gRPC proxy + executor
docker compose -f deploy/docker-compose.yml up -d redis opa council monitor council-grpc
docker compose -f deploy/docker-compose.yml --profile executor-openshell up -d --build

# Create an agent account in Redis from inside the council container
./scripts/agent_accounts.sh create agent-developer --role developer

# Optional: create or update a non-default council profile at runtime
./scripts/councils.sh create customer-git --template git-workflow-local
./scripts/councils.sh update-from-file customer-git ./scratch/customer-git.yaml --enabled

# Optional demo fixtures, for development machines with Python installed
python3 scripts/seed_accounts.py
```

The default Kind path also starts local port-forwards for the monitor, agent
signup, demo, and executor endpoints. Set `KIND_PORT_FORWARD=0` to skip that
step, or override individual local ports with `MONITOR_PORT`,
`AGENT_SIGNUP_PORT`, `DEMO_PORT`, and `EXECUTOR_OPENSHELL_PORT`.

This gives you these services:

| Service | URL | Purpose |
|---------|-----|---------|
| **Redis** | `localhost:6379` | State store (accounts, nonces, writs, audit). Requires authentication. |
| **Monitor / MCP** | `http://localhost:9901` | External MCP reverse proxy and observability dashboard. |
| **Council** | Docker network only | Internal MCP server with LLM-backed council members. |
| **Council gRPC** | `127.0.0.1:9904` | Thin gRPC proxy forwarding to MCP through the monitor. Bound to localhost only. |
| **OPA** | `http://localhost:8181` | Optional front-door intent policy service. The bundled service has no compose profile and is started by the default stack. |
| **Executor** | `http://localhost:9907` | Writ verification + isolated OpenShell-backed command execution |

The account CLI supports named identities with role presets:

| Role | Permissions |
|------|-------------|
| `readonly` | `shell:read`, `file:read` |
| `developer` | `shell:read`, `shell:write`, `file:read`, `file:write` |
| `operator` | developer permissions plus `infra:deploy` |
| `admin` | all permission scopes |

### 5. Verify the Deployment

```bash
# Monitor and upstream council are healthy
curl -s http://localhost:9901/healthz | python3 -m json.tool

# gRPC proxy is healthy
NOMOS_GRPC_SMOKE_ADDR=127.0.0.1:9904 uv run pytest tests/test_grpc_live_smoke.py

# Executor is healthy and has the council's signing key
curl -s http://localhost:9907/v1/health | python3 -m json.tool
```

### 6. Make Requests Against the Council

There are two ways to interact with the council: the **interactive CLI** for ad-hoc testing, and the **Python API** for programmatic integration.

#### Option A: Interactive CLI

The built-in council runner provides a REPL where you submit intents as JSON:

```bash
python -m nomos.nat.run --config configs/local_council_catalog.yaml
```

```
Nomos Council - Interactive Mode
Council members: ['Command Integrity Reviewer', 'Environment Isolation Reviewer', ...]
==============================================================

Type 'quit' to exit, 'pending' to list pending approvals.

council> {"auth_token": "<agent-developer token>", "agent_id": "agent-developer", "command": "/bin/ls -la /workspace", "description": "List files in the workspace directory", "target_resource": "/workspace", "justification": "Need to check current workspace contents"}
```

The council evaluates the intent using all configured LLM members, runs any debate rounds, and returns the decision with per-member votes and reasoning.

CLI commands:
- Paste a JSON intent object to submit it for evaluation
- `pending` — list intents awaiting approval
- `status <intent_id>` — check the status of a specific intent
- `quit` — exit

#### Option B: Python — Full Authorization + Execution via MCP

This is the end-to-end path: council authorizes a command and mints a signed writ, then you submit the writ to the executor for sandboxed execution.

```python
import asyncio, json, urllib.request
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

AUTH_TOKEN = "<token from agent_accounts.py>"  # agent-developer's token
AGENT_ID  = "agent-developer"

async def authorize_and_execute():
    # ── Step 1: Connect to the council MCP server ──────────────
    async with streamablehttp_client("http://localhost:9901/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # ── Step 2: Request authorization ──────────────────
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

    # ── Step 3: Submit the signed writ to the executor ─────────
    req = urllib.request.Request(
        "http://localhost:9907/v1/writs/execute",
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

### 7. Run the Tests

```bash
# Core package tests
pytest tests/ -v

# Smoke test (requires the stack from step 4)
python scripts/test_smoke.py

# Live E2E tests (requires stack + executor)
python scripts/test_live_e2e.py

# Standard suite wrapper with an HTML report
./scripts/run_test_suite.py --profile all
```

### Further Reading

- [Getting Started Walkthrough](docs/interactive/getting-started.html)
- [Workflow Walkthrough](docs/interactive/workflow.html) — current browser-based flow from agent request to signed execution
- [Deployment Setup](docs/deployment-setup.md) / [Interactive Planner](docs/interactive/deployment.html) — executor profiles, key rotation, production setup
- [MCP Request Builder](docs/interactive/mcp-integration.html) — build correct council tool calls
- [Multi-Council Router](docs/interactive/multi-council.html) — understand council selection and membership
- [Writ Executor Walkthrough](docs/interactive/writ-executor.html) — see executor verification and local policy gates
- [Certified Evidence Verification](docs/interactive/certified-evidence-verification.html) — follow how signed evidence becomes eligible for `certified_evidence`
- [Deployment Parameter Reference](docs/deployment-setup.md#parameter-reference) — council YAML, executor YAML, env vars

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

- Docker and Docker Compose
- An NVIDIA API key for the shipped council config. Anthropic/OpenAI keys are only needed if you switch members to direct-vendor backends.

### Start the Stack

```bash
# Start core infrastructure + default OpenShell executor profile
NVIDIA_API_KEY=nvapi-... docker compose -f deploy/docker-compose.yml \
    --profile executor-openshell up -d

# Or start with a project-specific executor profile
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) NVIDIA_API_KEY=nvapi-... \
    docker compose -f deploy/docker-compose.yml \
    --profile executor-dev up -d

# Create an agent account from inside the council container
./scripts/agent_accounts.sh create agent-developer --role developer

# Verify
docker compose -f deploy/docker-compose.yml --profile executor-openshell ps
```

Compose Redis Sentinel topology:

```bash
# Direct Docker Compose
docker compose -f deploy/docker-compose.yml \
    -f deploy/docker-compose.redis-sentinel.yml \
    --profile executor-openshell up -d --build

# Or through the deployment script; Sentinel is the default Compose topology
DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

This starts three Redis containers and three Sentinel containers, and wires
council, agent-signup, and the standard executor profiles to discover the
current Redis master through Sentinel. Set `COMPOSE_REDIS_TOPOLOGY=standalone`
only for local single-Redis runs that explicitly do not need failover.

### Services

| Service | Port | Image | Purpose |
|---------|------|-------|---------|
| **redis** | 6379 (localhost only) | `redis:7-alpine` | Nonce store, accounts, rate limiting |
| **opa** | 8181 (localhost only) | `openpolicyagent/opa` | Default front-door OPA policy filter |
| **monitor** | 9901 | `deploy-monitor` | External MCP reverse proxy, dashboard, and metrics |
| **council** | 9901 internal | `deploy-council` | MCP server with LLM council members |
| **council-grpc** | 9904 (localhost only) | `deploy-council` | gRPC proxy that forwards to MCP through the monitor |
| **executor-openshell** | 9907 | `deploy-executor-openshell` | Default trusted executor; isolated OpenShell-backed sandbox |
| **executor-default** | 9902 | `deploy-executor-default` | Legacy restrictive Docker executor |
| **executor-dev** | 9903 | `deploy-executor-dev` | Dev executor with extended tools and host bind mounts |

Redis, OPA, Council, monitor, and the gRPC proxy are no-profile services. Executors are profile-gated — choose the executor profile you want to run.

### Key Management

The council generates an Ed25519 key pair at first startup (`/app/keys/council.key` + `council.pub`). The executor reads the public key from the same volume to verify writ signatures.

```bash
# View the council's public key fingerprint
curl -s http://localhost:9907/v1/health | jq .key_fingerprint
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
python3 -m pip install -e ".[dev]"
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
                "auth_token": "your-agent-token",  # from agent_accounts.py
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
pytest tests/ -v

# Standard whole-repo wrapper and report
./scripts/run_test_suite.py --profile all

# By layer
pytest tests/level_0/          # Clock, Crypto, Types
pytest tests/level_1/          # Ledger, Policy
pytest tests/level_2/          # Capability, Quorum
pytest tests/level_3/          # 8 tests  — Executor
pytest tests/level_4/          # 10 tests — Council integration

# Writ system
pytest tests/test_writ_factory.py       # Writ minting + serialization
pytest tests/test_enforcer.py           # Executor verification pipeline + local policy
pytest tests/test_e2e_writ_lifecycle.py # Full lifecycle (mock)
pytest tests/test_key_rotation.py      # 5 tests  — Multi-key executor

# Auth system
pytest tests/test_authenticator.py     # 9 tests  — Token auth (requires Redis)
pytest tests/test_chairman_preauth.py  # Chairman pre-authorization
pytest tests/test_permissions.py       # Permission categorization

# Infrastructure
pytest tests/test_redis_store.py       # Redis operations (requires Redis)
```

### Live E2E Tests (Requires Running Stack)

```bash
# E2E against the default OpenShell executor (port 9907)
python scripts/test_live_e2e.py

# E2E against a specific executor profile
python scripts/test_live_e2e.py 9903 deploy-executor-dev-1
```

Tests **8 scenarios** against live containers:
- **File creation** — council approves, executor creates file, file verified
- **Replay rejection** — same writ resubmitted, nonce check blocks it
- **Tamper rejection** — modified writ submitted, signature check blocks it
- **Unknown client** — forged writ with attacker’s key rejected at signature stage
- **Denied path** — command referencing `/etc/shadow` blocked by local policy
- **Tool whitelist** — non-whitelisted binary rejected
- **Denied tool args** — `git push --force` blocked by per-tool argument restrictions
- **Blocked pattern** — `sudo` command blocked by regex pattern match

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
curl -s http://localhost:9905/v1/health | jq .

# Run E2E tests against your profile
python scripts/test_live_e2e.py 9905 deploy-executor-my-project-1
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

## License

MIT

---

## Acknowledgments

- Architecture inspired by John Lakos's work on large-scale C++ software design
- Built for integration with NVIDIA NeMo Agent Toolkit
- MCP protocol support for universal agent compatibility

</details>
