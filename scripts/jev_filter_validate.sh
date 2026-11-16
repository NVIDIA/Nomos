#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Validate the Jev pre-council attestation filter against a running Nomos Kind
# deployment. Two tiers, both executed inside the council pod:
#
#   Tier 1 (jev_filter_probe.py) — builds the Jev client from the pod's mounted
#     catalog + JEV_SECRET and evaluates a battery of intents against the live
#     API, asserting adversarial cases trip their expected signals. Proves the
#     in-cluster config/secret/catalog/egress and the filter's raw output.
#
#   Tier 2 (jev_filter_e2e.py) — submits an adversarial intent through the real
#     council_propose_intent MCP tool and reads the attestation back from the
#     ledger's first-class `attestation` slot. Proves the filter runs in the live
#     council flow (after OPA) and its output is persisted to the audit ledger.
#
# Usage:  scripts/jev_filter_validate.sh
# Env:    KIND_CONTEXT (default kind-nomos), KIND_NAMESPACE (default nomos),
#         ENV_FILE (default deploy/.env; supplies NOMOS_AGENT_ID/NOMOS_AUTH_TOKEN),
#         NOMOS_COUNCIL_ID (default council-prod).
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="${KIND_CONTEXT:-kind-nomos}"
NS="${KIND_NAMESPACE:-nomos}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/.env}"
COUNCIL_ID="${NOMOS_COUNCIL_ID:-council-prod}"

kc() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }

POD="$(kc get pods -l app.kubernetes.io/component=council \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[-1:].metadata.name}')"
[[ -n "$POD" ]] || { echo "no running council pod in $CONTEXT/$NS" >&2; exit 1; }
echo "council pod: $POD"

kc cp "$ROOT_DIR/scripts/jev_filter_probe.py" "$POD:/tmp/jev_filter_probe.py" -c council
kc cp "$ROOT_DIR/scripts/jev_filter_e2e.py" "$POD:/tmp/jev_filter_e2e.py" -c council

echo "== Tier 1: in-pod filter probe =="
kc exec "$POD" -c council -- python /tmp/jev_filter_probe.py

echo
echo "== Tier 2: end-to-end submit -> ledger attestation slot =="
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
kc exec "$POD" -c council -- env \
  NOMOS_AGENT_ID="${NOMOS_AGENT_ID:-agent-developer}" \
  NOMOS_AUTH_TOKEN="${NOMOS_AUTH_TOKEN:-}" \
  NOMOS_COUNCIL_ID="$COUNCIL_ID" \
  python /tmp/jev_filter_e2e.py

echo
echo "Jev filter validation complete."
