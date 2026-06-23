<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos local dev deployments

Two local paths bring up a **service-mode** Nomos stack (PostgreSQL durable store +
Vault transit signing + redis sentinel + nginx `/mcp` gateway + monitor + executor /
OpenShell + agent-signup + OPA), at functional parity:

| Path | Orchestrator | One command | Vault auth |
|------|--------------|-------------|------------|
| **docker-compose** | Docker | `deploy/scripts/compose-up.sh` | AppRole |
| **Kind** | Kubernetes (kind) | `deploy/kubernetes/scripts/kind-up.sh` | kubernetes-auth |

This page covers **docker-compose**. For Kind, see [`kubernetes/README.md`](kubernetes/README.md).

## Prerequisites

- **Docker Engine** running, plus the **`docker compose` v2** plugin (`docker compose version`).
- An **LLM API key** — NVIDIA (`NVIDIA_API_KEY`), Anthropic, or OpenAI. The council validates
  `llm_backends` at boot and crash-loops without one. The first run prompts for it; or put it in
  `deploy/.env` beforehand.

Nothing else to set up — every other secret is generated for you.

## Quick start

One command brings up **and verifies** the whole service-mode stack:

```bash
deploy/scripts/compose-up.sh
```

On the first run it prompts for the LLM key, then builds the images (a few minutes). Re-runs are
idempotent and reuse `deploy/.env`. Add `-y` to skip all prompts (the LLM key must already be in
`deploy/.env`). When it finishes you'll see `VERIFY PASSED` and a summary of URLs + credentials.

`compose-up.sh` runs, in order:

1. **Preflight** — checks `docker` + the `docker compose` v2 plugin.
2. **LLM key (required)** — prompts for `NVIDIA_API_KEY` (or Anthropic/OpenAI); the council
   validates `llm_backends` at boot and crash-loops without one. Stored in `deploy/.env`.
3. **Dev secrets** — auto-generates and persists every secret (redis password, account
   integrity key, dashboard/signup passwords, Postgres password, Vault dev root token, MCP
   proxy token, …) to the gitignored `deploy/.env` (`0600`). Idempotent: existing values are
   reused. `--regenerate-secrets` rotates them.
4. **Backends** — starts `postgres` + `vault`, then **bootstraps Vault** (transit engine,
   `nomos-council` / `nomos-executor` ed25519 keys, an `nomos-signer` AppRole) and writes the
   resulting `VAULT_ROLE_ID` / `VAULT_SECRET_ID` back to `deploy/.env`.
5. **Stack** — builds + brings up the rest in service mode (the council self-migrates the
   Postgres schema at startup).
6. **Register + verify** — registers a demo agent into Postgres and runs `compose-verify.sh`
   end-to-end (authorize → writ → execute → **signed evidence** via Vault transit).

On success it prints the published URLs + credentials (also in `deploy/.env`):

| Service | URL | Auth |
|---------|-----|------|
| council `/mcp` (gateway) | `http://localhost:9900/mcp` | `Authorization: Bearer <MONITOR_MCP_PROXY_TOKEN>` |
| monitor dashboard | `http://localhost:9901/` | `MONITOR_DASHBOARD_USER` / `…_PASSWORD` |
| agent-signup console | `https://localhost:9905/` | `NOMOS_SIGNUP_ADMIN_USER` / `…_PASSWORD` |
| demo UI | `http://localhost:8000/` | — |

## Other commands

```bash
deploy/scripts/compose-verify.sh                       # re-run the full verification
deploy/scripts/compose-register-agent.sh <id> [council]  # mint another agent (AGENT_ROLE=readonly|developer|operator)
deploy/scripts/compose-down.sh                         # tear down + clear volumes (Postgres + Vault reset together)
deploy/scripts/compose-down.sh --keep-volumes          # stop without clearing volumes
```

## How it is wired

- **Layered compose files** — `docker-compose.yml` (base) + `docker-compose.redis-sentinel.yml`
  (3 redis nodes + 3 sentinels) + `docker-compose.service.yml` (this overlay: adds `postgres`,
  `vault`, `council-gateway`, and flips council/executor/monitor/agent-signup to service mode).
  The `dc()` helper in `scripts/dev-lib.sh` composes all three with the `executor-openshell`
  profile.
- **Vault (AppRole)** — compose has no Kubernetes API, so the council/executor authenticate to
  Vault with `VAULT_ROLE_ID` / `VAULT_SECRET_ID` (the only non-k8s auth the app supports). The
  dev Vault is `server -dev` (in-memory) — its keys are **ephemeral**, so `compose-down.sh`
  clears the Postgres volume too, keeping the pair consistent.
- **Gateway** — `council-gateway` (nginx, `nginx/council-gateway.conf.template`) is the
  token-gated `/mcp` entrypoint: `Bearer <MONITOR_MCP_PROXY_TOKEN>` → council, anything else →
  `401`. The monitor remains the internal proxy + dashboard and reads the ledger via the council
  API (`MONITOR_LEDGER_SOURCE=api`).
- **agent-signup** runs the service-mode (Postgres) console only when built from the
  `feat/onboarding-console` image; otherwise it is reported as a non-fatal soft failure.

All generated credentials live in `deploy/.env` (gitignored). Re-running `compose-up.sh` is
idempotent; it re-bootstraps Vault (the dev server is in-memory) and reuses the `.env` values.
