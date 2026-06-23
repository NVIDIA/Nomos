#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Register an agent account directly into the service-mode PostgreSQL store the
# council reads (mints a token + grants a council). Uses the council image's
# PostgresStore — the redis-only agent_accounts.py CLI is gated off in postgres
# mode, and this needs no agent-signup console image. Prints AGENT_TOKEN=<hex>.
#
# Usage: kind-register-agent.sh <agent-id> [council-id]
# Env:   AGENT_ROLE (readonly|developer|operator, default developer),
#        KIND_CLUSTER_NAME NAMESPACE SECRET_NAME COUNCIL_IMAGE
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=kind-lib.sh
. "$SCRIPT_DIR/kind-lib.sh"

AGENT_ID="${1:?usage: kind-register-agent.sh <agent-id> [council-id]}"
COUNCIL_ID="${2:-council-prod}"
AGENT_ROLE="${AGENT_ROLE:-developer}"
SECRET_NAME="${SECRET_NAME:-nomos-secrets}"
COUNCIL_IMAGE="${COUNCIL_IMAGE:-nomos/council:kind}"
JOB="nomos-register-${AGENT_ID}"

case "$AGENT_ROLE" in
  readonly)  PERMS='{"shell:read", "file:read"}' ;;
  developer) PERMS='{"shell:read", "shell:write", "file:read", "file:write"}' ;;
  operator)  PERMS='{"shell:read", "shell:write", "file:read", "file:write", "infra:deploy"}' ;;
  *) die "AGENT_ROLE must be readonly|developer|operator" ;;
esac

kcn delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true

cat <<EOF | kcn apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: register
          image: ${COUNCIL_IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: AGENT_ID
              value: "${AGENT_ID}"
            - name: COUNCIL_ID
              value: "${COUNCIL_ID}"
            - name: POSTGRES_PASSWORD
              valueFrom: { secretKeyRef: { name: ${SECRET_NAME}, key: POSTGRES_PASSWORD } }
            - name: COUNCIL_DATABASE_URL
              value: "postgresql://nomos:\$(POSTGRES_PASSWORD)@nomos-postgres:5432/nomos"
            - name: COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
              valueFrom: { secretKeyRef: { name: ${SECRET_NAME}, key: COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY } }
          command: ["python", "-c"]
          args:
            - |
              import asyncio, hashlib, os, secrets
              from nomos.store.postgres_store import PostgresStore
              agent = os.environ["AGENT_ID"]; council = os.environ["COUNCIL_ID"]
              token = secrets.token_hex(32)
              th = hashlib.sha256(token.encode()).hexdigest()
              perms = ${PERMS}
              async def main():
                  store = PostgresStore(
                      os.environ["COUNCIL_DATABASE_URL"],
                      account_integrity_key=os.environ["COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY"],
                  )
                  await store.connect()
                  try:
                      await store.set_account(agent, th, agent, True)
                      await store.set_token_index(th, agent)
                      await store.set_account_permissions(agent, perms)
                      await store.grant_account_council(agent, council)
                  finally:
                      await store.close()
                  print("AGENT_TOKEN=" + token, flush=True)
              asyncio.run(main())
EOF

kcn wait --for=condition=complete "job/${JOB}" --timeout=120s >/dev/null
kcn logs "job/${JOB}" | grep '^AGENT_TOKEN='
