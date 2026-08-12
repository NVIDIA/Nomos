# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash
#
# Shared helpers for the Nomos local dev bootstrappers (Kind + docker-compose).
# SOURCE this file; do not run it. Lives at deploy/scripts/dev-lib.sh.
# Honors (with defaults): KIND_CLUSTER_NAME, NAMESPACE, HELM_RELEASE, COMPOSE_PROJECT,
# ROOT_DIR, ENV_FILE.

DEV_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "$DEV_LIB_DIR/../.." && pwd)}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"
NAMESPACE="${NAMESPACE:-nomos}"
HELM_RELEASE="${HELM_RELEASE:-nomos}"
# Must resolve to the same project scripts/deploy_stack.sh:compose_project_name
# produces, or these scripts address a project that does not exist. That is
# COMPOSE_PROJECT_NAME, else the compose file's directory name ("deploy"). The
# previous default of "nomos" never matched a deploy_stack.sh deployment, so
# `dc run` joined an empty project and could not reach any backend.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-$(basename "$ROOT_DIR/deploy")}}"

if [ -t 1 ]; then
  C_B=$'\033[1m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else
  C_B=; C_G=; C_Y=; C_R=; C_0=
fi
log()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf '  %s\xe2\x9c\x93%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

require_cmd() {
  local c
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"; done
}

kc()    { kubectl --context "kind-${KIND_CLUSTER_NAME}" "$@"; }
kcn()   { kubectl --context "kind-${KIND_CLUSTER_NAME}" -n "$NAMESPACE" "$@"; }
helmc() { helm --kube-context "kind-${KIND_CLUSTER_NAME}" "$@"; }

# docker compose wrapper: env from deploy/.env, the executor-openshell profile
# enabled. Used by the compose-* scripts.
#
# The overlay set MUST mirror scripts/deploy_stack.sh:compose_file_args, because
# these scripts act on whatever that deployed. This previously hard-coded
# docker-compose.service.yml, which flips COUNCIL_STORAGE_MODE to postgres and
# adds a postgres service. A default deploy uses the Redis/sentinel topology and
# never starts postgres, so every compose-* helper addressed a backend that was
# not running -- compose-register-agent.sh died with "failed to resolve host
# 'postgres'". Select overlays from the same environment deploy_stack.sh reads.
dc() {
  local -a compose_files=(-f "$ROOT_DIR/deploy/docker-compose.yml")
  case "${COMPOSE_REDIS_TOPOLOGY:-sentinel}" in
    standalone) ;;
    sentinel|"") compose_files+=(-f "$ROOT_DIR/deploy/docker-compose.redis-sentinel.yml") ;;
    *) die "Unknown COMPOSE_REDIS_TOPOLOGY '${COMPOSE_REDIS_TOPOLOGY}'. Use standalone or sentinel." ;;
  esac
  if [[ -n "${COMPOSE_EXTRA_FILES:-}" ]]; then
    local extra_file
    local -a extra_files=()
    IFS=: read -r -a extra_files <<<"$COMPOSE_EXTRA_FILES"
    for extra_file in "${extra_files[@]}"; do
      [[ -n "$extra_file" ]] && compose_files+=(-f "$extra_file")
    done
  fi
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_FILE" --profile executor-openshell \
    "${compose_files[@]}" "$@"
}

# ── readiness waits with concise, actionable failures (Kind) ──────────────────
# Never surface a bare "timed out waiting for the condition". On failure these say
# WHAT is not ready, the real reason (container waiting-reason or the latest Warning
# event), and the single command to investigate.

# diagnose_workload <kind> <name> <elapsed-seconds>: print a concise reason + next step.
diagnose_workload() {
  local kind="$1" name="$2" elapsed="$3" comp pod ctr ready desired wreason restarts evt kn
  comp="${name#"${HELM_RELEASE}-"}"
  kn="kubectl --context kind-${KIND_CLUSTER_NAME} -n ${NAMESPACE}"
  ready="$(kcn get "$kind" "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"; ready="${ready:-0}"
  desired="$(kcn get "$kind" "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)"; desired="${desired:-1}"
  printf '  %s\xe2\x9c\x97%s %s not ready (%s/%s) after %ss\n' "$C_R" "$C_0" "$name" "$ready" "$desired" "$elapsed" >&2
  pod="$(kcn get pod -l "app.kubernetes.io/component=$comp" \
        --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)"
  if [ -z "$pod" ]; then
    warn "no $comp pod scheduled — $kn get pods"
    return 0
  fi
  ctr="$(kcn get pod "$pod" -o jsonpath='{.spec.containers[0].name}' 2>/dev/null)"
  wreason="$(kcn get pod "$pod" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
  restarts="$(kcn get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)"
  evt="$(kcn get events --field-selector "involvedObject.name=$pod" \
        -o jsonpath='{range .items[*]}{.type}={.message}|{end}' 2>/dev/null \
        | tr '|' '\n' | sed -n 's/^Warning=//p' | tail -n1)"
  if [ -n "$wreason" ]; then
    warn "${pod}: ${wreason}${restarts:+ (restarts: $restarts)}${evt:+ — $evt}"
  elif [ -n "$evt" ]; then
    warn "${pod}: running but not Ready — ${evt}"
  else
    warn "${pod}: not Ready (no Warning events yet)"
  fi
  warn "→ ${kn} logs ${pod} -c ${ctr} --tail=50"
}

# confirm_wait "<thing> not ready" <next-seconds>: ask whether to keep waiting.
# Returns 0 to wait another round, 1 to give up. Non-interactive (NONINTERACTIVE=1
# or no tty) cannot block, so it gives up — the diagnostic was already printed.
confirm_wait() {
  local what="$1" secs="$2" ans
  if [ "${NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
    warn "non-interactive — giving up on ${what}"
    return 1
  fi
  read -r -p "  ${what} — [w]ait another ${secs}s or [g]ive up? [w] " ans || return 1
  case "$ans" in g|G|give*|q|Q|n|N|no) return 1 ;; *) return 0 ;; esac
}

# wait_rollout <kind> <name> [timeout]: poll for readiness; fast-surface a terminal
# container state (CrashLoop/ImagePull/...) instead of burning the whole timeout.
# On timeout/terminal it prints diagnose_workload then PROMPTS (confirm_wait) — it
# does not auto-fail; it only returns non-zero when the operator gives up.
wait_rollout() {
  local kind="$1" name="$2" timeout="${3:-180}" comp pod ready desired wreason i
  comp="${name#"${HELM_RELEASE}-"}"
  while :; do
    i=0
    while [ "$i" -lt "$timeout" ]; do
      ready="$(kcn get "$kind" "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"; ready="${ready:-0}"
      desired="$(kcn get "$kind" "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)"; desired="${desired:-1}"
      [ "$ready" -ge "$desired" ] 2>/dev/null && { ok "$name ready ($ready/$desired)"; return 0; }
      pod="$(kcn get pod -l "app.kubernetes.io/component=$comp" --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)"
      wreason="$(kcn get pod "$pod" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
      case "$wreason" in
        CrashLoopBackOff|ImagePullBackOff|ErrImagePull|InvalidImageName|CreateContainerConfigError|CreateContainerError|RunContainerError) break ;;
      esac
      sleep 3; i=$((i + 3))
    done
    diagnose_workload "$kind" "$name" "$i"
    confirm_wait "$name not ready" 120 || return 1
    timeout=120
  done
}

rand_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

# Read a KEY's value from $ENV_FILE (last assignment wins; surrounding quotes stripped).
env_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$ENV_FILE" 2>/dev/null | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}

# Write/replace KEY=VALUE in $ENV_FILE (created 0600 if missing). Unquoted values.
env_set() {
  local key="$1" val="$2" tmp
  if [ ! -f "$ENV_FILE" ]; then ( umask 077; : > "$ENV_FILE" ); fi
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    tmp="$(mktemp)"; chmod 600 "$tmp"
    awk -v k="$key" -v v="$val" '
      $0 ~ "^" k "=" { print k "=" v; done=1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

percent_encode_uri_component() {
  local value="$1" encoded="" char byte i
  local LC_ALL=C
  for ((i = 0; i < ${#value}; i++)); do
    char="${value:i:1}"
    case "$char" in
      [a-zA-Z0-9.~_-]) encoded+="$char" ;;
      *) printf -v byte '%%%02X' "'$char"; encoded+="$byte" ;;
    esac
  done
  printf '%s' "$encoded"
}

sync_postgres_url_password() {
  local password encoded
  password="$(env_get POSTGRES_PASSWORD)"  # pragma: allowlist secret
  [ -n "$password" ] || die "POSTGRES_PASSWORD is required for database URLs"  # pragma: allowlist secret
  encoded="$(percent_encode_uri_component "$password")"
  export POSTGRES_PASSWORD="$password"  # pragma: allowlist secret
  export POSTGRES_PASSWORD_URLENCODED="$encoded"  # pragma: allowlist secret
  env_set POSTGRES_PASSWORD_URLENCODED "$encoded"  # pragma: allowlist secret
  unset password encoded
}

# prompt VAR "label" [default] [secret]
# Non-interactive (NONINTERACTIVE=1 or no tty): uses the default silently.
prompt() {
  local __var="$1" label="$2" default="${3:-}" secret="${4:-}" val=""
  if [ "${NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
    val="$default"
  elif [ "$secret" = "secret" ]; then  # pragma: allowlist secret
    read -r -s -p "$label${default:+ [keep existing]}: " val; echo
    [ -z "$val" ] && val="$default"
  else
    read -r -p "$label${default:+ [$default]}: " val
    [ -z "$val" ] && val="$default"
  fi
  printf -v "$__var" '%s' "$val"
}

# Ensure $ENV_FILE has a random value for KEY (generate if missing or REGENERATE_SECRETS=1).
gen_or_keep() {
  local key="$1" cur
  cur="$(env_get "$key")"
  if [ -z "$cur" ] || [ "${REGENERATE_SECRETS:-0}" = "1" ]; then
    cur="$(rand_secret)"; env_set "$key" "$cur"
  fi
  printf '%s' "$cur"
}

# Keep the retained postgres-data role in sync with the configured client secret.
# Local socket authentication lets this run after Postgres is healthy even though
# the volume still contains the previous password.
sync_postgres_role_password() {
  local password escaped
  password="$(env_get POSTGRES_PASSWORD)"  # pragma: allowlist secret
  [ -n "$password" ] || die "POSTGRES_PASSWORD is required for role password rotation"  # pragma: allowlist secret
  escaped="$(printf '%s' "$password" | sed "s/'/''/g")"
  printf "ALTER ROLE nomos WITH PASSWORD '%s';\n" "$escaped" \
    | dc exec -T postgres psql -v ON_ERROR_STOP=1 -U nomos -d nomos >/dev/null
  unset password escaped
  ok "Postgres role password synchronized"
}

# Set a fixed dev default for KEY only if currently unset.
default_or_keep() {
  local key="$1" def="$2" cur
  cur="$(env_get "$key")"
  [ -z "$cur" ] && { cur="$def"; env_set "$key" "$cur"; }
  printf '%s' "$cur"
}
