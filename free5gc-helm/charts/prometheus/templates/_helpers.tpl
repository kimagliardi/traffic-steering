{{- define "prometheus.name" -}}
prometheus
{{- end -}}

{{- define "prometheus.chart" -}}
{{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "prometheus.fullname" -}}
{{ printf "%s-%s" .Release.Name "prometheus" }}
{{- end -}}
