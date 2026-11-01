import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { AdminErasurePreview, AdminUser } from '@/api/inqtrixClient'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { translations } from '@/i18n/translations'
import type { AccountErasureProgress } from './accountErasure'
import { ErasePreviewSummary, ErasureStatus, UserStatus, UsersPanel, enableWarningCopy } from './UsersPanel'

// Follows erasures through browser events, which this DOM-less lane has not.
vi.mock('./useAccountErasures', () => ({
  useAccountErasures: () => ({
    observationStopped: false,
    progress: {},
    retry: async () => undefined,
  }),
}))

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

  it('offers to erase the account again when its erasure is not readable here', () => {
    // Never started, or held by another administrator: erasing the account
    // again resumes it or says who holds it. Without the button the row
    // stayed "being erased" for good.
    const markup = render(
      <ErasureStatus
        onRetry={() => undefined}
        progress={{ ...running, completed: 0, phase: null, state: 'unknown', total: 0 }}
      />,
    )

    expect(markup).toContain('Wird gelöscht')
    expect(markup).not.toContain('(0/0)')
    expect(markup).toContain('Erneut versuchen')
    expect(markup).not.toContain('Löschen fehlgeschlagen')
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

function listed(partial: Partial<AdminUser>): AdminUser {
  return {
    disabled: false,
    display_name: 'Ada',
    email: 'ada@example.com',
    erasure_operation_id: null,
    id: 'user-1',
    instance_role: 'user',
    last_login_at: null,
    ...partial,
  }
}

describe('the state of an account the identity system manages', () => {
  it('names a tombstone and an erasure that waits for somebody to start it', () => {
    const markup = render(
      <UserStatus user={listed({ deprovisioned: true, disabled: true, erasure_pending: true })} />,
    )

    expect(markup).toContain('Vom IdP gelöscht')
    expect(markup).toContain('Löschung ausstehend')
    expect(markup).not.toContain('Deaktiviert')
  })

  it('says in visible text how a pending erasure starts', () => {
    // A tooltip on a badge reaches neither the keyboard nor a touch screen.
    const hint = translations.de.adminUsers.erasurePendingHint
    const markup = render(
      <UserStatus user={listed({ deprovisioned: true, disabled: true, erasure_pending: true })} />,
    )

    expect(markup).toContain(`>${hint}<`)
    expect(markup).not.toContain('title=')
  })

  it('says who disabled an account', () => {
    expect(render(<UserStatus user={listed({ disabled: true, disabled_source: 'provisioning' })} />)).toContain(
      'durch den Identitätsanbieter',
    )
    expect(render(<UserStatus user={listed({ disabled: true, disabled_source: 'admin' })} />)).not.toContain(
      'durch den Identitätsanbieter',
    )
  })

  it('names the role a tombstone comes back with before it is enabled', () => {
    const copy = enableWarningCopy(
      listed({ deprovisioned: true, disabled: true, instance_role: 'admin' }),
      translations.de,
    )

    expect(copy?.title).toBe('Kennung möglicherweise neu vergeben')
    expect(copy?.description).toContain('„Administrator“')
  })

  it('says that the role stays when the identity provider binds the account again', () => {
    // The role is not held only until the next binding: provisioning never
    // changes a role, so whoever holds the identifier keeps it.
    const tombstone = listed({ deprovisioned: true, disabled: true, instance_role: 'admin' })
    const de = enableWarningCopy(tombstone, translations.de)?.description
    const en = enableWarningCopy(tombstone, translations.en)?.description

    expect(de).toContain('mit seinen Daten und der Rolle „Administrator“ zurück')
    expect(de).toContain('wer die Kennung jetzt trägt, meldet sich damit an.')
    expect(de).toContain('Die Rolle bleibt, auch wenn der Identitätsanbieter das Konto wieder bindet.')
    expect(de).not.toContain('bis der Identitätsanbieter')
    expect(en).toContain('The role stays, even when the identity provider binds the account again.')
    expect(en).not.toContain('until the identity provider')
  })

  it('warns that the next sync may lock an account again, and stays quiet otherwise', () => {
    expect(
      enableWarningCopy(listed({ disabled: true, disabled_source: 'provisioning' }), translations.de)?.description,
    ).toContain('nächste Abgleich')
    expect(enableWarningCopy(listed({ disabled: true, disabled_source: 'admin' }), translations.de)).toBeNull()
  })
})

describe('the role picker of a row', () => {
  it('has a name that says which account it changes', () => {
    // Without one, a screen reader only announced the current value.
    const admin = {
      createUser: async () => undefined,
      reload: async () => undefined,
      resetPassword: async () => undefined,
      setDisabled: async () => undefined,
      setRole: async () => undefined,
      state: {
        available: true,
        demo: true,
        mutationError: null,
        status: 'ready',
        users: [listed({})],
      },
    } as unknown as Parameters<typeof UsersPanel>[0]['admin']
    const markup = render(<UsersPanel admin={admin} mode="local" sessionUserId={null} />)

    expect(markup).toMatch(/<button[^>]*role="combobox"[^>]*aria-label="Rolle: Ada"|<button[^>]*aria-label="Rolle: Ada"[^>]*role="combobox"/)
  })
})
