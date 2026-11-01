import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dialog } from './dialog'

// The portal target does not exist in this DOM-less lane; the panel renders in place.
vi.mock('react-dom', async (original) => ({
  ...await original<typeof import('react-dom')>(),
  createPortal: (node: unknown) => node,
}))

afterEach(() => {
  vi.unstubAllGlobals()
})

function closeButtonName(props: { closeLabel?: string }): string | null {
  vi.stubGlobal('document', { body: {} })
  const html = renderToStaticMarkup(
    <Dialog {...props as { closeLabel: string }} onClose={() => undefined} open title="Titel">Inhalt</Dialog>,
  )
  return html.match(/<button aria-label="([^"]*)"/)?.[1] ?? null
}

describe('Dialog close button', () => {
  it('carries the name the caller gives it', () => {
    expect(closeButtonName({ closeLabel: 'Schließen' })).toBe('Schließen')
  })

  it('has no name of its own in one language', () => {
    // A default read "Close" to screen readers in a German session.
    expect(closeButtonName({})).toBeNull()
  })
})
