{{- define "copilot.name" -}}
postmortem-copilot
{{- end -}}

{{- define "copilot.labels" -}}
app.kubernetes.io/name: {{ include "copilot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
