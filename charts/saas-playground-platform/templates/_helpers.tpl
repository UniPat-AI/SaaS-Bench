{{- define "pg.name" -}}
{{ .Values.nameOverride | default "saas-playground" }}
{{- end -}}

{{- define "pg.labels" -}}
app.kubernetes.io/name: {{ include "pg.name" . }}
app.kubernetes.io/part-of: saas-playground
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "pg.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pg.name" . }}
{{- end -}}

{{- define "pg.routerName" -}}
{{ include "pg.name" . }}-run-router
{{- end -}}

{{- define "pg.routerLabels" -}}
app.kubernetes.io/name: {{ include "pg.routerName" . }}
app.kubernetes.io/component: run-router
app.kubernetes.io/part-of: saas-playground
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "pg.routerSelectorLabels" -}}
app.kubernetes.io/name: {{ include "pg.routerName" . }}
app.kubernetes.io/component: run-router
{{- end -}}
