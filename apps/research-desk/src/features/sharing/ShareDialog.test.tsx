import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { ShareDialog } from './ShareDialog'

function dialogMarkup(overrides: {
  initialTab?: 'access' | 'activity' | 'overview'
  recipient?: boolean
  resourceType?: string
} = {}): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <ShareDialog
        demo
        onClose={() => undefined}
        ownerEmail="ada@example.de"
        ownerName="Ada Lovelace"
        refreshToken={0}
        resourceId="editor-document-1"
        resourceTitle="Untitled.md"
        resourceType={overrides.resourceType ?? 'editor_document'}
        {...(overrides.initialTab ? { initialTab: overrides.initialTab } : {})}
        {...(overrides.recipient
          ? {
              onLeave: () => undefined,
              recipientAccess: {
                ownerId: 'owner-1',
                ownerName: 'Grace Hopper',
                permission: 'edit' as const,
              },
            }
          : {})}
      />
    </LocaleProvider>,
  )
}

/** Whether the recipient-search field is present in the selected panel. */
function searchFieldPresent(markup: string): boolean {
  const input = markup.match(/<input[^>]*Personen suchen[^>]*>/)
  return Boolean(input) || markup.includes('Personen suchen')
}

describe('ShareDialog landing tab and focus', () => {
  it('opens sharing intent in the labelled access tab panel', () => {
    const markup = dialogMarkup()
    expect(searchFieldPresent(markup)).toBe(true)
    expect(markup).toContain('role="dialog"')
    expect(markup).toContain('aria-modal="true"')
    expect(markup).toContain('role="tablist"')
    expect(markup).toMatch(/aria-selected="true"[^>]*role="tab"[^>]*tabindex="0"[^>]*>Zugriff/)
    expect(markup).toMatch(/aria-labelledby="[^"]+-access-tab"[^>]*role="tabpanel"/)
  })

  it('opens details intent on the keyboard-active overview tab', () => {
    const markup = dialogMarkup({ initialTab: 'overview' })
    expect(searchFieldPresent(markup)).toBe(false)
    expect(markup).toMatch(/aria-selected="true"[^>]*role="tab"[^>]*tabindex="0"[^>]*>Übersicht/)
    expect(markup).toMatch(/aria-labelledby="[^"]+-overview-tab"[^>]*role="tabpanel"/)
  })

  it('keeps the sharing intent for non-editor resources', () => {
    const markup = dialogMarkup({ resourceType: 'knowledge_collection' })
    expect(searchFieldPresent(markup)).toBe(true)
    expect(markup).not.toContain('role="tablist"')
  })

  it('shows the recipient leave action only after the access tab is active', () => {
    const overview = dialogMarkup({ initialTab: 'overview', recipient: true })
    expect(overview).toMatch(/aria-selected="true"[^>]*role="tab"[^>]*tabindex="0"[^>]*>Übersicht/)
    expect(overview).not.toContain('Freigabe verlassen')

    const access = dialogMarkup({ initialTab: 'access', recipient: true })
    expect(access).toMatch(/aria-selected="true"[^>]*role="tab"[^>]*tabindex="0"[^>]*>Zugriff/)
    expect(access).toContain('Freigabe verlassen')
  })
})
