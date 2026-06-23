#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Bring up the full Nomos service-mode stack on a local Kind cluster:
#   in-cluster dev Vault (transit) + dev PostgreSQL + redis sentinel + nginx
#   gateway -> stateless council + monitor + executor/openshell + agent-signup
#   + OPA, reachable via nginx-ingress.
#
# Usage:
#   deploy/kubernetes/scripts/kind-up.sh
# Env:
#   KIND_CLUSTER_NAME (default: nomos)   NAMESPACE (default: nomos)
#   HELM_RELEASE (default: nomos)        SKIP_BUILD=1 to reuse already-loaded images
#   NVIDIA_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY): REQUIRED for the council
#     to start — the council validates its LLM-backend config at boot and exits if
#     no key is set. Infra (Vault/Postgres/gateway/OPA) comes up regardless.
set -Eeuo pipefail

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-nomos}"
NAMESPACE="${NAMESPACE:-nomos}"
HELM_RELEASE="${HELM_RELEASE:-nomos}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CHART_DIR="$ROOT_DIR/deploy/kubernetes/nomos"
VALUES_FILE="$ROOT_DIR/deploy/kubernetes/kind-values.yaml"
CLUSTER_CONFIG="$ROOT_DIR/deploy/kubernetes/kind-cluster.yaml"
INGRESS_MANIFEST="https://kind.sigs.k8s.io/examples/ingress/deploy-ingress-nginx.yaml"

kc() { kubectl --context "kind-${KIND_CLUSTER_NAME}" "$@"; }

echo "==> [1/6] Ensure Kind cluster '${KIND_CLUSTER_NAME}'"
if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  kind create cluster --name "$KIND_CLUSTER_NAME" --config "$CLUSTER_CONFIG"
else
  echo "    cluster exists"
fi

echo "==> [2/6] Build + load images"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "    SKIP_BUILD=1 — reusing loaded images"
else
  "$ROOT_DIR/deploy/kubernetes/scripts/build-kind-images.sh" "$KIND_CLUSTER_NAME"
fi

echo "==> [3/6] Install ingress-nginx"
kc apply -f "$INGRESS_MANIFEST"
kc -n ingress-nginx wait --for=condition=Ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s

echo "==> [4/6] helm upgrade --install (service mode)"
# No --wait: the vault-bootstrap post-install hook must run while the council/
# executor init-gates are still waiting on it; --wait would block the council
# readiness ahead of the hook and deadlock. Helm still waits for the hook Job.
helm --kube-context "kind-${KIND_CLUSTER_NAME}" upgrade --install "$HELM_RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" --create-namespace \
  -f "$VALUES_FILE" \
  --set "secrets.stringData.nvidiaApiKey=${NVIDIA_API_KEY:-}" \
  --set "secrets.stringData.anthropicApiKey=${ANTHROPIC_API_KEY:-}" \
  --set "secrets.stringData.openaiApiKey=${OPENAI_API_KEY:-}" \
  --timeout 10m

echo "==> [5/6] Wait for backends + workloads"
kc -n "$NAMESPACE" rollout status statefulset/nomos-postgres --timeout=180s
kc -n "$NAMESPACE" rollout status deployment/nomos-vault --timeout=120s
for d in council council-gateway council-grpc monitor executor-openshell openshell opa agent-signup; do
  kc -n "$NAMESPACE" rollout status "deployment/nomos-${d}" --timeout=300s || true
done

echo "==> [6/6] Done. Endpoints (add to /etc/hosts: '127.0.0.1 mcp.nomos.local monitor.nomos.local signup.nomos.local'):"
echo "    council MCP : http://mcp.nomos.local/mcp     (Bearer = monitorMcpProxyToken)"
echo "    monitor     : http://monitor.nomos.local/"
echo "    signup      : https://signup.nomos.local/    (Basic auth = signupAdmin*)"
echo ""
echo "Register a demo agent into PostgreSQL (service mode) with:"
echo "    deploy/kubernetes/scripts/kind-register-agent.sh <agent-id> [council-id]"
