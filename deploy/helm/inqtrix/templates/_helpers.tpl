{{/*
Common naming helpers.
*/}}
{{- define "inqtrix.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "inqtrix.fullname" -}}
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

{{- define "inqtrix.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Selector and full label sets. Both take a dict: { "root": $, "component": "api" }.
The component label keeps each Deployment's selector distinct.
*/}}
{{- define "inqtrix.selectorLabels" -}}
{{- $root := .root -}}
app.kubernetes.io/name: {{ include "inqtrix.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
{{- with .component }}
app.kubernetes.io/component: {{ . }}
{{- end }}
{{- end -}}

{{- define "inqtrix.labels" -}}
{{- $root := .root -}}
helm.sh/chart: {{ include "inqtrix.chart" $root }}
{{ include "inqtrix.selectorLabels" . }}
{{- with $root.Chart.AppVersion }}
app.kubernetes.io/version: {{ . | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
app.kubernetes.io/part-of: inqtrix
{{- end -}}

{{- define "inqtrix.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "inqtrix.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- if not .Values.serviceAccount.name -}}
{{- fail "serviceAccount.create=false requires an explicit serviceAccount.name; set name=default only when using the namespace default account is intentional" -}}
{{- end -}}
{{- .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
ServiceAccount for an API or worker workload. A component-specific name may
reference an externally managed ServiceAccount even when create=false. When a
component account is created without an explicit name, use a stable suffix so
cloud identity never leaks onto the shared web/collaboration account.
Takes { "root": $, "component": "api"|"worker" }.
*/}}
{{- define "inqtrix.workloadServiceAccountName" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $workload := index $root.Values.serviceAccount $component -}}
{{- if $workload.name -}}
{{- $workload.name -}}
{{- else if $workload.create -}}
{{- printf "%s-%s" (include "inqtrix.fullname" $root) $component | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "inqtrix.serviceAccountName" $root -}}
{{- end -}}
{{- end -}}

{{/*
Chart-controlled, unannotated accounts for workloads that must never inherit
API/worker cloud identity. Takes { "root": $, "component": string }.
*/}}
{{- define "inqtrix.internalServiceAccountName" -}}
{{- if .root.Values.serviceAccount.create -}}
{{- printf "%s-%s" (include "inqtrix.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "inqtrix.serviceAccountName" .root -}}
{{- end -}}
{{- end -}}

{{/*
Immutable first-party image reference. Takes:
  { "root": $, "component": "api"|"web"|"collaboration" }
Production defaults to fail-loud when a digest is absent. Local development
must opt into tag-only images with image.allowUnpinned=true.
*/}}
{{- define "inqtrix.image.reference" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $image := index $root.Values.image $component -}}
{{- $tag := default $root.Chart.AppVersion $image.tag -}}
{{- $repository := $image.repository -}}
{{- if $root.Values.image.registry -}}
{{- $repository = printf "%s/%s" (trimSuffix "/" $root.Values.image.registry) $repository -}}
{{- end -}}
{{- $digest := default "" $image.digest -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "image.%s.digest must be sha256:<64 lowercase hex characters>, got %q" $component $digest) -}}
{{- end -}}
{{- printf "%s:%s@%s" $repository $tag $digest -}}
{{- else if $root.Values.image.allowUnpinned -}}
{{- printf "%s:%s" $repository $tag -}}
{{- else -}}
{{- fail (printf "image.%s.digest is required for an immutable production image; set the release digest or explicitly set image.allowUnpinned=true for local development" $component) -}}
{{- end -}}
{{- end -}}

{{- define "inqtrix.image.api" -}}
{{- include "inqtrix.image.reference" (dict "root" . "component" "api") -}}
{{- end -}}

{{- define "inqtrix.image.web" -}}
{{- include "inqtrix.image.reference" (dict "root" . "component" "web") -}}
{{- end -}}

{{- define "inqtrix.image.collaboration" -}}
{{- include "inqtrix.image.reference" (dict "root" . "component" "collaboration") -}}
{{- end -}}

{{/*
Name of the Secret carrying the INQTRIX_* secret env: the user-supplied
existingSecret, otherwise the chart-managed Secret (named after the release).
*/}}
{{- define "inqtrix.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- include "inqtrix.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Secret carrying the FastAPI-to-Node collaboration bearer. A dedicated Secret
is optional; otherwise reuse the app Secret but inject only this one key into
the Node container.
*/}}
{{- define "inqtrix.collaborationSecretName" -}}
{{- if .Values.collaboration.secret.existingSecret -}}
{{- .Values.collaboration.secret.existingSecret -}}
{{- else -}}
{{- include "inqtrix.secretName" . -}}
{{- end -}}
{{- end -}}

{{/*
Shared envFrom for the api/worker/migrate pods: the non-secret ConfigMap plus
the Secret (chart-managed or user-supplied existingSecret) when one is present.
*/}}
{{- define "inqtrix.envFrom" -}}
envFrom:
  - configMapRef:
      name: {{ include "inqtrix.fullname" . }}
{{- if or .Values.secret.existingSecret .Values.secret.create }}
  - secretRef:
      name: {{ include "inqtrix.secretName" . }}
{{- end }}
{{- end -}}

{{/*
Tracing env entries (observability.tracing). Rendered into the API AND
worker containers so one trace spans the submit and the execution; the
OTLP headers stay in a Secret because they carry the project keys.
*/}}
{{- define "inqtrix.observabilityEnv" -}}
{{- with (.Values.observability | default dict).tracing }}
{{- if .enabled }}
- name: INQTRIX_TRACING
  value: {{ .mode | default "otlp" | quote }}
{{- if eq (.mode | default "otlp") "otlp" }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ required "observability.tracing.otlpEndpoint is required for mode otlp" .otlpEndpoint | quote }}
{{- end }}
{{- $headersSecret := .headersSecret | default dict }}
{{- if $headersSecret.name }}
- name: OTEL_EXPORTER_OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ $headersSecret.name }}
      key: {{ $headersSecret.key | default "OTEL_EXPORTER_OTLP_HEADERS" }}
{{- end }}
{{- /* No truthiness test here: an explicit sampleRate of 0 (record
       nothing) must render, while nil or the "" default must not. */}}
{{- if and (ne .sampleRate nil) (ne (toString .sampleRate) "") }}
- name: INQTRIX_TRACE_SAMPLE_RATE
  value: {{ .sampleRate | toString | quote }}
{{- end }}
{{- if .uiUrl }}
- name: INQTRIX_TRACE_UI_URL
  value: {{ .uiUrl | quote }}
{{- end }}
{{- /* Same nil-vs-"" guard: retentionDays 0 (job off) must render. */}}
{{- if and (ne .retentionDays nil) (ne (toString .retentionDays) "") }}
- name: INQTRIX_TRACE_RETENTION_DAYS
  value: {{ .retentionDays | toString | quote }}
{{- end }}
{{- if .spoolClaim }}
- name: INQTRIX_TRACE_SPOOL_DIR
  value: "/var/lib/inqtrix/traces"
{{- end }}
{{- if .profile }}
- name: OBSERVABILITY_PROFILE
  value: {{ .profile | quote }}
{{- end }}
{{- if and (ne .maxAttrBytes nil) (ne (toString .maxAttrBytes) "") }}
- name: INQTRIX_TRACE_MAX_ATTR_BYTES
  value: {{ .maxAttrBytes | toString | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- $observability := .Values.observability | default dict }}
{{- if $observability.logFormat }}
- name: INQTRIX_LOG_FORMAT
  value: {{ $observability.logFormat | quote }}
{{- end }}
{{- with $observability.retention }}
{{- /* Same nil-vs-"" guard as the trace knobs: 0 (keep forever) renders,
       an unset value must not. */}}
{{- if and (ne .auditDays nil) (ne (toString .auditDays) "") }}
- name: INQTRIX_AUDIT_RETENTION_DAYS
  value: {{ .auditDays | toString | quote }}
{{- end }}
{{- if and (ne .usageDays nil) (ne (toString .usageDays) "") }}
- name: INQTRIX_USAGE_RETENTION_DAYS
  value: {{ .usageDays | toString | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Shared trace-spool volume entries for mode "file" (see
observability.tracing.spoolClaim in values.yaml). volume/volumeMount pair
rendered into api AND worker; empty when no claim is configured. Fails the
render when file mode runs with a worker but no shared claim — a pod-local
spool would silently hide every worker span from the admin export.
*/}}
{{- define "inqtrix.traceSpoolVolume" -}}
{{- $tracing := (.Values.observability | default dict).tracing | default dict }}
{{- if and $tracing.enabled (eq ($tracing.mode | default "otlp") "file") .Values.worker.enabled (not $tracing.spoolClaim) }}
{{- fail "observability.tracing.mode=file with worker.enabled requires observability.tracing.spoolClaim (an RWX PVC shared by api and worker) — a pod-local spool hides worker spans from the admin trace export" }}
{{- end }}
{{- if $tracing.spoolClaim }}
- name: trace-spool
  persistentVolumeClaim:
    claimName: {{ $tracing.spoolClaim }}
{{- end }}
{{- end -}}

{{- define "inqtrix.traceSpoolVolumeMount" -}}
{{- $tracing := (.Values.observability | default dict).tracing | default dict }}
{{- if $tracing.spoolClaim }}
- name: trace-spool
  mountPath: /var/lib/inqtrix/traces
{{- end }}
{{- end -}}

{{/*
Return one explicitly supplied credential or fail before a workload is
rendered. Empty values and common template placeholders are never valid
runtime credentials. Takes { "name": string, "value": any }.
*/}}
{{- define "inqtrix.requiredCredentialValue" -}}
{{- $name := .name -}}
{{- $value := default "" .value | toString -}}
{{- $normalized := upper (replace "-" "_" $value) -}}
{{- if or (empty $value) (contains "CHANGE_ME" $normalized) -}}
{{- fail (printf "%s must be set to a non-placeholder credential when the bundled service is enabled" $name) -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/*
Effective database URL: an explicit secret.data.INQTRIX_DATABASE_URL wins;
otherwise, when the bundled Postgres is enabled, the in-cluster connection is
derived. Used by both the chart Secret and the migrate hook so they agree.
*/}}
{{- define "inqtrix.databaseUrl" -}}
{{- if hasKey .Values.secret.data "INQTRIX_DATABASE_URL" -}}
{{- .Values.secret.data.INQTRIX_DATABASE_URL -}}
{{- else if .Values.postgres.enabled -}}
{{- $password := include "inqtrix.requiredCredentialValue" (dict "name" "postgres.auth.password" "value" .Values.postgres.auth.password) -}}
{{- $username := default "" .Values.postgres.auth.username | toString -}}
{{- $database := default "" .Values.postgres.auth.database | toString -}}
{{- if not (regexMatch "^[A-Za-z0-9._~-]+$" $password) -}}
{{- fail "postgres.auth.password must use only URL-unreserved characters [A-Za-z0-9._~-] when the bundled Postgres is enabled (it is embedded verbatim into INQTRIX_DATABASE_URL). For an arbitrary password, use an external database and set secret.data.INQTRIX_DATABASE_URL with your own URL-encoding." -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9._~-]+$" $username) -}}
{{- fail "postgres.auth.username must be non-empty and use only URL-unreserved characters [A-Za-z0-9._~-] when bundled Postgres is enabled" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9._~-]+$" $database) -}}
{{- fail "postgres.auth.database must be non-empty and use only URL-unreserved characters [A-Za-z0-9._~-] when bundled Postgres is enabled" -}}
{{- end -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgres:5432/%s" $username $password (include "inqtrix.fullname" .) $database -}}
{{- end -}}
{{- end -}}

{{/*
Effective database URL for the APP (api/worker): an explicit
secret.data.INQTRIX_DATABASE_URL wins; with the bundled PgBouncer enabled the
derived URL points at the pooler (transaction mode) and disables asyncpg's
prepared-statement cache; otherwise it is the direct bundled-Postgres URL.
The migrate hook deliberately keeps "inqtrix.databaseUrl" (DIRECT) -- Alembic
DDL must never run through transaction pooling.
*/}}
{{- define "inqtrix.appDatabaseUrl" -}}
{{- if hasKey .Values.secret.data "INQTRIX_DATABASE_URL" -}}
{{- .Values.secret.data.INQTRIX_DATABASE_URL -}}
{{- else if and .Values.pgbouncer.enabled .Values.postgres.enabled -}}
{{- /* Trigger the shared password-charset guard (single source). */ -}}
{{- $_ := include "inqtrix.databaseUrl" . -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-pgbouncer:6432/%s?prepared_statement_cache_size=0" .Values.postgres.auth.username .Values.postgres.auth.password (include "inqtrix.fullname" .) .Values.postgres.auth.database -}}
{{- else -}}
{{- include "inqtrix.databaseUrl" . -}}
{{- end -}}
{{- end -}}

{{/*
Effective Valkey URL: explicit secret.data.INQTRIX_VALKEY_URL wins; otherwise
derived from the bundled Valkey (password embedded in the URL).
*/}}
{{- define "inqtrix.valkeyUrl" -}}
{{- if hasKey .Values.secret.data "INQTRIX_VALKEY_URL" -}}
{{- .Values.secret.data.INQTRIX_VALKEY_URL -}}
{{- else if .Values.valkey.enabled -}}
{{- $password := include "inqtrix.requiredCredentialValue" (dict "name" "valkey.password" "value" .Values.valkey.password) -}}
{{- if not (regexMatch "^[A-Za-z0-9._~-]+$" $password) -}}
{{- fail "valkey.password must use only URL-unreserved characters [A-Za-z0-9._~-] when the bundled Valkey is enabled (it is embedded verbatim into INQTRIX_VALKEY_URL). For an arbitrary password, use an external Valkey and set secret.data.INQTRIX_VALKEY_URL with your own URL-encoding." -}}
{{- end -}}
{{- printf "redis://:%s@%s-valkey:6379/0" $password (include "inqtrix.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Binary prefix of the BUNDLED broker. The queue speaks the Redis protocol, so the
bundled service runs either engine; only the binary names differ (valkey-server/
valkey-cli vs redis-server/redis-cli). Every server flag the chart passes and the
REDISCLI_AUTH variable the probes use are identical on both. An unknown value
fails the render rather than rendering a container that CrashLoopBackOffs on an
unresolvable command.

Image and engine must be set TOGETHER, but the chart does not name-match the
image to enforce it: verified 2026-08-15, the valkey image ships redis-server/
redis-cli compat symlinks (so engine=redis against it merely mislabels a working
Valkey), while a redis image has no valkey-* at all -- forgetting the engine
after changing the image fails at container start with a self-explanatory
"executable file `valkey-server` not found in $PATH". A registry-path heuristic
would add false render failures for mirrored images without catching more.
*/}}
{{- define "inqtrix.brokerBinary" -}}
{{- $engine := .Values.valkey.engine | default "valkey" -}}
{{- if not (has $engine (list "valkey" "redis")) -}}
{{- fail (printf "valkey.engine must be \"valkey\" or \"redis\", got %q" $engine) -}}
{{- end -}}
{{- $engine -}}
{{- end -}}

{{/*
Pod security context. seccompProfile is always RuntimeDefault, plus a fixed
non-root UID and fsGroup so a mounted PVC is group-owned and writable.
*/}}
{{- define "inqtrix.podSecurityContext" -}}
seccompProfile:
  type: RuntimeDefault
runAsNonRoot: true
runAsUser: {{ .Values.nonRootUid }}
fsGroup: {{ .Values.fsGroup }}
{{- with .Values.podSecurityContext }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Pod security context for a BUNDLED backing service (qdrant/valkey/minio).
seccompProfile is always RuntimeDefault, plus the service's own podSecurityContext
(a fixed non-root UID + fsGroup, so the data volume is owned and writable). Takes a
dict: { "root": $, "svc": .Values.<svc>.podSecurityContext }. The bundled Postgres
does NOT use this -- the official image's initdb looks the UID up in /etc/passwd, so
it stays pinned separately.
*/}}
{{- define "inqtrix.bundledPodSecurityContext" -}}
seccompProfile:
  type: RuntimeDefault
{{- with .svc }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Effective object-store backend: an explicit config/extraConfig value wins
(config first, matching the ConfigMap merge order), else s3 when the bundled
MinIO is enabled, else local. One source for the ConfigMap, the topology
checks, the volume wiring and NOTES. `default ""` keeps an all-empty coalesce
from turning into the string "<nil>".
*/}}
{{- define "inqtrix.objectStoreBackend" -}}
{{- $explicit := coalesce .Values.config.INQTRIX_OBJECT_STORE_BACKEND .Values.extraConfig.INQTRIX_OBJECT_STORE_BACKEND "" | default "" | toString -}}
{{- if $explicit -}}
{{- $explicit -}}
{{- else -}}
{{- ternary "s3" "local" .Values.s3.enabled -}}
{{- end -}}
{{- end -}}

{{/*
Highest number of API pods the release can run at once: the static
replicaCount, or the autoscaler ceiling when autoscaling is enabled.
*/}}
{{- define "inqtrix.apiReplicaCeiling" -}}
{{- if .Values.api.autoscaling.enabled -}}
{{- int .Values.api.autoscaling.maxReplicas -}}
{{- else -}}
{{- int .Values.api.replicaCount -}}
{{- end -}}
{{- end -}}

{{/*
Writable volumes/mounts for the api and worker pods under a read-only root
filesystem: scratch /tmp, the XDG cache,
/app/logs (so INQTRIX_LOG_ENABLED=true does not crash on the read-only root --
note these logs are ephemeral; container stdout is the durable sink), and the
local object store. The object store mounts at the SAME path the app writes to
(config.INQTRIX_OBJECT_STORE_PATH) so the two can never drift. Only the local
backend reads that path, so the persistence PVC is mounted only with the local
backend; every other case gets an emptyDir. A ReadWriteOnce PVC mounted by the
API and a worker on different nodes would leave one pod Pending.
*/}}
{{- define "inqtrix.appWritableVolumes" -}}
- name: tmp
  emptyDir: {}
- name: cache
  emptyDir: {}
- name: logs
  emptyDir: {}
- name: objectstore
{{- if and .Values.persistence.enabled (eq (include "inqtrix.objectStoreBackend" .) "local") }}
  persistentVolumeClaim:
    claimName: {{ include "inqtrix.fullname" . }}-objectstore
{{- else }}
  emptyDir: {}
{{- end }}
{{- if .Values.s3.caBundle.existingConfigMap }}
- name: object-store-ca
  configMap:
    name: {{ .Values.s3.caBundle.existingConfigMap }}
    items:
      - key: {{ .Values.s3.caBundle.key }}
        path: ca.crt
{{- end }}
{{- end -}}

{{- define "inqtrix.appWritableVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: cache
  mountPath: /app/.cache
- name: logs
  mountPath: /app/logs
- name: objectstore
  mountPath: {{ .Values.config.INQTRIX_OBJECT_STORE_PATH | default "/var/lib/inqtrix/objects" }}
{{- if .Values.s3.caBundle.existingConfigMap }}
- name: object-store-ca
  mountPath: {{ .Values.s3.caBundle.mountPath }}
  subPath: ca.crt
  readOnly: true
{{- end }}
{{- end -}}

{{/*
The Python web gateway writes no application state. /tmp is its only writable
path under a read-only root filesystem.
*/}}
{{- define "inqtrix.webWritableVolumes" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "inqtrix.webWritableVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}
