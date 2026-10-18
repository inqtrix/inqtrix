import { useEffect, useId, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import {
  fetchAdminRunTraceExport,
  listAdminRunEvents,
  type AdminAuditEvent,
} from '@/api/inqtrixClient'
import { X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { useModalFocusTrap } from '@/components/ui/use-modal-focus-trap'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { appMotion } from '@/motion/transitions'
import { seedAdminRunEvents } from './demo'

type StepEvent = {
  type: string
  sequence: number
  created_at: number
  data: Record<string, unknown>
}

function downloadJson(document_: Record<string, unknown>, name: string) {
  const blob = new Blob([JSON.stringify(document_, null, 2)], {
    type: 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * Right-hand run drawer of the audit panel:
 * metadata header from the audit row, the durable step list, and the
 * trace actions (JSON export; deep link when a trace UI is configured).
 * Works WITHOUT Langfuse — steps come from the durable run events.
 */
export function AuditRunDrawer({
  demo,
  onClose,
  row,
  runId,
  traceUiConfigured,
}: {
  demo: boolean
  onClose: () => void
  row: AdminAuditEvent | null
  runId: string
  traceUiConfigured: boolean
}) {
  const { locale, t } = useLocale()
  const titleId = useId()
  const panelRef = useRef<HTMLElement | null>(null)
  const reduceMotion = Boolean(useReducedMotion())
  const [steps, setSteps] = useState<StepEvent[] | null>(null)
  const [stepsError, setStepsError] = useState<string | null>(null)
  const [traceBusy, setTraceBusy] = useState(false)
  const [traceMessage, setTraceMessage] = useState<string | null>(null)
  const [traceUiUrl, setTraceUiUrl] = useState<string | null>(null)

  useModalFocusTrap({ onClose, open: true, panelRef })

  useEffect(() => {
    let cancelled = false
    if (demo) {
      setSteps(seedAdminRunEvents(runId))
      return () => {
        cancelled = true
      }
    }
    void listAdminRunEvents(runId)
      .then((page) => {
        if (!cancelled) setSteps(page.data)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setStepsError(
            error instanceof Error ? error.message : String(error),
          )
        }
      })
    return () => {
      cancelled = true
    }
  }, [demo, runId])

  const exportTrace = () => {
    setTraceBusy(true)
    setTraceMessage(null)
    void fetchAdminRunTraceExport(runId)
      .then((exported) => {
        downloadJson(exported, `trace-${runId}.json`)
        if (exported.ui_url) setTraceUiUrl(exported.ui_url)
        setTraceMessage(t.adminAudit.traceExported(exported.source))
      })
      .catch((error: unknown) => {
        setTraceMessage(
          error instanceof Error ? error.message : String(error),
        )
      })
      .finally(() => setTraceBusy(false))
  }

  // The deep link is INDEPENDENT of the export: requiring a download
  // first made "open the trace" a two-step detour. The audit row
  // already tells us whether a trace exists; the URL itself comes from
  // the backend (it carries the Langfuse htmlPath, so the UI needs no
  // project id of its own).
  const openTraceUi = () => {
    if (traceUiUrl) {
      window.open(traceUiUrl, '_blank')
      return
    }
    setTraceBusy(true)
    void fetchAdminRunTraceExport(runId)
      .then((exported) => {
        if (exported.ui_url) {
          setTraceUiUrl(exported.ui_url)
          window.open(exported.ui_url, '_blank')
        } else {
          setTraceMessage(t.adminAudit.traceNoUiUrl)
        }
      })
      .catch((error: unknown) => {
        setTraceMessage(
          error instanceof Error ? error.message : String(error),
        )
      })
      .finally(() => setTraceBusy(false))
  }

  const detailEntries = Object.entries(row?.detail ?? {})

  return (
    <AnimatePresence>
      <motion.div
        animate={{ opacity: 1 }}
        className="fixed bottom-0 left-0 right-0 top-[var(--header-h)] z-40 bg-background/70 backdrop-blur"
        exit={{ opacity: 0 }}
        initial={{ opacity: 0 }}
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) onClose()
        }}
        transition={reduceMotion ? { duration: 0 } : appMotion.panel}
      >
        <motion.section
          aria-labelledby={titleId}
          aria-modal="true"
          animate={{ opacity: 1, x: 0 }}
          className="absolute right-0 top-0 flex h-full w-full flex-col overflow-hidden border-l border-border bg-background shadow-lg sm:w-4/5 sm:max-w-[44rem]"
          exit={{ opacity: 0, x: reduceMotion ? 0 : 16 }}
          initial={{ opacity: 0, x: reduceMotion ? 0 : 16 }}
          ref={panelRef}
          role="dialog"
          tabIndex={-1}
          transition={reduceMotion ? { duration: 0 } : appMotion.panel}
        >
          <header className="flex inqtrix-panel-header shrink-0 items-center justify-between gap-3 border-b border-border px-3">
            <h2
              className="min-w-0 truncate t-section text-foreground"
              id={titleId}
            >
              {t.adminAudit.drawerTitle}
            </h2>
            <button
              aria-label={t.adminAudit.drawerClose}
              className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={onClose}
              type="button"
            >
              <X className="icon-sm" />
            </button>
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="grid gap-2 border-b border-border/65 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="t-mono text-foreground">{runId}</span>
                {row ? (
                  <StatusBadge
                    density="table"
                    label={t.adminAudit.outcomes[row.outcome]}
                    tone={
                      row.outcome === 'success'
                        ? 'success'
                        : row.outcome === 'denied'
                          ? 'warning'
                          : 'destructive'
                    }
                  />
                ) : null}
                {row?.actor_pseudonym ? (
                  <span className="t-meta text-muted-foreground">
                    {row.actor_pseudonym}
                  </span>
                ) : null}
              </div>
              {detailEntries.length > 0 ? (
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {detailEntries.map(([key, value]) => (
                    <span
                      className="t-meta-sm text-muted-foreground"
                      key={key}
                    >
                      {key}: {String(value)}
                    </span>
                  ))}
                </div>
              ) : null}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Button
                  disabled={traceBusy || demo}
                  onClick={exportTrace}
                  size="sm"
                  variant="outline"
                >
                  {t.adminAudit.exportTrace}
                </Button>
                {traceUiConfigured && row?.correlation?.trace_id ? (
                  <Button
                    onClick={openTraceUi}
                    disabled={traceBusy}
                    size="sm"
                    variant="ghost"
                  >
                    {t.adminAudit.openInLangfuse}
                  </Button>
                ) : null}
                {traceMessage ? (
                  <span className="t-meta-sm text-muted-foreground">
                    {traceMessage}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="px-4 py-3">
              <h3 className="t-caption pb-2 text-muted-foreground">
                {t.adminAudit.stepsTitle}
              </h3>
              {stepsError ? (
                <p className="t-meta text-destructive">{stepsError}</p>
              ) : steps === null ? (
                <p className="t-meta text-muted-foreground">
                  {t.adminAudit.stepsLoading}
                </p>
              ) : steps.length === 0 ? (
                <p className="t-meta text-muted-foreground">
                  {t.adminAudit.stepsEmpty}
                </p>
              ) : (
                <ol className="grid gap-1">
                  {steps.map((step) => (
                    <li
                      className="grid grid-cols-[6rem_minmax(0,1fr)] items-baseline gap-3 rounded-md px-2 py-1.5 transition-colors hover:bg-surface/45"
                      key={step.sequence}
                    >
                      <span className="t-mono tabular-nums text-muted-foreground">
                        {new Date(
                          step.created_at * 1000,
                        ).toLocaleTimeString(locale)}
                      </span>
                      <span className="min-w-0">
                        <span className="t-list text-foreground">
                          {step.type}
                        </span>
                        <span className="block truncate t-meta-sm text-muted-foreground">
                          {summarizeStep(step.data)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </div>
        </motion.section>
      </motion.div>
    </AnimatePresence>
  )
}

function summarizeStep(data: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(data)) {
    if (value == null || typeof value === 'object') continue
    parts.push(`${key}=${String(value)}`)
    if (parts.length >= 4) break
  }
  return parts.join(' · ')
}
