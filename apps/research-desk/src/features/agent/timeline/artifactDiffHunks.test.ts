import { describe, expect, it } from 'vitest'

import { diffHunkPlan } from './artifactDiffHunks'

const doc = (...lines: string[]) => lines.join('\n')

describe('diffHunkPlan (inline chip diff)', () => {
  it('returns no hunks for identical documents', () => {
    expect(diffHunkPlan('a\nb', 'a\nb')).toEqual({
      hunks: [],
      skippedAfter: 0,
    })
  })

  it('shows only the changed region with context and visible gaps', () => {
    const from = doc('l1', 'l2', 'l3', 'l4', 'l5', 'l6', 'l7', 'l8', 'l9', 'l10')
    const to = doc('l1', 'l2', 'l3', 'l4', 'l5', 'CHANGED', 'l7', 'l8', 'l9', 'l10')
    const plan = diffHunkPlan(from, to, 2)
    expect(plan.hunks).toHaveLength(1)
    const [hunk] = plan.hunks
    // 3 skipped before (l1-l3), context l4/l5, -l6 +CHANGED, context l7/l8.
    expect(hunk.skippedBefore).toBe(3)
    expect(hunk.lines.map((line) => `${line.type}:${line.text}`)).toEqual([
      'context:l4',
      'context:l5',
      'delete:l6',
      'insert:CHANGED',
      'context:l7',
      'context:l8',
    ])
    expect(plan.skippedAfter).toBe(2)
  })

  it('merges changes whose context windows touch into one hunk', () => {
    const from = doc('a', 'b', 'c', 'd', 'e')
    const to = doc('a', 'B', 'c', 'D', 'e')
    const plan = diffHunkPlan(from, to, 1)
    expect(plan.hunks).toHaveLength(1)
    expect(plan.skippedAfter).toBe(0)
  })

  it('keeps far-apart changes as separate hunks with a counted gap', () => {
    const middle = Array.from({ length: 12 }, (_, i) => `m${i}`)
    const from = doc('first', ...middle, 'last')
    const to = doc('FIRST', ...middle, 'LAST')
    const plan = diffHunkPlan(from, to, 2)
    expect(plan.hunks).toHaveLength(2)
    // 12 middle lines minus 2 trailing + 2 leading context lines.
    expect(plan.hunks[1].skippedBefore).toBe(8)
    expect(plan.skippedAfter).toBe(0)
  })

  it('handles pure insertion into an empty document', () => {
    const plan = diffHunkPlan('', 'eins\nzwei')
    expect(plan.hunks).toHaveLength(1)
    expect(plan.hunks[0].lines.every((line) => line.type === 'insert')).toBe(
      true,
    )
    expect(plan.hunks[0].lines).toHaveLength(2)
  })

  it('mirrors a half-sentence swap (+1/-1)', () => {
    const from = doc('# T', '', 'aus a und hoher X reagieren.', '', 'Rest.')
    const to = doc('# T', '', 'aus a und stabiler Y reagieren.', '', 'Rest.')
    const plan = diffHunkPlan(from, to, 3)
    expect(plan.hunks).toHaveLength(1)
    const changed = plan.hunks[0].lines.filter(
      (line) => line.type !== 'context',
    )
    expect(changed.map((line) => line.type)).toEqual(['delete', 'insert'])
  })
})
