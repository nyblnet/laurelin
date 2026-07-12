{{- define "laurelin.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "laurelin.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "laurelin.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "laurelin.labels" -}}
app.kubernetes.io/name: {{ include "laurelin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "laurelin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "laurelin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "laurelin.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
