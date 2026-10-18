import { describe, expect, it } from 'vitest'

import { selectableRevisions } from './revisions'

const entry = (revision: number, status: string) => ({ revision, status })

describe('selectableRevisions', () => {
  it('lists finished writes and user edits, never the streaming flushes', () => {
    // Otherwise a fresh memo lists "Revision 22" with fragments at 1 and
    // 3 — every section flush becomes a version to pick.
    const history = [
      entry(22, 'ready'),
      entry(21, 'ready'),
      ...Array.from({ length: 19 }, (_, index) => entry(20 - index, 'writing')),
      entry(1, 'writing'),
    ]
    expect(selectableRevisions(history, 22).map((e) => e.revision)).toEqual([22, 21])
  })

  it('always keeps the current revision, even while it is still being written', () => {
    const history = [entry(3, 'writing'), entry(2, 'writing'), entry(1, 'ready')]
    expect(selectableRevisions(history, 3).map((e) => e.revision)).toEqual([3, 1])
  })

  it('offers nothing to pick when only one version remains', () => {
    expect(selectableRevisions([entry(5, 'ready'), entry(4, 'writing')], 5)).toEqual([])
    expect(selectableRevisions(undefined, 1)).toEqual([])
  })
})
