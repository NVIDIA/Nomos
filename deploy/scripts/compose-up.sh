#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# One command to bring up a fully functioning Nomos dev system on docker-compose,
# at parity with the Kind deployment: prompts for the required LLM key,
# auto-generates + persists all dev secrets to the gitignored deploy/.env, starts
# the backends (PostgreSQL + dev Vault + redis sentinel), bootstraps Vault transit
# signing via AppRole, brings up the stack in SERVICE mode (council + nginx /mcp
# gateway + monitor-as-reader + executor/openshell + agent-signup + OPA + demo),
# registers a demo agent, and verifies end-to-end.
#
#   deploy/scripts/compose-up.sh           # interactive
#   deploy/scripts/compose-up.sh -y        # non-interactive (deploy/.env must hold the LLM key)
#
# Flags: -y/--yes (non-interactive)  --skip-build  --regenerate-secrets  --agent-id ID
# Env:   COMPOSE_PROJECT (default nomos), SKIP_BUILD, REGENERATE_SECRETS
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

SKIP_BUILD="${SKIP_BUILD:-0}"
AGENT_ID="${AGENT_ID:-dev-agent}"
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) NONINTERACTIVE=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --regenerate-secrets) REGENERATE_SECRETS=1 ;;
    --agent-id) AGENT_ID="${2:?}"; shift ;;
    --agent-id=*) AGENT_ID="${1#*=}" ;;
    -h|--help) sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done
export NONINTERACTIVE="${NONINTERACTIVE:-0}" REGENERATE_SECRETS="${REGENERATE_SECRETS:-0}"

GATEWAY_PORT="$(env_get COUNCIL_GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-9900}"
MONITOR_PORT="$(env_get MONITOR_PORT)"; MONITOR_PORT="${MONITOR_PORT:-9901}"
SIGNUP_PORT="$(env_get AGENT_SIGNUP_PORT)"; SIGNUP_PORT="${SIGNUP_PORT:-9905}"
DEMO_PORT="$(env_get DEMO_PORT)"; DEMO_PORT="${DEMO_PORT:-8000}"

# Wait until a compose service reports healthy (or running, if it has no healthcheck).
wait_healthy() {
  local svc="$1" timeout="${2:-180}" cid state="none" i
  for ((i = 0; i < timeout; i++)); do
    cid="$(dc ps -q "$svc" 2>/dev/null | head -n1)"
    if [ -n "$cid" ]; then
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo starting)"
      [ "$state" = healthy ] && return 0
      [ "$state" = running ] && return 0
      [ "$state" = exited ] && { warn "$svc exited unexpectedly"; return 1; }
    fi
    sleep 1
  done
  warn "$svc did not become healthy within ${timeout}s (last: $state)"
  return 1
}

# ── 1. preflight ────────────────────────────────────────────────────────
log "[1/9] Preflight"
require_cmd docker
docker info >/dev/null 2>&1 || die "docker daemon is not available"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is required"
ok "docker / docker compose present"

# ── 2. LLM key (required) ───────────────────────────────────────────────
log "[2/9] LLM API key (required — the council validates llm_backends at boot)"
if [ -z "$(env_get NVIDIA_API_KEY)$(env_get ANTHROPIC_API_KEY)$(env_get OPENAI_API_KEY)" ]; then
  prompt _k "NVIDIA_API_KEY (blank to use Anthropic/OpenAI instead)" "" secret
  if [ -n "$_k" ]; then
    env_set NVIDIA_API_KEY "$_k"
  else
    prompt _a "ANTHROPIC_API_KEY" "" secret; [ -n "$_a" ] && env_set ANTHROPIC_API_KEY "$_a"
    prompt _o "OPENAI_API_KEY" "" secret;    [ -n "$_o" ] && env_set OPENAI_API_KEY "$_o"
  fi
  unset _k _a _o
fi
[ -n "$(env_get NVIDIA_API_KEY)$(env_get ANTHROPIC_API_KEY)$(env_get OPENAI_API_KEY)" ] \
  || die "an LLM API key is required (NVIDIA / Anthropic / OpenAI). Aborting."
ok "LLM key present (stored in $ENV_FILE)"

# ── 3. dev secrets (generate + persist) ─────────────────────────────────
log "[3/9] Dev secrets (auto-generated, persisted to deploy/.env)"
for k in REDIS_PASSWORD COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY COUNCIL_MANAGEMENT_TOKEN \
         MONITOR_INTERNAL_TOKEN MONITOR_MCP_PROXY_TOKEN MONITOR_DASHBOARD_PASSWORD \
         NOMOS_SIGNUP_ADMIN_PASSWORD NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD \
         DEMO_AUTH_PASSWORD POSTGRES_PASSWORD VAULT_DEV_ROOT_TOKEN; do
  gen_or_keep "$k" >/dev/null
done
default_or_keep MONITOR_DASHBOARD_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_ADMIN_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_COUNCIL_ADMIN_USER council-admin >/dev/null
ok "secrets ready ($([ "$REGENERATE_SECRETS" = 1 ] && echo regenerated || echo 'generated if missing'))"

# ── 4. council catalog ──────────────────────────────────────────────────
# The base compose mounts deploy/dynamic-config at /app/dynamic-config; seed the
# read-only council catalog the same way scripts/deploy_stack.sh does.
log "[4/9] Council catalog (deploy/dynamic-config)"
DCFG="$ROOT_DIR/deploy/dynamic-config"
mkdir -p "$DCFG/councils"
[ -f "$DCFG/local_council_catalog.yaml" ] || cp "$ROOT_DIR/configs/local_council_catalog.yaml" "$DCFG/local_council_catalog.yaml"
for c in council.yaml git_workflow_council.yaml; do
  [ -f "$ROOT_DIR/configs/$c" ] && [ ! -f "$DCFG/$c" ] && cp "$ROOT_DIR/configs/$c" "$DCFG/$c" || true
done
ok "catalog present"

# ── 5. backends ─────────────────────────────────────────────────────────
log "[5/9] Start backends (postgres + vault)"
dc up -d postgres vault
wait_healthy postgres 120 || die "postgres did not become healthy"
wait_healthy vault 120 || die "vault did not become healthy"
ok "backends healthy"

# ── 6. vault bootstrap (transit + AppRole) ──────────────────────────────
log "[6/9] Bootstrap Vault transit signing (AppRole)"
boot="$(dc exec -T -e VAULT_TOKEN="$(env_get VAULT_DEV_ROOT_TOKEN)" -e TRANSIT_MOUNT=transit \
  vault sh -s < "$SCRIPT_DIR/vault-bootstrap-compose.sh")"
role="$(printf '%s\n' "$boot" | sed -n 's/^VAULT_ROLE_ID=//p' | tail -n1)"
secret="$(printf '%s\n' "$boot" | sed -n 's/^VAULT_SECRET_ID=//p' | tail -n1)"
[ -n "$role" ] && [ -n "$secret" ] || die "vault bootstrap did not return AppRole credentials"
env_set VAULT_ROLE_ID "$role"; env_set VAULT_SECRET_ID "$secret"
unset boot role secret
ok "transit keys + AppRole ready (role/secret id persisted to deploy/.env)"

# ── 7. deploy the stack ─────────────────────────────────────────────────
# Stage the bring-up: let the redis-sentinel topology settle (nodes sync as
# replicas, then the sentinels) before the council starts. A cold all-at-once
# `up` races the nodes' replica-sync against their healthchecks and compose
# aborts the whole command when a node is briefly unhealthy.
log "[7/9] Bring up the service-mode stack"
dc up -d redis redis-1 redis-2
for s in redis redis-1 redis-2; do wait_healthy "$s" 120 || die "$s did not become healthy"; done
dc up -d redis-sentinel-1 redis-sentinel-2 redis-sentinel-3
for s in redis-sentinel-1 redis-sentinel-2 redis-sentinel-3; do wait_healthy "$s" 90 || die "$s did not become healthy"; done
if [ "$SKIP_BUILD" = "1" ]; then
  dc up -d
else
  dc up -d --build
fi
ok "stack started"

# ── 8. waits ────────────────────────────────────────────────────────────
log "[8/9] Wait for council + workloads"
wait_healthy council 300 || die "council did not become healthy (check: dc logs council)"
for s in council-gateway council-grpc monitor executor-openshell openshell opa; do
  wait_healthy "$s" 180 || warn "$s not healthy yet (continuing)"
done
# agent-signup needs the feat/onboarding-console image to run in postgres mode; soft.
wait_healthy agent-signup 90 || warn "agent-signup not healthy — build from feat/onboarding-console (service-mode console); non-fatal"
ok "workloads settled"

# ── 9. register agent + verify ──────────────────────────────────────────
log "[9/9] Register demo agent + verify"
AGENT_TOKEN="$(AGENT_ROLE="${AGENT_ROLE:-developer}" "$SCRIPT_DIR/compose-register-agent.sh" "$AGENT_ID" 2>/dev/null \
  | sed -n 's/^AGENT_TOKEN=//p' | tail -n1 || true)"
if [ -n "$AGENT_TOKEN" ]; then
  env_set NOMOS_AGENT_ID "$AGENT_ID"; env_set NOMOS_AUTH_TOKEN "$AGENT_TOKEN"
  ok "registered agent '$AGENT_ID' (token saved to deploy/.env)"
else
  warn "agent registration did not return a token"
fi

AGENT_ID="$AGENT_ID" AGENT_TOKEN="$AGENT_TOKEN" "$SCRIPT_DIR/compose-verify.sh" \
  || die "verification failed — see the checks above"

# ── summary ─────────────────────────────────────────────────────────────
cat <<EOF

${C_G}${C_B}Nomos dev stack (docker-compose) is up and verified.${C_0}

  council /mcp : http://localhost:${GATEWAY_PORT}/mcp     Bearer: $(env_get MONITOR_MCP_PROXY_TOKEN)
  monitor      : http://localhost:${MONITOR_PORT}/        $(env_get MONITOR_DASHBOARD_USER) / $(env_get MONITOR_DASHBOARD_PASSWORD)
  agent-signup : https://localhost:${SIGNUP_PORT}/        $(env_get NOMOS_SIGNUP_ADMIN_USER) / $(env_get NOMOS_SIGNUP_ADMIN_PASSWORD)
                 council-create login:                    $(env_get NOMOS_SIGNUP_COUNCIL_ADMIN_USER) / $(env_get NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD)
  demo         : http://localhost:${DEMO_PORT}/
  demo agent   : ${AGENT_ID}  (token in deploy/.env: NOMOS_AUTH_TOKEN)

All dev credentials live in ${ENV_FILE} (gitignored). Re-run compose-up.sh anytime
(idempotent); --regenerate-secrets to rotate; compose-down.sh to tear down.
EOF
