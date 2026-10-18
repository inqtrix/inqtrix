import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('a refresh after a mutation keeps the table standing', () => {
  // Toggling one switch refetches the whole list. Replacing the table body
  // with a skeleton while that runs would collapse the page height, drop the
  // scroll position to the top, and push the row that was just toggled off
  // screen. The skeleton belongs to the first load, when there is nothing to
  // keep; a refresh has rows and must show them.
  const panels = [
    ['UsersPanel', 'src/features/admin/UsersPanel.tsx', 'rows.length === 0'],
    ['AccessTokensPanel', 'src/features/admin/AccessTokensPanel.tsx', 'state.tokens.length === 0'],
  ]

  it.each(panels)('%s shows the skeleton only when it has nothing to show', (_name, file, emptyCheck) => {
    const source = readFileSync(file as string, 'utf8')
    expect(source).toContain(`{state.status === 'loading' && ${emptyCheck} ? (`)
    // The bare form is what collapses a filled table mid-refresh.
    expect(source).not.toContain("{state.status === 'loading' ? (")
  })
})

describe('a refresh that FAILS keeps the table and says so', () => {
  // The skeleton was only half of it. The load error branch replaced the
  // rows with an empty array, so a transient failure rendered the "no
  // entries" empty state over a directory of 753 accounts — the page
  // collapsed to nothing, the scroll fell to the top again, and the panel
  // showed no error at all: both `loadFailed` strings existed in DE and EN
  // and were rendered nowhere.
  const hooks = [
    ['useAdminUsers', 'src/features/admin/useAdminUsers.ts', 'users: [],'],
    ['usePatTokens', 'src/features/admin/usePatTokens.ts', 'tokens: [],'],
  ]

  it.each(hooks)('%s does not empty its rows on a failed refresh', (_name, file, reset) => {
    const source = readFileSync(file as string, 'utf8')
    const failure = source.slice(source.indexOf('} catch (error) {'))
    expect(failure).toContain("status: 'error',")
    expect(failure).not.toContain(reset as string)
  })

  const panels = [
    ['UsersPanel', 'src/features/admin/UsersPanel.tsx', 't.adminUsers.loadFailed'],
    ['AccessTokensPanel', 'src/features/admin/AccessTokensPanel.tsx', 't.adminTokens.loadFailed'],
  ]

  it.each(panels)('%s renders the load failure it kept the rows through', (_name, file, key) => {
    const source = readFileSync(file as string, 'utf8')
    expect(source).toContain("{state.status === 'error' ? (")
    expect(source).toContain(key as string)
    expect(source).toContain('role="alert"')
  })
})

describe('the erasure confirmation counts before it asks', () => {
  // Deleting an account is the one irreversible action in user
  // management, and its blast radius is invisible: documents, tokens,
  // shares, and the replies colleagues wrote inside those documents. A
  // dialog that only asks "are you sure?" asks for confirmation of
  // something nobody can see.
  const source = readFileSync('src/features/admin/UsersPanel.tsx', 'utf8')
  const dialog = source.slice(source.indexOf('function EraseUserDialog'))

  it('cannot submit before the preview arrived', () => {
    expect(dialog).toContain('const canSubmit = Boolean(preview)')
    // A failed preview means the scope is unknown; deleting on unknown
    // scope is exactly the mistake this dialog exists to prevent.
    expect(dialog).toContain('setPreviewFailed(true)')
    expect(dialog).toContain('eraseCountFailed')
  })

  it('shows what other people lose, on its own line', () => {
    expect(dialog).toContain('preview.foreign_rows > 0')
    expect(dialog).toContain('eraseForeignWarning')
    expect(dialog).toContain('data-testid="erase-foreign-warning"')
  })

  it('requires the address to be typed, not a single click', () => {
    expect(dialog).toContain('typed.trim().toLowerCase()')
    expect(dialog).toContain('confirmPhrase.toLowerCase()')
    expect(dialog).toContain('&& confirmed &&')
  })

  it('refuses with the server reasons instead of a generic error', () => {
    expect(dialog).toContain('preview?.blockers.map')
    expect(dialog).toContain('eraseBlocked')
  })

  it('never offers to erase the signed-in administrator, nor an account already being erased', () => {
    const actions = source.slice(
      source.indexOf('aria-label={t.adminUsers.eraseButton}'),
    )
    expect(actions.slice(0, 400)).toContain('disabled={self || erasing}')
  })
})
