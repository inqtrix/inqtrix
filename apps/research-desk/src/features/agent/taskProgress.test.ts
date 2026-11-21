import { describe, expect, it } from 'vitest'

import { meaningfulResearchSnapshot, mergeResearchSnapshot } from './taskProgress'

describe('agent child progress', () => {
  it('never reads an empty child snapshot as progress', () => {
    expect(meaningfulResearchSnapshot({})).toBeUndefined()
    expect(meaningfulResearchSnapshot([])).toBeUndefined()
    expect(mergeResearchSnapshot({ current_node: 'search' }, {})).toEqual({ current_node: 'search' })
  })

  it('merges a later snapshot over the earlier one', () => {
    expect(mergeResearchSnapshot(
      { current_node: 'search', total_queries: 2 },
      { current_node: 'answer' },
    )).toEqual({ current_node: 'answer', total_queries: 2 })
  })
})
