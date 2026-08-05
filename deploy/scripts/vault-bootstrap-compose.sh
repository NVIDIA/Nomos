#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Bootstrap the compose dev Vault for Nomos service-mode signing: transit engine
# + ed25519 keys (nomos-council, nomos-executor) + an AppRole the council and
# executor use to sign/verify. The app supports kubernetes-auth (Kind) OR AppRole;
# compose has no k8s API, so AppRole is the only path.
#
# RUNS INSIDE the vault container (POSIX sh, no heredocs over stdin):
#   dc exec -T -e VAULT_TOKEN=<root> -e TRANSIT_MOUNT=transit vault sh -s < this
# Prints (for the caller to capture into deploy/.env):
#   VAULT_ROLE_ID=<uuid>
#   VAULT_SECRET_ID=<uuid>
set -eu

export VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
: "${VAULT_TOKEN:?VAULT_TOKEN (dev root token) is required}"
TRANSIT="${TRANSIT_MOUNT:-transit}"

# Wait for the dev server to come up + auto-unseal.
i=0
until vault status >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 30 ] && { echo "vault did not become ready" >&2; exit 1; }
  sleep 1
done

# Transit engine + signing keys (idempotent: enabling/creating an existing one is a no-op).
vault secrets enable -path="$TRANSIT" transit >/dev/null 2>&1 || true
vault write -f "$TRANSIT/keys/nomos-council"  type=ed25519 >/dev/null
vault write -f "$TRANSIT/keys/nomos-executor" type=ed25519 >/dev/null

# Policy: sign/verify/read both keys. Write via a file (printf, not a heredoc — the
# script itself arrives on stdin, so a heredoc here would consume the wrong stream).
POLICY_FILE="/tmp/nomos-signer.hcl"
printf '%s\n' \
  "path \"$TRANSIT/sign/nomos-council\"     { capabilities = [\"create\", \"update\"] }" \
  "path \"$TRANSIT/verify/nomos-council\"   { capabilities = [\"create\", \"update\"] }" \
  "path \"$TRANSIT/keys/nomos-council\"     { capabilities = [\"read\"] }" \
  "path \"$TRANSIT/sign/nomos-executor\"    { capabilities = [\"create\", \"update\"] }" \
  "path \"$TRANSIT/verify/nomos-executor\"  { capabilities = [\"create\", \"update\"] }" \
  "path \"$TRANSIT/keys/nomos-executor\"    { capabilities = [\"read\"] }" \
  > "$POLICY_FILE"
vault policy write nomos-signer "$POLICY_FILE" >/dev/null

# AppRole: one role for both council + executor. Long-lived secret-id (dev only).
vault auth enable approle >/dev/null 2>&1 || true
vault write auth/approle/role/nomos-signer \
  token_policies=nomos-signer token_ttl=20m token_max_ttl=1h \
  secret_id_num_uses=0 secret_id_ttl=0 >/dev/null

ROLE_ID="$(vault read -field=role_id auth/approle/role/nomos-signer/role-id)"
SECRET_ID="$(vault write -f -field=secret_id auth/approle/role/nomos-signer/secret-id)"
echo "VAULT_ROLE_ID=$ROLE_ID"
echo "VAULT_SECRET_ID=$SECRET_ID"
