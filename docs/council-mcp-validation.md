# Council Deployment and MCP Validation

Use this checklist after every local council deployment before handing the
service to an agent. It verifies the external MCP surface, the upstream council,
the gRPC proxy, account authentication, Writ minting, and the executor boundary.

The same first checks work for Kind and Docker Compose. The deeper smoke scripts
are Compose-local today because they talk to Redis on a host port and inspect the
executor container with `docker exec`.

## 1. Deploy One Known Target

From the repository root, choose one path.

```bash
# Default local Kubernetes path. Starts Kind port-forwards by default.
NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh

# Kind path for a pre-created nomos/nomos-secrets Secret.
# This validates the Secret and does not create or update deploy/.env.
KIND_USE_EXISTING_SECRET_ONLY=1 ./scripts/deploy_stack.sh

# Docker Compose path. Use this when you want the full local smoke scripts.
NVIDIA_API_KEY=nvapi-... DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
```

The script creates `deploy/.env`, generates missing local secrets, deploys the
stack, creates or verifies the default agent account, and stores
`NOMOS_AGENT_ID` plus `NOMOS_AUTH_TOKEN` when it mints a new token.
With `KIND_USE_EXISTING_SECRET_ONLY=1`, the script requires an existing
`nomos/nomos-secrets` Secret, validates required keys, and patches any newly
minted agent token back into that Secret instead of writing `deploy/.env`. If
the Secret is Helm-owned, it reuses the live Secret data through a temporary
`0600` Helm values file that is deleted after the upgrade.

If the account already existed, the raw token cannot be recovered because Redis
stores only a hash. Rotate deliberately when you need a fresh validation token:

```bash
AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

## 2. Load Local Validation Environment

The validation commands below assume the default local ports:

- MCP through monitor: `http://127.0.0.1:9901/mcp`
- Monitor health: `http://127.0.0.1:9901/healthz`
- gRPC proxy: `127.0.0.1:9904`
- OpenShell executor: `http://127.0.0.1:9907`

Load the generated account token into your shell. For the default bootstrap or
Compose path, use `deploy/.env`:

```bash
set -a
. deploy/.env
set +a

export NOMOS_MCP_LIVE_URL="${NOMOS_MCP_LIVE_URL:-http://127.0.0.1:9901/mcp}"
export NOMOS_GRPC_SMOKE_ADDR="${NOMOS_GRPC_SMOKE_ADDR:-127.0.0.1:9904}"
export NOMOS_EXECUTOR_URL="${NOMOS_EXECUTOR_URL:-http://127.0.0.1:9907}"
```

For a Kind deployment that uses only the Kubernetes Secret, do not source a
local env file. Read the agent values from `nomos-secrets` and avoid echoing the
token:

```bash
export NOMOS_MCP_LIVE_URL=http://127.0.0.1:9901/mcp
export NOMOS_GRPC_SMOKE_ADDR=127.0.0.1:9904
export NOMOS_EXECUTOR_URL=http://127.0.0.1:9907
export NOMOS_AGENT_ID="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_AGENT_ID}' | base64 -d)"
export NOMOS_AUTH_TOKEN="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_AUTH_TOKEN}' | base64 -d)"
test -n "$NOMOS_AGENT_ID"
test -n "$NOMOS_AUTH_TOKEN"
```

If the account already existed and the Secret does not contain a usable raw
token, rotate deliberately without creating `deploy/.env`:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

For remote Compose, replace `127.0.0.1` with the reachable
`NOMOS_PUBLIC_HOST` printed by `deploy_stack.sh`.

Install the local test/client dependencies once per checkout:

```bash
uv sync --extra dev
```

## 3. Check Service Health First

These checks should pass before any MCP test. If they fail, fix deployment
health before debugging client payloads.

```bash
# Monitor is reachable and can reach the upstream council debug endpoint.
curl -fsS http://127.0.0.1:9901/healthz | python3 -m json.tool

# Executor has the trusted executor contract and council public key loaded.
curl -fsS http://127.0.0.1:9907/v1/health | python3 -m json.tool
```

Expected signals:

| Check | Good Signal |
|---|---|
| Monitor health | HTTP 200 with `upstream.ok: true`. |
| Executor health | `status: ok`, `contract_version`, and a non-empty key fingerprint. |
| Agent token source | `NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` exist in `deploy/.env` or `nomos-secrets` unless `SKIP_ACCOUNT=1`. |

## 4. Run Portable MCP Contract Tests

These tests are the safest first validation path for both Kind and Compose.
They do not require direct Redis access from the host.

```bash
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed
```

This verifies:

- gRPC proxy can reach MCP through the monitor.
- MCP tool discovery works.
- Dynamic council read APIs respond.
- v2 routing rejects missing or unknown `council_id` values.
- The generated HTML report lands at `test-reports/nomos-test-report.html`.

## 5. Run Low-Risk Live Council MCP Tests

These tests call the LLM-backed council. They are intentionally low-risk and
should pass with the default `developer` account created by `deploy_stack.sh`.

```bash
NOMOS_MCP_LIVE_URL="$NOMOS_MCP_LIVE_URL" \
NOMOS_AGENT_ID="$NOMOS_AGENT_ID" \
NOMOS_AUTH_TOKEN="$NOMOS_AUTH_TOKEN" \
uv run pytest \
  tests/test_mcp_live_integration.py::test_live_council_exposes_expected_tools_and_key \
  tests/test_mcp_live_integration.py::test_live_council_fails_closed_for_invalid_auth \
  tests/test_mcp_live_integration.py::test_live_council_mints_and_indexes_low_risk_writ \
  -q
```

This verifies:

- MCP initializes with a real client session.
- `council_get_public_key` exposes a valid Ed25519 key.
- Invalid auth fails closed and returns no Writ.
- A stdout-only command is reviewed, approved, minted as a Writ, and indexed.

Run the whole live MCP authorization file only when the token has elevated
operator or admin permissions:

```bash
AGENT_ROLE=admin AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh

set -a
. deploy/.env
set +a
export NOMOS_MCP_LIVE_URL="${NOMOS_MCP_LIVE_URL:-http://127.0.0.1:9901/mcp}"

NOMOS_MCP_LIVE_URL="$NOMOS_MCP_LIVE_URL" \
NOMOS_AGENT_ID="$NOMOS_AGENT_ID" \
NOMOS_AUTH_TOKEN="$NOMOS_AUTH_TOKEN" \
uv run pytest tests/test_mcp_live_integration.py -q
```

The full file includes scenarios that intentionally exercise infrastructure
permission checks and certified evidence requirements.

## 6. Run Compose-Local Deep Smoke Tests

Use these after a Compose deployment when you want the full local auth and
executor path. They assume Redis is reachable on `127.0.0.1:6379` and the
default executor container is named `deploy-executor-openshell-1`.

```bash
DEPLOY_TARGET=compose ./scripts/deploy_stack.sh

set -a
. deploy/.env
set +a

uv run python scripts/test_smoke.py
uv run python scripts/test_live_e2e.py
```

`scripts/test_smoke.py` verifies Redis auth, MCP connectivity, tool listing,
valid and invalid token behavior, LLM council approval and denial paths, Writ
minting, executor submission, nonce replay rejection, tamper rejection, and
agent binding.

`scripts/test_live_e2e.py` verifies three live scenarios against the default
OpenShell executor:

- An approved command is minted as a Writ and executed in the sandbox.
- A bare command is denied while the absolute executable path is approved.
- A destructive command is reviewed by the council and the decision is shown.

## 7. Expected Failure Triage

| Symptom | Most Likely Cause | Next Check |
|---|---|---|
| `/healthz` returns 503 | Monitor cannot reach upstream council. | Council logs and `MONITOR_UPSTREAM` value. |
| MCP initializes but no tools appear | Council NAT/MCP startup failed. | Council logs and `configs/council_nat_workflow.yaml`. |
| Live MCP tests skip | `NOMOS_MCP_LIVE_URL` or `NOMOS_AUTH_TOKEN` is missing. | Reload `deploy/.env`, or load `NOMOS_AGENT_ID`/`NOMOS_AUTH_TOKEN` from `nomos-secrets` for Kind. Rotate the account if needed. |
| Invalid auth is accepted | Auth is disabled in the council profile. | Verify `require_auth: true` for the target council. |
| Low-risk Writ is denied | OPA or council reviewer rejected the command text. | Use absolute executable paths and detailed justification. |
| Executor rejects at `agent_binding` | Missing or wrong `X-Agent-ID`. | Header must match the Writ `target_agent_id`. |
| Executor rejects at `nonce` | Writ already executed. | Request a new Writ; Writs are single-use. |
| Executor rejects at `signature` | Writ was tampered or signed by an unknown key. | Check council public key sharing and key rotation state. |
| `test_smoke.py` fails on Kind | Smoke script expects host Redis access. | Use the portable MCP tests or run Compose. |
| `test_live_e2e.py` fails on Kind | Script uses `docker exec` against a Compose container name. | Use Compose for this script. |

## 8. Pass Criteria

A local deployment is ready for agent integration work when all of these are
true:

1. The chosen `deploy_stack.sh` path completes without errors.
2. Monitor `/healthz` returns HTTP 200 with `upstream.ok: true`.
3. Executor `/v1/health` returns `status: ok` and a key fingerprint.
4. `grpc-live` and `mcp-live` pass with `--require-deployed`.
5. The low-risk live MCP council tests pass with the intended agent account.
6. For Compose validation environments, `scripts/test_smoke.py` passes.

After that, client integration work should focus on agent UX, tool-call shaping,
and how the agent supplies intent metadata. The service-side deployment and MCP
contract are already validated.
