#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Tear down the Nomos Kind dev cluster. Generated dev secrets remain in
# deploy/.env (gitignored) so the next kind-up.sh re-run is identical.
#
# Usage: kind-down.sh        Env: KIND_CLUSTER_NAME (default nomos)
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=kind-lib.sh
. "$SCRIPT_DIR/kind-lib.sh"

if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  kind delete cluster --name "$KIND_CLUSTER_NAME"
  ok "deleted kind cluster '$KIND_CLUSTER_NAME'"
else
  ok "kind cluster '$KIND_CLUSTER_NAME' not present"
fi
log "Dev secrets remain in $ENV_FILE (rotate with: kind-up.sh --regenerate-secrets)"
