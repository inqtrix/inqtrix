import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

describe('a retired thread removes the card instead of merging it', () => {
  const hook = readFileSync(
    'src/features/editor/useCollaborationComments.ts',
    'utf8',
  )
  const store = readFileSync(
    'src/features/editor/../../../../../src/inqtrix/storage/editor_collaboration_postgres.py',
    'utf8',
  )

  // A thread added by accident must be deletable, not merely resolvable. The
  // dangerous half of deleting it is not the write but the READ: the merge
  // keys threads by id, so a deleted thread arriving on a page would be set
  // back into the list and the card would stand until a reload.
  it('drops the id rather than setting it', () => {
    const merge = hook.slice(
      hook.indexOf('const mergeThreads = useCallback('),
      hook.indexOf('const fetchComments = useCallback('),
    )
    expect(merge.length).toBeGreaterThan(200)
    expect(merge).toContain('if (thread.deleted_at !== null) byId.delete(thread.id)')
    expect(merge).toContain('else byId.set(thread.id, thread)')
  })

  // And the page has to carry it exactly once. A fresh load must not, or the
  // card would come back on every reload; an incremental page must, or a
  // client already holding the card would never learn it is gone.
  it('hides retired threads from a fresh load but not from an incremental page', () => {
    const listing = store.slice(
      store.indexOf('            # Incremental pages return transitions'),
      store.indexOf('            thread_rows = ('),
    )
    expect(listing.length).toBeGreaterThan(200)
    expect(listing).toContain('if since_revision == 0:')
    expect(listing).toContain('deleted_at.is_(None)')
  })
})
