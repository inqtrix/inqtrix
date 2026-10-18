import { describe, expect, it } from 'vitest'

import { BoundedLruCache, MARKDOWN_RENDER_CACHE_CAPACITY } from './boundedLruCache'

describe('BoundedLruCache', () => {
  it('evicts the least recently used result when the 257th entry is added', () => {
    const cache = new BoundedLruCache<number, string>(MARKDOWN_RENDER_CACHE_CAPACITY)
    for (let index = 0; index < MARKDOWN_RENDER_CACHE_CAPACITY; index += 1) {
      cache.set(index, `value-${index}`)
    }

    expect(cache.get(0)).toBe('value-0')
    cache.set(MARKDOWN_RENDER_CACHE_CAPACITY, 'newest')

    expect(cache.size).toBe(MARKDOWN_RENDER_CACHE_CAPACITY)
    expect(cache.has(0)).toBe(true)
    expect(cache.has(1)).toBe(false)
    expect(cache.get(MARKDOWN_RENDER_CACHE_CAPACITY)).toBe('newest')
  })

  it('updates an existing key without growing the cache', () => {
    const cache = new BoundedLruCache<string, number>(2)
    cache.set('first', 1)
    cache.set('second', 2)
    cache.set('first', 3)
    cache.set('third', 4)

    expect(cache.size).toBe(2)
    expect(cache.get('first')).toBe(3)
    expect(cache.has('second')).toBe(false)
  })

  it('keeps render snapshots pure until an entry is explicitly touched', () => {
    const cache = new BoundedLruCache<string, number>(2)
    cache.set('first', 1)
    cache.set('second', 2)

    expect(cache.peek('first')).toBe(1)
    cache.set('third', 3)

    expect(cache.has('first')).toBe(false)
    expect(cache.has('second')).toBe(true)
  })

  it('notifies mounted consumers when their entry is evicted', () => {
    const cache = new BoundedLruCache<string, number>(2)
    let notifications = 0
    const unsubscribe = cache.subscribe('first', () => {
      notifications += 1
    })
    cache.set('first', 1)
    notifications = 0
    cache.set('second', 2)
    cache.set('third', 3)

    expect(notifications).toBe(1)
    expect(cache.peek('first')).toBeUndefined()
    unsubscribe()
  })

  it('rejects invalid capacities', () => {
    expect(() => new BoundedLruCache(0)).toThrow(RangeError)
    expect(() => new BoundedLruCache(1.5)).toThrow(RangeError)
  })
})
