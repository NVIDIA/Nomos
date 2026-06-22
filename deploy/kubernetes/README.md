<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary

NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
property and proprietary rights in and to this material, related
documentation and any modifications thereto. Any use, reproduction,
disclosure or distribution of this material and related documentation
without an express license agreement from NVIDIA CORPORATION or
its affiliates is strictly prohibited.
-->

# Nomos Kubernetes Deployment

This directory starts the Kubernetes deployment framework. The first target is
Kind, using native Kubernetes `Secret` objects. The chart also includes a gated
External Secrets template so Vault can later populate the same secret contract
without changing pod specs.

## Layout

```text
deploy/kubernetes/
├── kind-values.yaml
├── nomos/
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── files/
│   │   ├── council/
│   │   ├── executor/
│   │   └── policies/
│   └── templates/
└── scripts/
    └── build-kind-images.sh
```

## Build Images For Kind

```bash
kind create cluster --name nomos
deploy/kubernetes/scripts/build-kind-images.sh
```

The script builds these local images and loads them into the Kind node:

- `nomos/council:kind`
- `nomos/monitor:kind`
- `nomos/executor:kind`
- `nomos/openshell:kind`
- `nomos/agent-signup:kind`
- `nomos/demo:kind`

## Install With Helm-Managed Kubernetes Secrets

```bash
helm upgrade --install nomos deploy/kubernetes/nomos \
  --namespace nomos --create-namespace \
  -f deploy/kubernetes/kind-values.yaml \
  --set secrets.stringData.nvidiaApiKey="$NVIDIA_API_KEY"
```

The chart creates a `nomos-secrets` Secret by default. For local Kind testing,
that is enough. The key names match the environment variables used by the
Docker Compose deployment.

## Install With A Pre-Created Secret

```bash
kubectl create namespace nomos
kubectl -n nomos create secret generic nomos-secrets \
  --from-literal=REDIS_PASSWORD=nomos-dev \
  --from-literal=NVIDIA_API_KEY="$NVIDIA_API_KEY" \
  --from-literal=COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY=kind-account-integrity-change-me \
  --from-literal=COUNCIL_MANAGEMENT_TOKEN=kind-council-management-change-me \
  --from-literal=MONITOR_INTERNAL_TOKEN=kind-monitor-internal-change-me \
  --from-literal=MONITOR_DASHBOARD_USER=admin \
  --from-literal=MONITOR_DASHBOARD_PASSWORD=admin \
  --from-literal=NOMOS_SIGNUP_ADMIN_USER=admin \
  --from-literal=NOMOS_SIGNUP_ADMIN_PASSWORD=admin

helm upgrade --install nomos deploy/kubernetes/nomos \
  --namespace nomos \
  -f deploy/kubernetes/kind-values.yaml \
  --set secrets.create=false \
  --set secrets.existingSecret=nomos-secrets
```

## Wait And Port Forward

`scripts/deploy_stack.sh` handles these port-forwards automatically for the
default Kind workflow. For manual Helm installs, run:

```bash
kubectl -n nomos wait --for=condition=available deploy/nomos-council --timeout=180s
kubectl -n nomos wait --for=condition=available deploy/nomos-monitor --timeout=180s
kubectl -n nomos wait --for=condition=available deploy/nomos-executor-openshell --timeout=180s
kubectl -n nomos wait --for=condition=available deploy/nomos-demo --timeout=180s

kubectl -n nomos port-forward svc/nomos-monitor 9901:9901
kubectl -n nomos port-forward svc/nomos-executor-openshell 9907:9907
kubectl -n nomos port-forward svc/nomos-agent-signup 9905:9905
kubectl -n nomos port-forward svc/nomos-demo 8000:8000
```

Then use:

- Monitor/MCP: `http://localhost:9901`
- Trusted executor: `http://localhost:9907`
- Signup UI: `https://localhost:9905`
- Demo UI: `https://localhost:8000`

## Vault / External Secrets Hook

For Vault-backed environments, install External Secrets Operator and create a
`SecretStore` or `ClusterSecretStore`. Then enable:

```bash
helm upgrade --install nomos deploy/kubernetes/nomos \
  --namespace nomos --create-namespace \
  -f deploy/kubernetes/kind-values.yaml \
  --set secrets.create=false \
  --set externalSecrets.enabled=true \
  --set externalSecrets.secretStoreRef.name=vault \
  --set externalSecrets.secretStoreRef.kind=ClusterSecretStore \
  --set externalSecrets.remoteKey=nomos/kind
```

The ExternalSecret writes the same `nomos-secrets` Secret consumed by all pods.

## Notes For Stress Testing

- The chart defaults to one replica for stateful services and the OpenShell
  executor pair. That is intentional for Kind and single-node stress tests.
- Executor nonce claiming still goes through Redis, so parallel request stress
  can validate writ uniqueness and execution outcomes.
- `executor-openshell` exposes service port `9907` and targets container port
  `9906`, matching the Compose default public port while preserving the internal
  adapter port.
- `openshell` has only an internal ClusterIP service on port `8080`.
- NetworkPolicies are present but disabled by default because Kind's default
  CNI does not enforce them. Enable with `networkPolicy.enabled=true` on a CNI
  that supports policy enforcement.
