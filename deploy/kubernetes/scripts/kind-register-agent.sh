#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Register an agent account directly into the service-mode PostgreSQL store the
# council reads (mints a developer-role token + grants a council). Uses the
# council image's PostgresStore — the redis-only agent_accounts.py CLI is gated
# off in postgres mode, and this does not require the agent-signup console image.
#
# Usage: kind-register-agent.sh <agent-id> [council-id]
set -Eeuo pipefail

AGENT_ID="${1:?usage: kind-register-agent.sh <agent-id> [council-id]}"
COUNCIL_ID="${2:-council-prod}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"
NAMESPACE="${NAMESPACE:-nomos}"
SECRET_NAME="${SECRET_NAME:-nomos-secrets}"
COUNCIL_IMAGE="${COUNCIL_IMAGE:-nomos/council:kind}"
JOB="nomos-register-${AGENT_ID}"

kc() { kubectl --context "kind-${KIND_CLUSTER_NAME}" -n "$NAMESPACE" "$@"; }

kc delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true

cat <<EOF | kc apply -f -
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
              perms = {"shell:read", "shell:write", "file:read", "file:write"}  # developer
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

kc wait --for=condition=complete "job/${JOB}" --timeout=120s
echo "--- registered '${AGENT_ID}' on council '${COUNCIL_ID}'. Token (shown once): ---"
kc logs "job/${JOB}" | grep '^AGENT_TOKEN='
