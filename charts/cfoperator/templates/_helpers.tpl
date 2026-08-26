{{/* Base name: <release>-cfoperator, truncated to the k8s 63-char limit. */}}
{{- define "cfoperator.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "cfoperator.labels" -}}
app.kubernetes.io/name: cfoperator
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "cfoperator.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Fail early when the two hard requirements are missing (CFOP-30: an LLM
     and a Prometheus URL; notify is deliberately optional — a blank webhook
     leaves that sink off rather than half-configured).

     The LLM check validates the SELECTED backend's credential: a hosted key
     next to the default ollama backend would template fine and then run the
     agent against an empty OLLAMA_URL, which is the worse failure — an
     install that looks configured and cannot think. */}}
{{- define "cfoperator.validate" -}}
{{- if not .Values.prometheus.url -}}
{{- fail "prometheus.url is required — the URL of the Prometheus this agent observes" -}}
{{- end -}}
{{- $backend := .Values.llm.backend -}}
{{- $cred := dict "ollama" .Values.llm.url "anthropic" .Values.llm.anthropicApiKey "groq" .Values.llm.groqApiKey "xai" .Values.llm.xaiApiKey "gemini" .Values.llm.geminiApiKey "deepseek" .Values.llm.deepseekApiKey -}}
{{- if not (hasKey $cred $backend) -}}
{{- fail (printf "llm.backend must be one of %s, got %q" (keys $cred | sortAlpha | join ", ") $backend) -}}
{{- end -}}
{{- if not (get $cred $backend) -}}
{{- if eq $backend "ollama" -}}
{{- fail "llm.backend is ollama but llm.url is empty — set your Ollama/OpenAI-compat endpoint, or pick a hosted backend with its key" -}}
{{- else -}}
{{- fail (printf "llm.backend is %s but its API key (llm.%sApiKey) is empty" $backend $backend) -}}
{{- end -}}
{{- end -}}
{{- if and (eq .Values.profile "remediate") (not .Values.remediate.gitRepo) -}}
{{- fail "profile: remediate requires remediate.gitRepo (owner/repo the executor opens PRs against)" -}}
{{- end -}}
{{- if and (ne .Values.profile "investigate") (ne .Values.profile "remediate") -}}
{{- fail (printf "profile must be investigate or remediate, got %q" .Values.profile) -}}
{{- end -}}
{{- end -}}

{{/* Postgres endpoint — bundled service or external. */}}
{{- define "cfoperator.pgHost" -}}
{{- if .Values.postgres.bundled -}}{{ include "cfoperator.fullname" . }}-postgres{{- else -}}{{ required "postgres.external.host is required when postgres.bundled is false" .Values.postgres.external.host }}{{- end -}}
{{- end -}}
{{- define "cfoperator.pgPort" -}}
{{- if .Values.postgres.bundled -}}5432{{- else -}}{{ .Values.postgres.external.port }}{{- end -}}
{{- end -}}
{{- define "cfoperator.pgDatabase" -}}
{{- if .Values.postgres.bundled -}}{{ .Values.postgres.database }}{{- else -}}{{ .Values.postgres.external.database }}{{- end -}}
{{- end -}}
{{- define "cfoperator.pgUser" -}}
{{- if .Values.postgres.bundled -}}{{ .Values.postgres.username }}{{- else -}}{{ .Values.postgres.external.username }}{{- end -}}
{{- end -}}

{{/* Generated secrets survive `helm upgrade`: reuse the live Secret's value
     when it exists, else the value from values.yaml, else random. */}}
{{- define "cfoperator.keptSecretValue" -}}
{{- $ := index . 0 -}}{{- $key := index . 1 -}}{{- $explicit := index . 2 -}}
{{- $name := printf "%s-generated" (include "cfoperator.fullname" $) -}}
{{- $existing := lookup "v1" "Secret" $.Release.Namespace $name -}}
{{- if $explicit -}}
{{- $explicit -}}
{{- else if and $existing (index $existing.data $key) -}}
{{- index $existing.data $key | b64dec -}}
{{- else -}}
{{- randAlphaNum 40 -}}
{{- end -}}
{{- end -}}

{{/* Env every python workload shares: database + config path. */}}
{{- define "cfoperator.dbEnv" -}}
- name: POSTGRES_HOST
  value: {{ include "cfoperator.pgHost" . | quote }}
- name: POSTGRES_PORT
  value: {{ include "cfoperator.pgPort" . | quote }}
- name: POSTGRES_DB
  value: {{ include "cfoperator.pgDatabase" . | quote }}
- name: POSTGRES_USER
  value: {{ include "cfoperator.pgUser" . | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "cfoperator.fullname" . }}-generated
      key: postgres-password
{{- end -}}

{{/* Wait for Postgres before starting: the agent ensures its own schema, but
     booting against a database that is still coming up would drop the console
     into legacy auth mode until the pod restarts. Same image, no extra pulls. */}}
{{- define "cfoperator.waitForDb" -}}
- name: wait-for-db
  image: {{ include "cfoperator.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - python
    - -c
    - |
      import os, socket, sys, time
      host, port = os.environ["POSTGRES_HOST"], int(os.environ["POSTGRES_PORT"])
      deadline = time.monotonic() + 300
      while time.monotonic() < deadline:
          try:
              socket.create_connection((host, port), timeout=3).close()
              sys.exit(0)
          except OSError:
              time.sleep(2)
      sys.exit(f"postgres {host}:{port} not reachable after 300s")
  env:
{{ include "cfoperator.dbEnv" . | indent 4 }}
{{- end -}}
