#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

#
# One-command local deployment for Nomos.
#
# Typical clean-host usage:
#
#   NVIDIA_API_KEY=nvapi-... ./scripts/deploy_stack.sh
#
# Optional knobs:
#
#   DEPLOY_TARGET=kind              kind (default) or compose
#   COMPOSE_REDIS_TOPOLOGY=sentinel|standalone
#                                   Compose Redis topology; default sentinel
#   COMPOSE_EXTRA_FILES=path[:path] Additional docker compose override files
#   KIND_CLUSTER_NAME=nomos         Kind cluster used for local Kubernetes deploys
#   KIND_CREATE_CLUSTER=1           Create the Kind cluster when it does not exist
#   KIND_USE_EXISTING_SECRET_ONLY=1 Use a pre-created Kind Secret without writing deploy/.env
#   KIND_PORT_FORWARD=1             Start local Kind port-forwards after deploy
#   KIND_PORT_FORWARD_ADDRESS=127.0.0.1
#   REDIS_PASSWORD=...              Override generated Redis password
#   AGENT_ID=alice-agent            Account to create if absent
#   AGENT_ROLE=developer            readonly|developer|operator|admin|demo
#   AGENT_COUNCILS=council-prod     Comma-separated councils to grant
#   AGENT_DISPLAY_NAME="Alice"      Display name for created account
#   AGENT_FORCE_ROTATE=1            Rotate token if account already exists
#   WITH_OPA=0                      Skip the bundled OPA sidecar only when using external OPA
#   WITH_AGENT_SIGNUP=1             Start local signup UI on AGENT_SIGNUP_PORT, default 9905
#   WITH_DEMO=1                     Start demo UI on DEMO_PORT, default 8000
#   NOMOS_PUBLIC_HOST=10.0.0.10     IP printed in external service URLs
#   EXECUTOR_OPENSHELL_PORT=9907    Host port for default OpenShell executor
#   EXECUTOR_PROFILE=executor-openshell executor-openshell|executor-default|...
#   SKIP_ACCOUNT=1                  Do not create an agent account
#   SKIP_BUILD=1                    Reuse already-built images

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_TARGET="${DEPLOY_TARGET:-kind}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/deploy/docker-compose.yml}"
COMPOSE_REDIS_TOPOLOGY="${COMPOSE_REDIS_TOPOLOGY:-}"
COMPOSE_REDIS_SENTINEL_FILE="${COMPOSE_REDIS_SENTINEL_FILE:-$ROOT_DIR/deploy/docker-compose.redis-sentinel.yml}"
COMPOSE_EXTRA_FILES="${COMPOSE_EXTRA_FILES:-}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
DYNAMIC_CONFIG_DIR="${DYNAMIC_CONFIG_DIR:-$ROOT_DIR/deploy/dynamic-config}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"
KIND_CONTEXT="${KIND_CONTEXT:-kind-$KIND_CLUSTER_NAME}"
KIND_NAMESPACE="${KIND_NAMESPACE:-nomos}"
KIND_CREATE_CLUSTER="${KIND_CREATE_CLUSTER:-1}"
KIND_VALUES_FILE="${KIND_VALUES_FILE:-$ROOT_DIR/deploy/kubernetes/kind-values.yaml}"
KIND_CHART_DIR="${KIND_CHART_DIR:-$ROOT_DIR/deploy/kubernetes/nomos}"
KIND_BUILD_SCRIPT="${KIND_BUILD_SCRIPT:-$ROOT_DIR/deploy/kubernetes/scripts/build-kind-images.sh}"
KIND_SECRET_NAME="${KIND_SECRET_NAME:-nomos-secrets}"
HELM_RELEASE="${HELM_RELEASE:-nomos}"
KIND_ROLLOUT_RESTART="${KIND_ROLLOUT_RESTART:-1}"
# Components excluded from the post-deploy rollout restart. These run upstream
# images (not the locally-built nomos/* tags), so restarting them serves no
# purpose — and restarting vault actively breaks the deploy by discarding the
# in-memory bootstrap that enables its Kubernetes auth method.
KIND_NO_RESTART_COMPONENTS="${KIND_NO_RESTART_COMPONENTS:-vault,opa,redis-sentinel,council-gateway}"
KIND_USE_EXISTING_SECRET_ONLY="${KIND_USE_EXISTING_SECRET_ONLY:-0}"
KIND_PORT_FORWARD="${KIND_PORT_FORWARD:-1}"
KIND_PORT_FORWARD_ADDRESS="${KIND_PORT_FORWARD_ADDRESS:-127.0.0.1}"
KIND_PORT_FORWARD_DIR="${KIND_PORT_FORWARD_DIR:-${TMPDIR:-/tmp}/nomos-kind-$KIND_CLUSTER_NAME-port-forwards}"
KIND_PORT_FORWARD_TIMEOUT_SECONDS="${KIND_PORT_FORWARD_TIMEOUT_SECONDS:-20}"
KIND_PORT_FORWARD_MONITOR="${KIND_PORT_FORWARD_MONITOR:-1}"
KIND_PORT_FORWARD_GRPC="${KIND_PORT_FORWARD_GRPC:-1}"
KIND_PORT_FORWARD_AGENT_SIGNUP="${KIND_PORT_FORWARD_AGENT_SIGNUP:-1}"
KIND_PORT_FORWARD_EXECUTOR="${KIND_PORT_FORWARD_EXECUTOR:-1}"
EXECUTOR_PROFILE="${EXECUTOR_PROFILE:-executor-openshell}"
AGENT_ID="${AGENT_ID:-agent-developer}"
AGENT_ROLE="${AGENT_ROLE:-developer}"
AGENT_COUNCILS="${AGENT_COUNCILS:-council-prod}"
AGENT_DISPLAY_NAME="${AGENT_DISPLAY_NAME:-$AGENT_ID}"
AGENT_FORCE_ROTATE="${AGENT_FORCE_ROTATE:-0}"
SKIP_ACCOUNT="${SKIP_ACCOUNT:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"
WITH_OPA="${WITH_OPA:-1}"
WITH_AGENT_SIGNUP="${WITH_AGENT_SIGNUP:-1}"
WITH_DEMO="${WITH_DEMO:-0}"
# Forward the demo only when the demo was actually requested. The Kind chart has
# no demo template at all (it is Compose-only), so defaulting this on made every
# Kind deploy fail at the port-forward step — "Port-forward for demo exited
# before listening on 127.0.0.1:8000" — even with all deployments Ready.
KIND_PORT_FORWARD_DEMO="${KIND_PORT_FORWARD_DEMO:-$WITH_DEMO}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-240}"

COMPOSE_PROFILES=()
if [[ -n "$EXECUTOR_PROFILE" ]]; then
  COMPOSE_PROFILES+=(--profile "$EXECUTOR_PROFILE")
fi

log() {
  printf '[deploy] %s\n' "$*"
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_supported_bash() {
  if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3) )); then
    die "Bash 4.3 or newer is required; found ${BASH_VERSION}. Install a newer Bash and ensure it appears first on PATH."
  fi
}

print_usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy_stack.sh [--help]

Examples:
  ./scripts/deploy_stack.sh
  DEPLOY_TARGET=kind ./scripts/deploy_stack.sh
  DEPLOY_TARGET=compose ./scripts/deploy_stack.sh
  DEPLOY_TARGET=compose COMPOSE_REDIS_TOPOLOGY=standalone ./scripts/deploy_stack.sh

Common environment knobs:
  DEPLOY_TARGET=kind|compose          Deployment target; default is kind.
  COMPOSE_REDIS_TOPOLOGY=sentinel|standalone
                                      Compose Redis topology; default is sentinel.
  COMPOSE_EXTRA_FILES=path[:path]     Additional Compose override files.
  KIND_CLUSTER_NAME=nomos             Local Kind cluster name.
  KIND_CREATE_CLUSTER=1               Create Kind cluster when missing.
  KIND_USE_EXISTING_SECRET_ONLY=1     Use pre-created Kind Secret; do not write deploy/.env.
  KIND_PORT_FORWARD=1                 Start Kind port-forwards.
  EXECUTOR_PROFILE=executor-openshell Executor profile to deploy.
  WITH_AGENT_SIGNUP=1                 Start the signup UI.
  WITH_DEMO=1                         Start the demo UI.
  SKIP_ACCOUNT=1                      Skip agent account creation.
  SKIP_BUILD=1                        Reuse already-built images.

EOF
}

compose_file_args() {
  local -n args_ref=$1
  args_ref=(-f "$COMPOSE_FILE")
  case "$COMPOSE_REDIS_TOPOLOGY" in
    standalone)
      ;;
    sentinel|"")
      args_ref+=(-f "$COMPOSE_REDIS_SENTINEL_FILE")
      ;;
    *)
      die "Unknown COMPOSE_REDIS_TOPOLOGY '$COMPOSE_REDIS_TOPOLOGY'. Use standalone or sentinel."
      ;;
  esac
  if [[ -n "$COMPOSE_EXTRA_FILES" ]]; then
    local file
    local -a extra_files=()
    IFS=: read -r -a extra_files <<<"$COMPOSE_EXTRA_FILES"
    for file in "${extra_files[@]}"; do
      [[ -n "$file" ]] && args_ref+=(-f "$file")
    done
  fi
}

compose() {
  local -a compose_files=()
  compose_file_args compose_files
  docker compose "${compose_files[@]}" "${COMPOSE_PROFILES[@]}" "$@"
}

compose_all_profiles() {
  local -a compose_files=()
  compose_file_args compose_files
  docker compose "${compose_files[@]}" \
    --profile executor-openshell \
    --profile executor-default \
    --profile executor-dev \
    "$@"
}

is_true() {
  [[ "$1" == "1" || "$1" == "true" ]]
}

compose_project_name() {
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    printf '%s' "$COMPOSE_PROJECT_NAME"
    return
  fi
  basename "$(dirname "$COMPOSE_FILE")"
}

remove_inactive_services() {
  local services=()

  if [[ "$EXECUTOR_PROFILE" != "executor-openshell" ]]; then
    services+=(executor-openshell openshell)
  fi
  if [[ "$EXECUTOR_PROFILE" != "executor-default" ]]; then
    services+=(executor-default)
  fi
  if [[ "$EXECUTOR_PROFILE" != "executor-dev" ]]; then
    services+=(executor-dev)
  fi
  if ! is_true "$WITH_DEMO"; then
    services+=(demo)
  fi
  if ! is_true "$WITH_AGENT_SIGNUP"; then
    services+=(agent-signup)
  fi
  if ! is_true "$WITH_OPA"; then
    services+=(opa)
  fi

  if [[ "${#services[@]}" -gt 0 ]]; then
    log "Removing inactive/stale services: ${services[*]}"
    compose_all_profiles rm -sf "${services[@]}" >/dev/null 2>&1 || true
  fi
}

ensure_host_port_available() {
  local port="$1"
  local label="$2"
  local project
  project="$(compose_project_name)"
  local owner
  owner="$(docker ps --filter "publish=$port" --format '{{.Names}}' | head -n 1)"
  if [[ -z "$owner" ]]; then
    return
  fi
  if [[ "$owner" == "$project"-* ]]; then
    return
  fi
  die "$label host port $port is already used by container '$owner'. Stop it or override the port in deploy/.env."
}

preflight_ports() {
  ensure_host_port_available "$(current_or_default MONITOR_PORT 9901)" "Monitor/MCP"
  ensure_host_port_available "$(current_or_default COUNCIL_GRPC_PORT 9904)" "Council gRPC"
  ensure_host_port_available "$(current_or_default REDIS_PORT 6379)" "Redis"
  if is_true "$WITH_OPA"; then
    ensure_host_port_available "$(current_or_default OPA_PORT 8181)" "OPA"
  fi
  if is_true "$WITH_AGENT_SIGNUP"; then
    ensure_host_port_available "$(current_or_default AGENT_SIGNUP_PORT 9905)" "Agent signup"
  fi
  if is_true "$WITH_DEMO"; then
    ensure_host_port_available "$(current_or_default DEMO_PORT 8000)" "Demo"
  fi
  if [[ "$EXECUTOR_PROFILE" == "executor-openshell" ]]; then
    ensure_host_port_available "$(current_or_default EXECUTOR_OPENSHELL_PORT 9907)" "OpenShell executor"
  elif [[ "$EXECUTOR_PROFILE" == "executor-default" ]]; then
    ensure_host_port_available "$(current_or_default EXECUTOR_DEFAULT_PORT 9902)" "Legacy executor"
  fi
}

env_file_value() {
  local key="$1"
  if [[ -f "$ENV_FILE" ]]; then
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE"
  fi
}

current_value() {
  local key="$1"
  local value="${!key:-}"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return
  fi
  env_file_value "$key"
}

current_or_default() {
  local key="$1"
  local default="$2"
  local value
  value="$(current_value "$key")"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$default"
  fi
}

resolve_compose_redis_topology() {
  COMPOSE_REDIS_TOPOLOGY="$(current_or_default COMPOSE_REDIS_TOPOLOGY sentinel)"
}

yaml_double_quote() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  printf '"%s"' "$value"
}

write_kind_secret_value() {
  local yaml_key="$1"
  local env_key="$2"
  local default="${3:-}"
  local value
  value="$(current_value "$env_key")"
  if [[ -z "$value" && -n "$default" ]]; then
    value="$default"
  fi
  if [[ -z "$value" ]]; then
    return
  fi
  printf '    %s: %s\n' "$yaml_key" "$(yaml_double_quote "$value")"
}

write_kind_secret_values() {
  local output_file="$1"
  umask 077
  {
    printf 'secrets:\n'
    printf '  create: true\n'
    printf '  existingSecret: ""\n'
    printf '  stringData:\n'
    write_kind_secret_value redisPassword REDIS_PASSWORD nomos-dev
    write_kind_secret_value nvidiaApiKey NVIDIA_API_KEY
    write_kind_secret_value anthropicApiKey ANTHROPIC_API_KEY
    write_kind_secret_value openaiApiKey OPENAI_API_KEY
    write_kind_secret_value councilRedisAccountIntegrityKey COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
    write_kind_secret_value councilManagementToken COUNCIL_MANAGEMENT_TOKEN
    write_kind_secret_value monitorInternalToken MONITOR_INTERNAL_TOKEN
    write_kind_secret_value monitorDashboardUser MONITOR_DASHBOARD_USER admin
    write_kind_secret_value monitorDashboardPassword MONITOR_DASHBOARD_PASSWORD
    write_kind_secret_value signupAdminUser NOMOS_SIGNUP_ADMIN_USER admin
    write_kind_secret_value signupAdminPassword NOMOS_SIGNUP_ADMIN_PASSWORD
    write_kind_secret_value demoAuthUser DEMO_AUTH_USER demo
    write_kind_secret_value demoAuthPassword DEMO_AUTH_PASSWORD
    write_kind_secret_value demoAuthToken NOMOS_AUTH_TOKEN
    write_kind_secret_value demoAgentId NOMOS_AGENT_ID
    write_kind_secret_value demoK8sAdminToken NOMOS_AUTH_TOKEN_K8S_ADMIN
    write_kind_secret_value demoK8sDevToken NOMOS_AUTH_TOKEN_K8S_DEV
    write_kind_secret_value demoK8sReadonlyToken NOMOS_AUTH_TOKEN_K8S_READONLY
  } >"$output_file"
  chmod 600 "$output_file"
}

write_kind_secret_patch_value() {
  local secret_key="$1"
  local env_key="$2"
  local default="${3:-}"
  local value
  value="$(current_value "$env_key")"
  if [[ -z "$value" && -n "$default" ]]; then
    value="$default"
  fi
  if [[ -z "$value" ]]; then
    return
  fi

  local encoded
  encoded="$(printf '%s' "$value" | base64 | tr -d '\n')"
  if [[ "$KIND_SECRET_PATCH_FIRST" == "1" ]]; then
    KIND_SECRET_PATCH_FIRST=0
  else
    printf ','
  fi
  printf '"%s":"%s"' "$secret_key" "$encoded"
}

write_kind_secret_patch() {
  local output_file="$1"
  umask 077
  local KIND_SECRET_PATCH_FIRST=1
  {
    printf '{"data":{'
    write_kind_secret_patch_value REDIS_PASSWORD REDIS_PASSWORD nomos-dev
    write_kind_secret_patch_value NVIDIA_API_KEY NVIDIA_API_KEY
    write_kind_secret_patch_value ANTHROPIC_API_KEY ANTHROPIC_API_KEY
    write_kind_secret_patch_value OPENAI_API_KEY OPENAI_API_KEY
    write_kind_secret_patch_value COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
    write_kind_secret_patch_value COUNCIL_MANAGEMENT_TOKEN COUNCIL_MANAGEMENT_TOKEN
    write_kind_secret_patch_value MONITOR_INTERNAL_TOKEN MONITOR_INTERNAL_TOKEN
    write_kind_secret_patch_value MONITOR_DASHBOARD_USER MONITOR_DASHBOARD_USER admin
    write_kind_secret_patch_value MONITOR_DASHBOARD_PASSWORD MONITOR_DASHBOARD_PASSWORD
    write_kind_secret_patch_value NOMOS_SIGNUP_ADMIN_USER NOMOS_SIGNUP_ADMIN_USER admin
    write_kind_secret_patch_value NOMOS_SIGNUP_ADMIN_PASSWORD NOMOS_SIGNUP_ADMIN_PASSWORD
    write_kind_secret_patch_value DEMO_AUTH_USER DEMO_AUTH_USER demo
    write_kind_secret_patch_value DEMO_AUTH_PASSWORD DEMO_AUTH_PASSWORD
    write_kind_secret_patch_value NOMOS_AUTH_TOKEN NOMOS_AUTH_TOKEN
    write_kind_secret_patch_value NOMOS_AGENT_ID NOMOS_AGENT_ID
    write_kind_secret_patch_value NOMOS_AUTH_TOKEN_K8S_ADMIN NOMOS_AUTH_TOKEN_K8S_ADMIN
    write_kind_secret_patch_value NOMOS_AUTH_TOKEN_K8S_DEV NOMOS_AUTH_TOKEN_K8S_DEV
    write_kind_secret_patch_value NOMOS_AUTH_TOKEN_K8S_READONLY NOMOS_AUTH_TOKEN_K8S_READONLY
    printf '}}\n'
  } >"$output_file"
  chmod 600 "$output_file"
}

sync_kind_existing_secret_from_env() {
  kind_secret_exists || return 0

  local patch_file
  patch_file="$(mktemp)"
  write_kind_secret_patch "$patch_file"
  if ! kubectl_kind -n "$KIND_NAMESPACE" patch secret "$KIND_SECRET_NAME" \
    --type merge \
    --patch-file "$patch_file" >/dev/null; then
    rm -f "$patch_file"
    return 1
  fi
  rm -f "$patch_file"
}

random_secret() {
  if have_cmd openssl; then
    openssl rand -hex 24
    return
  fi
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

detect_public_host() {
  if [[ -n "${NOMOS_PUBLIC_HOST:-}" ]]; then
    printf '%s' "$NOMOS_PUBLIC_HOST"
    return
  fi
  if [[ -n "${PUBLIC_HOST:-}" ]]; then
    printf '%s' "$PUBLIC_HOST"
    return
  fi
  if have_cmd hostname; then
    local ip
    for ip in $(hostname -I 2>/dev/null || true); do
      case "$ip" in
        127.*|::1|*:*) continue ;;
        *)
          printf '%s' "$ip"
          return
          ;;
      esac
    done

    local fqdn
    fqdn="$(hostname -f 2>/dev/null || true)"
    if [[ -n "$fqdn" && "$fqdn" != "localhost" ]]; then
      printf '%s' "$fqdn"
      return
    fi
    hostname
    return
  fi
  printf 'localhost'
}

is_ipv4_literal() {
  local host="$1"
  [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

validate_env_value() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    die "$key must not contain carriage returns or newlines."
  fi
}

set_env_var() {
  local key="$1"
  local value="$2"
  validate_env_value "$key" "$value"
  mkdir -p "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  local tmp
  tmp="$(mktemp)"
  if grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s\n' "$value" |
      awk -v key="$key" 'NR == FNR { value = $0; next } $1 ~ "^" key "=" { print key "=" value; next } { print }' - "$ENV_FILE" >"$tmp"
  else
    cat "$ENV_FILE" >"$tmp"
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

ensure_prereqs() {
  have_cmd docker || die "Docker is not installed or not on PATH."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
  local -a compose_files=()
  local i file
  compose_file_args compose_files
  for ((i = 0; i < ${#compose_files[@]}; i += 2)); do
    file="${compose_files[$((i + 1))]}"
    [[ -f "$file" ]] || die "Compose file not found: $file"
  done
}

kubectl_kind() {
  kubectl --context "$KIND_CONTEXT" "$@"
}

helm_kind() {
  helm --kube-context "$KIND_CONTEXT" "$@"
}

# ── concise, actionable rollout waits (never a bare "timed out waiting for the
# condition") ── print WHY a deployment is not Ready (pod container waiting-reason or
# latest Warning event) and the exact logs command.
_diagnose_kind_rollout() {
  local name="$1" elapsed="$2" comp pod ctr ready desired wreason rc evt
  comp="${name#"${HELM_RELEASE}-"}"
  ready="$(kubectl_kind -n "$KIND_NAMESPACE" get deploy "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"; ready="${ready:-0}"
  desired="$(kubectl_kind -n "$KIND_NAMESPACE" get deploy "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)"; desired="${desired:-1}"
  printf '[deploy] %s not ready (%s/%s) after %ss\n' "$name" "$ready" "$desired" "$elapsed" >&2
  pod="$(kubectl_kind -n "$KIND_NAMESPACE" get pod -l "app.kubernetes.io/component=$comp" --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)"
  [ -n "$pod" ] || { printf '[deploy]   no %s pod scheduled — kubectl --context %s -n %s get pods\n' "$comp" "$KIND_CONTEXT" "$KIND_NAMESPACE" >&2; return 0; }
  ctr="$(kubectl_kind -n "$KIND_NAMESPACE" get pod "$pod" -o jsonpath='{.spec.containers[0].name}' 2>/dev/null)"
  wreason="$(kubectl_kind -n "$KIND_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
  rc="$(kubectl_kind -n "$KIND_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)"
  evt="$(kubectl_kind -n "$KIND_NAMESPACE" get events --field-selector "involvedObject.name=$pod" -o jsonpath='{range .items[*]}{.type}={.message}|{end}' 2>/dev/null | tr '|' '\n' | sed -n 's/^Warning=//p' | tail -n1)"
  printf '[deploy]   %s: %s%s%s\n' "$pod" "${wreason:-running, not Ready}" "${rc:+ (restarts: $rc)}" "${evt:+ — $evt}" >&2
  printf '[deploy]   -> kubectl --context %s -n %s logs %s -c %s --tail=50\n' "$KIND_CONTEXT" "$KIND_NAMESPACE" "$pod" "$ctr" >&2
}

# Wait for each Kind deployment to become Ready. Fast-surface terminal pod states,
# print a concise diagnostic on timeout, then — when attached to a terminal — PROMPT
# to wait longer or give up (no auto-fail). Non-interactive gives up after the
# timeout. Returns non-zero if any deployment was given up on.
wait_kind_rollouts() {
  local names name comp ready desired wreason timeout i ans gaveup=0
  names="$(kubectl_kind -n "$KIND_NAMESPACE" get deploy \
    -l "app.kubernetes.io/instance=$HELM_RELEASE" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
  for name in $names; do
    comp="${name#"${HELM_RELEASE}-"}"
    timeout="$WAIT_TIMEOUT_SECONDS"
    while :; do
      i=0
      while [ "$i" -lt "$timeout" ]; do
        ready="$(kubectl_kind -n "$KIND_NAMESPACE" get deploy "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"; ready="${ready:-0}"
        desired="$(kubectl_kind -n "$KIND_NAMESPACE" get deploy "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)"; desired="${desired:-1}"
        [ "$ready" -ge "$desired" ] 2>/dev/null && break
        wreason="$(kubectl_kind -n "$KIND_NAMESPACE" get pod -l "app.kubernetes.io/component=$comp" --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
        case "$wreason" in
          CrashLoopBackOff|ImagePullBackOff|ErrImagePull|InvalidImageName|CreateContainerConfigError|CreateContainerError|RunContainerError) break ;;
        esac
        sleep 3; i=$((i + 3))
      done
      [ "$ready" -ge "$desired" ] 2>/dev/null && { log "ready: $name ($ready/$desired)"; break; }
      _diagnose_kind_rollout "$name" "$i"
      if [ -t 0 ]; then
        printf '[deploy]   %s not ready — [w]ait another 120s or [g]ive up? [w] ' "$name" >&2
        read -r ans || { gaveup=1; break; }
        case "$ans" in g|G|give*|n|N|no|q|Q) gaveup=1; break ;; esac
        timeout=120
      else
        gaveup=1; break
      fi
    done
  done
  [ "$gaveup" = 0 ]
}

ensure_kind_prereqs() {
  have_cmd docker || die "Docker is not installed or not on PATH."
  have_cmd kind || die "Kind is not installed or not on PATH."
  have_cmd kubectl || die "kubectl is not installed or not on PATH."
  have_cmd helm || die "Helm is not installed or not on PATH."
  if is_true "$KIND_PORT_FORWARD"; then
    have_cmd timeout || die "GNU timeout is required when Kind port-forwarding is enabled. Install GNU Coreutils or set KIND_PORT_FORWARD=0."
  fi
  [[ -x "$KIND_BUILD_SCRIPT" ]] || die "Kind image build script is not executable: $KIND_BUILD_SCRIPT"
  [[ -f "$KIND_VALUES_FILE" ]] || die "Kind values file not found: $KIND_VALUES_FILE"
  [[ -f "$KIND_CHART_DIR/Chart.yaml" ]] || die "Helm chart not found: $KIND_CHART_DIR"
}

kind_cluster_exists() {
  kind get clusters | grep -Fxq "$KIND_CLUSTER_NAME"
}

ensure_kind_cluster() {
  if kind_cluster_exists; then
    log "Using Kind cluster: $KIND_CLUSTER_NAME"
  elif is_true "$KIND_CREATE_CLUSTER"; then
    log "Creating Kind cluster: $KIND_CLUSTER_NAME"
    kind create cluster --name "$KIND_CLUSTER_NAME"
  else
    die "Kind cluster '$KIND_CLUSTER_NAME' does not exist. Create it or set KIND_CREATE_CLUSTER=1."
  fi
  kubectl_kind cluster-info >/dev/null
}

kind_secret_exists() {
  kubectl_kind -n "$KIND_NAMESPACE" get secret "$KIND_SECRET_NAME" >/dev/null 2>&1
}

kind_secret_is_helm_owned() {
  local metadata
  metadata="$(
    kubectl_kind -n "$KIND_NAMESPACE" get secret "$KIND_SECRET_NAME" \
      -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}{"\n"}{.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}{.metadata.annotations.meta\.helm\.sh/release-namespace}{"\n"}' \
      2>/dev/null || true
  )"
  local managed_by release_name release_namespace
  managed_by="$(printf '%s\n' "$metadata" | sed -n '1p')"
  release_name="$(printf '%s\n' "$metadata" | sed -n '2p')"
  release_namespace="$(printf '%s\n' "$metadata" | sed -n '3p')"
  [[ "$managed_by" == "Helm" && "$release_name" == "$HELM_RELEASE" && "$release_namespace" == "$KIND_NAMESPACE" ]]
}

kind_secret_data_present() {
  local key="$1"
  local value
  value="$(kubectl_kind -n "$KIND_NAMESPACE" get secret "$KIND_SECRET_NAME" -o "jsonpath={.data.$key}" 2>/dev/null || true)"
  [[ -n "$value" ]]
}

patch_kind_secret_literal() {
  local key="$1"
  local value="$2"
  local patch_file
  patch_file="$(mktemp)"
  printf '{"data":{"%s":"%s"}}\n' \
    "$key" \
    "$(printf '%s' "$value" | base64 | tr -d '\n')" >"$patch_file"
  if ! kubectl_kind -n "$KIND_NAMESPACE" patch secret "$KIND_SECRET_NAME" \
    --type merge \
    --patch-file "$patch_file" >/dev/null; then
    rm -f "$patch_file"
    return 1
  fi
  rm -f "$patch_file"
}

ensure_kind_demo_auth_secret() {
  if [[ -n "${DEMO_AUTH_USER:-}" ]]; then
    patch_kind_secret_literal DEMO_AUTH_USER "$DEMO_AUTH_USER"
  elif ! kind_secret_data_present DEMO_AUTH_USER; then
    patch_kind_secret_literal DEMO_AUTH_USER demo
  fi

  if [[ -n "${DEMO_AUTH_PASSWORD:-}" ]]; then
    patch_kind_secret_literal DEMO_AUTH_PASSWORD "$DEMO_AUTH_PASSWORD"
    log "Updated DEMO_AUTH_PASSWORD in Kubernetes Secret $KIND_NAMESPACE/$KIND_SECRET_NAME"
  elif ! kind_secret_data_present DEMO_AUTH_PASSWORD; then
    patch_kind_secret_literal DEMO_AUTH_PASSWORD "$(random_secret)"
    log "Generated DEMO_AUTH_PASSWORD in Kubernetes Secret $KIND_NAMESPACE/$KIND_SECRET_NAME"
  fi
}

ensure_kind_existing_secret_only() {
  kind_secret_exists || die "KIND_USE_EXISTING_SECRET_ONLY=1 requires existing Kubernetes Secret: $KIND_NAMESPACE/$KIND_SECRET_NAME"

  local -a required_keys=(
    REDIS_PASSWORD
    NVIDIA_API_KEY
    COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
    COUNCIL_MANAGEMENT_TOKEN
    MONITOR_INTERNAL_TOKEN
    MONITOR_DASHBOARD_USER
    MONITOR_DASHBOARD_PASSWORD
    NOMOS_SIGNUP_ADMIN_USER
    NOMOS_SIGNUP_ADMIN_PASSWORD
  )
  local -a missing_keys=()
  local key
  for key in "${required_keys[@]}"; do
    if ! kind_secret_data_present "$key"; then
      missing_keys+=("$key")
    fi
  done

  if [[ "${#missing_keys[@]}" -gt 0 ]]; then
    die "KIND_USE_EXISTING_SECRET_ONLY=1 requires non-empty Secret keys in $KIND_NAMESPACE/$KIND_SECRET_NAME: ${missing_keys[*]}"
  fi

  ensure_kind_demo_auth_secret
}

write_kind_secret_value_from_secret() {
  local yaml_key="$1"
  local secret_key="$2"
  local encoded value
  encoded="$(kubectl_kind -n "$KIND_NAMESPACE" get secret "$KIND_SECRET_NAME" -o "jsonpath={.data.$secret_key}" 2>/dev/null || true)"
  [[ -n "$encoded" ]] || return 0
  value="$(printf '%s' "$encoded" | base64 -d)"
  [[ -n "$value" ]] || return 0
  printf '    %s: %s\n' "$yaml_key" "$(yaml_double_quote "$value")"
}

write_kind_secret_values_from_existing_secret() {
  local output_file="$1"
  umask 077
  {
    printf 'secrets:\n'
    printf '  create: true\n'
    printf '  existingSecret: ""\n'
    printf '  stringData:\n'
    write_kind_secret_value_from_secret redisPassword REDIS_PASSWORD
    write_kind_secret_value_from_secret nvidiaApiKey NVIDIA_API_KEY
    write_kind_secret_value_from_secret anthropicApiKey ANTHROPIC_API_KEY
    write_kind_secret_value_from_secret openaiApiKey OPENAI_API_KEY
    write_kind_secret_value_from_secret councilRedisAccountIntegrityKey COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
    write_kind_secret_value_from_secret councilManagementToken COUNCIL_MANAGEMENT_TOKEN
    write_kind_secret_value_from_secret monitorInternalToken MONITOR_INTERNAL_TOKEN
    write_kind_secret_value_from_secret monitorDashboardUser MONITOR_DASHBOARD_USER
    write_kind_secret_value_from_secret monitorDashboardPassword MONITOR_DASHBOARD_PASSWORD
    write_kind_secret_value_from_secret signupAdminUser NOMOS_SIGNUP_ADMIN_USER
    write_kind_secret_value_from_secret signupAdminPassword NOMOS_SIGNUP_ADMIN_PASSWORD
    write_kind_secret_value_from_secret demoAuthUser DEMO_AUTH_USER
    write_kind_secret_value_from_secret demoAuthPassword DEMO_AUTH_PASSWORD
    write_kind_secret_value_from_secret demoAuthToken NOMOS_AUTH_TOKEN
    write_kind_secret_value_from_secret demoAgentId NOMOS_AGENT_ID
    write_kind_secret_value_from_secret demoK8sAdminToken NOMOS_AUTH_TOKEN_K8S_ADMIN
    write_kind_secret_value_from_secret demoK8sDevToken NOMOS_AUTH_TOKEN_K8S_DEV
    write_kind_secret_value_from_secret demoK8sReadonlyToken NOMOS_AUTH_TOKEN_K8S_READONLY
  } >"$output_file"
  chmod 600 "$output_file"
}

kind_localhost_url() {
  local scheme="$1"
  local port="$2"
  local path="${3:-/}"
  printf '%s://127.0.0.1:%s%s' "$scheme" "$port" "$path"
}

kind_port_forward_specs() {
  local monitor_port grpc_port agent_signup_port demo_port executor_port
  monitor_port="${KIND_FORWARD_MONITOR_PORT:-$(current_or_default MONITOR_PORT 9901)}"
  grpc_port="${KIND_FORWARD_GRPC_PORT:-$(current_or_default COUNCIL_GRPC_PORT 9904)}"
  agent_signup_port="${KIND_FORWARD_AGENT_SIGNUP_PORT:-$(current_or_default AGENT_SIGNUP_PORT 9905)}"
  demo_port="${KIND_FORWARD_DEMO_PORT:-$(current_or_default DEMO_PORT 8000)}"
  executor_port="${KIND_FORWARD_EXECUTOR_PORT:-$(current_or_default EXECUTOR_OPENSHELL_PORT 9907)}"

  if is_true "$KIND_PORT_FORWARD_MONITOR"; then
    printf 'monitor\t%s-monitor\t%s\t9901\t%s\t%s\n' \
      "$HELM_RELEASE" "$monitor_port" \
      "$(kind_localhost_url http "$monitor_port" /)" \
      "$(kind_localhost_url http "$monitor_port" /healthz)"
  fi
  if is_true "$KIND_PORT_FORWARD_GRPC"; then
    printf 'council-grpc\t%s-council-grpc\t%s\t9904\tgrpc://127.0.0.1:%s\ttcp://127.0.0.1:%s\n' \
      "$HELM_RELEASE" "$grpc_port" "$grpc_port" "$grpc_port"
  fi
  if is_true "$KIND_PORT_FORWARD_AGENT_SIGNUP"; then
    printf 'agent-signup\t%s-agent-signup\t%s\t9905\t%s\t%s\n' \
      "$HELM_RELEASE" "$agent_signup_port" \
      "$(kind_localhost_url https "$agent_signup_port" /)" \
      "$(kind_localhost_url https "$agent_signup_port" /healthz)"
  fi
  if is_true "$KIND_PORT_FORWARD_DEMO"; then
    printf 'demo\t%s-demo\t%s\t8000\t%s\t%s\n' \
      "$HELM_RELEASE" "$demo_port" \
      "$(kind_localhost_url https "$demo_port" /)" \
      "$(kind_localhost_url https "$demo_port" /api/health)"
  fi
  if is_true "$KIND_PORT_FORWARD_EXECUTOR"; then
    printf 'executor\t%s-executor-openshell\t%s\t9907\t%s\t%s\n' \
      "$HELM_RELEASE" "$executor_port" \
      "$(kind_localhost_url http "$executor_port" /)" \
      "$(kind_localhost_url http "$executor_port" /v1/health)"
  fi
}

kind_port_is_open() {
  local port="$1"
  timeout 1 bash -c "</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1
}

stop_kind_port_forward() {
  local name="$1"
  local pid_file="$KIND_PORT_FORWARD_DIR/$name.pid"
  [[ -f "$pid_file" ]] || return 0

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if [[ "$(ps -p "$pid" -o comm= 2>/dev/null | tr -d '[:space:]')" != "kubectl" ]]; then
    return 0
  fi

  kill "$pid" >/dev/null 2>&1 || return 0
  local _attempt
  for _attempt in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
  kill -9 "$pid" >/dev/null 2>&1 || true
}

start_kind_port_forward() {
  local name="$1"
  local service="$2"
  local local_port="$3"
  local remote_port="$4"
  local url="$5"
  mkdir -p "$KIND_PORT_FORWARD_DIR"
  chmod 700 "$KIND_PORT_FORWARD_DIR"

  local pid_file="$KIND_PORT_FORWARD_DIR/$name.pid"
  local log_file="$KIND_PORT_FORWARD_DIR/$name.log"

  stop_kind_port_forward "$name"
  if kind_port_is_open "$local_port"; then
    log "Port-forward for $name already responds on 127.0.0.1:$local_port"
    return
  fi

  log "Forwarding $name: $url -> svc/$service:$remote_port"
  if have_cmd setsid; then
    setsid kubectl --context "$KIND_CONTEXT" -n "$KIND_NAMESPACE" port-forward \
      --address "$KIND_PORT_FORWARD_ADDRESS" \
      "svc/$service" "$local_port:$remote_port" \
      >"$log_file" 2>&1 &
  else
    nohup kubectl --context "$KIND_CONTEXT" -n "$KIND_NAMESPACE" port-forward \
      --address "$KIND_PORT_FORWARD_ADDRESS" \
      "svc/$service" "$local_port:$remote_port" \
      >"$log_file" 2>&1 &
  fi
  local pid="$!"
  printf '%s\n' "$pid" >"$pid_file"

  local start
  start="$(date +%s)"
  while true; do
    if kind_port_is_open "$local_port"; then
      return
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      tail -n 40 "$log_file" >&2 || true
      die "Port-forward for $name exited before listening on 127.0.0.1:$local_port"
    fi
    if (( "$(date +%s)" - start > KIND_PORT_FORWARD_TIMEOUT_SECONDS )); then
      tail -n 40 "$log_file" >&2 || true
      die "Timed out waiting for $name port-forward on 127.0.0.1:$local_port"
    fi
    sleep 0.5
  done
}

verify_kind_port_forwards() {
  if ! have_cmd curl; then
    log "curl is not installed; skipping local port-forward HTTP verification"
    return
  fi

  local name service local_port remote_port url health_url
  while IFS=$'\t' read -r name service local_port remote_port url health_url; do
    [[ -n "$name" ]] || continue
    case "$health_url" in
      tcp://*)
        if ! kind_port_is_open "$local_port"; then
          tail -n 40 "$KIND_PORT_FORWARD_DIR/$name.log" >&2 || true
          die "Port-forward TCP check failed for $name at $health_url"
        fi
        ;;
      *)
        local curl_args=(-fsS --max-time 10 -o /dev/null)
        if [[ "$health_url" == https://* ]]; then
          curl_args=(-kfsS --max-time 10 -o /dev/null)
        fi
        if ! curl "${curl_args[@]}" "$health_url"; then
          tail -n 40 "$KIND_PORT_FORWARD_DIR/$name.log" >&2 || true
          die "Port-forward health check failed for $name at $health_url"
        fi
        ;;
    esac
  done < <(kind_port_forward_specs)
}

start_kind_port_forwards() {
  if ! is_true "$KIND_PORT_FORWARD"; then
    log "Skipping Kind port-forwards because KIND_PORT_FORWARD=$KIND_PORT_FORWARD"
    return
  fi

  local name service local_port remote_port url health_url
  while IFS=$'\t' read -r name service local_port remote_port url health_url; do
    [[ -n "$name" ]] || continue
    start_kind_port_forward "$name" "$service" "$local_port" "$remote_port" "$url"
  done < <(kind_port_forward_specs)
  verify_kind_port_forwards
}

print_kind_urls() {
  local name service local_port remote_port url health_url
  while IFS=$'\t' read -r name service local_port remote_port url health_url; do
    [[ -n "$name" ]] || continue
    case "$name" in
      monitor) printf 'Monitor UI:      %s\n' "$url" ;;
      council-grpc) printf 'Council gRPC:    %s\n' "$url" ;;
      agent-signup) printf 'Agent signup:    %s\n' "$url" ;;
      demo) printf 'Demo UI:         %s\n' "$url" ;;
      executor) printf 'Executor:        %s\n' "$url" ;;
      *) printf '%s: %s\n' "$name" "$url" ;;
    esac
  done < <(kind_port_forward_specs)
}

ensure_env() {
  if [[ -f "$ENV_FILE" ]]; then
    chmod 600 "$ENV_FILE"
  fi

  local key value
  for key in REDIS_PASSWORD COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY COUNCIL_MANAGEMENT_TOKEN; do
    value="${!key:-}"
    if [[ -n "$value" ]]; then
      validate_env_value "$key" "$value"
    fi
  done

  if [[ ! -f "$ENV_FILE" ]]; then
    log "Creating $ENV_FILE"
    cat >"$ENV_FILE" <<'EOF'
# Generated by scripts/deploy_stack.sh
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
NVIDIA_API_KEY=
REDIS_PASSWORD=
COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY=
COUNCIL_MANAGEMENT_TOKEN=
MONITOR_INTERNAL_TOKEN=
MONITOR_DASHBOARD_USER=admin
MONITOR_DASHBOARD_PASSWORD=
NOMOS_SIGNUP_ADMIN_USER=admin
NOMOS_SIGNUP_ADMIN_PASSWORD=
DEMO_AUTH_USER=demo
DEMO_AUTH_PASSWORD=
NOMOS_PUBLIC_HOST=
COUNCIL_OPA_ENABLED=true
COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE=
NOMOS_DEPLOY_PROFILE=dev
NOMOS_ALPHA_ALLOW_PUBLIC_BIND=
EOF
    chmod 600 "$ENV_FILE"
  fi

  for key in REDIS_PASSWORD COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY COUNCIL_MANAGEMENT_TOKEN; do
    value="${!key:-}"
    if [[ -n "$value" ]]; then
      set_env_var "$key" "$value"
    fi
  done

  local redis_password
  redis_password="$(current_value REDIS_PASSWORD)"
  local account_integrity_key
  account_integrity_key="$(current_value COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY)"
  if [[ -z "$redis_password" ]]; then
    redis_password="$(random_secret)"
    set_env_var REDIS_PASSWORD "$redis_password"
    log "Generated REDIS_PASSWORD in deploy/.env"
  fi

  local council_management_token
  council_management_token="$(current_value COUNCIL_MANAGEMENT_TOKEN)"
  if [[ -z "$council_management_token" ]]; then
    council_management_token="$(random_secret)"
    set_env_var COUNCIL_MANAGEMENT_TOKEN "$council_management_token"
    log "Generated COUNCIL_MANAGEMENT_TOKEN in deploy/.env"
  fi

  local account_integrity_key
  account_integrity_key="$(current_value COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY)"
  if [[ -z "$account_integrity_key" ]]; then
    account_integrity_key="$(random_secret)"
    set_env_var COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY "$account_integrity_key"
    log "Generated COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY in deploy/.env"
  fi

  if [[ -n "${NOMOS_DEPLOY_PROFILE:-}" ]]; then
    set_env_var NOMOS_DEPLOY_PROFILE "$NOMOS_DEPLOY_PROFILE"
  fi

  local deploy_profile
  deploy_profile="$(current_or_default NOMOS_DEPLOY_PROFILE dev)"
  case "$deploy_profile" in
    production|prod)
      deploy_profile=alpha
      set_env_var NOMOS_DEPLOY_PROFILE alpha
      ;;
  esac

  if [[ "$deploy_profile" == "alpha" ]]; then
    set_env_var COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE true
    if [[ -z "$(current_value NOMOS_ALPHA_ALLOW_PUBLIC_BIND)" ]]; then
      set_env_var NOMOS_ALPHA_ALLOW_PUBLIC_BIND true
    fi
  elif [[ -n "${COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE:-}" ]]; then
    set_env_var COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE "$COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE"
  fi

  if [[ -n "${MONITOR_INTERNAL_TOKEN:-}" ]]; then
    set_env_var MONITOR_INTERNAL_TOKEN "$MONITOR_INTERNAL_TOKEN"
  fi

  local monitor_internal_token
  monitor_internal_token="$(current_value MONITOR_INTERNAL_TOKEN)"
  if [[ -z "$monitor_internal_token" ]]; then
    monitor_internal_token="$(random_secret)"
    set_env_var MONITOR_INTERNAL_TOKEN "$monitor_internal_token"
    log "Generated MONITOR_INTERNAL_TOKEN in deploy/.env"
  fi

  if [[ -n "${MONITOR_DASHBOARD_USER:-}" ]]; then
    set_env_var MONITOR_DASHBOARD_USER "$MONITOR_DASHBOARD_USER"
  fi
  if [[ -n "${MONITOR_DASHBOARD_PASSWORD:-}" ]]; then
    set_env_var MONITOR_DASHBOARD_PASSWORD "$MONITOR_DASHBOARD_PASSWORD"
  fi

  local monitor_dashboard_user
  monitor_dashboard_user="$(current_value MONITOR_DASHBOARD_USER)"
  if [[ -z "$monitor_dashboard_user" ]]; then
    set_env_var MONITOR_DASHBOARD_USER admin
  fi

  local monitor_dashboard_password
  monitor_dashboard_password="$(current_value MONITOR_DASHBOARD_PASSWORD)"
  if [[ -z "$monitor_dashboard_password" ]]; then
    monitor_dashboard_password="$(random_secret)"
    set_env_var MONITOR_DASHBOARD_PASSWORD "$monitor_dashboard_password"
    log "Generated MONITOR_DASHBOARD_PASSWORD in deploy/.env"
  fi

  if [[ -n "${NOMOS_PUBLIC_HOST:-}" ]]; then
    set_env_var NOMOS_PUBLIC_HOST "$NOMOS_PUBLIC_HOST"
  elif [[ -n "${PUBLIC_HOST:-}" ]]; then
    set_env_var NOMOS_PUBLIC_HOST "$PUBLIC_HOST"
  fi

  local public_host
  public_host="$(current_value NOMOS_PUBLIC_HOST)"
  if [[ -z "$public_host" ]]; then
    public_host="$(detect_public_host)"
    set_env_var NOMOS_PUBLIC_HOST "$public_host"
    log "Set NOMOS_PUBLIC_HOST=$public_host in deploy/.env"
  elif [[ -z "${NOMOS_PUBLIC_HOST:-}" && -z "${PUBLIC_HOST:-}" ]]; then
    if ! is_ipv4_literal "$public_host"; then
      local detected_public_host
      detected_public_host="$(detect_public_host)"
      if is_ipv4_literal "$detected_public_host" && [[ "$detected_public_host" != "$public_host" ]]; then
        set_env_var NOMOS_PUBLIC_HOST "$detected_public_host"
        log "Replaced hostname NOMOS_PUBLIC_HOST=$public_host with IP $detected_public_host in deploy/.env"
      fi
    fi
  fi

  if [[ -n "${NOMOS_SIGNUP_ADMIN_USER:-}" ]]; then
    set_env_var NOMOS_SIGNUP_ADMIN_USER "$NOMOS_SIGNUP_ADMIN_USER"
  fi
  if [[ -n "${NOMOS_SIGNUP_ADMIN_PASSWORD:-}" ]]; then
    set_env_var NOMOS_SIGNUP_ADMIN_PASSWORD "$NOMOS_SIGNUP_ADMIN_PASSWORD"
  fi

  local signup_admin_user
  signup_admin_user="$(current_value NOMOS_SIGNUP_ADMIN_USER)"
  if [[ -z "$signup_admin_user" ]]; then
    set_env_var NOMOS_SIGNUP_ADMIN_USER admin
  fi

  local signup_admin_password
  signup_admin_password="$(current_value NOMOS_SIGNUP_ADMIN_PASSWORD)"
  if [[ -z "$signup_admin_password" ]]; then
    signup_admin_password="$(random_secret)"
    set_env_var NOMOS_SIGNUP_ADMIN_PASSWORD "$signup_admin_password"
    log "Generated NOMOS_SIGNUP_ADMIN_PASSWORD in deploy/.env"
  fi

  if [[ -n "${DEMO_AUTH_USER:-}" ]]; then
    set_env_var DEMO_AUTH_USER "$DEMO_AUTH_USER"
  fi

  local demo_auth_user
  demo_auth_user="$(current_value DEMO_AUTH_USER)"
  if [[ -z "$demo_auth_user" ]]; then
    set_env_var DEMO_AUTH_USER demo
  fi

  if [[ -n "${DEMO_AUTH_PASSWORD:-}" ]]; then
    set_env_var DEMO_AUTH_PASSWORD "$DEMO_AUTH_PASSWORD"
  fi

  local demo_auth_password
  demo_auth_password="$(current_value DEMO_AUTH_PASSWORD)"
  if [[ -z "$demo_auth_password" ]]; then
    demo_auth_password="$(random_secret)"
    set_env_var DEMO_AUTH_PASSWORD "$demo_auth_password"
    log "Generated DEMO_AUTH_PASSWORD in deploy/.env"
  fi

  for key in NVIDIA_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY; do
    local value
    value="${!key:-}"
    if [[ -n "$value" ]]; then
      set_env_var "$key" "$value"
    fi
  done

  if [[ "$WITH_OPA" == "1" || "$WITH_OPA" == "true" ]]; then
    set_env_var COUNCIL_OPA_ENABLED true
  fi

  local nvidia_key anthropic_key openai_key
  nvidia_key="$(current_value NVIDIA_API_KEY)"
  anthropic_key="$(current_value ANTHROPIC_API_KEY)"
  openai_key="$(current_value OPENAI_API_KEY)"
  if [[ -z "$nvidia_key" && "${ALLOW_MISSING_NVIDIA_API_KEY:-0}" != "1" && "${ALLOW_MISSING_NVIDIA_API_KEY:-}" != "true" ]]; then
    die "The shipped council config requires NVIDIA_API_KEY. Run: NVIDIA_API_KEY=nvapi-... $0"
  fi
  if [[ -z "$nvidia_key" && -z "$anthropic_key" && -z "$openai_key" ]]; then
    die "Set NVIDIA_API_KEY=... before running, or populate deploy/.env with a configured backend key."
  fi
}

ensure_runtime_read_permissions() {
  # Docker preserves host file modes for COPY inputs and bind mounts. Some
  # enterprise checkouts use 0640/0750, but runtime containers switch to
  # non-root users that are not in the host group. Make only non-secret repo
  # source/config files readable; keep deploy/.env at 0600.
  local paths=(
    "$ROOT_DIR/nomos"
    "$ROOT_DIR/nomos_runtime"
    "$ROOT_DIR/configs"
    "$ROOT_DIR/trusted_executor_contract"
    "$ROOT_DIR/executors"
    "$ROOT_DIR/demo"
    "$ROOT_DIR/agent_signup"
    "$ROOT_DIR/monitor"
    "$ROOT_DIR/policies"
    "$ROOT_DIR/deploy/executor.yaml"
    "$ROOT_DIR/deploy/executor-dev.yaml"
    "$ROOT_DIR/deploy/executor-openshell.yaml"
  )

  local path
  for path in "${paths[@]}"; do
    [[ -e "$path" ]] || continue
    chmod -R a+rX "$path"
  done
  [[ -f "$ENV_FILE" ]] && chmod 600 "$ENV_FILE"
  [[ -d "$DYNAMIC_CONFIG_DIR" ]] && chmod -R a+rwX "$DYNAMIC_CONFIG_DIR"
}

ensure_dynamic_council_config() {
  mkdir -p "$DYNAMIC_CONFIG_DIR/councils"

  if [[ ! -f "$DYNAMIC_CONFIG_DIR/local_council_catalog.yaml" ]]; then
    log "Seeding dynamic council catalog"
    cp "$ROOT_DIR/configs/local_council_catalog.yaml" "$DYNAMIC_CONFIG_DIR/local_council_catalog.yaml"
  fi

  local config_name
  for config_name in council.yaml git_workflow_council.yaml; do
    if [[ -f "$ROOT_DIR/configs/$config_name" && ! -f "$DYNAMIC_CONFIG_DIR/$config_name" ]]; then
      cp "$ROOT_DIR/configs/$config_name" "$DYNAMIC_CONFIG_DIR/$config_name"
    fi
  done

  chmod -R a+rwX "$DYNAMIC_CONFIG_DIR"
}

wait_for_health() {
  local service="$1"
  local container_id
  local start
  start="$(date +%s)"

  log "Waiting for $service health"
  while true; do
    container_id="$(compose ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      local status
      status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$status" in
        healthy|running)
          log "$service is $status"
          return
          ;;
        unhealthy|exited|dead)
          compose logs --tail=80 "$service" >&2 || true
          die "$service became $status"
          ;;
      esac
    fi

    if (( "$(date +%s)" - start > WAIT_TIMEOUT_SECONDS )); then
      compose ps >&2 || true
      compose logs --tail=120 "$service" >&2 || true
      die "Timed out waiting for $service"
    fi
    sleep 3
  done
}

create_account() {
  [[ "$SKIP_ACCOUNT" == "1" || "$SKIP_ACCOUNT" == "true" ]] && return

  local redis_password
  redis_password="$(current_value REDIS_PASSWORD)"
  local account_integrity_key
  account_integrity_key="$(current_value COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY)"

  log "Creating agent account if needed: $AGENT_ID ($AGENT_ROLE)"
  local output
  output="$(
    compose run --rm --no-deps \
      -e REDIS_PASSWORD="$redis_password" \
      -e COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY="$account_integrity_key" \
      -e AGENT_ID="$AGENT_ID" \
      -e AGENT_ROLE="$AGENT_ROLE" \
      -e AGENT_COUNCILS="$AGENT_COUNCILS" \
      -e AGENT_DISPLAY_NAME="$AGENT_DISPLAY_NAME" \
      -e AGENT_FORCE_ROTATE="$AGENT_FORCE_ROTATE" \
      --entrypoint python council - <<'PY'
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

import redis
from redis.sentinel import Sentinel

ROLE_PERMISSIONS = {
    "readonly": ("shell:read", "file:read"),
    "developer": ("shell:read", "shell:write", "file:read", "file:write"),
    "operator": ("shell:read", "shell:write", "file:read", "file:write", "infra:deploy"),
    "admin": (
        "shell:read",
        "shell:write",
        "file:read",
        "file:write",
        "file:delete",
        "infra:deploy",
        "infra:destroy",
    ),
    "demo": ("shell:read", "shell:write", "file:read", "file:write"),
}

agent_id = os.environ["AGENT_ID"]
role = os.environ["AGENT_ROLE"]
councils = tuple(c.strip() for c in os.environ.get("AGENT_COUNCILS", "council-prod").split(",") if c.strip())
display_name = os.environ["AGENT_DISPLAY_NAME"]
force = os.environ.get("AGENT_FORCE_ROTATE", "0").lower() in {"1", "true", "yes"}
redis_password = os.environ["REDIS_PASSWORD"]
account_integrity_key = os.environ.get("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "")

if role not in ROLE_PERMISSIONS:
    print(f"ERROR=unknown role {role!r}", file=sys.stderr)
    sys.exit(2)
if not councils:
    print("ERROR=AGENT_COUNCILS must include at least one council id", file=sys.stderr)
    sys.exit(2)

redis_mode = os.environ.get("COUNCIL_REDIS_MODE", "standalone").strip().lower()
if redis_mode == "sentinel":
    sentinel_nodes = []
    for node in os.environ.get("COUNCIL_REDIS_SENTINEL_NODES", "").split(","):
        node = node.strip()
        if not node:
            continue
        host, sep, port = node.rpartition(":")
        if not sep:
            print(f"ERROR=invalid Sentinel node {node!r}", file=sys.stderr)
            sys.exit(2)
        sentinel_nodes.append((host, int(port)))
    if not sentinel_nodes:
        print("ERROR=COUNCIL_REDIS_SENTINEL_NODES is required in sentinel mode", file=sys.stderr)
        sys.exit(2)
    sentinel = Sentinel(
        sentinel_nodes,
        sentinel_kwargs={
            "password": redis_password,
            "socket_connect_timeout": 1.0,
            "socket_timeout": 1.0,
        },
        password=redis_password,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        decode_responses=True,
    )
    client = sentinel.master_for(os.environ.get("COUNCIL_REDIS_SENTINEL_SERVICE_NAME", "nomos-master"))
else:
    client = redis.Redis(host="redis", port=6379, db=0, password=redis_password, decode_responses=True)
client.ping()

account_key = f"nomos:accounts:{agent_id}"
perms_key = f"nomos:accounts:{agent_id}:perms"
councils_key = f"nomos:accounts:{agent_id}:councils"
existing = client.hgetall(account_key)

def refresh_account_integrity():
    if not account_integrity_key:
        return
    account = client.hgetall(account_key)
    if not account:
        return
    account.pop("auth_token", None)
    account.pop("_integrity_hmac", None)
    permissions = sorted(client.smembers(perms_key))
    account_councils = sorted(client.smembers(councils_key))
    payload = {
        "agent_id": agent_id,
        "account": dict(sorted(account.items())),
        "permissions": permissions,
        "councils": account_councils,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(account_integrity_key.encode(), raw, hashlib.sha256).hexdigest()
    client.hset(account_key, mapping={"_integrity_hmac": signature})

if existing and not force:
    pipe = client.pipeline()
    for council_id in councils:
        pipe.sadd(councils_key, council_id)
        pipe.sadd(f"nomos:councils:{council_id}:agents", agent_id)
    pipe.hset(account_key, mapping={"updated_at": str(time.time())})
    pipe.execute()
    refresh_account_integrity()
    print("NOMOS_ACCOUNT_STATUS=exists")
    print(f"NOMOS_AGENT_ID={agent_id}")
    print(f"NOMOS_ROLE={existing.get('role', role)}")
    print(f"NOMOS_COUNCILS={','.join(councils)}")
    sys.exit(0)

raw_token = secrets.token_hex(32)
token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
now = str(time.time())
old_hash = existing.get("token_hash") if existing else None

pipe = client.pipeline()
old_councils = client.smembers(councils_key)
pipe.hset(
    account_key,
    mapping={
        "token_hash": token_hash,
        "display_name": display_name or agent_id,
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
        "enabled": "1",
        "role": role,
    },
)
pipe.delete(perms_key)
pipe.sadd(perms_key, *ROLE_PERMISSIONS[role])
pipe.delete(councils_key)
for council_id in old_councils - set(councils):
    pipe.srem(f"nomos:councils:{council_id}:agents", agent_id)
for council_id in councils:
    pipe.sadd(councils_key, council_id)
    pipe.sadd(f"nomos:councils:{council_id}:agents", agent_id)
if old_hash:
    pipe.delete(f"nomos:token_index:{old_hash}")
pipe.set(f"nomos:token_index:{token_hash}", agent_id)
pipe.execute()
refresh_account_integrity()

print("NOMOS_ACCOUNT_STATUS=rotated" if existing else "NOMOS_ACCOUNT_STATUS=created")
print(f"NOMOS_AGENT_ID={agent_id}")
print(f"NOMOS_ROLE={role}")
print(f"NOMOS_COUNCILS={','.join(councils)}")
print(f"NOMOS_TOKEN={raw_token}")
PY
  )"

  printf '%s\n' "$output"

  local token
  token="$(printf '%s\n' "$output" | awk -F= '$1 == "NOMOS_TOKEN" {print $2; exit}')"
  if [[ -n "$token" ]]; then
    set_env_var NOMOS_AGENT_ID "$AGENT_ID"
    set_env_var NOMOS_AUTH_TOKEN "$token"
    if [[ "$WITH_DEMO" == "1" || "$WITH_DEMO" == "true" ]]; then
      set_env_var NOMOS_AUTH_TOKEN "$token"
      set_env_var NOMOS_AGENT_ID "$AGENT_ID"
    fi
    log "Stored NOMOS_AGENT_ID and NOMOS_AUTH_TOKEN in deploy/.env"
  else
    log "Account already existed; token was not changed or recovered."
  fi
}

kind_council_deployment() {
  kubectl_kind -n "$KIND_NAMESPACE" get deploy \
    -l "app.kubernetes.io/instance=$HELM_RELEASE,app.kubernetes.io/component=council" \
    -o jsonpath='{.items[0].metadata.name}'
}

sync_kind_agent_secret() {
  local token="$1"
  [[ -n "$token" ]] || return 0
  kind_secret_exists || return 0

  local patch_file
  patch_file="$(mktemp)"
  printf '{"data":{"NOMOS_AGENT_ID":"%s","NOMOS_AUTH_TOKEN":"%s"}}\n' \
    "$(printf '%s' "$AGENT_ID" | base64 | tr -d '\n')" \
    "$(printf '%s' "$token" | base64 | tr -d '\n')" >"$patch_file"
  if ! kubectl_kind -n "$KIND_NAMESPACE" patch secret "$KIND_SECRET_NAME" \
    --type merge \
    --patch-file "$patch_file" >/dev/null; then
    rm -f "$patch_file"
    return 1
  fi
  rm -f "$patch_file"
}

store_kind_agent_token() {
  local token="$1"
  if is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    sync_kind_agent_secret "$token"
    log "Stored Kind NOMOS_AGENT_ID and NOMOS_AUTH_TOKEN in Kubernetes Secret $KIND_NAMESPACE/$KIND_SECRET_NAME"
    return
  fi

  set_env_var NOMOS_AGENT_ID "$AGENT_ID"
  set_env_var NOMOS_AUTH_TOKEN "$token"
  sync_kind_agent_secret "$token"
  log "Stored Kind NOMOS_AGENT_ID and NOMOS_AUTH_TOKEN in deploy/.env"
}

create_kind_account() {
  [[ "$SKIP_ACCOUNT" == "1" || "$SKIP_ACCOUNT" == "true" ]] && return

  local deployment
  deployment="$(kind_council_deployment)"
  [[ -n "$deployment" ]] || die "Could not find Kind council deployment for release $HELM_RELEASE."

  log "Creating Kind agent account if needed: $AGENT_ID ($AGENT_ROLE)"
  local output
  output="$(
    kubectl_kind -n "$KIND_NAMESPACE" exec "deploy/$deployment" -c council -- \
      env \
        AGENT_ID="$AGENT_ID" \
        AGENT_ROLE="$AGENT_ROLE" \
        AGENT_COUNCILS="$AGENT_COUNCILS" \
        AGENT_DISPLAY_NAME="$AGENT_DISPLAY_NAME" \
        AGENT_FORCE_ROTATE="$AGENT_FORCE_ROTATE" \
      sh -s <<'SH'
set -eu
export NOMOS_REDIS_URL="redis://:${COUNCIL_REDIS_PASSWORD}@${COUNCIL_REDIS_HOST}:${COUNCIL_REDIS_PORT}/0"

# Redis stores only a token hash, so an existing account's raw token cannot be
# recovered — AGENT_FORCE_ROTATE is the documented way to mint a fresh one
# (docs/council-mcp-validation.md). The compose path already honours it; without
# this the Kind path silently ignored it and reported the token as unrecoverable,
# leaving no way to obtain credentials for a redeployed cluster.
case "${AGENT_FORCE_ROTATE:-0}" in
  1|true|TRUE|yes|YES)
    if python scripts/agent_accounts.py rotate-token --json "$AGENT_ID" \
        >/tmp/nomos-agent-account.json 2>/tmp/nomos-agent-account.err; then
      cat /tmp/nomos-agent-account.json
      exit 0
    fi
    ;;
esac

if python scripts/agent_accounts.py show --json "$AGENT_ID" >/tmp/nomos-agent-account.json 2>/tmp/nomos-agent-account.err; then
  cat /tmp/nomos-agent-account.json
  exit 0
fi

set -- create --json --role "$AGENT_ROLE" --display-name "$AGENT_DISPLAY_NAME"
old_ifs="$IFS"
IFS=,
for council_id in $AGENT_COUNCILS; do
  if [ -n "$council_id" ]; then
    set -- "$@" --council "$council_id"
  fi
done
IFS="$old_ifs"
set -- "$@" "$AGENT_ID"
python scripts/agent_accounts.py "$@"
SH
  )"

  printf '%s\n' "$output"

  local token
  token="$(printf '%s\n' "$output" | awk -F'"' '$2 == "token" {print $4; exit}')"
  if [[ -n "$token" ]]; then
    store_kind_agent_token "$token"
    kubectl_kind -n "$KIND_NAMESPACE" rollout restart deployment "$HELM_RELEASE-demo" >/dev/null 2>&1 || true
    return
  fi

  # No token. That is only benign when the account genuinely exists already —
  # its raw token is unrecoverable because only a hash is stored. If the account
  # is absent too, the deploy has produced a stack with NO usable credentials and
  # must not report success: `agent_accounts.py` refuses every account operation
  # under COUNCIL_STORAGE_MODE=postgres, which is the Kind default, so this path
  # silently produced credential-less deployments that still exited 0.
  if printf '%s\n' "$output" | grep -q '"agent_id"'; then
    log "Kind account already existed; token was not changed or recovered."
    log "  Use AGENT_FORCE_ROTATE=1 to mint a fresh token."
    return
  fi

  log "ERROR: could not create or read agent account '$AGENT_ID'."
  printf '%s\n' "$output" | sed 's/^/[deploy]   /' >&2
  die "agent account provisioning failed — the deployment has no usable credentials"
}

main_compose() {
  cd "$ROOT_DIR"
  ensure_prereqs
  ensure_env
  resolve_compose_redis_topology
  ensure_dynamic_council_config
  ensure_runtime_read_permissions
  remove_inactive_services
  preflight_ports

  local build_flag=("--build")
  if [[ "$SKIP_BUILD" == "1" || "$SKIP_BUILD" == "true" ]]; then
    build_flag=()
  fi

  local -a redis_services=(redis)
  if [[ "$COMPOSE_REDIS_TOPOLOGY" == "sentinel" ]]; then
    redis_services+=(redis-1 redis-2 redis-sentinel-1 redis-sentinel-2 redis-sentinel-3)
  fi

  log "Starting Redis topology ($COMPOSE_REDIS_TOPOLOGY), OPA, council, monitor, and gRPC proxy"
  compose up -d "${redis_services[@]}"
  if [[ "$WITH_OPA" == "1" || "$WITH_OPA" == "true" ]]; then
    # OPA loads policy files at startup. Recreate the bundled sidecar so
    # deploys pick up policy changes instead of serving stale/empty data.
    compose up -d --force-recreate opa
    compose up -d "${build_flag[@]}" council monitor council-grpc
  else
    compose up -d "${build_flag[@]}" council monitor council-grpc
  fi

  local redis_service
  for redis_service in "${redis_services[@]}"; do
    wait_for_health "$redis_service"
  done
  wait_for_health council
  wait_for_health monitor
  wait_for_health council-grpc

  if [[ "$WITH_AGENT_SIGNUP" == "1" || "$WITH_AGENT_SIGNUP" == "true" ]]; then
    log "Starting agent signup UI"
    compose up -d "${build_flag[@]}" agent-signup
    wait_for_health agent-signup
  fi

  create_account

  if [[ -n "$EXECUTOR_PROFILE" ]]; then
    log "Starting executor profile: $EXECUTOR_PROFILE"
    compose up -d "${build_flag[@]}" "$EXECUTOR_PROFILE"
    wait_for_health "$EXECUTOR_PROFILE"
  fi

  if [[ "$WITH_DEMO" == "1" || "$WITH_DEMO" == "true" ]]; then
    log "Starting demo UI"
    compose up -d "${build_flag[@]}" demo
    wait_for_health demo
  fi

  log "Deployment complete"
  compose ps
  printf '\n'
  local public_host
  public_host="$(current_value NOMOS_PUBLIC_HOST)"
  public_host="${public_host:-$(detect_public_host)}"
  local monitor_port redis_port council_grpc_port
  monitor_port="$(current_or_default MONITOR_PORT 9901)"
  redis_port="$(current_or_default REDIS_PORT 6379)"
  council_grpc_port="$(current_or_default COUNCIL_GRPC_PORT 9904)"
  printf 'Council MCP:     http://%s:%s/mcp\n' "$public_host" "$monitor_port"
  printf 'Monitor UI:      http://%s:%s/\n' "$public_host" "$monitor_port"
  printf 'Monitor auth:    user %s, password in %s\n' "$(current_value MONITOR_DASHBOARD_USER)" "$ENV_FILE"
  printf 'Redis:           %s:%s\n' "$public_host" "$redis_port"
  if [[ "$WITH_OPA" == "1" || "$WITH_OPA" == "true" ]]; then
    printf 'OPA:             http://%s:%s\n' "$public_host" "$(current_or_default OPA_PORT 8181)"
  fi
  if [[ "$WITH_AGENT_SIGNUP" == "1" || "$WITH_AGENT_SIGNUP" == "true" ]]; then
    local agent_signup_port
    agent_signup_port="$(current_or_default AGENT_SIGNUP_PORT 9905)"
    printf 'Agent signup:    https://%s:%s/\n' "$public_host" "$agent_signup_port"
    printf 'Agent admin:     https://%s:%s/admin\n' "$public_host" "$agent_signup_port"
    printf 'Agent auth:      user %s, password in %s\n' "$(current_value NOMOS_SIGNUP_ADMIN_USER)" "$ENV_FILE"
  fi
  printf 'Council gRPC:    %s:%s\n' "$public_host" "$council_grpc_port"
  if [[ "$EXECUTOR_PROFILE" == "executor-default" ]]; then
    printf 'Executor:        http://%s:%s\n' "$public_host" "$(current_or_default EXECUTOR_DEFAULT_PORT 9902)"
  fi
  if [[ "$EXECUTOR_PROFILE" == "executor-openshell" ]]; then
    printf 'Executor:        http://%s:%s\n' "$public_host" "$(current_or_default EXECUTOR_OPENSHELL_PORT 9907)"
    printf 'OpenShell:       internal service openshell:8080\n'
  fi
  if [[ "$WITH_DEMO" == "1" || "$WITH_DEMO" == "true" ]]; then
    printf 'Demo UI:         https://%s:%s\n' "$public_host" "$(current_or_default DEMO_PORT 8000)"
  fi
  printf 'Env file:        %s\n' "$ENV_FILE"
}

main_kind() {
  cd "$ROOT_DIR"
  ensure_kind_prereqs
  if is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    log "Using existing Kind Secret only; deploy/.env will not be created or updated"
  else
    ensure_env
  fi
  ensure_kind_cluster
  if is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    ensure_kind_existing_secret_only
  fi

  if [[ "$SKIP_BUILD" == "1" || "$SKIP_BUILD" == "true" ]]; then
    log "Skipping Kind image rebuild/load"
  else
    log "Building and loading images into Kind cluster: $KIND_CLUSTER_NAME"
    "$KIND_BUILD_SCRIPT" "$KIND_CLUSTER_NAME"
  fi

  local secret_values_file
  secret_values_file=""
  if ! is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    secret_values_file="$(mktemp)"
    write_kind_secret_values "$secret_values_file"
  fi

  local helm_args=(
    upgrade
    --install
    "$HELM_RELEASE"
    "$KIND_CHART_DIR"
    --namespace
    "$KIND_NAMESPACE"
    --create-namespace
    -f
    "$KIND_VALUES_FILE"
  )

  local deploy_profile
  deploy_profile="$(current_or_default NOMOS_DEPLOY_PROFILE dev)"
  helm_args+=(--set "deploymentProfile=$deploy_profile")
  if [[ "$deploy_profile" == "alpha" ]]; then
    helm_args+=(
      --set "council.allowUncertifiedEvidence=true"
      --set "council.allowPublicBind=true"
      --set "networkPolicy.enabled=true"
    )
  fi

  if is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    if kind_secret_is_helm_owned; then
      log "Reusing Helm-managed Kubernetes Secret data: $KIND_NAMESPACE/$KIND_SECRET_NAME"
      secret_values_file="$(mktemp)"
      write_kind_secret_values_from_existing_secret "$secret_values_file"
      helm_args+=(-f "$secret_values_file")
    else
      log "Using existing Kubernetes Secret: $KIND_NAMESPACE/$KIND_SECRET_NAME"
      helm_args+=(--set secrets.create=false --set "secrets.existingSecret=$KIND_SECRET_NAME")
    fi
  elif kind_secret_exists && ! kind_secret_is_helm_owned; then
    if [[ "$deploy_profile" == "alpha" ]]; then
      log "Syncing existing Kubernetes Secret from deploy/.env for alpha profile"
      sync_kind_existing_secret_from_env
    fi
    log "Using existing Kubernetes Secret: $KIND_NAMESPACE/$KIND_SECRET_NAME"
    helm_args+=(--set secrets.create=false --set "secrets.existingSecret=$KIND_SECRET_NAME")
  else
    helm_args+=(-f "$secret_values_file")
  fi

  log "Deploying Helm release $HELM_RELEASE to namespace $KIND_NAMESPACE"
  if helm_kind "${helm_args[@]}"; then
    [[ -n "$secret_values_file" ]] && rm -f "$secret_values_file"
  else
    [[ -n "$secret_values_file" ]] && rm -f "$secret_values_file"
    return 1
  fi

  if is_true "$KIND_ROLLOUT_RESTART"; then
    # Only the deployments running locally-built nomos/* images need this; they
    # share one mutable tag, so a restart is how a rebuild takes effect.
    #
    # The infrastructure deployments run upstream images and must be excluded.
    # Restarting vault in particular BREAKS the deploy: it runs `server -dev`
    # (in-memory), and the Helm post-install hook that enables Kubernetes auth
    # and the transit keys has already run and deleted itself
    # (hook-delete-policy: hook-succeeded). A restarted vault comes back empty,
    # nothing re-runs the bootstrap, and the council's wait-for-backends init
    # container then blocks forever on `auth/kubernetes/login` with HTTP 403.
    log "Restarting Kind deployments so same-tag local images are used"
    kubectl_kind -n "$KIND_NAMESPACE" rollout restart deployment \
      -l "app.kubernetes.io/instance=$HELM_RELEASE,app.kubernetes.io/component notin ($KIND_NO_RESTART_COMPONENTS)"
  fi

  log "Waiting for Kind deployment rollouts"
  wait_kind_rollouts || die "gave up waiting for some deployments — see the diagnostics above"

  create_kind_account

  start_kind_port_forwards

  log "Deployment complete"
  kubectl_kind -n "$KIND_NAMESPACE" get deploy,pod,svc
  printf '\n'
  print_kind_urls
  if is_true "$KIND_PORT_FORWARD"; then
    printf 'Port-forward logs: %s\n' "$KIND_PORT_FORWARD_DIR"
  fi
  if is_true "$KIND_USE_EXISTING_SECRET_ONLY"; then
    printf 'Secret source:    Kubernetes Secret %s/%s\n' "$KIND_NAMESPACE" "$KIND_SECRET_NAME"
  else
    printf 'Env file:        %s\n' "$ENV_FILE"
  fi
}

main() {
  case "${1:-}" in
    -h|--help)
      print_usage
      return 0
      ;;
  esac

  require_supported_bash

  case "$DEPLOY_TARGET" in
    kind|kubernetes)
      main_kind
      ;;
    compose|docker|docker-compose)
      main_compose
      ;;
    *)
      die "Unknown DEPLOY_TARGET '$DEPLOY_TARGET'. Use kind or compose."
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
