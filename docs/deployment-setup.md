<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos Deployment Setup Crash Course

This document explains the deployment knobs in Nomos, where they live, how they
flow into running services, and which values are one-shot deployment controls
versus persistent runtime configuration.

The main entrypoint is:

```bash
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh
```

Generate `NVIDIA_API_KEY` by opening the [Build/API page](https://build.nvidia.com/)
for any configured model and choosing **Generate API Key**. These NVIDIA API
Catalog NIM inference endpoints are intended for evaluation and trial use. For
production deployments, use a self-hosted NVIDIA NIM endpoint or configure
another production-suitable inference backend, such as the documented
direct-provider alternatives. Review the
[NVIDIA API Trial Terms](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)
before relying on the hosted API Catalog endpoints.

The default council config and the commented mixed-model example require
only `NVIDIA_API_KEY`. To use the commented
direct-vendor alternatives, replace the active `llm_backends`, update every
`members[].backend`, `chairman.backend`, and `code_review.backend` reference,
and set the matching provider key. For the local Compose/Kind bootstrappers,
set `ALLOW_MISSING_NVIDIA_API_KEY=1` only if no active backend uses NVIDIA. See
[Anthropic authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
and the [OpenAI quickstart](https://developers.openai.com/api/docs/quickstart).

`DEPLOY_TARGET` defaults to `kind`. That path requires Docker, Kind, kubectl,
and Helm. If you only want Docker Compose, make the target explicit:

```bash
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

The script creates `deploy/.env` when missing, keeps it at `0600`, generates
local secrets, deploys services, creates or verifies the default agent account,
and stores `NOMOS_AGENT_ID`/`NOMOS_AUTH_TOKEN` when it mints a new token.

If a Kind cluster already has a pre-created `nomos/nomos-secrets` Secret and
you do not want a host-local env file, use:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh
```

That path validates the existing Secret and patches newly minted agent tokens
back into the Kubernetes Secret instead of writing `deploy/.env`. For
Helm-owned Secrets, it reuses live Secret data through a temporary `0600` Helm
values file that is deleted after the upgrade. For Secrets created outside
Helm, it deploys with `secrets.create=false`.

For subsequent runs after `deploy/.env` contains `NVIDIA_API_KEY`, this is
enough:

```bash
./scripts/deploy_stack.sh
```

For remote Docker Compose:

```bash
DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

For remote Docker Compose with the demo UI:

```bash
DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh
```

After deployment, run the post-deploy validation ladder before connecting
agents:

```bash
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed
```

Then run the low-risk live council MCP tests with the generated
`NOMOS_AGENT_ID`, `NOMOS_AUTH_TOKEN`, and `MONITOR_MCP_PROXY_TOKEN` — the last
is the Bearer token the monitor requires on `/mcp`, and without it every call is
refused with `401` before any council behaviour runs. All three are written to
`deploy/.env`. The full checklist is in
[Council MCP Validation](council-mcp-validation.md).

## Mental Model

There are five layers of configuration:

1. Shell environment
   - One-shot deployment controls, for example `DEPLOY_TARGET=compose`,
     `WITH_DEMO=1`, `SKIP_BUILD=1`.
   - Values exported in the shell generally win over persisted values.

2. `deploy/.env`
   - Persistent host-local deployment configuration.
   - Created and updated by `scripts/deploy_stack.sh`.
   - Contains generated passwords/tokens, API keys, public host, dashboard
     credentials, and selected runtime settings.
   - Do not commit this file.
   - Not used when `KIND_USE_EXISTING_SECRET_ONLY=1`.

3. Compose files
   - `deploy/docker-compose.yml`
   - `deploy/docker-compose.redis-sentinel.yml`
   - Optional extra files from `COMPOSE_EXTRA_FILES`.
   - These turn env values into container env vars, port publishes, volumes,
     profiles, and service dependencies.

4. Helm values and Kubernetes Secrets
   - `deploy/kubernetes/nomos/values.yaml`
   - `deploy/kubernetes/kind-values.yaml`
   - `scripts/deploy_stack.sh` renders a temporary secret values file from
     `deploy/.env` for Kind.
   - With `KIND_USE_EXISTING_SECRET_ONLY=1`, the script uses the existing
     `nomos-secrets` Secret instead of `deploy/.env`. Helm-owned Secrets remain
     Helm-managed; non-Helm-owned Secrets are referenced with
     `secrets.create=false`.
   - Chart templates turn values into Services, Deployments, StatefulSets,
     PVCs, NetworkPolicies, ConfigMaps, and Secret references.

5. Application config files
   - `configs/council_nat_workflow.yaml`
   - `configs/local_council_catalog.yaml`
   - `configs/council.yaml`
   - `deploy/dynamic-config/`
   - These contain `${ENV_VAR}` placeholders. At runtime, the Python config
     loader resolves those placeholders from the container environment.

## Configuration Precedence

For values read by `scripts/deploy_stack.sh` through `current_value` or
`current_or_default`, precedence is:

1. Shell environment variable.
2. `deploy/.env` value.
3. Script default.

Not every deployment control is read from `deploy/.env`. The safest rule:

- Use shell variables for one-shot controls like `DEPLOY_TARGET`, `WITH_DEMO`,
  `WITH_AGENT_SIGNUP`, `WITH_OPA`, `EXECUTOR_PROFILE`, `SKIP_BUILD`,
  `SKIP_ACCOUNT`, and most `KIND_*` toggles.
- Use `deploy/.env` for persistent runtime values like passwords, API keys,
  public host, ports, signup settings, dashboard credentials, and Redis
  topology.

`COMPOSE_REDIS_TOPOLOGY` is a special case: it is read from either the shell or
`deploy/.env`, and defaults to `sentinel`.

## Main Deployment Commands

### Docker Compose, Remote Host

```bash
DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

Starts:

- Redis Sentinel topology by default.
- OPA, unless `WITH_OPA=0`.
- Council.
- Monitor/MCP proxy.
- gRPC proxy.
- Agent signup, unless `WITH_AGENT_SIGNUP=0`.
- The selected executor profile, default `executor-openshell`.
- Demo UI only when `WITH_DEMO=1`.

### Docker Compose With Demo

```bash
DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh
```

The demo publishes `${DEMO_PORT:-8000}:8000`.

### Docker Compose Standalone Redis

Use only when a single Redis process is acceptable:

```bash
DEPLOY_TARGET=compose COMPOSE_REDIS_TOPOLOGY=standalone ./scripts/deploy_stack.sh
```

Or persist it:

```bash
COMPOSE_REDIS_TOPOLOGY=standalone
```

in `deploy/.env`.

### Kind

```bash
./scripts/deploy_stack.sh
```

`DEPLOY_TARGET` defaults to `kind`. The Kind path:

- Creates the Kind cluster when missing, unless `KIND_CREATE_CLUSTER=0`.
- Builds and loads local images, unless `SKIP_BUILD=1`.
- Installs the Helm chart with `deploy/kubernetes/kind-values.yaml`.
- Disables the chart's Ingress objects unless the cluster already has the `nginx`
  `IngressClass`, an `ingress-ready=true` node *and* host ports 80/443 mapped on
  the control-plane node — all of which come from
  `deploy/kubernetes/scripts/kind-up.sh` and `kind-cluster.yaml`. A cluster this
  script created cannot gain the host port mappings later, so installing
  ingress-nginx on it (for example by running `kind-up.sh` against it) does not
  make the hostnames reachable; recreate it with `kind-up.sh` for hostname ingress.
- Creates or syncs the `nomos-secrets` Secret, unless
  `KIND_USE_EXISTING_SECRET_ONLY=1`.
- Creates the default agent account, unless `SKIP_ACCOUNT=1`.
- Starts local port-forwards, unless `KIND_PORT_FORWARD=0`.

Kind uses `deploy/kubernetes/kind-values.yaml`, which does not set `demo`, so the
chart default `demo.enabled=false` applies. **The demo UI is a Compose-only
service:** the chart carries `demo.*` values, Secret keys and NetworkPolicies but
no demo Deployment or Service, so setting `demo.enabled=true` renders no
workload. Use `WITH_DEMO=1` with `DEPLOY_TARGET=compose` for the demo.

### Kind With Pre-Created Secret

Use this path for clusters where secrets are already published to
Kubernetes and no local `deploy/.env` should be written:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh
```

The Secret must already exist as `nomos/nomos-secrets` and must contain
non-empty runtime keys for Redis, the shipped NVIDIA-backed council config,
monitor, signup, and account integrity. If the default agent account already
exists but the raw token is missing from the Secret, rotate intentionally:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

## Post-Deploy Validation

The deployment is not ready for agent integration until the external MCP path is
proven. Use [Council MCP Validation](council-mcp-validation.md) as the canonical
checklist.

Minimum Kind or Compose validation:

```bash
curl -fsS http://127.0.0.1:9901/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:9907/v1/health | python3 -m json.tool

uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed
```

Compose-only deep validation:

```bash
uv run --env-file deploy/.env python scripts/test_smoke.py
uv run --env-file deploy/.env python scripts/test_live_e2e.py
```

The Compose-only scripts expect host Redis access and a Docker executor
container name. The environment file must come from the same Compose deployment
being tested. For Kind, keep to the portable MCP and gRPC tests unless you add
matching port-forwards and container lookup logic.

## Deployment Control Flags

These are evaluated by `scripts/deploy_stack.sh`.

| Variable | Default | Where It Lives | Effect |
|---|---:|---|---|
| `DEPLOY_TARGET` | `kind` | Shell | Selects `kind`/`kubernetes` or `compose`/`docker` path. |
| `ENV_FILE` | `deploy/.env` | Shell | Persistent env file used by deploy script. |
| `WAIT_TIMEOUT_SECONDS` | `240` | Shell | Health and rollout wait timeout. |
| `SKIP_BUILD` | `0` | Shell | Reuse existing images. |
| `SKIP_ACCOUNT` | `0` | Shell | Skip agent account creation. |
| `WITH_OPA` | `1` | Shell | Compose: start bundled OPA and set `COUNCIL_OPA_ENABLED=true`. |
| `WITH_AGENT_SIGNUP` | `1` | Shell | Compose: start signup UI/API. |
| `WITH_DEMO` | `0` | Shell | Compose: start demo UI. |
| `EXECUTOR_PROFILE` | `executor-openshell` | Shell | Compose profile/service to start. |
| `AGENT_ID` | `agent-developer` | Shell | Account to create if absent. |
| `AGENT_ROLE` | `developer` | Shell | Account role: `readonly`, `developer`, `operator`, `admin`, `demo`. |
| `AGENT_COUNCILS` | `council-prod` | Shell | Comma-separated council memberships. |
| `AGENT_DISPLAY_NAME` | `$AGENT_ID` | Shell | Display name for created account. |
| `AGENT_FORCE_ROTATE` | `0` | Shell | Rotate token when account already exists. |

## Compose-Specific Controls

| Variable | Default | Where It Lives | Effect |
|---|---:|---|---|
| `COMPOSE_FILE` | `deploy/docker-compose.yml` | Shell | Base Compose file. |
| `COMPOSE_REDIS_TOPOLOGY` | `sentinel` | Shell or `deploy/.env` | `sentinel` adds the Redis Sentinel override; `standalone` keeps one Redis. |
| `COMPOSE_REDIS_SENTINEL_FILE` | `deploy/docker-compose.redis-sentinel.yml` | Shell | Override file for Redis HA/Sentinel. |
| `COMPOSE_EXTRA_FILES` | empty | Shell | Colon-separated extra Compose override files. |
| `COMPOSE_PROJECT_NAME` | derived from Compose project | Shell | Compose project/container name prefix. |

`scripts/deploy_stack.sh` starts Redis first. In Sentinel mode it starts:

- `redis`
- `redis-1`
- `redis-2`
- `redis-sentinel-1`
- `redis-sentinel-2`
- `redis-sentinel-3`

Then it starts OPA, council, monitor, gRPC proxy, signup, executor, and demo
based on the selected flags.

## Kind and Helm Controls

| Variable | Default | Where It Lives | Effect |
|---|---:|---|---|
| `KIND_CLUSTER_NAME` | `nomos` | Shell | Kind cluster name. |
| `KIND_CONTEXT` | `kind-$KIND_CLUSTER_NAME` | Shell | kubectl/helm context. |
| `KIND_NAMESPACE` | `nomos` | Shell | Kubernetes namespace. |
| `KIND_CREATE_CLUSTER` | `1` | Shell | Create Kind cluster if missing. |
| `KIND_VALUES_FILE` | `deploy/kubernetes/kind-values.yaml` | Shell | Values file layered on chart defaults. |
| `KIND_CHART_DIR` | `deploy/kubernetes/nomos` | Shell | Helm chart path. |
| `KIND_BUILD_SCRIPT` | `deploy/kubernetes/scripts/build-kind-images.sh` | Shell | Image build/load script. |
| `KIND_SECRET_NAME` | `nomos-secrets` | Shell | Existing/created Secret name. |
| `HELM_RELEASE` | `nomos` | Shell | Helm release name and resource prefix. |
| `KIND_ROLLOUT_RESTART` | `1` | Shell | Restart deployments after Helm so same-tag local images refresh. |
| `KIND_USE_EXISTING_SECRET_ONLY` | `0` | Shell | Use an existing Kind Secret and do not create/update `deploy/.env`. |
| `KIND_PORT_FORWARD` | `1` | Shell | Start local port-forwards. |
| `KIND_PORT_FORWARD_ADDRESS` | `127.0.0.1` | Shell | Bind address for kubectl port-forward. |
| `KIND_PORT_FORWARD_DIR` | `/tmp/nomos-kind-$KIND_CLUSTER_NAME-port-forwards` | Shell | Port-forward pid/log directory. |
| `KIND_PORT_FORWARD_TIMEOUT_SECONDS` | `20` | Shell | Port-forward wait timeout. |
| `KIND_PORT_FORWARD_MONITOR` | `1` | Shell | Forward monitor/MCP. |
| `KIND_PORT_FORWARD_GRPC` | `1` | Shell | Forward council gRPC proxy. |
| `KIND_PORT_FORWARD_AGENT_SIGNUP` | `1` | Shell | Forward signup UI/API. |
| `KIND_PORT_FORWARD_DEMO` | `$WITH_DEMO` (so `0`) | Shell | Forward demo UI. |
| `KIND_PORT_FORWARD_EXECUTOR` | `1` | Shell | Forward executor. |
| `KIND_FORWARD_MONITOR_PORT` | `MONITOR_PORT` or `9901` | Shell or `deploy/.env` via `current_or_default` | Local forwarded monitor port. |
| `KIND_FORWARD_GRPC_PORT` | `COUNCIL_GRPC_PORT` or `9904` | Shell or `deploy/.env` via `current_or_default` | Local forwarded gRPC port. |
| `KIND_FORWARD_AGENT_SIGNUP_PORT` | `AGENT_SIGNUP_PORT` or `9905` | Shell or `deploy/.env` via `current_or_default` | Local forwarded signup port. |
| `KIND_FORWARD_DEMO_PORT` | `DEMO_PORT` or `8000` | Shell or `deploy/.env` via `current_or_default` | Local forwarded demo port. |
| `KIND_FORWARD_EXECUTOR_PORT` | `EXECUTOR_OPENSHELL_PORT` or `9907` | Shell or `deploy/.env` via `current_or_default` | Local forwarded executor port. |

## Persistent `deploy/.env` Values

`scripts/deploy_stack.sh` creates `deploy/.env` if missing and keeps it at
`0600`. These values are host-local and should not be committed.

| Variable | Generated? | Used By | Effect |
|---|---:|---|---|
| `NVIDIA_API_KEY` | No | Council | Required by the NVIDIA API Catalog NIM inference endpoints. |
| `ANTHROPIC_API_KEY` | No | Council | Used only after activating the matching commented Anthropic backends. |
| `OPENAI_API_KEY` | No | Council | Used only after activating the matching commented OpenAI backend. |
| `REDIS_PASSWORD` | Yes | Redis, council, signup, executors | Redis auth password. |
| `COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY` | Yes | Council, signup, account CLI | HMAC key for account-critical Redis state. |
| `COUNCIL_MANAGEMENT_TOKEN` | Yes | Council | Required for mutating dynamic-council MCP tools. |
| `MONITOR_INTERNAL_TOKEN` | Yes | Council, monitor | Internal telemetry auth from council to monitor. |
| `MONITOR_MCP_PROXY_TOKEN` | Yes | Monitor, gRPC proxy, demo, every MCP client | Bearer token the monitor enforces on `/mcp`. Without it every MCP call is refused with `401`. |
| `MONITOR_DASHBOARD_USER` | Defaults `admin` | Monitor | Browser dashboard basic-auth user. |
| `MONITOR_DASHBOARD_PASSWORD` | Yes | Monitor | Browser dashboard basic-auth password. |
| `NOMOS_SIGNUP_ADMIN_USER` | Defaults `admin` | Signup | Signup admin user. |
| `NOMOS_SIGNUP_ADMIN_PASSWORD` | Yes | Signup | Signup admin password. |
| `DEMO_AUTH_USER` | Defaults `demo` | Demo | Demo UI/API basic-auth user. |
| `DEMO_AUTH_PASSWORD` | Yes | Demo | Demo UI/API basic-auth password. |
| `NOMOS_PUBLIC_HOST` | Auto-detected | Deploy output, signup endpoint | Public host/IP advertised to users. |
| `COUNCIL_OPA_ENABLED` | Defaults `true` | Council | Enable council-side OPA callout. |
| `COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE` | Depends on profile | Council | Allow plain evidence instead of certified executor evidence. |
| `NOMOS_DEPLOY_PROFILE` | Defaults `dev` | Council, monitor, signup | Deployment profile. `prod`/`production` is normalized to `alpha`. |
| `NOMOS_ALPHA_ALLOW_PUBLIC_BIND` | Profile-dependent | Council | Allows public bind in alpha profile. |
| `NOMOS_AGENT_ID` | After account creation | Demo, Kind Secret | Created account ID. |
| `NOMOS_AUTH_TOKEN` | After account creation | Demo, clients | Created account token. |

Optional values commonly added to `deploy/.env`:

| Variable | Default | Used By | Effect |
|---|---:|---|---|
| `COUNCIL_CONFIG_FILE` | `../configs/council.yaml` | Compose council mount | Alternate static mount; does not replace the live dynamic profile. See [changing the council model configuration](#changing-the-council-model-configuration). |
| `COMPOSE_REDIS_TOPOLOGY` | `sentinel` | Deploy script | Select Compose Redis topology. |
| `REDIS_SENTINEL_SERVICE_NAME` | `nomos-master` | Compose Sentinel | Sentinel master name. |
| `COUNCIL_OPA_URL` | `http://opa:8181` | Council | External/custom OPA URL. |
| `COUNCIL_OPA_DECISION_PATH` | `/v1/data/nomos/intent/decision` | Council | OPA decision API path. |
| `COUNCIL_OPA_POLICY_ID` | `strict-shell` | Council | Default OPA policy ID. |
| `GIT_WORKFLOW_COUNCIL_OPA_POLICY_ID` | `git-workflow-local` | Council | Git workflow OPA policy ID. |
| `COUNCIL_OPA_TIMEOUT_MS` | `500` | Council | OPA timeout. |
| `COUNCIL_OPA_FAIL_CLOSED` | `true` | Council | Deny if OPA is unavailable. |
| `OPA_IMAGE` | `openpolicyagent/opa:latest` | Compose OPA | OPA image. |
| `OPA_POLICY_DIR` | `../policies` | Compose OPA | Policy directory mounted into OPA. |
| `OPA_LOG_LEVEL` | `info` | Compose OPA | OPA log level. |
| `MONITOR_PORT` | `9901` | Compose, deploy output, signup | Public monitor/MCP port. |
| `MONITOR_LEDGER_PATHS` | `/app/ledger/*.jsonl` | Monitor | Ledger files used by dashboard. |
| `REDIS_BIND_ADDR` | `0.0.0.0` | Compose Redis | Host bind address. |
| `REDIS_PORT` | `6379` | Compose Redis, output | Host Redis port. |
| `OPA_BIND_ADDR` | `0.0.0.0` | Compose OPA | Host bind address. |
| `OPA_PORT` | `8181` | Compose OPA | Host OPA port. |
| `AGENT_SIGNUP_BIND_ADDR` | `0.0.0.0` | Compose signup | Host bind address. |
| `AGENT_SIGNUP_PORT` | `9905` | Compose signup | Host signup port. |
| `COUNCIL_GRPC_BIND_ADDR` | `0.0.0.0` | Compose gRPC | Host bind address. |
| `COUNCIL_GRPC_PORT` | `9904` | Compose gRPC | Host gRPC port. |
| `DEMO_PORT` | `8000` | Compose demo | Host demo port. |
| `EXECUTOR_DEFAULT_PORT` | `9902` | Compose executor-default | Host default executor port. |
| `EXECUTOR_OPENSHELL_PORT` | `9907` | Compose executor-openshell | Host OpenShell executor port. |
| `EXECUTOR_UID` | profile-dependent | Compose executors | Container user ID. |
| `EXECUTOR_GID` | profile-dependent | Compose executors | Container group ID. |
| `NOMOS_SIGNUP_COUNCIL_ID` | `council-prod` | Signup | Default signup council. |
| `NOMOS_SIGNUP_ALLOWED_COUNCILS` | `council-prod` | Signup | Allowed signup councils. |
| `NOMOS_SIGNUP_DEFAULT_ROLE` | `developer` | Signup | Default signup role. |
| `NOMOS_SIGNUP_ALLOWED_ROLES` | `readonly,developer` | Signup | Allowed signup roles. |
| `NOMOS_REDIS_PREFIX` | `nomos:` | Signup/account CLI | Redis key prefix. |
| `DEMO_AUTH_USER` | `demo` | Demo | Override demo HTTP Basic auth username. |
| `DEMO_AUTH_PASSWORD` | generated | Demo | Override demo HTTP Basic auth password. |
| `NOMOS_EXECUTOR_URL` | `http://executor-openshell:9906` | Demo | Executor URL used by demo. |
| `NOMOS_AUTH_TOKEN_K8S_ADMIN` | empty | Demo | Optional demo persona token. |
| `NOMOS_AUTH_TOKEN_K8S_DEV` | empty | Demo | Optional demo persona token. |
| `NOMOS_AUTH_TOKEN_K8S_READONLY` | empty | Demo | Optional demo persona token. |

## Compose Service Propagation

### Redis

Base Compose starts one Redis service:

- `REDIS_PASSWORD` -> `redis-server --requirepass`.
- `REDIS_BIND_ADDR` and `REDIS_PORT` -> host port publish.
- `redis-data` volume -> `/data`.

Sentinel override adds:

- `redis-1`, `redis-2`
- `redis-sentinel-1`, `redis-sentinel-2`, `redis-sentinel-3`
- `redis-data-1`, `redis-data-2`

Sentinel variables:

| Variable | Default | Propagation |
|---|---:|---|
| `REDIS_SENTINEL_SERVICE_NAME` | `nomos-master` | Redis nodes, Sentinel processes, client env. |
| `REDIS_SENTINEL_DOWN_AFTER_MS` | `5000` | Sentinel config. |
| `REDIS_SENTINEL_FAILOVER_TIMEOUT_MS` | `30000` | Sentinel config. |
| `REDIS_CONNECT_TIMEOUT_MS` | `1000` | Council/signup/executor client env. |
| `REDIS_COMMAND_TIMEOUT_MS` | `1000` | Council/signup/executor client env. |

Client env in Sentinel mode:

- Council receives `COUNCIL_REDIS_MODE=sentinel`,
  `COUNCIL_REDIS_SENTINEL_SERVICE_NAME`, and
  `COUNCIL_REDIS_SENTINEL_NODES`.
- Signup receives `NOMOS_SIGNUP_REDIS_MODE=sentinel`,
  `NOMOS_SIGNUP_REDIS_SENTINEL_SERVICE_NAME`, and
  `NOMOS_SIGNUP_REDIS_SENTINEL_NODES`.
- Executors receive `EXECUTOR_REDIS_MODE=sentinel`,
  `EXECUTOR_REDIS_SENTINEL_SERVICE_NAME`, and
  `EXECUTOR_REDIS_SENTINEL_NODES`.

The application config model validates that Sentinel mode has both a master
name and at least one Sentinel node.

### Council

Compose service: `council`

Important propagation:

- `NOMOS_DEPLOY_PROFILE` -> app deployment profile.
- `NOMOS_ALPHA_ALLOW_PUBLIC_BIND` -> production/alpha preflight behavior.
- `NVIDIA_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` -> LLM backends.
- Redis env -> `configs/council.yaml` `redis:` block.
- OPA env -> `configs/council.yaml` `opa:` behavior.
- Telemetry env -> monitor latency side channel.
- `COUNCIL_CATALOG_PATH=/app/dynamic-config/local_council_catalog.yaml`.
- `COUNCIL_DYNAMIC_PROFILE_DIR=/app/dynamic-config/councils`.
- `COUNCIL_DYNAMIC_LEDGER_DIR=/app/ledger`.
- `COUNCIL_MANAGEMENT_TOKEN` -> dynamic-council mutation auth.
- `COUNCIL_PUBLIC_KEY_EXPORT_PATH=/app/public-keys/council.pub`.

Important volumes:

- `${COUNCIL_CONFIG_FILE:-../configs/council.yaml}` -> `/app/configs/council.yaml:ro`
- `council-keys` -> `/app/keys`
- `council-public-keys` -> `/app/public-keys`
- `council-ledger` -> `/app/ledger`
- `./dynamic-config` -> `/app/dynamic-config`

### Monitor

Compose service: `monitor`

- Publishes `${MONITOR_PORT:-9901}:9901`.
- Proxies external MCP `/mcp` traffic to `council:9901`.
- Serves dashboard at `/`.
- Reads `MONITOR_MCP_PROXY_TOKEN` and enforces it as `Authorization: Bearer` on
  `/mcp`. Every MCP client — the gRPC proxy, the demo, and your own agents —
  must send it, or the request is refused with `401` before the council is
  reached. `/debug/tools/list` is deliberately unauthenticated, so it does not
  exercise this token.
- Reads `MONITOR_DASHBOARD_USER` and `MONITOR_DASHBOARD_PASSWORD`.
- Reads `MONITOR_INTERNAL_TOKEN` for internal telemetry.
- Reads `MONITOR_LEDGER_PATHS` for dashboard ledger visibility.

### Council gRPC Proxy

Compose service: `council-grpc`

- Publishes `${COUNCIL_GRPC_BIND_ADDR:-0.0.0.0}:${COUNCIL_GRPC_PORT:-9904}:9904`.
- Uses `COUNCIL_MCP_URL=http://monitor:9901/mcp` and
  `COUNCIL_MCP_PROXY_TOKEN=${MONITOR_MCP_PROXY_TOKEN}`.
- Tags traffic through monitor so gRPC appears in unified stats.

### Agent Signup

Compose service: `agent-signup`

- Publishes `${AGENT_SIGNUP_BIND_ADDR:-0.0.0.0}:${AGENT_SIGNUP_PORT:-9905}:9905`.
- Uses Redis for account storage.
- Uses `NOMOS_ACCOUNT_INTEGRITY_KEY` from
  `COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY`.
- Advertises `NOMOS_SIGNUP_COUNCIL_ENDPOINT` as
  `http://${NOMOS_PUBLIC_HOST:-localhost}:${MONITOR_PORT:-9901}/mcp`.
- Controlled by `WITH_AGENT_SIGNUP`.

### Demo

Compose service: `demo`

- Controlled by `WITH_DEMO=1`.
- Publishes `${DEMO_PORT:-8000}:8000`.
- Uses `NOMOS_COUNCIL_URL=http://monitor:9901/mcp`.
- Uses `NOMOS_EXECUTOR_URL`, default `http://executor-openshell:9906`.
- Reads `NOMOS_AUTH_TOKEN`, `NOMOS_AGENT_ID`, and optional K8s persona tokens.

### Executors

Default profile: `executor-openshell`

- Starts `openshell` plus `executor-openshell`.
- Publishes `${EXECUTOR_OPENSHELL_PORT:-9907}:9906`.
- Executor container uses `EXECUTOR_REDIS_URL` and, in Sentinel mode,
  `EXECUTOR_REDIS_MODE` plus Sentinel env.
- OpenShell runtime is internal on `openshell:8080`.
- `OPENSHELL_ENDPOINT` defaults to `http://openshell:8080`.
- `OPENSHELL_REQUEST_TIMEOUT` defaults to `30`.

Other profiles:

- `executor-default`: publishes `${EXECUTOR_DEFAULT_PORT:-9902}:9902`.
- `executor-dev`: host-network dev profile on `9903`.

## Helm Values Propagation

Chart root:

- `deploy/kubernetes/nomos/values.yaml`
- `deploy/kubernetes/kind-values.yaml`

Kind uses `kind-values.yaml` layered over chart defaults.

### Global and Images

| Helm value | Effect |
|---|---|
| `global.imagePullPolicy` | Default pull policy for app images. |
| `global.podAnnotations` | Added to pod templates. |
| `global.nodeSelector` | Common node selector. |
| `global.tolerations` | Common tolerations. |
| `global.affinity` | Common affinity. |
| `deploymentProfile` | Becomes `NOMOS_DEPLOY_PROFILE`. |
| `nameOverride` / `fullnameOverride` | Resource naming. |
| `images.*.repository` | Image repository per component. |
| `images.*.tag` | Image tag per component. |
| `images.*.pullPolicy` | Per-image pull policy override. |

### Kubernetes Secrets

The chart can use three secret modes:

1. Helm-managed Secret:
   - `secrets.create=true`
   - values come from `secrets.stringData`.
   - If `KIND_USE_EXISTING_SECRET_ONLY=1` finds that `nomos-secrets` is already
     Helm-owned, it keeps this mode and fills `secrets.stringData` from the live
     Kubernetes Secret through a temporary file.

2. Pre-created Secret:
   - `secrets.create=false`
   - `secrets.existingSecret=nomos-secrets`
   - `KIND_USE_EXISTING_SECRET_ONLY=1` selects this mode for local Kind and
     validates the Secret before Helm runs when the Secret is not Helm-owned.

3. External Secrets:
   - `externalSecrets.enabled=true`
   - External Secrets Operator writes the same Kubernetes Secret contract.

`secrets.keys` maps logical fields to Kubernetes Secret key names. Pods refer to
those keys through `secretKeyRef`, so the pod specs do not care whether the
source is Helm stringData, a pre-created Secret, or Vault/ESO.

Important secret keys:

| Logical value | Default Secret key |
|---|---|
| Redis password | `REDIS_PASSWORD` |
| NVIDIA API key | `NVIDIA_API_KEY` |
| Anthropic API key | `ANTHROPIC_API_KEY` |
| OpenAI API key | `OPENAI_API_KEY` |
| Account integrity key | `COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY` |
| Council management token | `COUNCIL_MANAGEMENT_TOKEN` |
| Monitor internal token | `MONITOR_INTERNAL_TOKEN` |
| Monitor MCP proxy token | `MONITOR_MCP_PROXY_TOKEN` |
| Monitor dashboard user | `MONITOR_DASHBOARD_USER` |
| Monitor dashboard password | `MONITOR_DASHBOARD_PASSWORD` |
| Signup admin user | `NOMOS_SIGNUP_ADMIN_USER` |
| Signup admin password | `NOMOS_SIGNUP_ADMIN_PASSWORD` |
| Demo Basic auth password | `DEMO_AUTH_PASSWORD` |
| Demo token | `NOMOS_AUTH_TOKEN` |
| Demo agent ID | `NOMOS_AGENT_ID` |
| Demo K8s admin token | `NOMOS_AUTH_TOKEN_K8S_ADMIN` |
| Demo K8s dev token | `NOMOS_AUTH_TOKEN_K8S_DEV` |
| Demo K8s readonly token | `NOMOS_AUTH_TOKEN_K8S_READONLY` |

### Redis Helm Values

| Helm value | Default | Effect |
|---|---:|---|
| `redis.enabled` | `true` | Render bundled Redis resources. |
| `redis.topology.mode` | `sentinel` | `sentinel` or `standalone`. |
| `redis.replicas` | `3` | Redis StatefulSet replicas in Sentinel mode. |
| `redis.service.port` | `6379` | Redis service/container port. |
| `redis.sentinel.masterName` | `nomos-master` | Sentinel master name. |
| `redis.sentinel.quorum` | `2` | Sentinel quorum. |
| `redis.sentinel.replicas` | `3` | Sentinel Deployment replicas. |
| `redis.sentinel.service.port` | `26379` | Sentinel service/container port. |
| `redis.sentinel.downAfterMilliseconds` | `5000` | Sentinel down-after setting. |
| `redis.sentinel.failoverTimeoutMilliseconds` | `30000` | Sentinel failover timeout. |
| `redis.sentinel.parallelSyncs` | `1` | Sentinel parallel syncs. |
| `redis.config.maxmemory` | `192mb` | Redis memory cap. |
| `redis.config.maxmemoryPolicy` | `noeviction` | Redis eviction policy. |
| `redis.persistence.enabled` | `true` | PVC-backed data. |
| `redis.persistence.size` | `1Gi` | Redis PVC size. |
| `redis.persistence.appendfsync` | `everysec` | AOF fsync policy. |
| `redis.persistence.save` | configured list | RDB save points. |
| `redis.podDisruptionBudget.enabled` | `true` | Render PDB. |
| `redis.resources` | requests/limits | Redis resource requests/limits. |

Computed helpers:

- `nomos.redisMode` validates `standalone` or `sentinel`.
- `nomos.redisSentinelNodes` renders `<release>-redis-sentinel:<port>`.

Pods receive:

- Council: `COUNCIL_REDIS_MODE`, `COUNCIL_REDIS_SENTINEL_SERVICE_NAME`,
  `COUNCIL_REDIS_SENTINEL_NODES`.
- Signup: `NOMOS_SIGNUP_REDIS_MODE`,
  `NOMOS_SIGNUP_REDIS_SENTINEL_SERVICE_NAME`,
  `NOMOS_SIGNUP_REDIS_SENTINEL_NODES`.
- Executor: `EXECUTOR_REDIS_MODE`,
  `EXECUTOR_REDIS_SENTINEL_SERVICE_NAME`,
  `EXECUTOR_REDIS_SENTINEL_NODES`.

### OPA Helm Values

| Helm value | Default | Effect |
|---|---:|---|
| `opa.enabled` | `true` | Render bundled OPA. |
| `opa.logLevel` | `info` | OPA log level. |
| `opa.service.port` | `8181` | OPA service port. |
| `opa.resources` | requests 25m / 64Mi, limits 250m / 256Mi | OPA resources. |

Council OPA values:

| Helm value | Default | Env var |
|---|---:|---|
| `council.opa.enabled` | `true` | `COUNCIL_OPA_ENABLED` with `opa.enabled`. |
| `council.opa.decisionPath` | `/v1/data/nomos/intent/decision` | `COUNCIL_OPA_DECISION_PATH` |
| `council.opa.policyId` | `strict-shell` | `COUNCIL_OPA_POLICY_ID` |
| `council.opa.gitWorkflowPolicyId` | `git-workflow-local` | `GIT_WORKFLOW_COUNCIL_OPA_POLICY_ID` |
| `council.opa.timeoutMs` | `500` | `COUNCIL_OPA_TIMEOUT_MS` |
| `council.opa.failClosed` | `true` | `COUNCIL_OPA_FAIL_CLOSED` |

### Council Helm Values

| Helm value | Default | Effect |
|---|---:|---|
| `council.replicas` | `1` | Council Deployment replicas. |
| `council.service.port` | `9901` | Internal council service port. |
| `council.telemetry.enabled` | `true` | `COUNCIL_TELEMETRY_ENABLED`. |
| `council.telemetry.timeoutMs` | `250` | `COUNCIL_TELEMETRY_TIMEOUT_MS`. |
| `council.allowUncertifiedEvidence` | `true` | `COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE`. |
| `council.allowPublicBind` | `false` | `NOMOS_ALPHA_ALLOW_PUBLIC_BIND`. |
| `council.persistence.keys.*` | enabled, `256Mi` | Council private key PVC. |
| `council.persistence.publicKeys.*` | enabled, `64Mi` | Public key PVC shared with executor. |
| `council.persistence.ledger.*` | enabled, `1Gi` | Ledger PVC. |
| `council.persistence.dynamicConfig.*` | enabled, `256Mi` | Dynamic council config PVC. |
| `council.resources` | requests 100m / 256Mi, limits 1 / 1Gi | Resource requests/limits. |

### Monitor, gRPC, Signup, Demo

| Helm value | Default | Effect |
|---|---:|---|
| `monitor.enabled` | `true` | Render monitor. |
| `monitor.service.port` | `9901` | Monitor service port. |
| `councilGrpc.enabled` | `true` | Render gRPC proxy. |
| `councilGrpc.service.port` | `9904` | gRPC service port. |
| `agentSignup.enabled` | `true` | Render signup UI/API. |
| `agentSignup.service.port` | `9905` | Signup service port. |
| `agentSignup.councilEndpoint` | internal monitor URL | Signup-displayed MCP endpoint. |
| `agentSignup.allowedCouncils` | `council-prod` | Signup allowed councils. |
| `agentSignup.defaultRole` | `developer` | Signup default role. |
| `agentSignup.allowedRoles` | `readonly,developer` | Signup allowed roles. |
| `demo.enabled` | `false` | Gates the demo NetworkPolicies only. The chart ships no demo Deployment or Service, so this renders no workload — the demo UI is Compose-only. |
| `demo.service.port` | `8000` | Demo port referenced by the NetworkPolicies. |
| `demo.auth.user/password` | empty | Direct demo HTTP Basic auth values; otherwise Secret refs are used. |
| `demo.auth token fields` | empty | Direct demo agent token values; otherwise Secret refs are used. |

### Executor and OpenShell Helm Values

| Helm value | Default | Effect |
|---|---:|---|
| `executorOpenshell.enabled` | `true` | Render OpenShell executor adapter. |
| `executorOpenshell.replicas` | `1` | Executor replicas. |
| `executorOpenshell.service.port` | `9907` | Service port. |
| `executorOpenshell.service.targetPort` | `9906` | Container port. |
| `executorOpenshell.redisKeyPrefix` | `nomos` | Redis prefix env as `nomos:`. |
| `executorOpenshell.requestTimeout` | `30` | OpenShell request timeout. |
| `executorOpenshell.persistence.keys.*` | enabled, `256Mi` | Executor key PVC. |
| `executorOpenshell.persistence.workspace.*` | enabled, `2Gi` | Workspace PVC. |
| `openshell.enabled` | `true` | Render OpenShell runtime. |
| `openshell.service.port` | `8080` | Internal OpenShell service port. |
| `openshell.allowedWorkdirs` | `/workspace` | Runtime allowed workdirs. |
| `networkPolicy.enabled` | `false` | Render/enforce NetworkPolicies when CNI supports them. |

## Application Config Flow

NAT starts from:

```text
configs/council_nat_workflow.yaml
```

It points each tool at:

```text
${COUNCIL_CATALOG_PATH:-configs/local_council_catalog.yaml}
```

Compose sets:

```text
COUNCIL_CATALOG_PATH=/app/dynamic-config/local_council_catalog.yaml
```

The deploy script seeds `deploy/dynamic-config/` with:

- `local_council_catalog.yaml`
- `council.yaml`
- `git_workflow_council.yaml` when present
- `councils/` for dynamic profiles

The catalog maps council IDs to config files:

```yaml
default_council_id: council-prod
councils:
  council-prod:
    config_file: council.yaml
```

`configs/council.yaml` contains runtime placeholders such as:

```yaml
redis:
  mode: "${COUNCIL_REDIS_MODE:-standalone}"
  host: "${COUNCIL_REDIS_HOST}"
  port: "${COUNCIL_REDIS_PORT:-6379}"
  password: "${COUNCIL_REDIS_PASSWORD}"
  sentinel_service_name: "${COUNCIL_REDIS_SENTINEL_SERVICE_NAME:-}"
  sentinel_nodes: "${COUNCIL_REDIS_SENTINEL_NODES:-}"
```

Those placeholders are resolved by `CouncilSettings` and `RedisConfig` inside
the container. In Sentinel mode, validation requires both a master name and
sentinel nodes.

### Changing the Council Model Configuration

`configs/council.yaml` and the Helm-packaged copy at
`deploy/kubernetes/nomos/files/council/council.yaml` are release templates, kept
identical by `test_shipped_council_templates_match`. Editing a template does not
change a council that is already running: the first bootstrap copies the
template to a live location and later runs leave that copy alone, so operator
edits survive a redeploy.

| Deployment | Live council profile |
|---|---|
| Compose | `deploy/dynamic-config/council.yaml` on the host |
| Helm `storage.mode=pvc` | `/app/dynamic-config/council.yaml` on the `-dynamic-config` claim |
| Helm `storage.mode=service` (the Kind default) | `/bootstrap-config`, mounted read-only from the packaged ConfigMap |

In Compose, edit the host file, put any new provider key in `deploy/.env`, then
recreate the `council` service, which reads provider keys from its environment
only at startup. In Helm PVC mode, edit the profile in the council pod; the
runtime reloads changed catalog and profile files on the next request. In
service mode the catalog is read-only, so update the packaged files under
`deploy/kubernetes/nomos/files/council/`, run `helm upgrade` with the release's
existing values, and restart the council.

Copy the live profile before editing it so you can restore it.
`COUNCIL_CONFIG_FILE` is a separate static mount at `/app/configs/council.yaml`;
it does not replace the live dynamic profile. When running management commands
from a checkout, pass `--catalog` pointing at a writable copy of the catalog
and its profiles so the command does not edit the release templates.

After a model change, run the
[endpoint compatibility and deployed MCP checks](council-mcp-validation.md)
against the intended council and confirm which model it actually requested. A
passing health check does not prove routing.

## Account Creation Flow

By default, `deploy_stack.sh` creates or verifies one agent account.

Compose path:

1. Starts Redis topology.
2. Runs a one-off `council` container with inline Python.
3. Writes account data to Redis.
4. Stores `NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` in `deploy/.env` when a new
   token is minted.

Kind path:

1. Deploys Helm.
2. Execs into the council pod.
3. Runs `scripts/agent_accounts.py`.
4. Stores `NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` in `deploy/.env`.
5. Patches the Kubernetes Secret with the token.
6. Attempts a `rollout restart` of `<release>-demo` so it would pick up the
   token. The chart renders no demo Deployment, so this is a tolerated no-op on
   a stock Kind deploy.

Skip with:

```bash
SKIP_ACCOUNT=1 ./scripts/deploy_stack.sh
```

Rotate with:

```bash
AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

Manage accounts on a Docker-only host:

```bash
./scripts/agent_accounts.sh create alice-agent --role developer
./scripts/agent_accounts.sh rotate-token alice-agent
./scripts/agent_accounts.sh grant-council alice-agent council-prod
./scripts/agent_accounts.sh list
```

The account helper uses the same Compose Redis topology selection as the main
deploy script. It defaults to Sentinel and can read `COMPOSE_REDIS_TOPOLOGY`
from `deploy/.env`.

## Public Endpoints

Compose publishes these by default when the relevant service is enabled:

| Endpoint | Default URL | Gate |
|---|---|---|
| Monitor UI | `http://$NOMOS_PUBLIC_HOST:9901/` | Always in Compose deploy. |
| MCP | `http://$NOMOS_PUBLIC_HOST:9901/mcp` | Always in Compose deploy. Requires `Authorization: Bearer $MONITOR_MCP_PROXY_TOKEN`. |
| gRPC | `$NOMOS_PUBLIC_HOST:9904` | Always in Compose deploy. |
| Agent signup | `https://$NOMOS_PUBLIC_HOST:9905/` | `WITH_AGENT_SIGNUP=1` default. |
| Agent admin | `https://$NOMOS_PUBLIC_HOST:9905/admin` | `WITH_AGENT_SIGNUP=1` default. |
| OpenShell executor | `http://$NOMOS_PUBLIC_HOST:9907` | `EXECUTOR_PROFILE=executor-openshell`. |
| Demo UI | `https://$NOMOS_PUBLIC_HOST:8000` | `WITH_DEMO=1`. |
| Redis | `$NOMOS_PUBLIC_HOST:6379` | Always published by base Compose. |
| OPA | `http://$NOMOS_PUBLIC_HOST:8181` | `WITH_OPA=1` default. |

Kind uses local port-forwards instead of host-published ports:

| Port-forward | Default local URL | Gate |
|---|---|---|
| Monitor/MCP | `http://127.0.0.1:9901` | `KIND_PORT_FORWARD_MONITOR=1`. |
| Council gRPC | `127.0.0.1:9904` | `KIND_PORT_FORWARD_GRPC=1`. |
| Signup | `https://127.0.0.1:9905` | `KIND_PORT_FORWARD_AGENT_SIGNUP=1`. |
| Demo | n/a | The chart has no demo workload to forward to; the demo is Compose-only. |
| Executor | `http://127.0.0.1:9907` | `KIND_PORT_FORWARD_EXECUTOR=1`. |

## Persistent State

Compose volumes:

- `redis-data`, `redis-data-1`, `redis-data-2`
- `council-keys`
- `council-public-keys`
- `council-ledger`
- executor key/workspace volumes

Host bind mounts:

- `deploy/dynamic-config` -> dynamic council catalog/profiles.
- `deploy/executor*.yaml` -> executor configs.
- `policies` -> OPA policies.

Helm PVCs:

- Redis persistence.
- Council private keys.
- Council public keys.
- Council ledger.
- Council dynamic config.
- Executor keys.
- Executor workspace.

## Common Recipes

Remote Compose with Sentinel, OpenShell, signup, and demo:

```bash
DEPLOY_TARGET=compose WITH_DEMO=1 EXECUTOR_PROFILE=executor-openshell ./scripts/deploy_stack.sh
```

Remote Compose with explicit public host:

```bash
NOMOS_PUBLIC_HOST=203.0.113.10 DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh
```

Remote Compose with external OPA:

```bash
DEPLOY_TARGET=compose WITH_OPA=0 COUNCIL_OPA_URL=https://opa.example.internal ./scripts/deploy_stack.sh
```

Remote Compose with no signup UI:

```bash
DEPLOY_TARGET=compose WITH_AGENT_SIGNUP=0 ./scripts/deploy_stack.sh
```

Remote Compose with standalone Redis:

```bash
DEPLOY_TARGET=compose COMPOSE_REDIS_TOPOLOGY=standalone ./scripts/deploy_stack.sh
```

Kind with defaults:

```bash
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh
```

Kind with an existing Kubernetes Secret and no local env writes:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh
```

Kind without local port-forwards:

```bash
KIND_PORT_FORWARD=0 ./scripts/deploy_stack.sh
```

Kind reusing images:

```bash
SKIP_BUILD=1 ./scripts/deploy_stack.sh
```

## Troubleshooting Checklist

### Demo endpoint is missing

Compose:

```bash
DEPLOY_TARGET=compose WITH_DEMO=1 ./scripts/deploy_stack.sh
```

Then look for:

```text
Demo UI: https://<host>:8000
```

Kind:

- Not available. The chart ships no demo Deployment or Service, so there is
  nothing to enable or port-forward. Use the Compose path above.

### Sentinel validation fails with missing `sentinel_nodes`

Check that the Sentinel override is active:

```bash
docker compose -f deploy/docker-compose.yml \
  -f deploy/docker-compose.redis-sentinel.yml \
  --profile executor-openshell config --services
```

Expected services include:

```text
redis-1
redis-2
redis-sentinel-1
redis-sentinel-2
redis-sentinel-3
```

Also check the council container has:

```text
COUNCIL_REDIS_MODE=sentinel
COUNCIL_REDIS_SENTINEL_SERVICE_NAME=nomos-master
COUNCIL_REDIS_SENTINEL_NODES=redis-sentinel-1:26379,redis-sentinel-2:26379,redis-sentinel-3:26379
```

### Wrong public URL is printed

Set:

```bash
NOMOS_PUBLIC_HOST=<reachable-ip-or-dns>
```

The deploy script writes it to `deploy/.env` and prints URLs from that value.

### Port conflict

Override the relevant port in the shell or `deploy/.env`:

```bash
MONITOR_PORT=19901 DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

The script preflights common host ports before starting services.

### Account token did not change

Existing accounts are not rotated unless requested:

```bash
AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

### Compose did not pick up a topology change

Force recreation of Redis topology services:

```bash
docker compose -f deploy/docker-compose.yml \
  -f deploy/docker-compose.redis-sentinel.yml \
  --profile executor-openshell \
  up -d --force-recreate redis redis-1 redis-2 redis-sentinel-1 redis-sentinel-2 redis-sentinel-3
```

Do not use `down -v` unless you intentionally want to delete volumes.
