{{- define "helix.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helix.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "helix.caseServiceName" -}}{{ include "helix.fullname" . }}-case-service{{- end -}}
{{- define "helix.engineName" -}}{{ include "helix.fullname" . }}-engine{{- end -}}
{{- define "helix.workerName" -}}{{ include "helix.fullname" . }}-worker{{- end -}}

{{- define "helix.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else if and .Values.mysql .Values.mysql.enabled -}}
mysql+aiomysql://{{ .Values.mysql.auth.username }}:{{ .Values.mysql.auth.password }}@{{ .Release.Name }}-mysql:3306/{{ .Values.mysql.auth.database }}
{{- else if and .Values.mariadb .Values.mariadb.enabled -}}
mysql+aiomysql://{{ .Values.mariadb.auth.username }}:{{ .Values.mariadb.auth.password }}@{{ .Release.Name }}-mariadb:3306/{{ .Values.mariadb.auth.database }}
{{- else -}}
{{ .Values.externalDatabaseUrl }}
{{- end -}}
{{- end -}}

{{- define "helix.databaseBackend" -}}
{{- if .Values.postgresql.enabled -}}
postgresql
{{- else if and .Values.mysql .Values.mysql.enabled -}}
mysql
{{- else if and .Values.mariadb .Values.mariadb.enabled -}}
mariadb
{{- else -}}
{{ .Values.externalDatabaseBackend | default "postgresql" }}
{{- end -}}
{{- end -}}

{{- define "helix.redisUrl" -}}
{{- if .Values.valkey.enabled -}}
redis://{{ .Release.Name }}-valkey-primary:6379/0
{{- else -}}
{{ .Values.externalRedisUrl }}
{{- end -}}
{{- end -}}
{{/* Services extracted from case-service at roadmap 13c-e. */}}
{{- define "helix.analyticsServiceName" -}}{{ include "helix.fullname" . }}-analytics-service{{- end -}}
{{- define "helix.hxgraphServiceName" -}}{{ include "helix.fullname" . }}-hxgraph-service{{- end -}}
{{- define "helix.hxmigrateServiceName" -}}{{ include "helix.fullname" . }}-hxmigrate-service{{- end -}}
{{- define "helix.commerceServiceName" -}}{{ include "helix.fullname" . }}-commerce-service{{- end -}}
{{- define "helix.portalServiceName" -}}{{ include "helix.fullname" . }}-portal-service{{- end -}}
{{- define "helix.userServiceName" -}}{{ include "helix.fullname" . }}-user-service{{- end -}}
{{- define "helix.jobsName" -}}{{ include "helix.fullname" . }}-jobs{{- end -}}

{{/*
The database URL carries a PASSWORD, so it belongs in a Secret rather than in
every Deployment's env block. Rendered inline it is readable by anyone who can
`get deployments` — a far wider group than `get secrets` on most clusters — and
it lands in the helm release history too. `existingSecret` lets an operator
bring their own (from an external secret manager) and have the chart write none.
*/}}
{{- define "helix.dbSecretName" -}}
{{- if .Values.databaseSecret.existingSecret -}}
{{ .Values.databaseSecret.existingSecret }}
{{- else -}}
{{ include "helix.fullname" . }}-db
{{- end -}}
{{- end -}}

{{- define "helix.dbSecretKey" -}}
{{- .Values.databaseSecret.key | default "database-url" -}}
{{- end -}}

{{/*
R5d 6a — the connection pooler (PgBouncer). `pooler.enabled` (default false)
adds a PgBouncer Deployment in front of the bundled PostgreSQL and points every
workload at it through VELARIS_CASE_DATABASE_POOLER_URL. Off = nothing rendered,
every workload connects directly, exactly as before. The direct URL stays in
VELARIS_CASE_DATABASE_URL for migrations and admin jobs.
*/}}
{{- define "helix.poolerName" -}}{{ include "helix.fullname" . }}-pooler{{- end -}}
{{- define "helix.poolerOn" -}}
{{- if (.Values.pooler | default dict).enabled }}true{{ end -}}
{{- end -}}
{{- define "helix.poolerUrl" -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "helix.poolerName" . }}:6432/{{ .Values.postgresql.auth.database }}
{{- end -}}
{{- define "helix.poolerSecretKey" -}}
{{- .Values.databaseSecret.poolerKey | default "pooler-url" -}}
{{- end -}}
{{- define "helix.poolerCommerceUrl" -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "helix.poolerName" . }}:6432/{{ .Values.postgresql.auth.database }}_commerce
{{- end -}}
{{- define "helix.poolerCommerceSecretKey" -}}
{{- .Values.databaseSecret.poolerCommerceKey | default "pooler-commerce-url" -}}
{{- end -}}
{{/*
R5e step 7 — commerce-service's own pool on the same pooler. `optional`: an
existingSecret without the key leaves commerce on the shared pool, as before.
*/}}
{{- define "helix.poolerCommerceEnv" -}}
{{- if include "helix.poolerOn" . }}
- name: VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "helix.dbSecretName" . }}
      key: {{ include "helix.poolerCommerceSecretKey" . }}
      optional: true
{{- end }}
{{- end -}}
{{- define "helix.poolerPortalUrl" -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "helix.poolerName" . }}:6432/{{ .Values.postgresql.auth.database }}_portal
{{- end -}}
{{- define "helix.poolerPortalSecretKey" -}}
{{- .Values.databaseSecret.poolerPortalKey | default "pooler-portal-url" -}}
{{- end -}}
{{/*
R5e step 8 — portal-service's own pool, the same way (`optional` likewise).
*/}}
{{- define "helix.poolerPortalEnv" -}}
{{- if include "helix.poolerOn" . }}
- name: VELARIS_CASE_PORTAL_DATABASE_POOLER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "helix.dbSecretName" . }}
      key: {{ include "helix.poolerPortalSecretKey" . }}
      optional: true
{{- end }}
{{- end -}}
{{- define "helix.poolerUserUrl" -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "helix.poolerName" . }}:6432/{{ .Values.postgresql.auth.database }}_user
{{- end -}}
{{- define "helix.poolerUserSecretKey" -}}
{{- .Values.databaseSecret.poolerUserKey | default "pooler-user-url" -}}
{{- end -}}
{{/*
R5e step 10 — user-service's own pool, the same way (`optional` likewise).
*/}}
{{- define "helix.poolerUserEnv" -}}
{{- if include "helix.poolerOn" . }}
- name: VELARIS_CASE_USER_DATABASE_POOLER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "helix.dbSecretName" . }}
      key: {{ include "helix.poolerUserSecretKey" . }}
      optional: true
{{- end }}
{{- end -}}
{{- define "helix.poolerEnv" -}}
{{- if include "helix.poolerOn" . }}
- name: VELARIS_CASE_DATABASE_POOLER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "helix.dbSecretName" . }}
      key: {{ include "helix.poolerSecretKey" . }}
{{- end }}
{{- end -}}

{{/*
Pod and container hardening, applied to every Velaris workload.

The bundled PostgreSQL and Valkey subcharts have shipped these since day one;
our own six deployments had NOTHING — no `runAsNonRoot`, no dropped
capabilities, privilege escalation allowed — so a container escape had more to
work with than it needed. The images already run as an unprivileged user, so
this states the same thing where the cluster can ENFORCE it, and satisfies the
`restricted` Pod Security Standard.

`readOnlyRootFilesystem` is NOT set here and is off by default: case-service
writes uploaded documents to the container filesystem when local storage is
configured, and turning it on silently breaks that. `values.yaml` carries the
switch with the reason.
*/}}
{{- define "helix.podSecurityContext" -}}
runAsNonRoot: {{ .Values.podSecurity.runAsNonRoot }}
{{- if .Values.podSecurity.runAsUser }}
runAsUser: {{ .Values.podSecurity.runAsUser }}
{{- end }}
{{- if .Values.podSecurity.fsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
{{- end }}
seccompProfile:
  type: {{ .Values.podSecurity.seccompProfile }}
{{- end -}}

{{/*
Mutual TLS to Temporal (worker.temporalTls.secretName — a Secret holding
ca.crt, tls.crt and tls.key). Empty = nothing rendered: plaintext, as before.
*/}}
{{- define "helix.temporalTlsOn" -}}
{{- if (.Values.worker.temporalTls | default dict).secretName }}true{{ end -}}
{{- end -}}
{{- define "helix.temporalTlsEnv" -}}
- name: VELARIS_CASE_TEMPORAL_TLS_CA
  value: /etc/velaris/temporal-tls/ca.crt
- name: VELARIS_CASE_TEMPORAL_TLS_CERT
  value: /etc/velaris/temporal-tls/tls.crt
- name: VELARIS_CASE_TEMPORAL_TLS_KEY
  value: /etc/velaris/temporal-tls/tls.key
{{- with (.Values.worker.temporalTls | default dict).serverName }}
- name: VELARIS_CASE_TEMPORAL_TLS_SERVER_NAME
  value: {{ . | quote }}
{{- end }}
{{- end -}}
{{- define "helix.temporalTlsMount" -}}
volumeMounts:
  - name: temporal-tls
    mountPath: /etc/velaris/temporal-tls
    readOnly: true
{{- end -}}
{{- define "helix.temporalTlsVolume" -}}
volumes:
  - name: temporal-tls
    secret:
      secretName: {{ .Values.worker.temporalTls.secretName | quote }}
      defaultMode: 0440   # owner + the pod's fsGroup; never world
{{- end -}}

{{- define "helix.containerSecurityContext" -}}
allowPrivilegeEscalation: false
privileged: false
readOnlyRootFilesystem: {{ .Values.podSecurity.readOnlyRootFilesystem }}
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
R5d 11b — the read replica, for the services that route reads (case, analytics,
hxgraph, commerce, user). Rendered only when readReplica.url is set (or comes
from an existingSecret): unset = every read on the main, as before.
*/}}
{{- define "helix.readReplicaEnv" -}}
{{- $r := .Values.readReplica | default dict }}
{{- if or $r.url (and $r.fromExistingSecret .Values.databaseSecret.existingSecret) }}
- name: VELARIS_CASE_DATABASE_READ_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "helix.dbSecretName" . }}
      key: {{ .Values.databaseSecret.readKey | default "read-url" }}
{{- end }}
{{- end -}}

{{/* The token signing secret, for every service that signs or checks a
sign-in token. First in each env list, so a value an operator sets in a
service's own `env` still comes later and wins. */}}
{{- define "helix.authSecretEnv" -}}
- name: VELARIS_CASE_AUTH_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.authSecret.existingSecret | default (printf "%s-auth" (include "helix.fullname" .)) }}
      key: {{ .Values.authSecret.key }}
{{- end -}}
