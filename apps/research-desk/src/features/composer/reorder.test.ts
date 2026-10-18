import { describe, expect, it } from 'vitest'
import { moveItem } from './reorder'

describe('moveItem', () => {
  it('moves an item forward', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a'])
  })

  it('moves an item backward', () => {
    expect(moveItem(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b'])
  })

  it('returns a copy unchanged for a no-op move', () => {
    const input = ['a', 'b', 'c']
    const result = moveItem(input, 1, 1)
    expect(result).toEqual(input)
    expect(result).not.toBe(input)
  })

  it('returns a copy unchanged for an out-of-range index', () => {
    expect(moveItem(['a', 'b'], 0, 5)).toEqual(['a', 'b'])
    expect(moveItem(['a', 'b'], 5, 0)).toEqual(['a', 'b'])
    expect(moveItem(['a', 'b'], 0, -1)).toEqual(['a', 'b'])
  })
})
