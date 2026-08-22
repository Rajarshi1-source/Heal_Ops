{{/* Common labels. `app` is the selector the injectors and chaos tests target by name. */}}
{{- define "chaos.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: chaos-app
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app: {{ .name }}
{{- end }}

{{/* Container spec shared by every fault service. */}}
{{- define "chaos.container" -}}
- name: {{ .svc.name }}
  image: "{{ .root.Values.image.repository }}:{{ .root.Values.image.tag }}"
  imagePullPolicy: {{ .root.Values.image.pullPolicy }}
  args: ["{{ .svc.name }}"]
  ports:
    - name: http
      containerPort: 8080
  env:
    - name: SERVICE
      value: {{ .svc.name | quote }}
    {{- range $k, $v := .svc.env }}
    {{- if $v }}
    - name: {{ $k }}
      value: {{ $v | quote }}
    {{- end }}
    {{- end }}
  resources:
    {{- toYaml .svc.resources | nindent 4 }}
  {{- with .svc.resizePolicy }}
  resizePolicy:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- if .svc.volume }}
  volumeMounts:
    - name: logs
      mountPath: {{ .svc.env.LOG_DIR }}
  {{- end }}
  securityContext:
    allowPrivilegeEscalation: false
    runAsNonRoot: true
    capabilities:
      drop: ["ALL"]
{{- end }}
