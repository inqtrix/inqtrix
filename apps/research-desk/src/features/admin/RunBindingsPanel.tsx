import { useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { TranslationDictionary } from '@/i18n/translations'
import {
  type RunBindingGroup,
  runBindingGroups,
  serverContracts,
} from './runBindingsModel'
import { useAdminRunBindings } from './useAdminRunBindings'

type Notice = { tone: 'success' | 'error'; text: string }

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function statusLabel(status: string, t: TranslationDictionary): string {
  return (t.adminSystem.runBindingStatus as Record<string, string>)[status] ?? status
}

/**
 * Open runs per mode, status and workflow version, and the cancel of the runs
 * this server cannot run.
 *
 * Only counts reach this view: the run list of the endpoint stays unread, so
 * no question, title, owner or run id appears. The cancel is the operator's
 * way out after a rollback, when runs bound to a newer version stop every
 * worker that takes them over. It renders on the System page, which only an
 * instance admin reaches; the endpoints check the role again.
 */
export function RunBindingsPanel({ demo }: { demo: boolean }) {
  const { t } = useLocale()
  const { cancel, reload, state } = useAdminRunBindings({ demo, enabled: true })
  const [confirming, setConfirming] = useState<RunBindingGroup | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  // The cancel button may be gone once the counts reload (nothing left to
  // end), so the dialog hands the focus back to the section's heading.
  const headingRef = useRef<HTMLHeadingElement>(null)

  const groups = state.bindings ? runBindingGroups(state.bindings) : []

  const confirm = async (group: RunBindingGroup) => {
    setBusy(true)
    try {
      const outcome = await cancel(group.mode)
      setNotice({
        tone: 'success',
        text: t.adminSystem.runBindingsResult(outcome.cancelled, outcome.cancel_requested),
      })
    } catch (error) {
      setNotice({ tone: 'error', text: t.adminSystem.runBindingsCancelFailed(messageOf(error)) })
    } finally {
      setBusy(false)
      setConfirming(null)
    }
  }

  return (
    <section className="min-w-0 bg-transparent" data-testid="admin-run-bindings">
      <div className="flex items-center justify-between gap-2 border-b border-border/65 px-3 py-2">
        <h3 className="t-section text-foreground" ref={headingRef} tabIndex={-1}>
          {t.adminSystem.runBindingsTitle}
        </h3>
        {demo ? null : (
          <Button
            data-testid="admin-run-bindings-reload"
            disabled={busy || state.status === 'loading'}
            onClick={() => void reload()}
            size="sm"
            type="button"
            variant="ghost"
          >
            {t.adminSystem.runBindingsReload}
          </Button>
        )}
      </div>
      <div className="grid gap-2 py-2">
        <p className="px-3 t-meta text-muted-foreground">{t.adminSystem.runBindingsIntro}</p>
        {state.bindings ? (
          <>
            <div className="grid min-h-9 gap-2 rounded-md px-3 py-2 sm:grid-cols-[220px_minmax(0,1fr)] sm:items-center sm:gap-6">
              <h4 className="t-list text-foreground">{t.adminSystem.runBindingsServerVersions}</h4>
              <div className="flex min-w-0 flex-wrap items-center gap-1.5 sm:justify-end">
                {serverContracts(state.bindings).map(([mode, contract]) => (
                  <StatusBadge
                    density="table"
                    key={mode}
                    label={t.adminSystem.runBindingsModeVersion(mode, contract)}
                    tone="neutral"
                  />
                ))}
              </div>
            </div>
            {groups.length === 0 ? (
              <p className="px-3 py-1 t-meta text-muted-foreground">{t.adminSystem.runBindingsNone}</p>
            ) : (
              groups.map((group) => (
                <RunBindingGroupRow
                  busy={busy}
                  group={group}
                  key={group.mode}
                  onCancel={() => setConfirming(group)}
                  showCancel={!demo}
                />
              ))
            )}
          </>
        ) : (
          <p className="px-3 py-1 t-meta text-muted-foreground">
            {state.status === 'loading' || state.status === 'idle'
              ? t.adminSystem.runBindingsLoading
              : state.error ?? t.adminSystem.runBindingsUnavailable}
          </p>
        )}
        {notice ? (
          <p
            className={
              notice.tone === 'error'
                ? 'px-3 t-meta text-destructive'
                : 'px-3 t-meta text-muted-foreground'
            }
            role="status"
          >
            {notice.text}
          </p>
        ) : null}
      </div>
      {confirming ? (
        <Dialog
          closeLabel={t.adminSystem.runBindingsBack}
          description={confirming.mode}
          onClose={() => setConfirming(null)}
          open
          returnFocusTarget={headingRef.current}
          title={countLabel(
            confirming.outsideRuns,
            t.adminSystem.runBindingsConfirmTitleOne,
            t.adminSystem.runBindingsConfirmTitle,
          )}
        >
          <div className="space-y-4">
            <p className="t-body text-foreground">
              {countLabel(
                confirming.outsideRuns,
                t.adminSystem.runBindingsConfirmBodyOne,
                t.adminSystem.runBindingsConfirmBody,
              ).replaceAll('{mode}', () => confirming.mode)}
            </p>
            <p className="t-meta text-muted-foreground">{t.adminSystem.runBindingsConfirmNote}</p>
            <div className="flex items-center justify-end gap-2 pt-1">
              <Button onClick={() => setConfirming(null)} size="sm" type="button" variant="ghost">
                {t.adminSystem.runBindingsBack}
              </Button>
              <Button
                data-testid="admin-run-bindings-confirm"
                disabled={busy}
                onClick={() => void confirm(confirming)}
                size="sm"
                type="button"
                variant="destructive"
              >
                {t.adminSystem.runBindingsConfirmAction}
              </Button>
            </div>
          </div>
        </Dialog>
      ) : null}
    </section>
  )
}

/**
 * One mode's counts and its cancel. While a cancel runs, the button stays in
 * place, disabled, instead of leaving the layout.
 */
export function RunBindingGroupRow({
  busy,
  group,
  onCancel,
  showCancel,
}: {
  busy: boolean
  group: RunBindingGroup
  onCancel: () => void
  showCancel: boolean
}) {
  const { t } = useLocale()
  const outsideShown = group.rows.some((row) => row.outside && !row.asked)

  return (
    <div className="grid gap-1.5 rounded-md px-3 py-2 transition-colors hover:bg-surface/45">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="t-mono text-foreground">{group.mode}</span>
          <span className="t-meta text-muted-foreground">
            {t.adminSystem.runBindingsServerContract(group.serverContract)}
          </span>
        </div>
        {group.outsideRuns > 0 && showCancel ? (
          <Button
            aria-label={countLabel(
              group.outsideRuns,
              t.adminSystem.runBindingsCancelAriaOne,
              t.adminSystem.runBindingsCancelAria,
            ).replaceAll('{mode}', () => group.mode)}
            data-testid={`admin-run-bindings-cancel-${group.mode}`}
            disabled={busy}
            onClick={onCancel}
            size="sm"
            type="button"
            variant="outline"
          >
            {countLabel(
              group.outsideRuns,
              t.adminSystem.runBindingsCancelOne,
              t.adminSystem.runBindingsCancel,
            )}
          </Button>
        ) : null}
      </div>
      <ul className="grid gap-0.5">
        {group.rows.map((row) => (
          <li
            className="grid grid-cols-[6rem_minmax(0,1fr)_auto] items-center gap-2 t-meta"
            key={`${row.contract}:${row.status}`}
          >
            <span className="t-mono text-foreground">{t.adminSystem.runBindingsVersion(row.contract)}</span>
            <span className="text-muted-foreground">{statusLabel(row.status, t)}</span>
            <span className="flex items-center justify-end gap-1.5 tabular-nums text-foreground">
              {row.outside && !row.asked ? (
                <StatusBadge density="table" label={t.adminSystem.runBindingsOutside} tone="warning" />
              ) : null}
              {countLabel(row.runs, t.adminSystem.runBindingsRunsOne, t.adminSystem.runBindingsRuns)}
            </span>
          </li>
        ))}
      </ul>
      {outsideShown ? (
        <p className="t-hint text-muted-foreground">{t.adminSystem.runBindingsOutsideHint}</p>
      ) : null}
    </div>
  )
}
