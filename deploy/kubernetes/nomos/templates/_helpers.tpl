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

{{- define "nomos.image" -}}
{{- $root := .root -}}
{{- $image := index $root.Values.images .name -}}
{{- printf "%s:%s" $image.repository ($image.tag | default $root.Chart.AppVersion) -}}
{{- end -}}

{{- define "nomos.pullPolicy" -}}
{{- $root := .root -}}
{{- $image := index $root.Values.images .name -}}
{{- default $root.Values.global.imagePullPolicy $image.pullPolicy -}}
{{- end -}}

{{- define "nomos.commonPodFields" -}}
serviceAccountName: {{ include "nomos.serviceAccountName" . }}
{{- with .Values.global.nodeSelector }}
nodeSelector:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.global.affinity }}
affinity:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .Values.global.tolerations }}
tolerations:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{- define "nomos.secretKeyRef" -}}
secretKeyRef:
  name: {{ include "nomos.secretName" .root }}
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
