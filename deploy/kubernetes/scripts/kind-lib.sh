# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash
#
# Shared helpers for the Nomos Kind dev bootstrapper. SOURCE this file; do not run it.
# Honors (with defaults): KIND_CLUSTER_NAME, NAMESPACE, HELM_RELEASE, ROOT_DIR, ENV_FILE.

KIND_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "$KIND_LIB_DIR/../../.." && pwd)}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"
NAMESPACE="${NAMESPACE:-nomos}"
HELM_RELEASE="${HELM_RELEASE:-nomos}"

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

# prompt VAR "label" [default] [secret]
# Non-interactive (NONINTERACTIVE=1 or no tty): uses the default silently.
prompt() {
  local __var="$1" label="$2" default="${3:-}" secret="${4:-}" val=""
  if [ "${NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
    val="$default"
  elif [ "$secret" = "secret" ]; then
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

# Set a fixed dev default for KEY only if currently unset.
default_or_keep() {
  local key="$1" def="$2" cur
  cur="$(env_get "$key")"
  [ -z "$cur" ] && { cur="$def"; env_set "$key" "$cur"; }
  printf '%s' "$cur"
}
