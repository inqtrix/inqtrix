import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ConversationSkeleton } from './conversation-skeleton'

describe('ConversationSkeleton', () => {
  it('distributes a filled silhouette across the complete viewport', () => {
    const markup = renderToStaticMarkup(
      <ConversationSkeleton anchor="top" fill />,
    )
    const root = markup.match(/^<div[^>]*>/)?.[0] ?? ''

    expect(root).toContain('data-conversation-skeleton-anchor="top"')
    expect(root).toContain('h-full')
    expect(root).toContain('justify-between')
    expect(root).not.toContain('justify-end')
  })

  it('retains bottom anchoring for compact conversation placeholders', () => {
    const markup = renderToStaticMarkup(
      <ConversationSkeleton anchor="bottom" />,
    )
    const root = markup.match(/^<div[^>]*>/)?.[0] ?? ''

    expect(root).toContain('data-conversation-skeleton-anchor="bottom"')
    expect(root).toContain('justify-end')
    expect(root).not.toContain('justify-between')
  })
})
