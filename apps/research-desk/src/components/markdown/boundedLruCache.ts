export class BoundedLruCache<Key, Value> {
  readonly maxEntries: number

  private readonly entries = new Map<Key, Value>()
  private readonly listeners = new Map<Key, Set<() => void>>()

  constructor(maxEntries: number) {
    if (!Number.isInteger(maxEntries) || maxEntries < 1) {
      throw new RangeError('BoundedLruCache maxEntries must be a positive integer.')
    }
    this.maxEntries = maxEntries
  }

  get size(): number {
    return this.entries.size
  }

  get(key: Key): Value | undefined {
    const value = this.peek(key)
    if (!this.touch(key)) return undefined
    return value
  }

  peek(key: Key): Value | undefined {
    return this.entries.get(key)
  }

  touch(key: Key): boolean {
    if (!this.entries.has(key)) return false
    const value = this.entries.get(key) as Value
    this.entries.delete(key)
    this.entries.set(key, value)
    return true
  }

  has(key: Key): boolean {
    return this.entries.has(key)
  }

  subscribe(key: Key, listener: () => void): () => void {
    const listeners = this.listeners.get(key) ?? new Set()
    listeners.add(listener)
    this.listeners.set(key, listeners)
    return () => {
      listeners.delete(listener)
      if (listeners.size === 0) this.listeners.delete(key)
    }
  }

  set(key: Key, value: Value): void {
    this.entries.delete(key)
    this.entries.set(key, value)

    let evictedKey: Key | undefined
    let didEvict = false
    if (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next()
      if (!oldest.done) {
        evictedKey = oldest.value
        didEvict = true
        this.entries.delete(oldest.value)
      }
    }

    this.notify(key)
    if (didEvict && !Object.is(evictedKey, key)) this.notify(evictedKey as Key)
  }

  private notify(key: Key): void {
    for (const listener of this.listeners.get(key) ?? []) listener()
  }
}

export const MARKDOWN_RENDER_CACHE_CAPACITY = 256
