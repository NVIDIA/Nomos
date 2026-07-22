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
# Run the Nomos account CLI from inside the council container.
#
# This wrapper is intended for deployment hosts that have Docker/Compose but do
# not have Python or project dependencies installed on the host.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/deploy/docker-compose.yml}"
COMPOSE_REDIS_TOPOLOGY="${COMPOSE_REDIS_TOPOLOGY:-}"
COMPOSE_REDIS_SENTINEL_FILE="${COMPOSE_REDIS_SENTINEL_FILE:-$ROOT_DIR/deploy/docker-compose.redis-sentinel.yml}"
COMPOSE_EXTRA_FILES="${COMPOSE_EXTRA_FILES:-}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
NOMOS_REDIS_PREFIX="${NOMOS_REDIS_PREFIX:-nomos:}"
ACCOUNT_SKIP_BUILD="${ACCOUNT_SKIP_BUILD:-0}"
ACCOUNT_SKIP_REDIS_START="${ACCOUNT_SKIP_REDIS_START:-0}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/agent_accounts.sh <command> [options]

Examples:
  ./scripts/agent_accounts.sh create alice-agent --role developer
  ./scripts/agent_accounts.sh rotate-token alice-agent
  ./scripts/agent_accounts.sh grant-council alice-agent council-infra-prod
  ./scripts/agent_accounts.sh councils alice-agent
  ./scripts/agent_accounts.sh members council-prod
  ./scripts/agent_accounts.sh list
  ./scripts/agent_accounts.sh disable alice-agent

This runs scripts/agent_accounts.py inside the council container and connects
to Redis on the Docker network. Host Python is not required.

New accounts are granted the default council membership when --council is
omitted. Set NOMOS_DEFAULT_COUNCIL_ID to override the default.

Environment:
  COMPOSE_FILE               Compose file path (default: deploy/docker-compose.yml)
  COMPOSE_REDIS_TOPOLOGY     sentinel or standalone (default: sentinel)
  COMPOSE_EXTRA_FILES        Additional docker compose override files, colon-separated
  ENV_FILE                   Env file path (default: deploy/.env)
  REDIS_PASSWORD             Override Redis password
  NOMOS_REDIS_URL               Override full Redis URL used by the container CLI
  NOMOS_REDIS_MODE              Override Redis mode used by the container CLI
  NOMOS_REDIS_SENTINEL_PASSWORD Override Redis Sentinel password used by the CLI
  NOMOS_REDIS_PREFIX            Redis key prefix (default: nomos:)
  NOMOS_DEFAULT_COUNCIL_ID      Default council for new accounts (default: council-prod)
  ACCOUNT_SKIP_BUILD=1       Do not build the council image before running
  ACCOUNT_SKIP_REDIS_START=1 Do not start the bundled Redis service first
EOF
}

die() {
  printf '[accounts] ERROR: %s\n' "$*" >&2
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

compose() {
  local -a compose_files=()
  compose_file_args compose_files
  docker compose "${compose_files[@]}" "$@"
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

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

require_supported_bash
have_cmd docker || die "Docker is not installed or not on PATH."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
COMPOSE_REDIS_TOPOLOGY="$(current_value COMPOSE_REDIS_TOPOLOGY)"
COMPOSE_REDIS_TOPOLOGY="${COMPOSE_REDIS_TOPOLOGY:-sentinel}"
compose_files=()
compose_file_args compose_files
for ((i = 0; i < ${#compose_files[@]}; i += 2)); do
  compose_file="${compose_files[$((i + 1))]}"
  [[ -f "$compose_file" ]] || die "Compose file not found: $compose_file"
done

redis_password="$(current_value REDIS_PASSWORD)"
redis_password="${redis_password:-nomos-dev}"
redis_url="${NOMOS_REDIS_URL:-redis://:${redis_password}@redis:6379/0}"
account_integrity_key="$(current_value COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY)"
export NOMOS_REDIS_URL="$redis_url"
export NOMOS_REDIS_PREFIX
export COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY="$account_integrity_key"
if [[ "$COMPOSE_REDIS_TOPOLOGY" == "sentinel" ]]; then
  redis_sentinel_service_name="$(current_value REDIS_SENTINEL_SERVICE_NAME)"
  export NOMOS_REDIS_MODE="${NOMOS_REDIS_MODE:-sentinel}"
  export NOMOS_REDIS_SENTINEL_SERVICE_NAME="${NOMOS_REDIS_SENTINEL_SERVICE_NAME:-${redis_sentinel_service_name:-nomos-master}}"
  export NOMOS_REDIS_SENTINEL_NODES="${NOMOS_REDIS_SENTINEL_NODES:-redis-sentinel-1:26379,redis-sentinel-2:26379,redis-sentinel-3:26379}"
fi

if [[ "$ACCOUNT_SKIP_REDIS_START" != "1" && "$ACCOUNT_SKIP_REDIS_START" != "true" ]]; then
  redis_services=(redis)
  if [[ "$COMPOSE_REDIS_TOPOLOGY" == "sentinel" ]]; then
    redis_services+=(redis-1 redis-2 redis-sentinel-1 redis-sentinel-2 redis-sentinel-3)
  fi
  compose up -d "${redis_services[@]}"
fi

if [[ "$ACCOUNT_SKIP_BUILD" != "1" && "$ACCOUNT_SKIP_BUILD" != "true" ]]; then
  compose build council
fi

compose run --rm --no-deps \
  --env NOMOS_REDIS_URL \
  --env NOMOS_REDIS_MODE \
  --env NOMOS_REDIS_SENTINEL_SERVICE_NAME \
  --env NOMOS_REDIS_SENTINEL_NODES \
  --env NOMOS_REDIS_SENTINEL_PASSWORD \
  --env NOMOS_REDIS_PREFIX \
  --env COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY \
  --entrypoint python \
  council scripts/agent_accounts.py "$@"
