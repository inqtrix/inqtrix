import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { AdminRunBindings } from '@/api/inqtrixClient'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { translations } from '@/i18n/translations'
import { RunBindingGroupRow, RunBindingsPanel } from './RunBindingsPanel'
import type { AdminRunBindingsState } from './useAdminRunBindings'

const hook = vi.hoisted(() => ({
  state: { bindings: null, error: null, status: 'idle' } as AdminRunBindingsState,
}))

// Reads and cancels over the network, which this DOM-less lane has not.
vi.mock('./useAdminRunBindings', () => ({
  useAdminRunBindings: () => ({
    cancel: async () => ({ mode: '', server_contract: null, cancelled: 0, cancel_requested: 0 }),
    reload: async () => undefined,
    state: hook.state,
  }),
}))

const t = translations.de

function render(bindings: AdminRunBindings | null, { demo = false } = {}): string {
  hook.state = bindings
    ? { bindings, error: null, status: 'ready' }
    : { bindings: null, error: null, status: 'loading' }
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <RunBindingsPanel demo={demo} />
    </LocaleProvider>,
  )
}

const afterRollback: AdminRunBindings = {
  contracts: { agent_kernel: 1, research: 1 },
  counts: [
    { mode: 'agent_kernel', status: 'queued', workflow_contract: 2, runs: 2 },
    { mode: 'agent_kernel', status: 'running', workflow_contract: 1, runs: 1 },
    { mode: 'research', status: 'queued', workflow_contract: 1, runs: 3 },
  ],
}

describe('the admin view of open runs per version', () => {
  it('offers to end only the runs this server cannot run, per mode', () => {
    const markup = render(afterRollback)

    expect(markup).toContain('data-testid="admin-run-bindings-cancel-agent_kernel"')
    expect(markup).toContain('2 Läufe beenden')
    expect(markup).not.toContain('admin-run-bindings-cancel-research')
    expect(markup.split(t.adminSystem.runBindingsOutside)).toHaveLength(2)
    expect(markup).toContain('Dieser Server: Version 1')
    expect(markup).toContain('agent_kernel · Version 1')
    expect(markup).toContain('In der Warteschlange')
  })

  it('says it shows counts only, and shows nothing of a run but its counts', () => {
    // A payload that carries more than counts must still show only counts.
    const withRuns = {
      ...afterRollback,
      runs: [{ run_id: 'run_0f3a9c', question: 'Vertrauliche Frage des Nutzers' }],
    } as unknown as AdminRunBindings
    const markup = render(withRuns)

    expect(markup).toContain(t.adminSystem.runBindingsIntro)
    expect(markup).not.toContain('run_0f3a9c')
    expect(markup).not.toContain('Vertrauliche Frage')
  })

  it('names the mode in each cancel and says in plain text why runs cannot run', () => {
    const markup = render(afterRollback)

    expect(markup).toContain('aria-label="2 Läufe des Modus agent_kernel beenden"')
    expect(markup).toContain(`>${t.adminSystem.runBindingsOutsideHint}<`)
  })

  it('offers a refresh of the counts', () => {
    expect(render(afterRollback)).toContain('data-testid="admin-run-bindings-reload"')
  })

  it('counts runs already asked to stop apart and does not offer them again', () => {
    const asked = {
      contracts: { agent_kernel: 1 },
      counts: [{ mode: 'agent_kernel', status: 'cancel_requested', workflow_contract: 2, runs: 2 }],
      runs: [],
      runs_total: 0,
      truncated: false,
    } as unknown as AdminRunBindings
    const markup = render(asked)

    expect(markup).toContain(t.adminSystem.runBindingStatus.cancel_requested)
    expect(markup).not.toContain('admin-run-bindings-cancel-agent_kernel')
    expect(markup).not.toContain(t.adminSystem.runBindingsOutside)
  })

  it('keeps the cancel in place, disabled, while a cancel runs', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <RunBindingGroupRow
          busy
          group={{
            mode: 'agent_kernel',
            outsideRuns: 2,
            rows: [{ asked: false, contract: 2, outside: true, runs: 2, status: 'queued' }],
            serverContract: 1,
          }}
          onCancel={() => undefined}
          showCancel
        />
      </LocaleProvider>,
    )

    expect(markup).toMatch(/<button[^>]*data-testid="admin-run-bindings-cancel-agent_kernel"[^>]*disabled=""/)
  })

  it('offers no cancel in the demo', () => {
    const markup = render(afterRollback, { demo: true })

    expect(markup).not.toContain('admin-run-bindings-cancel-')
  })

  it('says when no run is open, and while it loads', () => {
    expect(render({ contracts: { research: 1 }, counts: [] })).toContain('Keine offenen Läufe.')
    expect(render(null)).toContain('Offene Läufe werden geladen.')
  })
})
