<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Getting started

This guide takes you from a clean Linux host to a running Nomos stack and one
end-to-end request: the council reviews a command against the task context the
agent sends with it, approves it and returns a signed Writ, and the executor
verifies the Writ and runs the command. Allow about 15 minutes.

New to Nomos? Read [how a request flows](architecture.md#how-a-request-flows)
first. Already have a running council and only need to connect an agent? Go to
[Integrate your own agent](integrate-your-agent.md).

## Prerequisites

Nomos deployment is supported and tested on Linux. Other host environments,
including native macOS, are not currently part of the supported or tested
platform matrix. They may work when the required dependencies are installed,
but platform-specific setup or troubleshooting may be necessary. For the most
consistent experience, use a Linux host or Linux virtual machine.

- Kind and Docker Compose deployments, as well as account-management
  operations, require Bash 4.3 or newer.
- Default Kind path: Docker, Kind, kubectl, Helm, GNU `timeout` when
  `KIND_PORT_FORWARD=1`, and an NVIDIA API key.
- Docker Compose path: Docker Compose v2 and an NVIDIA API key.
- Anthropic/OpenAI keys are only needed if you switch members to direct-vendor backends.



## 1. Choose a Local Target

The fastest path is `scripts/deploy_stack.sh`. It creates `deploy/.env`,
generates local secrets, deploys the stack, creates or verifies the first agent
account, and prints the URLs you should use.

> **Which path am I on?** This quick start uses `scripts/deploy_stack.sh`. Its
> Kind path deploys **service mode**: `deploy/kubernetes/kind-values.yaml` sets
> `storage.mode: service`, so PostgreSQL, dev Vault transit signing and an nginx
> `/mcp` gateway come up with the stack. Its Compose path
> (`DEPLOY_TARGET=compose`) is the one that uses a redis durable store. `deploy/`
> documents a second pair of bootstrappers — `deploy/scripts/compose-up.sh` and
> `deploy/kubernetes/scripts/kind-up.sh` — which deploy service mode on both
> targets. They are not interchangeable: the URLs, the ports and the credential
> layout differ. Start here unless you specifically need service mode on
> Compose; see [`deploy/README.md`](../deploy/README.md) for that path.

| Target | Use When | Key Prerequisites |
|---|---|---|
| Kind, default | You want the closest local Kubernetes path. | Docker, Kind, kubectl, Helm, NVIDIA API key |
| Docker Compose | You want a simpler Docker-only startup or a remote VM install. | Docker Compose v2, NVIDIA API key |

The default council uses the same model for all roles through
[NVIDIA API Catalog](https://build.nvidia.com/). Retries and debate rounds can
add requests, so size provider quota for the enabled roles and expected
workload. Commented mixed-model and direct-vendor examples are in
`configs/council.yaml`; see
[One model by default, several if you want them](reference/configuration.md#one-model-by-default-several-if-you-want-them).
Open the Build/API page for any configured model, choose **Generate API Key**,
and store the generated value as `NVIDIA_API_KEY`.
If the hosted example model is unavailable and you have a supported NVIDIA GPU,
follow the [self-hosted quick start](self-hosted-models.md#quick-start-docker-compose)
to run a local NIM instead. The bundled hosted and self-hosted models are
examples for getting started; select production models based on your use case,
measured authorization quality, latency, hardware, and licensing requirements.

The hosted NVIDIA API Catalog endpoints are intended for evaluation and trial
use. Their response time includes shared endpoint queueing and model serving,
so it can vary with endpoint load and rate limits and does not measure Nomos
processing overhead. A slow response, timeout, or HTTP 429 can prevent a council
member from completing its review, in which case Nomos fails closed.

If a trial endpoint is temporarily overloaded or returns HTTP 429, wait before
retrying. When evaluating a model whose normal response time exceeds the current
limit, update `request_timeout_seconds` on that `llm_backends` entry in the
active council config and restart the council. For predictable production
latency, use a self-hosted NVIDIA NIM or another production inference backend,
such as the documented direct-provider alternatives, sized for your workload.
Review the
[NVIDIA API Trial Terms](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)
before relying on the hosted API Catalog endpoints.
Anthropic/OpenAI keys are only needed after
activating the matching commented direct-vendor backends.

## 2. Start the Council

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

Agent bootstrap distinguishes four states and reports only credential presence,
never the credential value:

- An absent account is created and its one-time raw token is written to the
  protected store.
- An existing account is reused without mutation only after the protected raw
  token is SHA-256 hashed and compared in constant time with the account hash;
  the token index must also resolve to that account.
- An existing account with a missing or unverifiable protected token stops the
  deployment before authenticated scenarios and prints safe recovery choices.
- `AGENT_FORCE_ROTATE=1` explicitly rotates the selected account and replaces
  its stored token.

Redis stores only a SHA-256 hash, so a lost raw token cannot be recovered from
the account record. Restore the protected file or Secret from your secret
manager when its credential is still valid. Verification is read-only and
prints neither the raw candidate nor the complete stored hash. Provisioning
creates another identity and leaves existing credentials valid; rotation
replaces a selected identity's token and immediately invalidates its old
credential.

For repeatable validation of a shared or long-lived deployment, provision a
dedicated identity with membership only in the configured validation council
(the default is `council-prod`). It is created with the `developer` role,
because the documented authenticated end-to-end checks write under `/workspace`
and so need `file:write`; operator and admin infrastructure checks remain
separately opt-in. Because the identity is dedicated and separate from the
deployment account, it can be rotated without disturbing `NOMOS_AUTH_TOKEN` —
though rotating it still invalidates any client already holding the old
`NOMOS_VALIDATION_AUTH_TOKEN`.

Run a normal deployment first. `VALIDATION_IDENTITY=1` provisions the validation
account *in addition to* the deployment account — it is not a way to bootstrap a
fresh deployment on its own, and using it before the standard account exists
leaves the deployment without its `NOMOS_AGENT_ID` credential:

```bash
./scripts/deploy_stack.sh                      # standard deployment account
VALIDATION_IDENTITY=1 ./scripts/deploy_stack.sh  # then the validation identity
```

This stores separate `NOMOS_VALIDATION_AGENT_ID` and
`NOMOS_VALIDATION_AUTH_TOKEN` values, preserving the deployment account's
`NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN`. Local values remain in the `0600`
`deploy/.env`; Kind also persists them in `nomos/nomos-secrets`. A clean
checkout can reuse the Kubernetes values with
`KIND_USE_EXISTING_SECRET_ONLY=1 VALIDATION_IDENTITY=1`. If the validation
account exists but those values are absent, restore them, choose a different
dedicated `AGENT_ID`, or deliberately rotate only the validation identity:

```bash
VALIDATION_IDENTITY=1 AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

## 3. Find the Running Services

The deploy script prints the active URLs at the end.

| Service | Kind default with port-forwards | Compose default |
|---|---|---|
| Monitor UI | `http://127.0.0.1:9901/` | `http://$NOMOS_PUBLIC_HOST:9901/` |
| Council MCP | `http://127.0.0.1:9901/mcp` | `http://$NOMOS_PUBLIC_HOST:9901/mcp` |
| Council gRPC | `127.0.0.1:9904` | `$NOMOS_PUBLIC_HOST:9904` |
| Agent signup | `https://127.0.0.1:9905/` | `https://$NOMOS_PUBLIC_HOST:9905/` |
| Demo UI | not deployed (the Kind chart has no demo) | `https://$NOMOS_PUBLIC_HOST:8000/` when `WITH_DEMO=1` |
| Executor | `http://127.0.0.1:9907` | `http://$NOMOS_PUBLIC_HOST:9907` |

The Demo UI and Demo API require HTTP Basic auth. `deploy_stack.sh` generates
`DEMO_AUTH_PASSWORD` for normal bootstrap runs and stores it in `deploy/.env`.
With `KIND_USE_EXISTING_SECRET_ONLY=1`, the password lives in the
`nomos/nomos-secrets` Secret under `DEMO_AUTH_PASSWORD`; the username defaults
to `demo` through `DEMO_AUTH_USER`.

Compose publishes host ports by default. On a remote machine, set
`NOMOS_PUBLIC_HOST=<reachable-ip-or-dns>` before deploy and put firewall or
reverse-proxy policy around exposed ports.

## 4. Verify the Stack

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
export MONITOR_MCP_PROXY_TOKEN="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.MONITOR_MCP_PROXY_TOKEN}' | base64 -d)"
```

When using the dedicated validation identity, map its protected values into the
standard client variables without printing the token:

```bash
set -a
. deploy/.env
set +a
export NOMOS_AGENT_ID="$NOMOS_VALIDATION_AGENT_ID"
export NOMOS_AUTH_TOKEN="$NOMOS_VALIDATION_AUTH_TOKEN"
test -n "$NOMOS_AGENT_ID" && test -n "$NOMOS_AUTH_TOKEN"
```

For the full post-deploy validation ladder, use
[Council MCP Validation](council-mcp-validation.md). It separates the
portable Kind/Compose MCP checks from Compose-only smoke scripts.

## 5. Manage Agent Accounts

`deploy_stack.sh` creates `agent-developer` by default unless `SKIP_ACCOUNT=1`
is set. To add accounts later, use the helper for your target.

**Docker Compose.** `scripts/agent_accounts.sh` runs the accounts CLI inside the
Compose council container, so it works only on the Compose target:

```bash
./scripts/agent_accounts.sh create alice-agent --role developer
./scripts/agent_accounts.sh rotate-token alice-agent
./scripts/agent_accounts.sh disable alice-agent
./scripts/agent_accounts.sh list
```

**Kind.** `deploy/kubernetes/scripts/kind-register-agent.sh` runs the same
provisioning code as an in-cluster Job against the store the council reads, and
prints the new token once:

```bash
AGENT_ROLE=developer ./deploy/kubernetes/scripts/kind-register-agent.sh alice-agent council-prod
```

Role presets:

| Role | Permissions |
|---|---|
| `readonly` | `shell:read`, `file:read` |
| `developer` | `shell:read`, `shell:write`, `file:read`, `file:write` |
| `operator` | developer permissions plus `infra:deploy` |
| `admin` | all permission scopes |

## 6. Make an End-to-End Request

This path asks the council to authorize a command, receives a signed writ, and
submits that writ to the executor.

```python
import asyncio, json, urllib.request
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

AUTH_TOKEN  = "<NOMOS_AUTH_TOKEN from deploy/.env>"
PROXY_TOKEN = "<MONITOR_MCP_PROXY_TOKEN from deploy/.env>"
AGENT_ID    = "<NOMOS_AGENT_ID from deploy/.env>"  # the account that owns AUTH_TOKEN

async def authorize_and_execute():
    # The monitor fronts the council with a Bearer-authenticated /mcp proxy.
    # Both tokens are written to deploy/.env by the deploy script.
    async with streamablehttp_client(
        "http://127.0.0.1:9901/mcp",
        headers={"Authorization": f"Bearer {PROXY_TOKEN}"},
        # Above the monitor proxy deadline (180s), which is above the council's
        # retry budget (150s). The client default is 30s, so a healthy council
        # response -- measured up to 47s -- can arrive after this call has
        # already hung up.
        timeout=240,
    ) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            result = await session.call_tool("council_authorize", arguments={
                "auth_token": AUTH_TOKEN,
                "agent_id": AGENT_ID,
                # Exclusive creation: `write_text` would overwrite an existing
                # file and would follow a symlink, either of which makes the
                # blast_radius below untrue. Opening with "x" fails instead.
                "command": "/usr/local/bin/python3 -c 'open(\"/workspace/test.txt\", \"x\").write(\"hello\\\\n\")'",
                "working_directory": "/workspace",
                "description": "Create a test file in the workspace",
                # The task this step serves. Reviewers deny a command that goes
                # beyond it.
                "context": "First end-to-end check of a new Nomos stack",
                "target_resource": "/workspace",
                "justification": "Testing the end-to-end authorization flow",
                # The council denies a state-changing command that does not state
                # its blast radius. Say what changes and how to undo it.
                "blast_radius": "Creates exactly one new file, /workspace/test.txt, "
                                "and fails without modifying anything if that path "
                                "already exists or is a symlink. Nothing else is read "
                                "or modified. Rollback: delete the created file.",
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

    if not exec_result["success"]:
        # A rejected Writ has no exit code; the error and stage say why.
        print(f"Rejected at {exec_result.get('verification_stage')}: {exec_result.get('error')}")
        return
    print(f"Exit code: {exec_result['exit_code']}")
    print(f"Stdout: {exec_result.get('stdout', '')}")

asyncio.run(authorize_and_execute())
```

A denial returns `status: "denied"` with `guidance`. The executor returns HTTP
200 even when it rejects a Writ, so check `success` and `verification_stage` in
its response. To wrap your own agent the same way, with denial handling and
signed receipts, see [Integrate your own agent](integrate-your-agent.md).

## 7. Run Tests

```bash
# Core package tests
uv run pytest tests/ -v

# Portable deployed MCP and gRPC checks for Kind or Compose
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed

# Compose-only smoke test (requires host Redis access and deploy/.env from this stack)
uv run --env-file deploy/.env python scripts/test_smoke.py

# Compose-only live E2E demo (requires the default executor container name)
uv run --env-file deploy/.env python scripts/test_live_e2e.py

# Standard local regression wrapper with an HTML report
uv run ./scripts/run_test_suite.py --profile integration
```

## More deployment options

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
| **agent-signup** | `9905` | Dev-only self-service account console |
| **demo** | `8000` | Council Chamber demo UI |
| **executor-openshell** | `9907` | Default trusted executor; isolated OpenShell-backed sandbox |
| **executor-default** | `9902` | Legacy restrictive Docker executor |
| **executor-dev** | `9903` | Dev executor with extended tools and host bind mounts |

Redis, OPA, council, monitor, the gRPC proxy, agent-signup, and the demo are
no-profile Compose services — a bare `docker compose up` starts all of them.
Executors are profile-gated; choose the executor profile you want to run.
`scripts/deploy_stack.sh` starts the no-profile services selectively, so
agent-signup comes up only when `WITH_AGENT_SIGNUP` is set (default `1`) and the
demo only when `WITH_DEMO` is set (default `0`).
Set the `*_BIND_ADDR` and `*_PORT` variables in `deploy/.env` or the shell when
you need tighter exposure than the Compose defaults.

### Key Management

The default Kind path signs Writs with dev Vault transit
(`signing.provider: vault` in `deploy/kubernetes/kind-values.yaml`). The file
keys below apply to the Compose path.

The council generates an Ed25519 key pair at first startup (`/app/keys/council.key` + `council.pub`). The executor reads the public key from the same volume to verify writ signatures.

```bash
# View the council's public key fingerprint
curl -s http://127.0.0.1:9907/v1/health | python3 -m json.tool
```

### Key Rotation

The shipped executors load a single council public key, so rotating the council
key is not zero-downtime: Writs signed with the old key fail verification once
the executor trusts only the new one. Rotate during a quiet window:
1. Generate the new key pair.
2. Switch the council to sign with the new key and restart the executor with
   the new public key.
3. Agents re-request any Writ that was minted before the switch; Writs live at
   at most 300 seconds in the shipped config, so none survive long.

(`WritEnforcer` accepts `additional_keys` for several trusted keys, but the
shipped executor startup does not pass it.)
