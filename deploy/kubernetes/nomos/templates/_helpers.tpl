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

{{- define "nomos.mtlsVolume" -}}
{{- if .root.Values.mtls.enabled }}
{{- $secretName := get .root.Values.mtls.identities .component -}}
{{- if not .root.Values.mtls.caSecretName }}{{ fail "mtls.caSecretName is required when mtls.enabled=true" }}{{- end }}
{{- if not $secretName }}{{ fail (printf "mtls.identities.%s is required when mtls.enabled=true" .component) }}{{- end }}
- name: nomos-mtls
  projected:
    defaultMode: 0400
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
