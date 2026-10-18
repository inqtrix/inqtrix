import type { AdminSystemRuntime } from '@/api/inqtrixClient'
import { Info } from '@/components/icons'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type {
  InqtrixCapabilities,
  InqtrixHealth,
} from '@/features/researchRuns/types'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { TranslationDictionary } from '@/i18n/translations'
import type { ReactNode } from 'react'
import { deriveSystemFeatureRows } from './adminModel'

type FeatureDetail = {
  description: string
  disabled: string
  enabled: string
  label: string
}

type RuntimeStatus = 'idle' | 'loading' | 'ready' | 'error'

/** "knowledge" -> "Knowledge", "hybrid_retrieval" -> "Hybrid Retrieval". */
function humanizeFeature(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

function featureDetailFor(
  key: string,
  t: TranslationDictionary,
): FeatureDetail {
  const details = t.adminSystem.featureDetails as Record<string, FeatureDetail>
  const known = details[key]
  if (known) return known

  const label = humanizeFeature(key)
  return {
    description: t.adminSystem.unknownFeatureDescription(label),
    disabled: t.adminSystem.unknownFeatureDisabled,
    enabled: t.adminSystem.unknownFeatureEnabled,
    label,
  }
}

/**
 * Read-only system view. Health/capabilities stay the public discovery
 * surfaces; runtime details come from the admin-gated system endpoint so the
 * UI does not infer infrastructure from feature flags.
 */
export function SystemStatusPanel({
  capabilities,
  health,
  runtime,
  runtimeError,
  runtimeStatus,
}: {
  capabilities: InqtrixCapabilities | null
  health: InqtrixHealth | null
  runtime: AdminSystemRuntime | null
  runtimeError: string | null
  runtimeStatus: RuntimeStatus
}) {
  const { locale, t } = useLocale()
  const features = deriveSystemFeatureRows(capabilities?.features, runtime).filter(
    (feature) => feature.key !== 'openapi',
  )
  const modelCount = health?.models_catalog?.length ?? 0
  const openapiOn = runtime?.api.openapi ?? capabilities?.features.openapi ?? false

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <SystemStatusSection title={t.adminSystem.identityTitle}>
        <SystemValueRow title={t.adminSystem.authMode}>
          <StatusBadge
            density="table"
            label={health?.auth_mode ?? t.adminSystem.none}
            tone="neutral"
          />
        </SystemValueRow>
        <SystemValueRow title={t.adminSystem.llmProvider}>
          <RuntimeCode value={health?.llm?.provider} />
        </SystemValueRow>
        <SystemValueRow title={t.adminSystem.searchProvider}>
          <RuntimeCode value={health?.search?.provider} />
        </SystemValueRow>
        <SystemValueRow title={t.adminSystem.modelPicker}>
          <span className="t-meta text-muted-foreground">
            {countLabel(modelCount, t.adminSystem.modelPickerCountOne, t.adminSystem.modelPickerCount)}
          </span>
        </SystemValueRow>
      </SystemStatusSection>

      <SystemStatusSection title={t.adminSystem.runtimeTitle}>
        {runtime ? (
          <>
            <SystemValueRow title={t.adminSystem.storageBackend}>
              <RuntimeValueGroup>
                <RuntimeCode value={runtime.storage.backend} />
                <StatusBadge
                  density="table"
                  label={
                    runtime.storage.durable
                      ? t.adminSystem.durableStorage
                      : t.adminSystem.volatileStorage
                  }
                  tone={runtime.storage.durable ? 'success' : 'neutral'}
                />
              </RuntimeValueGroup>
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.runStore}>
              <RuntimeCode value={runtime.runs.store} />
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.runQueue}>
              <RuntimeValueGroup>
                <RuntimeCode value={runtime.runs.queue} />
                <AvailabilityBadge available={runtime.runs.queue_available} />
                <StatusBadge
                  density="table"
                  label={
                    runtime.runs.worker_dispatch
                      ? t.adminSystem.workerDispatch
                      : t.adminSystem.inProcessExecution
                  }
                  tone={runtime.runs.worker_dispatch ? 'brand' : 'neutral'}
                />
                {runtime.runs.worker_dispatch
                  && runtime.runs.queue_consumers === 0 ? (
                  <StatusBadge
                    density="table"
                    label={t.adminSystem.queueNoConsumers}
                    tone="warning"
                  />
                ) : null}
                {runtime.runs.worker_dispatch
                  && runtime.runs.queue_consumers != null ? (
                  <span className="t-meta text-muted-foreground">
                    {countLabel(
                      runtime.runs.queue_consumers,
                      t.adminSystem.queueConsumersOne,
                      t.adminSystem.queueConsumers,
                    )}
                    {runtime.runs.queue_depth == null
                      ? ''
                      : ` · ${countLabel(
                        runtime.runs.queue_depth,
                        t.adminSystem.queueMessagesOne,
                        t.adminSystem.queueMessages,
                      )}`}
                  </span>
                ) : null}
              </RuntimeValueGroup>
            </SystemValueRow>
            {/* Effective concurrency of THIS api process, read from the
                admin-gated runtime endpoint -- the panel's answer to "which
                value actually governs here". The wording names the scope:
                admission vs. execution is decided by the dispatch badge
                above, and the worker fleet's total capacity is unknown to
                the api and deliberately not claimed. */}
            {/* Version-skew guard, same idiom as queue_consumers above: an
                older api image does not publish these fields, and a row
                that would read "undefined" is worse than no row. */}
            {runtime.runs.admission_max_concurrent != null ? (
              <SystemValueRow title={t.adminSystem.runAdmissionLimit}>
                <RuntimeValueGroup>
                  <RuntimeCode
                    value={String(runtime.runs.admission_max_concurrent)}
                  />
                  {runtime.runs.queue_max_size != null ? (
                    <span className="t-meta text-muted-foreground">
                      {t.adminSystem.runQueueSize(runtime.runs.queue_max_size)}
                    </span>
                  ) : null}
                </RuntimeValueGroup>
              </SystemValueRow>
            ) : null}
            {runtime.api.chat_max_concurrent != null ? (
              <SystemValueRow title={t.adminSystem.chatConcurrencyLimit}>
                <RuntimeCode value={String(runtime.api.chat_max_concurrent)} />
              </SystemValueRow>
            ) : null}
            {runtime.api.stream_reader_workers != null ? (
              <SystemValueRow title={t.adminSystem.streamReaderWorkers}>
                <RuntimeCode
                  value={String(runtime.api.stream_reader_workers)}
                />
              </SystemValueRow>
            ) : null}
            {runtime.agents?.checkpointer_pool_size != null ? (
              <SystemValueRow title={t.adminSystem.agentCheckpointerPool}>
                <RuntimeCode
                  value={String(runtime.agents.checkpointer_pool_size)}
                />
              </SystemValueRow>
            ) : null}
          </>
        ) : (
          <RuntimeUnavailable error={runtimeError} status={runtimeStatus} />
        )}
      </SystemStatusSection>

      <SystemStatusSection title={t.adminSystem.filesKnowledgeTitle}>
        {runtime ? (
          <>
            <SystemValueRow title={t.adminSystem.fileStorage}>
              <RuntimeValueGroup>
                <RuntimeCode value={runtime.files.object_store} />
                <AvailabilityBadge available={runtime.files.object_store_available} />
                <span className="t-meta text-muted-foreground">
                  {runtime.files.enabled
                    ? t.adminSystem.fileStorageDetail(
                        runtimeLabel(runtime.files.blob_storage, t),
                        formatBytes(runtime.files.max_file_bytes, locale),
                      )
                    : t.adminSystem.notConfigured}
                </span>
              </RuntimeValueGroup>
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.documentParser}>
              <RuntimeCode value={runtime.files.document_parser} />
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.embeddingProvider}>
              <RuntimeValueGroup>
                <RuntimeCode value={runtime.knowledge.embedding_provider} />
                <span className="t-meta text-muted-foreground">
                  {runtime.knowledge.embedding_model ?? t.adminSystem.notConfigured}
                </span>
              </RuntimeValueGroup>
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.vectorStore}>
              <RuntimeValueGroup>
                <RuntimeCode value={runtime.knowledge.vector_store} />
                <AvailabilityBadge
                  available={runtime.knowledge.vector_store_available}
                />
                <StatusBadge
                  density="table"
                  label={
                    runtime.knowledge.hybrid_retrieval
                      ? t.adminSystem.hybridOn
                      : t.adminSystem.hybridOff
                  }
                  tone={runtime.knowledge.hybrid_retrieval ? 'success' : 'neutral'}
                />
                {runtime.knowledge.sparse ? (
                  <span className="t-meta text-muted-foreground">
                    {runtimeLabel(runtime.knowledge.sparse, t)}
                  </span>
                ) : null}
              </RuntimeValueGroup>
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.reranker}>
              <RuntimeCode value={runtime.knowledge.reranker} />
            </SystemValueRow>
          </>
        ) : (
          <RuntimeUnavailable error={runtimeError} status={runtimeStatus} />
        )}
      </SystemStatusSection>

      <SystemStatusSection title={t.adminSystem.observabilityTitle}>
        {runtime?.observability ? (
          <>
            <SystemValueRow title={t.adminSystem.tracingMode}>
              <RuntimeValueGroup>
                {/* Raw mode code, NOT RuntimeCode: runtimeLabels maps
                    "local" to a storage label from another context. */}
                <span className="t-mono text-foreground">
                  {runtime.observability.tracing}
                </span>
                {runtime.observability.tracing !== 'off' ? (
                  <StatusBadge
                    density="table"
                    label={
                      runtime.observability.tracing_active
                        ? t.adminSystem.tracingActive
                        : t.adminSystem.tracingInactive
                    }
                    tone={
                      runtime.observability.tracing_active
                        ? 'success'
                        : 'warning'
                    }
                  />
                ) : null}
                {runtime.observability.tracing_active
                  && runtime.observability.sample_rate < 1 ? (
                  <span className="t-meta text-muted-foreground">
                    {t.adminSystem.tracingSampleRate(
                      runtime.observability.sample_rate,
                    )}
                  </span>
                ) : null}
              </RuntimeValueGroup>
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.traceContent}>
              <StatusBadge
                density="table"
                label={
                  runtime.observability.content_capture
                    ? t.adminSystem.traceContentOn
                    : t.adminSystem.traceContentOff
                }
                tone={
                  runtime.observability.content_capture ? 'warning' : 'neutral'
                }
              />
            </SystemValueRow>
            <SystemValueRow title={t.adminSystem.traceRetention}>
              <RuntimeValueGroup>
                <span className="t-meta text-muted-foreground">
                  {runtime.observability.retention_days != null
                    ? countLabel(
                        runtime.observability.retention_days,
                        t.adminSystem.traceRetentionDaysOne,
                        t.adminSystem.traceRetentionDays,
                      )
                    : runtime.observability.spool
                      ? t.adminSystem.traceRetentionSpool
                      : t.adminSystem.none}
                </span>
                {/* All three prune jobs (trace/audit/ledger) live in the
                    worker; with durable storage but no worker the configured
                    days describe an intent, not an enforced policy. */}
                {!runtime.observability.retention_enforced
                  && (runtime.observability.retention_days != null
                    || runtime.storage.durable) ? (
                  <StatusBadge
                    density="table"
                    label={t.adminSystem.retentionNotEnforced}
                    tone="warning"
                  />
                ) : null}
              </RuntimeValueGroup>
            </SystemValueRow>
          </>
        ) : (
          <RuntimeUnavailable error={runtimeError} status={runtimeStatus} />
        )}
      </SystemStatusSection>

      <section className="min-w-0 bg-transparent">
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-border/65 px-3 py-2">
          <h3 className="t-section text-foreground">
            {t.adminSystem.featuresTitle}
          </h3>
          <span className="t-caption text-muted-foreground">
            {t.adminUsers.colStatus}
          </span>
        </div>
        <div className="grid gap-1 py-1">
          <FeatureGateRow
            detail={featureDetailFor('openapi', t)}
            featureKey="openapi"
            isOn={openapiOn}
            reasonCode={null}
            state={openapiOn ? 'enabled' : 'disabled'}
          />
          {features.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <p className="t-section text-foreground">
                {t.adminSystem.unavailable}
              </p>
            </div>
          ) : (
            features.map((feature) => (
              <FeatureGateRow
                detail={featureDetailFor(feature.key, t)}
                featureKey={feature.key}
                isOn={feature.on}
                key={feature.key}
                reasonCode={
                  capabilities?.feature_status?.[feature.key]?.reason_code ?? null
                }
                state={
                  capabilities?.feature_status?.[feature.key]?.state
                    ?? (feature.on ? 'enabled' : 'disabled')
                }
              />
            ))
          )}
        </div>
      </section>
    </div>
  )
}

function SystemStatusSection({
  children,
  title,
}: {
  children: ReactNode
  title: string
}) {
  return (
    <section className="min-w-0 bg-transparent">
      <div className="border-b border-border/65 px-3 py-2">
        <h3 className="t-section text-foreground">{title}</h3>
      </div>
      <div className="grid gap-1 py-1">{children}</div>
    </section>
  )
}

function SystemValueRow({
  children,
  title,
}: {
  children: ReactNode
  title: string
}) {
  return (
    <div className="grid min-h-9 gap-2 rounded-md px-3 py-2 transition-colors hover:bg-surface/45 sm:grid-cols-[220px_minmax(0,1fr)] sm:items-center sm:gap-6">
      <h4 className="t-list text-foreground">{title}</h4>
      <div className="min-w-0 sm:text-right">{children}</div>
    </div>
  )
}

function RuntimeValueGroup({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5 sm:justify-end">
      {children}
    </div>
  )
}

function RuntimeCode({ value }: { value: string | null | undefined }) {
  const { t } = useLocale()
  const displayValue = value && value.trim() ? runtimeLabel(value, t) : t.adminSystem.none

  return (
    <span className="t-mono text-foreground">
      {displayValue}
    </span>
  )
}

function AvailabilityBadge({ available }: { available: boolean }) {
  const { t } = useLocale()

  return (
    <StatusBadge
      density="table"
      label={available ? t.adminSystem.reachable : t.adminSystem.unreachable}
      tone={available ? 'success' : 'destructive'}
    />
  )
}

function RuntimeUnavailable({
  error,
  status,
}: {
  error: string | null
  status: RuntimeStatus
}) {
  const { t } = useLocale()
  const message =
    status === 'loading'
      ? t.adminSystem.runtimeLoading
      : error ?? t.adminSystem.runtimeUnavailable

  return (
    <div className="rounded-md px-3 py-3">
      <p className="t-meta text-muted-foreground">{message}</p>
    </div>
  )
}

function FeatureGateRow({
  detail,
  featureKey,
  isOn,
  reasonCode,
  state,
}: {
  detail: FeatureDetail
  featureKey: string
  isOn: boolean
  reasonCode: string | null
  state: 'degraded' | 'disabled' | 'enabled'
}) {
  const { t } = useLocale()
  const statusLabel = state === 'degraded'
    ? t.adminSystem.featureDegraded
    : isOn
      ? t.adminSystem.featureOn
      : t.adminSystem.featureOff

  return (
    <div className="grid min-h-9 grid-cols-[minmax(7rem,13rem)_1.25rem_minmax(0,1fr)_auto] items-center gap-2 rounded-md px-3 py-2 transition-colors hover:bg-surface/45">
      <span className="truncate t-list text-foreground">{detail.label}</span>
      <FeatureInfoTooltip
        detail={detail}
        featureKey={featureKey}
        isOn={isOn}
        reasonCode={reasonCode}
        state={state}
      />
      <span aria-hidden="true" />
      <StatusBadge
        className="min-w-8 justify-center"
        density="table"
        label={statusLabel}
        tone={state === 'degraded' ? 'warning' : isOn ? 'success' : 'neutral'}
      />
    </div>
  )
}

function FeatureInfoTooltip({
  detail,
  featureKey,
  isOn,
  reasonCode,
  state,
}: {
  detail: FeatureDetail
  featureKey: string
  isOn: boolean
  reasonCode: string | null
  state: 'degraded' | 'disabled' | 'enabled'
}) {
  const { t } = useLocale()
  const statusLabel = state === 'degraded'
    ? t.adminSystem.featureDegraded
    : isOn
      ? t.adminSystem.featureOn
      : t.adminSystem.featureOff
  const reason = reasonCode
    ? (t.adminSystem.featureReasons as Record<string, string>)[reasonCode]
    : null

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          aria-label={t.adminSystem.featureInfoLabel(detail.label)}
          className="inline-flex size-5 shrink-0 items-center justify-center rounded-full text-muted-foreground/45 transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          type="button"
        >
          <Info className="icon-sm" />
        </button>
      </TooltipTrigger>
      <TooltipContent
        className="w-80 rounded-md border border-border bg-card p-3 text-left shadow-lg"
        side="right"
        sideOffset={8}
      >
        <div className="grid gap-2.5">
          <div>
            <p className="t-card text-foreground">{detail.label}</p>
            <p className="t-meta-sm text-muted-foreground">
              {t.adminSystem.featureManifestKey(featureKey)}
            </p>
          </div>
          <p className="t-meta-sm leading-relaxed text-muted-foreground">
            {detail.description}
          </p>
          <div className="grid gap-1.5">
            <FeatureInfoLine
              label={t.adminSystem.featureStatus}
              value={statusLabel}
            />
            <FeatureInfoLine
              label={t.adminSystem.featureEffect}
              value={isOn ? detail.enabled : detail.disabled}
            />
            {reason ? (
              <p className="t-meta-sm rounded-md bg-warning-subtle px-2 py-1.5 text-warning">
                {reason}
              </p>
            ) : null}
          </div>
        </div>
      </TooltipContent>
    </Tooltip>
  )
}

function FeatureInfoLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-0.5 rounded-md border border-border bg-surface/50 px-2 py-1.5">
      <span className="t-caption text-muted-foreground/65">{label}</span>
      <span className="t-meta-sm text-foreground">{value}</span>
    </div>
  )
}

function runtimeLabel(value: string, t: TranslationDictionary): string {
  const labels = t.adminSystem.runtimeLabels as Record<string, string>
  return labels[value] ?? value
}

function formatBytes(bytes: number | null, locale: string): string | null {
  if (bytes == null) return null
  if (bytes >= 1_048_576) {
    const mb = bytes / 1_048_576
    return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(mb)} MB`
  }
  return `${new Intl.NumberFormat(locale).format(bytes)} B`
}
