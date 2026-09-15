<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos local dev on Kind

A single command brings up a fully functioning Nomos **service-mode** stack on a
local [Kind](https://kind.sigs.k8s.io) cluster — the same architecture as
production, using Kind-native primitives:

PostgreSQL (durable ledger/accounts/writs) · Vault transit signing (dev) ·
redis sentinel · nginx gateway → stateless council · monitor · executor/openshell ·
agent-signup console · OPA — reachable via nginx-ingress.

## Bring it up

```bash
deploy/kubernetes/scripts/kind-up.sh
```

It will:
1. **prompt for `NVIDIA_API_KEY`** — required by the default council models
   routed through NVIDIA API Catalog NIM inference endpoints and stored in
   `deploy/.env`. Choose **Generate API Key** for a model on
   [build.nvidia.com](https://build.nvidia.com/).
2. **auto-generate** every other dev secret (redis/postgres/vault/dashboard/signup/…)
   and persist them to `deploy/.env` (gitignored, `0600`).
3. create the cluster, build + load images, install ingress-nginx, deploy, wait for
   the Vault bootstrap + all workloads, register a demo agent, and **verify**
   end-to-end (authorize → writ → execute → signed evidence).

Re-runs are **idempotent** (no prompts once `deploy/.env` is populated).

NVIDIA API Catalog NIM inference endpoints are intended for evaluation and
trial use. For production deployments, use a self-hosted NVIDIA NIM endpoint
or another production-suitable inference backend, such as the documented
direct-provider alternatives. Review the
[NVIDIA API Trial Terms](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)
before relying on the hosted API Catalog endpoints.
Operators who activate the commented direct-vendor block must replace all
backend and role references, supply the matching provider key, and set
`ALLOW_MISSING_NVIDIA_API_KEY=1` for this bootstrapper.
Provider setup: [Anthropic authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
or [OpenAI quickstart](https://developers.openai.com/api/docs/quickstart).

### Useful flags / env
- `-y` / `--yes` — non-interactive (requires the LLM key already in `deploy/.env`)
- `--skip-build` — reuse already-loaded images
- `--regenerate-secrets` — rotate all generated dev secrets
- `--agent-id ID` — demo agent id (default `dev-agent`)
- `KIND_CLUSTER_NAME` / `NAMESPACE` / `HELM_RELEASE` (default `nomos`)

## Access

Add to `/etc/hosts`:

```
127.0.0.1 mcp.nomos.local monitor.nomos.local signup.nomos.local
```

| URL | Auth |
|---|---|
| `http://mcp.nomos.local/mcp` | Bearer = `MONITOR_MCP_PROXY_TOKEN` (in `deploy/.env`) |
| `http://monitor.nomos.local/` | Basic = `MONITOR_DASHBOARD_USER` / `_PASSWORD` |
| `https://signup.nomos.local/` | Basic = `NOMOS_SIGNUP_ADMIN_USER` / `_PASSWORD` |

All dev credentials live in **`deploy/.env`**.

## Other commands

```bash
deploy/kubernetes/scripts/kind-verify.sh                 # re-run the smoke
deploy/kubernetes/scripts/kind-register-agent.sh NAME    # register another agent (AGENT_ROLE=readonly|developer|operator)
deploy/kubernetes/scripts/kind-down.sh                   # tear the cluster down (keeps deploy/.env)
```

## Layout

```text
deploy/kubernetes/
├── kind-cluster.yaml        # Kind config (ingress-ready, host ports 80/443)
├── kind-values.yaml         # Helm overlay: service mode + dev Vault/Postgres + ingress
├── nomos/                   # the Helm chart (service-mode)
└── scripts/
    ├── kind-up.sh           # one-command bootstrapper (prompts, secrets, deploy, verify)
    ├── kind-verify.sh       # health + functional smoke
    ├── kind-register-agent.sh
    ├── kind-down.sh
    ├── kind-lib.sh          # shared helpers (sourced)
    └── build-kind-images.sh
```

## Notes
- **inotify**: if pods hang with networking timeouts (`kube-proxy: too many open
  files`), raise the host limit — multiple Kind clusters exhaust it:
  `sudo sysctl -w fs.inotify.max_user_instances=1280 fs.inotify.max_user_watches=1048576`.
- **agent-signup**: the service-mode console runs in postgres mode; if the pod is not
  ready, verify reports it as a non-fatal warning. Agent registration uses a
  PostgresStore Job and does not depend on the console.
- **NetworkPolicies** are disabled by default (Kind's default CNI does not enforce
  them); enable with `networkPolicy.enabled=true` on a policy-capable CNI.
- `scripts/deploy_stack.sh` is the **root quick-start** path and is still
  supported; it deploys a redis durable store rather than service mode. These
  scripts supersede its Kind path *for service mode only* — they are a different
  topology, not a newer version of the same one. The root `README.md` points here
  for that choice.
