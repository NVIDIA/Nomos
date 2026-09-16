<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Council Deployment and MCP Validation

Use this checklist after every local council deployment before handing the
service to an agent. It verifies the external MCP surface, the upstream council,
the gRPC proxy, account authentication, Writ minting, and the executor boundary.

The same first checks work for Kind and Docker Compose. The deeper smoke scripts
are Compose-local today because they talk to Redis on a host port and inspect the
executor container with `docker exec`.

## Public Model Endpoint Compatibility

Run this check before deploying a changed model configuration. It calls live
endpoints and consumes API quota, so provide every API-key environment variable
referenced by the selected configuration. It uses the production backend and
response parsers for every voting member plus enabled chairman and code-review
roles, including a harmless packaged file. Requests run sequentially with SDK
and agent retries disabled; the configured request timeout remains in force.

```bash
uv run python scripts/test_public_nim_smoke.py --config configs/council.yaml
```

To check the commented mixed-model example, the snippet below uncomments the
marked backend block and assigns those backends to voting members. Model names
and endpoint settings come from the configuration:

```bash
export COUNCIL_SMOKE_DIR="$(mktemp -d)"
uv run python - <<'PY'
import os
from pathlib import Path
import yaml

source = Path("configs/council.yaml").read_text()
start = "  # BEGIN OPTIONAL MIXED BACKENDS"
end = "  # END OPTIONAL MIXED BACKENDS"
assert source.count(start) == source.count(end) == 1
before, rest = source.split(start)
block, after = rest.split(end)
active = "\n".join(line.replace("  # ", "  ", 1) for line in block.splitlines())
config = yaml.safe_load(before + active + after)
assignments = {"environment-isolation": "nim-deepseek", "blast-containment": "nim-nemotron"}
for member in config["members"]:
    if member["id"] in assignments:
        member["backend"] = assignments[member["id"]]
output = Path(os.environ["COUNCIL_SMOKE_DIR"]) / "council-trio.yaml"
output.write_text(yaml.safe_dump(config, sort_keys=False))
print(output)
PY
uv run python scripts/test_public_nim_smoke.py \
  --config "$COUNCIL_SMOKE_DIR/council-trio.yaml"
```

Keep each run's exit status and output. The script records the configuration
hash and per-role backend, sanitized endpoint, requested model, returned model
when available, elapsed time, and outcome without credentials or model response
bodies. A valid denial is compatible; transport/authentication failures,
timeouts, malformed responses, parsing errors, and fallback-only results fail
the check. Remaining roles still run and any failure produces a nonzero exit.
Diagnose failures before a separately recorded rerun; do not silently substitute
endpoints/models or increase timeouts to turn a failing run green.

A passing endpoint check establishes compatibility for these requests. Evaluate
security quality with representative security and benign-use cases. Verify
deployed profile selection with the tests below after
[changing the council model configuration](deployment-setup.md#changing-the-council-model-configuration).
Record the deployment mode and revision, profile hash, requested model routing,
and completed versus skipped checks. Confirm the outgoing model identifier
through an inference request after updating the profile.

For Compose, rerun the same check against the exact profile mounted by the
running service. Clear provider keys exported earlier so `deploy/.env` supplies
the values used by Compose without printing them:

```bash
env -u NVIDIA_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  uv run --env-file deploy/.env python scripts/test_public_nim_smoke.py \
  --config deploy/dynamic-config/council.yaml
```

For a dynamically created council, pass its profile instead:

```bash
env -u NVIDIA_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  uv run --env-file deploy/.env python scripts/test_public_nim_smoke.py \
  --config "deploy/dynamic-config/councils/$COUNCIL_ID.yaml"
```

Save the per-role `requested_model` records, then run the deployed MCP requests
below for the same council. In a shared environment, correlate those MCP request
timestamps with the provider's request log. A deployment validation using a
recording endpoint can assert the request-body `model` values directly and
confirm profile reloads without exposing provider credentials.

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

For a shared or long-lived deployment, create or reuse the dedicated validation
identity instead of rotating the deployment account. It carries the `developer`
role so the authenticated scenarios below — which create files under
`/workspace` — can actually run; operator and admin infrastructure checks stay
separately opt-in.

Run a standard deployment first. This flag adds the validation account alongside
the deployment account rather than replacing the bootstrap, so running it against
a deployment that has never been bootstrapped leaves the stack without its
`NOMOS_AGENT_ID` credential:

```bash
./scripts/deploy_stack.sh                        # standard deployment account
VALIDATION_IDENTITY=1 ./scripts/deploy_stack.sh  # then the validation identity
```

Its raw token is persisted exactly once under
`NOMOS_VALIDATION_AGENT_ID`/`NOMOS_VALIDATION_AUTH_TOKEN` in the protected
`0600` env file and, for Kind, in `nomos/nomos-secrets`. The presence check
never prints the token. Redis retains only a SHA-256 hash, so an absent raw token
cannot be recovered. If the account exists but the protected token is missing,
the script stops before authenticated validation and offers three explicit
choices: restore the protected secret, provision another dedicated identity, or
deliberately rotate the selected identity. Rotation invalidates old clients;
provisioning and restoring do not.

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

For the dedicated validation identity, map its separate protected values after
sourcing the file:

```bash
export NOMOS_AGENT_ID="$NOMOS_VALIDATION_AGENT_ID"
export NOMOS_AUTH_TOKEN="$NOMOS_VALIDATION_AUTH_TOKEN"
test -n "$NOMOS_AGENT_ID" && test -n "$NOMOS_AUTH_TOKEN"
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

For a dedicated Kind validation identity, read the separate keys without
printing them. A clean checkout can reuse the same Secret:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 VALIDATION_IDENTITY=1 ./scripts/deploy_stack.sh
export NOMOS_AGENT_ID="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_VALIDATION_AGENT_ID}' | base64 -d)"
export NOMOS_AUTH_TOKEN="$(kubectl --context kind-nomos -n nomos get secret nomos-secrets -o jsonpath='{.data.NOMOS_VALIDATION_AUTH_TOKEN}' | base64 -d)"
test -n "$NOMOS_AGENT_ID" && test -n "$NOMOS_AUTH_TOKEN"
```

Only when invalidating the validation identity's old credential is acceptable,
rotate it deliberately without creating `deploy/.env`:

```bash
KIND_USE_EXISTING_SECRET_ONLY=1 VALIDATION_IDENTITY=1 AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
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

Also run the packaged-file scenario explicitly. It is gated separately because
it asserts a particular model verdict and reasoning, rather than only endpoint
compatibility; without this flag pytest skips it:

```bash
NOMOS_MCP_LIVE_PROBABILISTIC_DEMO=1 \
uv run pytest \
  tests/test_mcp_live_integration.py::test_live_council_reviews_packaged_file_attachment \
  -q
```

This checks that the deployed code-review role sees the file and its digest and
that the full council approves the harmless scenario. A valid model denial may
pass the public compatibility check while failing this stronger scenario. Record
that distinction; do not count a skipped packaged-file test as validation.

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

uv run --env-file deploy/.env python scripts/test_smoke.py
uv run --env-file deploy/.env python scripts/test_live_e2e.py
```

Use the `deploy/.env` generated by the same Compose deployment so the scripts
receive its Redis credentials, integrity key, agent identity, and auth token.

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
| Live MCP tests skip, `NOMOS_MCP_LIVE_URL` unset | The live-test target is not configured. | Set `NOMOS_MCP_LIVE_URL` to the deployment's MCP URL (section 2). No credential action changes this. |
| Live MCP tests skip, `NOMOS_AUTH_TOKEN` unset | No agent credential is loaded. | Restore the protected credential, or provision/reuse `VALIDATION_IDENTITY=1`; rotate only when invalidating the selected identity is intentional. |
| Deploy stops with `Could not reach the account store` | Redis was unreachable, so the credential could not be verified either way. | Retry once Redis is reachable. **Do not rotate** — the token is unverified, not known-bad, and rotating would invalidate a credential that may still be good. |
| Deploy stops with `token does not authenticate account` | The stored token does not match, **or** the account is disabled or absent. Verification deliberately does not say which, so the message alone does not distinguish them. | Check with `agent_accounts.py show <agent_id>`. Disabled → `agent_accounts.py enable <agent_id>`; absent → provision it. Only when the account is present and enabled is the token itself wrong: restore the protected secret, or rotate deliberately with `AGENT_FORCE_ROTATE=1`. |
| Deploy stops with `is not a member of council(s)` | The account exists but is outside the configured `AGENT_COUNCILS`. | `agent_accounts.py grant-council <agent_id> <council_id>`, or provision a separate identity for that council. Restoring or rotating the token will not help — the credential is valid, the membership is wrong. |
| Deploy stops with `invalid account-integrity envelope` | The stored record no longer matches its integrity HMAC, so live authentication would reject the account. | Any account-mutating command re-signs the envelope; `agent_accounts.py enable <agent_id>` is the least invasive because it leaves the token unchanged. Recreate with `create --force` only if the record itself is wrong. Rotating the token is not a fix. |
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
