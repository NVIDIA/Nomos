#!/usr/bin/env bash
#
# Run the Nomos council catalog CLI from inside the council container.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/deploy/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
COUNCIL_SKIP_BUILD="${COUNCIL_SKIP_BUILD:-0}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/councils.sh <command> [options]

Examples:
  ./scripts/councils.sh list
  ./scripts/councils.sh show council-prod
  ./scripts/councils.sh create customer-git --template git-workflow-local
  ./scripts/councils.sh update-from-file customer-git ./scratch/customer-git.yaml --enabled
  ./scripts/councils.sh enable customer-git
  ./scripts/councils.sh disable customer-git
  ./scripts/councils.sh validate

This runs scripts/councils.py inside the council container against the dynamic
catalog mounted at /app/dynamic-config. Host Python is not required.
EOF
}

die() {
  printf '[councils] ERROR: %s\n' "$*" >&2
  exit 1
}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
[[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"

if [[ "$COUNCIL_SKIP_BUILD" != "1" && "$COUNCIL_SKIP_BUILD" != "true" ]]; then
  compose build council
fi

compose run --rm --no-deps \
  --env COUNCIL_CATALOG_PATH=/app/dynamic-config/local_council_catalog.yaml \
  --env COUNCIL_DYNAMIC_PROFILE_DIR=/app/dynamic-config/councils \
  --env COUNCIL_DYNAMIC_LEDGER_DIR=/app/ledger \
  --entrypoint python \
  council scripts/councils.py "$@"
