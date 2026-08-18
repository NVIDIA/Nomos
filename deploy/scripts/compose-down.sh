#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Tear down the Nomos docker-compose dev stack. Removes volumes (-v) so the
# ephemeral Postgres + in-memory dev Vault reset together — the next compose-up.sh
# re-bootstraps Vault and re-registers the agent against a clean store. Generated
# dev secrets remain in deploy/.env (gitignored) so the re-run is otherwise identical.
#
# Usage: compose-down.sh [--keep-volumes]
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

KEEP_VOLUMES=0
[[ "${1:-}" = "--keep-volumes" ]] && KEEP_VOLUMES=1

# The service overlay requires this value during Compose interpolation, even for
# `down`. Reuse the configured value when possible; otherwise supply a teardown-only
# placeholder so recovery still works after a missing or empty .env entry.
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(env_get POSTGRES_PASSWORD)}"  # pragma: allowlist secret
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-unused-for-compose-down}"  # pragma: allowlist secret
POSTGRES_PASSWORD_URLENCODED="${POSTGRES_PASSWORD_URLENCODED:-$(env_get POSTGRES_PASSWORD_URLENCODED)}"  # pragma: allowlist secret
export POSTGRES_PASSWORD_URLENCODED="${POSTGRES_PASSWORD_URLENCODED:-unused-for-compose-down}"  # pragma: allowlist secret

if [[ "$KEEP_VOLUMES" = "1" ]]; then
  dc down --remove-orphans
  ok "stack stopped (volumes kept — note: dev Vault is in-memory and resets on restart)"
else
  dc down -v --remove-orphans
  ok "stack + volumes removed (Postgres + Vault state cleared)"
fi
log "Dev secrets remain in $ENV_FILE (rotate with: compose-up.sh --regenerate-secrets)"
