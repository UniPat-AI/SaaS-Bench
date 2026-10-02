{{- define "run.fullname" -}}
run-{{ .Values.runId | required "runId is required" }}
{{- end -}}

{{- define "run.selectorLabels" -}}
saas-playground.run-id: {{ .Values.runId | required "runId is required" | quote }}
{{- end -}}

{{- define "run.labels" -}}
app.kubernetes.io/managed-by: saas-playground
app.kubernetes.io/part-of: saas-playground
{{ include "run.selectorLabels" . }}
{{- end -}}
