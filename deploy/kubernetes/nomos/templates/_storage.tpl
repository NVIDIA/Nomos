# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
{{/*
Proposal 07 (PVC-free HA storage) helpers.

storage.mode selects the durable-state architecture: "pvc" keeps today's
PVC-backed file paths; "service" renders the PostgreSQL/object-storage/signer
wiring and no application PVCs. All service-mode env names are provisional
until the Nomos application implements Proposal 07.
*/}}

{{- define "nomos.storageMode" -}}
{{- $mode := default "pvc" (.Values.storage).mode -}}
{{- if not (or (eq $mode "pvc") (eq $mode "service")) -}}
{{- fail "storage.mode must be 'pvc' or 'service'" -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{/*
Secret name for a backend: explicit override value wins, then the component
secret map, then the chart-level secret.
Args: root, component (componentSecrets key), override (explicit secret name).
*/}}
{{- define "nomos.backendSecretName" -}}
{{- if .override -}}
{{- .override -}}
{{- else -}}
{{- include "nomos.componentSecretName" (dict "root" .root "component" .component) -}}
{{- end -}}
{{- end -}}

{{- define "nomos.postgresHost" -}}
{{- printf "%s-postgres" (include "nomos.fullname" .) -}}
{{- end -}}

{{- define "nomos.minioEndpoint" -}}
{{- printf "http://%s-minio:%v" (include "nomos.fullname" .) .Values.objectStorage.dev.service.port -}}
{{- end -}}

{{/*
Database DSN env entries (service mode). The env NAME is per-consumer (council
uses COUNCIL_DATABASE_URL per app-contract-v1); the secret PROPERTY stays
DATABASE_URL. Dev postgres composes the DSN from the chart secret via $(VAR)
interpolation so no password is materialized in the manifest; external
postgres reads the full DSN from the backend secret.
Args: root, urlEnv (env var name).
*/}}
{{- define "nomos.databaseUrlEnv" -}}
{{- $root := .root -}}
{{- if $root.Values.postgres.dev.enabled }}
- name: POSTGRES_PASSWORD
  valueFrom:
    {{- include "nomos.secretKeyRef" (dict "root" $root "key" $root.Values.secrets.keys.postgresPassword) | nindent 4 }}
- name: {{ .urlEnv }}
  value: {{ printf "postgresql://%s:$(POSTGRES_PASSWORD)@%s:%v/%s" $root.Values.postgres.dev.username (include "nomos.postgresHost" $root) $root.Values.postgres.dev.service.port $root.Values.postgres.dev.database | quote }}
{{- else }}
- name: {{ .urlEnv }}
  valueFrom:
    secretKeyRef:
      name: {{ include "nomos.backendSecretName" (dict "root" $root "component" "postgres" "override" $root.Values.postgres.external.secretName) }}
      key: {{ $root.Values.postgres.external.urlKey }}
{{- end }}
{{- end -}}

{{/*
Object storage env entries (service mode, objectStorage.enabled). With the dev
MinIO the endpoint and credentials are auto-wired to the in-cluster service.
Args: root.
*/}}
{{- define "nomos.objectStorageEnv" -}}
{{- $root := .root -}}
{{- $os := $root.Values.objectStorage -}}
- name: NOMOS_OBJECT_STORAGE_PROVIDER
  value: {{ $os.provider | quote }}
- name: NOMOS_OBJECT_STORAGE_BUCKET
  value: {{ $os.bucket | quote }}
{{- if $os.region }}
- name: NOMOS_OBJECT_STORAGE_REGION
  value: {{ $os.region | quote }}
{{- end }}
- name: NOMOS_OBJECT_STORAGE_ENDPOINT
  value: {{ ternary (include "nomos.minioEndpoint" $root) $os.endpoint $os.dev.enabled | quote }}
- name: NOMOS_OBJECT_STORAGE_FORCE_PATH_STYLE
  value: {{ $os.forcePathStyle | quote }}
{{- if $os.dev.enabled }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    {{- include "nomos.secretKeyRef" (dict "root" $root "key" $root.Values.secrets.keys.minioRootUser) | nindent 4 }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    {{- include "nomos.secretKeyRef" (dict "root" $root "key" $root.Values.secrets.keys.minioRootPassword) | nindent 4 }}
{{- else }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "nomos.backendSecretName" (dict "root" $root "component" "objectStorage" "override" $os.secretName) }}
      key: {{ $os.accessKeyIdKey }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "nomos.backendSecretName" (dict "root" $root "component" "objectStorage" "override" $os.secretName) }}
      key: {{ $os.secretAccessKeyKey }}
{{- end }}
{{- end -}}

{{/*
Signer env entries (service mode). Pods never receive private key bytes:
kubernetes auth uses the projected token volume (see nomos.vaultTokenVolume);
approle reads role/secret ids from the signer secret.
Args: root, keyIdEnv (env var name), keyId (value).
*/}}
{{- define "nomos.signerEnv" -}}
{{- $root := .root -}}
{{- $s := $root.Values.signing -}}
- name: NOMOS_SIGNER_PROVIDER
  value: {{ $s.provider | quote }}
- name: {{ .keyIdEnv }}
  value: {{ .keyId | quote }}
{{- if eq $s.provider "vault" }}
- name: NOMOS_VAULT_ADDR
  value: {{ $s.vault.addr | quote }}
{{- if $s.vault.namespace }}
- name: NOMOS_VAULT_NAMESPACE
  value: {{ $s.vault.namespace | quote }}
{{- end }}
- name: NOMOS_VAULT_TRANSIT_MOUNT
  value: {{ $s.vault.transitMount | quote }}
{{- if eq $s.vault.auth.method "kubernetes" }}
{{- /* Names per app-contract-v1. The projected volume places the token FILE
at exactly /var/run/secrets/nomos/vault-token — the app's built-in default
for NOMOS_VAULT_K8S_TOKEN_PATH (P1 @ 1d8fe74). */}}
- name: NOMOS_VAULT_K8S_TOKEN_PATH
  value: /var/run/secrets/nomos/vault-token
- name: NOMOS_VAULT_K8S_ROLE
  value: {{ $s.vault.auth.role | quote }}
{{- else if eq $s.vault.auth.method "approle" }}
- name: VAULT_ROLE_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "nomos.backendSecretName" (dict "root" $root "component" "signer" "override" $s.secretName) }}
      key: {{ $root.Values.secrets.keys.vaultRoleId }}
- name: VAULT_SECRET_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "nomos.backendSecretName" (dict "root" $root "component" "signer" "override" $s.secretName) }}
      key: {{ $root.Values.secrets.keys.vaultSecretId }}
{{- else }}
{{- fail "signing.vault.auth.method must be 'kubernetes' or 'approle'" }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Projected, audience-scoped ServiceAccount token for Vault kubernetes auth.
Deliberately NOT automountServiceAccountToken (forbidden by the hardening
check); the token is scoped to the Vault audience and short-lived.
*/}}
{{- define "nomos.vaultTokenVolumeNeeded" -}}
{{- /* Needed whenever the vault signer uses kubernetes auth — including the
Phase 0 migration jobs, which run while runtime is still in pvc mode. */ -}}
{{- if and (eq .Values.signing.provider "vault") (eq .Values.signing.vault.auth.method "kubernetes") -}}
true
{{- end -}}
{{- end -}}

{{- define "nomos.vaultTokenVolume" -}}
- name: vault-token
  projected:
    sources:
      - serviceAccountToken:
          path: vault-token
          audience: {{ .Values.signing.vault.auth.audience }}
          expirationSeconds: {{ .Values.signing.vault.auth.expirationSeconds }}
{{- end -}}

{{- define "nomos.vaultTokenVolumeMount" -}}
- name: vault-token
  mountPath: /var/run/secrets/nomos
  readOnly: true
{{- end -}}

{{/*
Topology spread constraints; renders nothing at effective replicas <= 1 so the
default single-replica output is unchanged.
Args: root, component (label), config (<comp>.topologySpreadConstraints), replicas.
*/}}
{{- define "nomos.topologySpread" -}}
{{- $root := .root -}}
{{- $cfg := .config -}}
{{- if and $cfg $cfg.enabled (gt (int .replicas) 1) }}
topologySpreadConstraints:
  - maxSkew: {{ $cfg.maxSkew | default 1 }}
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: {{ $cfg.whenUnsatisfiable | default "ScheduleAnyway" }}
    labelSelector:
      matchLabels:
        {{- include "nomos.selectorLabels" $root | nindent 8 }}
        app.kubernetes.io/component: {{ .component }}
  - maxSkew: {{ $cfg.maxSkew | default 1 }}
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "nomos.selectorLabels" $root | nindent 8 }}
        app.kubernetes.io/component: {{ .component }}
{{- end }}
{{- end -}}

{{/*
NetworkPolicy egress for the service-mode backends. Dev backends use pod
selectors; external backends require explicit CIDRs (0.0.0.0/0 is forbidden
by the hardening check — fail loud rather than render an unreachable backend).
*/}}
{{- define "nomos.networkPolicyPostgresEgress" -}}
{{- if .Values.postgres.dev.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "nomos.selectorLabels" . | nindent 10 }}
          app.kubernetes.io/component: postgres
  ports:
    - protocol: TCP
      port: {{ .Values.postgres.dev.service.port }}
{{- else }}
{{- if not .Values.networkPolicy.egress.postgres.cidrs }}
{{- fail "networkPolicy.enabled with storage.mode=service requires networkPolicy.egress.postgres.cidrs (or postgres.dev.enabled)" }}
{{- end }}
- to:
    {{- range .Values.networkPolicy.egress.postgres.cidrs }}
    - ipBlock:
        cidr: {{ . | quote }}
    {{- end }}
  ports:
    - protocol: TCP
      port: {{ .Values.networkPolicy.egress.postgres.port }}
{{- end }}
{{- end -}}

{{- define "nomos.networkPolicyObjectStorageEgress" -}}
{{- if .Values.objectStorage.dev.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "nomos.selectorLabels" . | nindent 10 }}
          app.kubernetes.io/component: minio
  ports:
    - protocol: TCP
      port: {{ .Values.objectStorage.dev.service.port }}
{{- else }}
{{- if not .Values.networkPolicy.egress.objectStorage.cidrs }}
{{- fail "networkPolicy.enabled with objectStorage.enabled requires networkPolicy.egress.objectStorage.cidrs (or objectStorage.dev.enabled)" }}
{{- end }}
- to:
    {{- range .Values.networkPolicy.egress.objectStorage.cidrs }}
    - ipBlock:
        cidr: {{ . | quote }}
    {{- end }}
  ports:
    - protocol: TCP
      port: {{ .Values.networkPolicy.egress.objectStorage.port }}
{{- end }}
{{- end -}}

{{- define "nomos.networkPolicyVaultEgress" -}}
{{- if (.Values.vault).dev.enabled }}
- to:
    - podSelector:
        matchLabels:
          {{- include "nomos.selectorLabels" . | nindent 10 }}
          app.kubernetes.io/component: vault
  ports:
    - protocol: TCP
      port: {{ .Values.vault.dev.service.port }}
{{- else }}
{{- if not .Values.networkPolicy.egress.vault.cidrs }}
{{- fail "networkPolicy.enabled with signing.provider=vault requires networkPolicy.egress.vault.cidrs" }}
{{- end }}
- to:
    {{- range .Values.networkPolicy.egress.vault.cidrs }}
    - ipBlock:
        cidr: {{ . | quote }}
    {{- end }}
  ports:
    - protocol: TCP
      port: {{ .Values.networkPolicy.egress.vault.port }}
{{- end }}
{{- end -}}

{{/*
Init container that blocks until the service-mode backends are ready: PostgreSQL
TCP + an authenticated Vault transit-key read (which proves Vault is up, the
kubernetes-auth role is bound, and the transit key exists). Without this, the
council CrashLoops because it signs the genesis ledger entry during startup.
Args: root, image (images.<name>), keyId (transit key to verify), securityContext.
*/}}
{{- define "nomos.backendWaitInitContainer" -}}
{{- $root := .root -}}
- name: wait-for-backends
  image: {{ include "nomos.image" (dict "root" $root "name" .image) | quote }}
  imagePullPolicy: {{ include "nomos.pullPolicy" (dict "root" $root "name" .image) }}
  command: ["python3", "-c"]
  args:
    - |
      import json, os, socket, sys, time, urllib.request
      addr = os.environ["NOMOS_VAULT_ADDR"].rstrip("/")
      mount = os.environ.get("NOMOS_VAULT_TRANSIT_MOUNT", "transit")
      role = os.environ["NOMOS_VAULT_K8S_ROLE"]
      key = os.environ["NOMOS_WAIT_KEY_ID"]
      tok_path = os.environ.get("NOMOS_VAULT_K8S_TOKEN_PATH", "/var/run/secrets/nomos/vault-token")
      pg_host = os.environ.get("NOMOS_WAIT_PG_HOST", "")
      pg_port = int(os.environ.get("NOMOS_WAIT_PG_PORT", "5432"))

      def vault_ready():
          jwt = open(tok_path).read().strip()
          body = json.dumps({"role": role, "jwt": jwt}).encode()
          login = urllib.request.Request(addr + "/v1/auth/kubernetes/login", data=body, method="POST")
          with urllib.request.urlopen(login, timeout=5) as resp:
              token = json.load(resp)["auth"]["client_token"]
          read = urllib.request.Request(
              "%s/v1/%s/keys/%s" % (addr, mount, key),
              headers={"X-Vault-Token": token}, method="GET")
          urllib.request.urlopen(read, timeout=5).read()
          return True

      def pg_ready():
          if not pg_host:
              return True
          socket.create_connection((pg_host, pg_port), timeout=5).close()
          return True

      deadline = time.time() + 300
      while time.time() < deadline:
          try:
              if pg_ready() and vault_ready():
                  print("backends ready: postgres + vault transit key %r" % key, flush=True)
                  sys.exit(0)
          except Exception as exc:  # noqa: BLE001
              print("waiting for backends: %s" % exc, flush=True)
          time.sleep(3)
      print("timed out waiting for backends", file=sys.stderr)
      sys.exit(1)
  env:
    - name: NOMOS_WAIT_KEY_ID
      value: {{ .keyId | quote }}
    {{- if $root.Values.postgres.dev.enabled }}
    - name: NOMOS_WAIT_PG_HOST
      value: {{ include "nomos.postgresHost" $root | quote }}
    - name: NOMOS_WAIT_PG_PORT
      value: {{ $root.Values.postgres.dev.service.port | quote }}
    {{- end }}
    {{- include "nomos.signerEnv" (dict "root" $root "keyIdEnv" "NOMOS_WAIT_SIGNER_KEYID" "keyId" .keyId) | nindent 4 }}
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  resources:
    requests:
      cpu: 25m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi
  volumeMounts:
    {{- include "nomos.vaultTokenVolumeMount" $root | nindent 4 }}
{{- end -}}

{{/*
Lightweight init container that blocks until PostgreSQL accepts TCP connections.
For components that open a PG pool at startup but do NOT sign (e.g. agent-signup).
Args: root, image (images.<name>).
*/}}
{{- define "nomos.postgresWaitInitContainer" -}}
{{- $root := .root -}}
- name: wait-for-postgres
  image: {{ include "nomos.image" (dict "root" $root "name" .image) | quote }}
  imagePullPolicy: {{ include "nomos.pullPolicy" (dict "root" $root "name" .image) }}
  command: ["python3", "-c"]
  args:
    - |
      import os, socket, sys, time
      host = os.environ["NOMOS_WAIT_PG_HOST"]
      port = int(os.environ.get("NOMOS_WAIT_PG_PORT", "5432"))
      deadline = time.time() + 180
      while time.time() < deadline:
          try:
              socket.create_connection((host, port), timeout=5).close()
              print("postgres ready at %s:%d" % (host, port), flush=True)
              sys.exit(0)
          except Exception as exc:  # noqa: BLE001
              print("waiting for postgres: %s" % exc, flush=True)
          time.sleep(3)
      print("timed out waiting for postgres", file=sys.stderr)
      sys.exit(1)
  env:
    - name: NOMOS_WAIT_PG_HOST
      value: {{ include "nomos.postgresHost" $root | quote }}
    - name: NOMOS_WAIT_PG_PORT
      value: {{ $root.Values.postgres.dev.service.port | quote }}
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  resources:
    requests:
      cpu: 25m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi
{{- end -}}
