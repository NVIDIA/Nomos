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
# One command to bring up a fully functioning Nomos dev system on local Kind:
# prompts for the required LLM key, auto-generates + persists all dev secrets to
# the gitignored deploy/.env, creates the cluster, builds/loads images, installs
# ingress-nginx, deploys (service mode: PostgreSQL + Vault transit + redis sentinel
# + gateway + council + monitor + executor/openshell + agent-signup + OPA),
# registers a demo agent, and verifies the running cluster.
#
#   deploy/kubernetes/scripts/kind-up.sh            # interactive
#   deploy/kubernetes/scripts/kind-up.sh -y         # non-interactive (deploy/.env must hold the LLM key)
#
# Flags: -y/--yes (non-interactive)  --skip-build  --regenerate-secrets  --agent-id ID
# Env:   KIND_CLUSTER_NAME NAMESPACE HELM_RELEASE (default nomos), SKIP_BUILD, REGENERATE_SECRETS
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../scripts/dev-lib.sh
. "$SCRIPT_DIR/../../scripts/dev-lib.sh"

SKIP_BUILD="${SKIP_BUILD:-0}"
AGENT_ID="${AGENT_ID:-dev-agent}"
while [ $# -gt 0 ]; do
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

CHART_DIR="$ROOT_DIR/deploy/kubernetes/nomos"
VALUES_FILE="$ROOT_DIR/deploy/kubernetes/kind-values.yaml"
CLUSTER_CONFIG="$ROOT_DIR/deploy/kubernetes/kind-cluster.yaml"
INGRESS_MANIFEST="https://kind.sigs.k8s.io/examples/ingress/deploy-ingress-nginx.yaml"

# ── 1. preflight ────────────────────────────────────────────────────────
log "[1/9] Preflight"
require_cmd docker kind kubectl helm
docker info >/dev/null 2>&1 || die "docker daemon is not available"
inst="$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || true)"
nclusters="$(kind get clusters 2>/dev/null | grep -c . || true)"
if [ -n "$inst" ] && [ "$inst" -lt 512 ]; then
  warn "fs.inotify.max_user_instances=$inst is low (you have ${nclusters:-0} kind cluster(s))."
  warn "If pods hang with network timeouts (kube-proxy 'too many open files'), raise it:"
  warn "  sudo sysctl -w fs.inotify.max_user_instances=1280 fs.inotify.max_user_watches=1048576"
fi
ok "docker / kind / kubectl / helm present"

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
         POSTGRES_PASSWORD VAULT_DEV_ROOT_TOKEN; do
  gen_or_keep "$k" >/dev/null
done
default_or_keep MONITOR_DASHBOARD_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_ADMIN_USER admin >/dev/null
default_or_keep NOMOS_SIGNUP_COUNCIL_ADMIN_USER council-admin >/dev/null
ok "secrets ready ($([ "$REGENERATE_SECRETS" = 1 ] && echo regenerated || echo 'generated if missing'))"

OVERRIDE="$(mktemp)"; chmod 600 "$OVERRIDE"
trap 'rm -f "$OVERRIDE"' EXIT
emit() { printf '    %s: "%s"\n' "$1" "$(env_get "$2")"; }
{
  echo "secrets:"
  echo "  stringData:"
  emit redisPassword REDIS_PASSWORD
  emit councilRedisAccountIntegrityKey COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
  emit councilManagementToken COUNCIL_MANAGEMENT_TOKEN
  emit monitorInternalToken MONITOR_INTERNAL_TOKEN
  emit monitorMcpProxyToken MONITOR_MCP_PROXY_TOKEN
  emit monitorDashboardUser MONITOR_DASHBOARD_USER
  emit monitorDashboardPassword MONITOR_DASHBOARD_PASSWORD
  emit signupAdminUser NOMOS_SIGNUP_ADMIN_USER
  emit signupAdminPassword NOMOS_SIGNUP_ADMIN_PASSWORD
  emit signupCouncilAdminUser NOMOS_SIGNUP_COUNCIL_ADMIN_USER
  emit signupCouncilAdminPassword NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD
  emit postgresPassword POSTGRES_PASSWORD
  emit vaultDevRootToken VAULT_DEV_ROOT_TOKEN
  emit nvidiaApiKey NVIDIA_API_KEY
  emit anthropicApiKey ANTHROPIC_API_KEY
  emit openaiApiKey OPENAI_API_KEY
} > "$OVERRIDE"

# ── 4. cluster ──────────────────────────────────────────────────────────
log "[4/9] Kind cluster '$KIND_CLUSTER_NAME'"
if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  ok "cluster exists"
else
  kind create cluster --name "$KIND_CLUSTER_NAME" --config "$CLUSTER_CONFIG"
  ok "cluster created"
fi

# ── 5. images ───────────────────────────────────────────────────────────
log "[5/9] Build + load images"
if [ "$SKIP_BUILD" = "1" ]; then
  ok "SKIP_BUILD=1 — reusing loaded images"
else
  "$SCRIPT_DIR/build-kind-images.sh" "$KIND_CLUSTER_NAME"
fi

# ── 6. ingress-nginx ────────────────────────────────────────────────────
log "[6/9] ingress-nginx"
kc apply -f "$INGRESS_MANIFEST" >/dev/null
if ! kc -n ingress-nginx wait --for=condition=Ready pod \
       -l app.kubernetes.io/component=controller --timeout=120s >/dev/null 2>&1; then
  warn "controller not ready; re-triggering the admission-cert jobs and retrying"
  kc -n ingress-nginx delete job ingress-nginx-admission-create ingress-nginx-admission-patch \
     --ignore-not-found >/dev/null 2>&1 || true
  kc apply -f "$INGRESS_MANIFEST" >/dev/null
  kc -n ingress-nginx wait --for=condition=Ready pod \
     -l app.kubernetes.io/component=controller --timeout=180s >/dev/null 2>&1 \
     || die "ingress-nginx controller did not become ready"
fi
# Controller Ready != admission webhook serving. helm's Ingress apply (step 7) gets
# "connection refused" to validate.nginx.ingress.kubernetes.io until the admission
# Service has ready endpoints; wait for them so the deploy doesn't race the webhook.
for _ in $(seq 1 60); do
  [ -n "$(kc -n ingress-nginx get endpoints ingress-nginx-controller-admission \
            -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)" ] && break
  sleep 2
done
ok "ingress-nginx ready (controller + admission webhook)"

# ── 7. deploy ───────────────────────────────────────────────────────────
log "[7/9] Deploy (helm upgrade --install, service mode)"
# No --wait: the vault-bootstrap post-install hook must run while council/executor
# init-gates wait on it; --wait would block the council ahead of the hook → deadlock.
helmc upgrade --install "$HELM_RELEASE" "$CHART_DIR" \
  -n "$NAMESPACE" --create-namespace \
  -f "$VALUES_FILE" -f "$OVERRIDE" --timeout 10m
ok "release applied (vault bootstrapped via post-install hook)"

# ── 8. waits ────────────────────────────────────────────────────────────
# wait_rollout prints a concise, actionable reason (never a bare kubectl "timed out
# waiting for the condition") and then PROMPTS to wait longer or give up — it returns
# non-zero only if the operator gives up (or, with -y, after the timeout).
log "[8/9] Wait for backends + workloads"
wait_rollout statefulset nomos-postgres 180 || die "gave up on postgres — fix the cause above, then re-run"
wait_rollout deployment  nomos-vault     150 || die "gave up on vault — fix the cause above, then re-run"
notready=""
for d in council council-gateway council-grpc monitor executor-openshell openshell opa agent-signup; do
  wait_rollout deployment "nomos-${d}" 300 || notready="${notready} ${d}"
done
[ -n "$notready" ] && warn "not ready:${notready} (see diagnostics above) — verify will re-check"
ok "workloads settled"

# ── 9. register agent + verify ──────────────────────────────────────────
log "[9/9] Register demo agent + verify"
AGENT_TOKEN="$("$SCRIPT_DIR/kind-register-agent.sh" "$AGENT_ID" 2>/dev/null \
  | sed -n 's/^AGENT_TOKEN=//p' | tail -n1 || true)"
if [ -n "$AGENT_TOKEN" ]; then
  env_set NOMOS_AGENT_ID "$AGENT_ID"; env_set NOMOS_AUTH_TOKEN "$AGENT_TOKEN"
  ok "registered agent '$AGENT_ID' (token saved to deploy/.env)"
else
  warn "agent registration did not return a token"
fi

AGENT_ID="$AGENT_ID" AGENT_TOKEN="$AGENT_TOKEN" "$SCRIPT_DIR/kind-verify.sh" \
  || die "verification failed — see the checks above"

# ── summary ─────────────────────────────────────────────────────────────
cat <<EOF

${C_G}${C_B}Nomos dev stack is up and verified.${C_0}

Add to /etc/hosts:  127.0.0.1 mcp.nomos.local monitor.nomos.local signup.nomos.local

  council /mcp : http://mcp.nomos.local/mcp     Bearer: $(env_get MONITOR_MCP_PROXY_TOKEN)
  monitor      : http://monitor.nomos.local/    $(env_get MONITOR_DASHBOARD_USER) / $(env_get MONITOR_DASHBOARD_PASSWORD)
  agent-signup : https://signup.nomos.local/    $(env_get NOMOS_SIGNUP_ADMIN_USER) / $(env_get NOMOS_SIGNUP_ADMIN_PASSWORD)
                 council-create login:          $(env_get NOMOS_SIGNUP_COUNCIL_ADMIN_USER) / $(env_get NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD)
  demo agent   : ${AGENT_ID}  (token in deploy/.env: NOMOS_AUTH_TOKEN)

All dev credentials live in ${ENV_FILE} (gitignored). Re-run kind-up.sh anytime
(idempotent); --regenerate-secrets to rotate; kind-down.sh to tear down.
EOF
