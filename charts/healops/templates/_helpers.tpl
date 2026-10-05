{{/* Names. The engine Service is `healops-engine`: Alertmanager's webhook URL depends on it. */}}
{{- define "healops.engine" -}}healops-engine{{- end }}
{{- define "healops.secret" -}}{{ .Values.existingSecret | default "healops-engine" }}{{- end }}

{{- define "healops.labels" -}}
app.kubernetes.io/name: healops
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
# HealOps never remediates HealOps. The policy file denies it; every object says so as well.
healops.io/remediation: disabled
{{- end }}

{{- define "healops.selector" -}}
app.kubernetes.io/name: healops
app.kubernetes.io/component: {{ . }}
{{- end }}

{{/* Datastore hosts: the in-cluster dev copies, or whatever the values point at. */}}
{{- define "healops.dbHost" -}}
{{- if .Values.devDatastores.enabled }}healops-postgres{{ else }}{{ .Values.database.host }}{{ end }}
{{- end }}
{{- define "healops.redisUrl" -}}
{{- if .Values.devDatastores.enabled }}redis://healops-valkey:6379/0{{ else }}{{ .Values.redis.url }}{{ end }}
{{- end }}
{{- define "healops.s3Endpoint" -}}
{{- if .Values.devDatastores.enabled }}http://healops-s3:8333{{ else }}{{ .Values.s3.endpoint }}{{ end }}
{{- end }}
{{- define "healops.appPassword" -}}
{{- if .Values.devDatastores.enabled }}healops{{ else }}{{ .Values.database.appPassword }}{{ end }}
{{- end }}
{{- define "healops.ownerPassword" -}}
{{- if .Values.devDatastores.enabled }}{{ .Values.devDatastores.postgres.password }}{{ else }}{{ .Values.database.ownerPassword }}{{ end }}
{{- end }}

{{/* The pod-level hardening every HealOps workload shares. */}}
{{- define "healops.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}
{{- define "healops.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
