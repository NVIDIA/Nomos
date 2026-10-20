# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
{{/*
Expand chart name.
*/}}
{{- define "nomos.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name.
*/}}
{{- define "nomos.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "nomos.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" -}}
{{- end -}}

{{- define "nomos.labels" -}}
helm.sh/chart: {{ include "nomos.chart" . }}
app.kubernetes.io/name: {{ include "nomos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "nomos.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nomos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "nomos.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "nomos.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "nomos.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "nomos.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "nomos.componentSecretName" -}}
{{- $root := .root -}}
{{- $component := .component | default "" -}}
{{- if and $root.Values.secrets.componentSecrets.enabled $component (get $root.Values.secrets.componentSecrets $component) -}}
{{- get $root.Values.secrets.componentSecrets $component -}}
{{- else -}}
{{- include "nomos.secretName" $root -}}
{{- end -}}
{{- end -}}

{{- define "nomos.image" -}}
{{- $root := .root -}}
{{- $image := index $root.Values.images .name -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository ($image.tag | default $root.Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{- define "nomos.pullPolicy" -}}
{{- $root := .root -}}
{{- $image := index $root.Values.images .name -}}
{{- default $root.Values.global.imagePullPolicy $image.pullPolicy -}}
{{- end -}}

{{- define "nomos.commonPodFields" -}}
{{- $root := . -}}
{{- $component := "" -}}
{{- if hasKey . "root" -}}
{{- $root = .root -}}
{{- $component = .component | default "" -}}
{{- end -}}
{{- if and $root.Values.mtls.enabled $component }}
serviceAccountName: {{ printf "%s-%s" (include "nomos.fullname" $root) $component | trunc 63 | trimSuffix "-" }}
{{- else }}
serviceAccountName: {{ include "nomos.serviceAccountName" $root }}
{{- end }}
automountServiceAccountToken: {{ $root.Values.serviceAccount.automountServiceAccountToken }}
{{- with $root.Values.global.nodeSelector }}
nodeSelector:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with $root.Values.global.affinity }}
affinity:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with $root.Values.global.tolerations }}
tolerations:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{- define "nomos.secretKeyRef" -}}
secretKeyRef:
  name: {{ include "nomos.componentSecretName" (dict "root" .root "component" (.component | default "")) }}
  key: {{ .key }}
  {{- if .optional }}
  optional: true
  {{- end }}
{{- end -}}

{{- define "nomos.redisMode" -}}
{{- $mode := default "sentinel" .Values.redis.topology.mode -}}
{{- if not (or (eq $mode "standalone") (eq $mode "sentinel")) -}}
{{- fail "redis.topology.mode must be 'standalone' or 'sentinel'" -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{- define "nomos.redisSentinelNodes" -}}
{{- printf "%s-redis-sentinel:%v" (include "nomos.fullname" .) .Values.redis.sentinel.service.port -}}
{{- end -}}

{{- define "nomos.networkPolicyRouteIngressPeers" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: ingress-nginx
{{- end -}}

{{- define "nomos.networkPolicyDnsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end -}}

{{- define "nomos.networkPolicyRedisEgress" -}}
- to:
    - podSelector:
        matchLabels:
          {{- include "nomos.selectorLabels" . | nindent 10 }}
          app.kubernetes.io/component: redis
  ports:
    - protocol: TCP
      port: {{ .Values.redis.service.port }}
{{- if eq (include "nomos.redisMode" .) "sentinel" }}
- to:
    - podSelector:
        matchLabels:
          {{- include "nomos.selectorLabels" . | nindent 10 }}
          app.kubernetes.io/component: redis-sentinel
  ports:
    - protocol: TCP
      port: {{ .Values.redis.sentinel.service.port }}
{{- end }}
{{- end -}}

{{- define "nomos.mtlsClientEnv" -}}
{{- if .root.Values.mtls.enabled }}
- name: {{ .prefix }}_TLS_ENABLED
  value: "true"
- name: {{ .prefix }}_TLS_CA_FILE
  value: /run/nomos/tls/ca.crt
- name: {{ .prefix }}_TLS_CERT_FILE
  value: /run/nomos/tls/tls.crt
- name: {{ .prefix }}_TLS_KEY_FILE
  value: /run/nomos/tls/tls.key
{{- end }}
{{- end -}}

{{- define "nomos.mtlsListenerEnv" -}}
{{- if .root.Values.mtls.enabled }}
- name: {{ .prefix }}_TLS_ENABLED
  value: "true"
- name: {{ .prefix }}_TLS_CERT_FILE
  value: /run/nomos/tls/tls.crt
- name: {{ .prefix }}_TLS_KEY_FILE
  value: /run/nomos/tls/tls.key
- name: {{ .prefix }}_TLS_CLIENT_CA_FILE
  value: /run/nomos/tls/ca.crt
{{- end }}
{{- end -}}

{{- define "nomos.mtlsVolumeMount" -}}
{{- if .Values.mtls.enabled }}
- name: nomos-mtls
  mountPath: /run/nomos/tls
  readOnly: true
{{- end }}
{{- end -}}

{{/*
Validated projected-file mode for mTLS material.

Kubernetes takes defaultMode as a decimal number, so the octal literal 0440 in
values.yaml arrives here as 288. A mode carrying world bits would expose tls.key
to every process that can read the pod filesystem, so reject it at render time
rather than ship it. A leading zero is read as octal only inside a values file,
never through --set, which is the easy way to turn 0644 into a world-readable
key by accident.
*/}}
{{- define "nomos.mtlsSecretDefaultMode" -}}
{{- $mode := .Values.mtls.secretDefaultMode -}}
{{- if kindIs "string" $mode }}{{ fail (printf "mtls.secretDefaultMode must be a number, got the string %q; pass 288 rather than 0440 when using --set, because a leading zero is only read as octal inside a values file" $mode) }}{{- end }}
{{- $int := int $mode -}}
{{- if or (lt $int 1) (gt $int 511) }}{{ fail (printf "mtls.secretDefaultMode must be between 1 and 511 (0777), got %v" $mode) }}{{- end }}
{{- if ne (mod $int 8) 0 }}{{ fail (printf "mtls.secretDefaultMode=%v would grant world access to tls.key; use a mode with no world bits, for example 288 (0440)" $mode) }}{{- end }}
{{- $int -}}
{{- end -}}

{{- define "nomos.mtlsVolume" -}}
{{- if .root.Values.mtls.enabled }}
{{- $secretName := get .root.Values.mtls.identities .component -}}
{{- if not .root.Values.mtls.caSecretName }}{{ fail "mtls.caSecretName is required when mtls.enabled=true" }}{{- end }}
{{- if not $secretName }}{{ fail (printf "mtls.identities.%s is required when mtls.enabled=true" .component) }}{{- end }}
{{- $mode := include "nomos.mtlsSecretDefaultMode" .root -}}
- name: nomos-mtls
  projected:
    defaultMode: {{ $mode }}
    sources:
      - secret:
          name: {{ .root.Values.mtls.caSecretName }}
          items:
            - key: ca.crt
              path: ca.crt
      - secret:
          name: {{ $secretName }}
          items:
            - key: tls.crt
              path: tls.crt
            - key: tls.key
              path: tls.key
{{- end }}
{{- end -}}
{{/*
Effective monitor stats source. The gateway takes /mcp away from the monitor's
proxy, which leaves the inline registry blind, so an unset value means "redis"
whenever the gateway is enabled. Used by the monitor env and the NetworkPolicy
rules that let the monitor reach redis; they must agree.
*/}}
{{- define "nomos.monitorStatsSource" -}}
{{- .Values.monitor.statsSource | default (ternary "redis" "" .Values.councilGateway.enabled) -}}
{{- end -}}
