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

KEEP_VOLUMES=0
[[ "${1:-}" = "--keep-volumes" ]] && KEEP_VOLUMES=1

if [[ "$KEEP_VOLUMES" = "1" ]]; then
  dc down --remove-orphans
  ok "stack stopped (volumes kept — note: dev Vault is in-memory and resets on restart)"
else
  dc down -v --remove-orphans
  ok "stack + volumes removed (Postgres + Vault state cleared)"
fi
log "Dev secrets remain in $ENV_FILE (rotate with: compose-up.sh --regenerate-secrets)"
