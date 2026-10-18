import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { AdminErasurePreview } from '@/api/inqtrixClient'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import type { AccountErasureProgress } from './accountErasure'
import { ErasePreviewSummary, ErasureStatus } from './UsersPanel'

function render(node: ReactNode): string {
  return renderToStaticMarkup(<LocaleProvider initialLocale="de">{node}</LocaleProvider>)
}

const running: AccountErasureProgress = {
  completed: 2,
  error: null,
  operationId: 'del_1',
  phase: 'aggregates_removed',
  state: 'running',
  total: 5,
}

describe('an account being erased shows how far it got', () => {
  it('names the step and the count while it runs', () => {
    const markup = render(<ErasureStatus onRetry={() => undefined} progress={running} />)

    expect(markup).toContain('Wird gelöscht')
    expect(markup).toContain('Inhalte entfernt (2/5)')
  })

  it('names the reason and offers to try again when it failed', () => {
    const markup = render(
      <ErasureStatus
        onRetry={() => undefined}
        progress={{ ...running, error: 'Der Dienst ist nicht erreichbar.', phase: null, state: 'failed' }}
      />,
    )

    expect(markup).toContain('Löschen fehlgeschlagen')
    expect(markup).toContain('Der Dienst ist nicht erreichbar.')
    expect(markup).toContain('Erneut versuchen')
  })

  it('says only that it is being erased when the erasure is not readable here', () => {
    const markup = render(
      <ErasureStatus
        onRetry={() => undefined}
        progress={{ ...running, completed: 0, phase: null, state: 'unknown', total: 0 }}
      />,
    )

    expect(markup).toContain('Wird gelöscht')
    expect(markup).not.toContain('(0/0)')
    expect(markup).not.toContain('Erneut versuchen')
  })
})

describe('the confirmation names what goes with the account and what stays', () => {
  const preview: AdminErasurePreview = {
    blockers: [],
    cleared_rows: 1,
    delegated_rows: 0,
    deleted_rows: 3,
    foreign_rows: 0,
    lines: [],
    reassigned_rows: 0,
    user_id: 'user-1',
  }

  it('names the content deleted outside the database and the edits that stay', () => {
    const markup = render(
      <ErasePreviewSummary preview={{ ...preview, delegated_rows: 4, reassigned_rows: 2 }} />,
    )

    expect(markup).toContain('Dazu werden 4 Elemente samt ihren Daten außerhalb der Datenbank gelöscht')
    expect(markup).toContain('2 Bearbeitungen in Dokumenten anderer Personen bleiben erhalten')
    expect(markup).toContain('im Hintergrund')
  })

  it('leaves out the lines that would count nothing', () => {
    const markup = render(<ErasePreviewSummary preview={preview} />)

    expect(markup).not.toContain('data-testid="erase-delegated"')
    expect(markup).not.toContain('data-testid="erase-reassigned"')
  })
})
