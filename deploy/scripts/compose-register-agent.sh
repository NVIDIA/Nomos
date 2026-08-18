#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Register an agent account against the store the council reads, by running the
# accounts CLI inside the council image via `docker compose run`.
#
# This is a thin wrapper: nomos/auth/provisioning.py owns the write sequence and
# the role table, so this script no longer carries its own copy of either. It
# previously inlined a Python snippet against PostgresStore with the permission
# sets duplicated in bash string literals.
#
# Prints AGENT_TOKEN=<hex>.
#
# Usage: compose-register-agent.sh <agent-id> [council-id]
# Env:   AGENT_ROLE (readonly|developer|operator|admin|demo, default developer)
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

AGENT_ID="${1:?usage: compose-register-agent.sh <agent-id> [council-id]}"
COUNCIL_ID="${2:-council-prod}"
AGENT_ROLE="${AGENT_ROLE:-developer}"

# The council image carries COUNCIL_STORAGE_MODE, COUNCIL_DATABASE_URL and
# COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY, which the CLI reads directly — so the
# backend and the signing key are resolved the same way the council resolves
# them. --no-deps since the backends are already up.
# Keep stderr: discarding it turned every failure into a bare exit 1 with no
# output at all, so a wrong backend, an unreachable database or a bad council id
# were indistinguishable from each other and from success.
#
# Keep it in a SEPARATE stream from stdout, though. The token is recovered by
# parsing JSON out of stdout, and diagnostics on stderr — the unsigned-store
# warning, a driver error, compose's own progress lines — share no schema with
# that JSON. Merging them puts attacker- or environment-influenced text into the
# stream a parser trusts.
stdout_file="$(mktemp)"
stderr_file="$(mktemp)"
trap 'rm -f "$stdout_file" "$stderr_file"' EXIT
if ! dc run --rm --no-deps -T \
    --entrypoint python council scripts/agent_accounts.py \
      create --json --role "$AGENT_ROLE" --council "$COUNCIL_ID" "$AGENT_ID" \
    >"$stdout_file" 2>"$stderr_file"; then
  cat "$stderr_file" >&2
  cat "$stdout_file" >&2
  die "account registration failed for '$AGENT_ID'"
fi

token="$(awk -F'"' '$2 == "token" {print $4; exit}' "$stdout_file")"

if [[ -z "$token" ]]; then
  cat "$stderr_file" >&2
  cat "$stdout_file" >&2
  die "account registration produced no token for '$AGENT_ID'"
fi
printf 'AGENT_TOKEN=%s\n' "$token"
