#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
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
# Env:   COMPOSE_PROJECT (default: the compose dir name, 'deploy'), SKIP_BUILD, REGENERATE_SECRETS
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

# These bootstrappers deploy the SERVICE topology: they start postgres and vault,
# which are defined only in docker-compose.service.yml. dc() selects overlays from
# the environment (mirroring scripts/deploy_stack.sh) rather than hard-coding that
# file, so declare it here. Without this, `dc up -d postgres vault` cannot resolve
# either service and every later dc call runs without the service-mode overrides.
# Prepend rather than overwrite, so an explicit COMPOSE_EXTRA_FILES still applies.
SERVICE_OVERLAY="$ROOT_DIR/deploy/docker-compose.service.yml"
if [[ ":${COMPOSE_EXTRA_FILES:-}:" != *":$SERVICE_OVERLAY:"* ]]; then
  COMPOSE_EXTRA_FILES="${COMPOSE_EXTRA_FILES:+$COMPOSE_EXTRA_FILES:}$SERVICE_OVERLAY"
fi
export COMPOSE_EXTRA_FILES

SKIP_BUILD="${SKIP_BUILD:-0}"
AGENT_ID="${AGENT_ID:-dev-agent}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) NONINTERACTIVE=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --regenerate-secrets) REGENERATE_SECRETS=1 ;;
    --agent-id) AGENT_ID="${2:?}"; shift ;;
    --agent-id=*) AGENT_ID="${1#*=}" ;;
    # Anchor the help block by content (not fixed line numbers) so it survives
    # header/license changes shifting the file.
    -h|--help) sed -n '/^# One command to bring up/,/^# Env:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done
export NONINTERACTIVE="${NONINTERACTIVE:-0}" REGENERATE_SECRETS="${REGENERATE_SECRETS:-0}"

GATEWAY_PORT="$(env_get COUNCIL_GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-9900}"
MONITOR_PORT="$(env_get MONITOR_PORT)"; MONITOR_PORT="${MONITOR_PORT:-9901}"
SIGNUP_PORT="$(env_get AGENT_SIGNUP_PORT)"; SIGNUP_PORT="${SIGNUP_PORT:-9905}"
DEMO_PORT="$(env_get DEMO_PORT)"; DEMO_PORT="${DEMO_PORT:-8000}"

# Concise, actionable failure for a compose service (never a bare "did not become
# healthy"): what failed, its state, the last log line, and the command to dig in.
_compose_diag() {
  local svc="$1" cid="$2" state="$3" elapsed="$4" last
  printf '  %s\xe2\x9c\x97%s %s not healthy (state: %s) after %ss\n' "$C_R" "$C_0" "$svc" "${state:-unknown}" "$elapsed" >&2
  if [[ -n "$cid" ]]; then
    last="$(docker logs --tail=3 "$cid" 2>&1 | tail -n1 | cut -c1-160)"
    [[ -n "$last" ]] && warn "last log: $last"
    warn "→ docker logs --tail=50 $cid"
  else
    warn "no container for $svc yet — docker compose -p $COMPOSE_PROJECT ps"
  fi
}

# Wait until a compose service reports healthy (or running, if it has no healthcheck).
# On timeout/exit it prints _compose_diag then PROMPTS (confirm_wait) — it does not
# auto-fail; it only returns non-zero when the operator gives up.
wait_healthy() {
  local svc="$1" timeout="${2:-180}" cid state="none" i
  while :; do
    i=0
    while [[ "$i" -lt "$timeout" ]]; do
      cid="$(dc ps -q "$svc" 2>/dev/null | head -n1)"
      if [[ -n "$cid" ]]; then
        state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo starting)"
        [[ "$state" = healthy ]] && return 0
        [[ "$state" = running ]] && return 0
        [[ "$state" = exited ]] && break
      fi
      sleep 1; i=$((i + 1))
    done
    _compose_diag "$svc" "$(dc ps -q "$svc" 2>/dev/null | head -n1)" "$state" "$i"
    confirm_wait "$svc not healthy" 120 || return 1
    timeout=120
  done
}

# ── 1. preflight ────────────────────────────────────────────────────────
log "[1/9] Preflight"
require_cmd docker
docker info >/dev/null 2>&1 || die "docker daemon is not available"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is required"
ok "docker / docker compose present"

# ── 2. LLM key (required) ───────────────────────────────────────────────
log "[2/9] LLM API key (required — the council validates llm_backends at boot)"
if needs_nvidia_api_key; then
  prompt _k "NVIDIA_API_KEY" "" secret
  [[ -n "$_k" ]] && env_set NVIDIA_API_KEY "$_k"
  unset _k
fi
if needs_nvidia_api_key; then
  die "The council config requires NVIDIA_API_KEY. Set it in deploy/.env, or set" \
    "ALLOW_MISSING_NVIDIA_API_KEY=1 if you have configured a non-NVIDIA backend."
fi
[[ -n "$(env_get NVIDIA_API_KEY)$(env_get ANTHROPIC_API_KEY)$(env_get OPENAI_API_KEY)" ]] \
  || die "Set NVIDIA_API_KEY, or populate deploy/.env with a configured backend key."
ok "LLM key present (stored in $ENV_FILE)"

# ── 3. dev secrets (generate + persist) ─────────────────────────────────
log "[3/9] Dev secrets (auto-generated, persisted to deploy/.env)"
# Same set as kind-up.sh plus DEMO_AUTH_PASSWORD — compose runs the demo service
# (the Kind chart does not). compose substitutes these into the stack from deploy/.env.
for k in REDIS_PASSWORD COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY COUNCIL_MANAGEMENT_TOKEN \
         MONITOR_INTERNAL_TOKEN MONITOR_MCP_PROXY_TOKEN MONITOR_DASHBOARD_PASSWORD \
         NOMOS_SIGNUP_ADMIN_PASSWORD NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD \
         DEMO_AUTH_PASSWORD POSTGRES_PASSWORD VAULT_DEV_ROOT_TOKEN; do
  gen_or_keep "$k" >/dev/null
done
sync_postgres_url_password
default_or_keep MONITOR_DASHBOARD_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_ADMIN_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_COUNCIL_ADMIN_USER council-admin >/dev/null
ok "secrets ready ($([[ "$REGENERATE_SECRETS" = 1 ]] && echo regenerated || echo 'generated if missing'))"

# ── 4. council catalog ──────────────────────────────────────────────────
# The base compose mounts deploy/dynamic-config at /app/dynamic-config; seed the
# read-only council catalog the same way scripts/deploy_stack.sh does.
log "[4/9] Council catalog (deploy/dynamic-config)"
DCFG="$ROOT_DIR/deploy/dynamic-config"
mkdir -p "$DCFG/councils"
[[ -f "$DCFG/local_council_catalog.yaml" ]] || cp "$ROOT_DIR/configs/local_council_catalog.yaml" "$DCFG/local_council_catalog.yaml"
for c in council.yaml git_workflow_council.yaml; do
  [[ -f "$ROOT_DIR/configs/$c" && ! -f "$DCFG/$c" ]] && cp "$ROOT_DIR/configs/$c" "$DCFG/$c" || true
done
ok "catalog present"

# ── 5. backends ─────────────────────────────────────────────────────────
log "[5/9] Start backends (postgres + vault)"
dc up -d postgres vault
wait_healthy postgres 120 || die "postgres did not become healthy"
wait_healthy vault 120 || die "vault did not become healthy"
sync_postgres_role_password
ok "backends healthy"

# ── 6. vault bootstrap (transit + AppRole) ──────────────────────────────
# The dev Vault is in-memory, so every run (re)bootstraps it (idempotent) and mints
# a fresh AppRole secret-id. The resulting env change makes step 7's `up` recreate
# council/executor with the new credentials — expected on idempotent re-runs.
log "[6/9] Bootstrap Vault transit signing (AppRole)"
boot="$(dc exec -T -e VAULT_TOKEN="$(env_get VAULT_DEV_ROOT_TOKEN)" -e TRANSIT_MOUNT=transit \
  vault sh -s < "$SCRIPT_DIR/vault-bootstrap-compose.sh")"
role="$(printf '%s\n' "$boot" | sed -n 's/^VAULT_ROLE_ID=//p' | tail -n1)"
secret="$(printf '%s\n' "$boot" | sed -n 's/^VAULT_SECRET_ID=//p' | tail -n1)"
[[ -n "$role" && -n "$secret" ]] || die "vault bootstrap did not return AppRole credentials"
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
if [[ "$SKIP_BUILD" = "1" ]]; then
  dc up -d
else
  dc up -d --build
fi
ok "stack started"

# ── 8. waits ────────────────────────────────────────────────────────────
log "[8/9] Wait for council + workloads"
wait_healthy council 300 || die "gave up on council — see the diagnostic above"
# Non-fatal: wait_healthy already printed an actionable diagnostic for any that lag;
# verify re-checks them. (monitor now uses /livez, so it no longer waits on council.)
for s in council-gateway council-grpc monitor executor-openshell openshell opa agent-signup; do
  wait_healthy "$s" 180 || true
done
ok "workloads settled"

# ── 9. register agent + verify ──────────────────────────────────────────
log "[9/9] Register demo agent + verify"
# Let the wrapper's stderr through and fail hard. Discarding it turned every
# registration failure (unreachable database, unsigned store, "already exists")
# into one indistinguishable warning, after which verify ran against whatever
# stale NOMOS_AUTH_TOKEN deploy/.env still held and failed somewhere else.
# AGENT_FORCE keeps re-runs idempotent: `create` refuses an existing account
# without --force, and the token in deploy/.env is replaced right below.
AGENT_TOKEN="$(AGENT_ROLE="${AGENT_ROLE:-developer}" AGENT_FORCE=1 "$SCRIPT_DIR/compose-register-agent.sh" "$AGENT_ID" \
  | sed -n 's/^AGENT_TOKEN=//p' | tail -n1)" \
  || die "agent registration failed for '$AGENT_ID' — see the error above"
[[ -n "$AGENT_TOKEN" ]] || die "agent registration did not return a token for '$AGENT_ID'"
env_set NOMOS_AGENT_ID "$AGENT_ID"; env_set NOMOS_AUTH_TOKEN "$AGENT_TOKEN"
ok "registered agent '$AGENT_ID' (token saved to deploy/.env)"

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
