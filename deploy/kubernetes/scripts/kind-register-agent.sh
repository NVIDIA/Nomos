#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Register an agent account against the store the council reads, by running the
# accounts CLI in-cluster as a Job on the council image.
#
# This is a thin wrapper: nomos/auth/provisioning.py owns the write sequence and
# the role table, so this script no longer carries its own copy of either. It
# previously inlined a Python snippet against PostgresStore with the permission
# sets duplicated in bash string literals.
#
# Runs in-cluster (no local Python or DB reachability needed). Prints AGENT_TOKEN=<hex>.
#
# Usage: kind-register-agent.sh <agent-id> [council-id]
# Env:   AGENT_ROLE (readonly|developer|operator|admin|demo, default developer),
#        KIND_CLUSTER_NAME NAMESPACE SECRET_NAME COUNCIL_IMAGE
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../scripts/dev-lib.sh
. "$SCRIPT_DIR/../../scripts/dev-lib.sh"

AGENT_ID="${1:?usage: kind-register-agent.sh <agent-id> [council-id]}"
COUNCIL_ID="${2:-council-prod}"
AGENT_ROLE="${AGENT_ROLE:-developer}"
SECRET_NAME="${SECRET_NAME:-nomos-secrets}"
COUNCIL_IMAGE="${COUNCIL_IMAGE:-nomos/council:kind}"
JOB="nomos-register-${AGENT_ID}"

kcn delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true

cat <<EOF2 | kcn apply -f - >/dev/null
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
            - name: COUNCIL_STORAGE_MODE
              value: postgres
            - name: POSTGRES_PASSWORD
              valueFrom: { secretKeyRef: { name: ${SECRET_NAME}, key: POSTGRES_PASSWORD } }
            - name: COUNCIL_DATABASE_URL
              value: "postgresql://nomos:\$(POSTGRES_PASSWORD)@nomos-postgres:5432/nomos"
            # Must match the council's key, or the account's integrity HMAC will
            # not verify and the agent silently fails to authenticate.
            - name: COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY
              valueFrom: { secretKeyRef: { name: ${SECRET_NAME}, key: COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY } }
          command: ["python", "scripts/agent_accounts.py"]
          args: ["create", "--json", "--role", "${AGENT_ROLE}", "--council", "${COUNCIL_ID}", "${AGENT_ID}"]
EOF2

kcn wait --for=condition=complete "job/${JOB}" --timeout=120s >/dev/null
token="$(kcn logs "job/${JOB}" | awk -F'"' '$2 == "token" {print $4; exit}')"
[[ -n "$token" ]] || die "account registration produced no token for '$AGENT_ID'"
printf 'AGENT_TOKEN=%s\n' "$token"
