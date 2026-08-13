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

# Validate BEFORE interpolating. The heredoc below is unquoted, so these values
# expand straight into a JSON flow sequence inside YAML; a quote, comma or
# bracket would escape the sequence and rewrite the manifest. The CLI's own
# validate_agent_id/validate_council_id run inside the container, long after the
# manifest has been applied, so they cannot guard this path. Same character
# classes as scripts/agent_accounts.py.
require_pattern() {
  local name="$1" value="$2" pattern="$3"
  [[ "$value" =~ $pattern ]] || die "$name contains characters that are not allowed: '$value'"
}
require_pattern "agent-id" "$AGENT_ID" '^[A-Za-z0-9_-]+$'
require_pattern "council-id" "$COUNCIL_ID" '^[A-Za-z0-9_*.-]+$'
require_pattern "AGENT_ROLE" "$AGENT_ROLE" '^[a-z]+$'

# AGENT_ID and Kubernetes object names have different contracts, and conflating
# them fails at `kubectl apply` rather than anywhere useful. An agent id may
# legitimately contain uppercase and underscores and has no length bound, while
# metadata.name is RFC1123: lowercase alphanumeric or '-', at most 63 characters,
# starting and ending alphanumeric. `Agent_1`, `agent-` and any long id all pass
# validation above and are then rejected by the API server.
#
# Derive the Job name instead of reusing the id, keeping AGENT_ID itself intact
# for provisioning. The trailing hash keeps ids that slugify identically (say
# `Agent_1` and `agent-1`) from colliding on one Job.
dns_safe_job_name() {
  local raw="$1" slug hash
  hash="$(printf '%s' "$raw" | sha256sum | cut -c1-8)"
  slug="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-')"
  slug="$(printf '%s' "$slug" | sed -E 's/-+/-/g; s/^-+//; s/-+$//')"
  slug="${slug:0:30}"
  slug="$(printf '%s' "$slug" | sed -E 's/-+$//')"
  [[ -n "$slug" ]] || slug="agent"
  printf 'nomos-register-%s-%s' "$slug" "$hash"
}
JOB="$(dns_safe_job_name "$AGENT_ID")"

kcn delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true

# The Job prints the raw token on stdout, which is the only way to get it back
# out. Delete the Job as soon as it is read so the credential does not sit in
# readable pod logs for the TTL window.
cleanup_job() { kcn delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup_job EXIT

cat <<EOF2 | kcn apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
spec:
  # No retries. `create` is not idempotent and is invoked without --force, so a
  # pod that commits the account and then dies before its JSON reaches stdout
  # leaves a durable, enabled account whose raw token no longer exists anywhere.
  # The retry would then fail with "already exists" and the credential would be
  # unrecoverable. Failing the whole registration is the recoverable outcome.
  backoffLimit: 0
  # Short TTL as a backstop; the trap above deletes the Job as soon as the token
  # has been read, so the log window is normally seconds rather than minutes.
  ttlSecondsAfterFinished: 30
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
