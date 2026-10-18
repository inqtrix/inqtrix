import { useCallback, useEffect, useSyncExternalStore } from 'react'

import type { BoundedLruCache } from './boundedLruCache'

type MarkdownCacheEntryOptions<Key, Value> = {
  cache: BoundedLruCache<Key, Value>
  cacheKey: Key
}

export function useMarkdownCacheEntry<Key, Value>({
  cache,
  cacheKey,
}: MarkdownCacheEntryOptions<Key, Value>): Value | undefined {
  const readSnapshot = useCallback(() => cache.peek(cacheKey), [cache, cacheKey])
  const subscribeToKey = useCallback(
    (listener: () => void) => cache.subscribe(cacheKey, listener),
    [cache, cacheKey],
  )
  const entry = useSyncExternalStore(subscribeToKey, readSnapshot, readSnapshot)

  useEffect(() => {
    if (entry !== undefined) cache.touch(cacheKey)
  }, [cache, cacheKey, entry])

  return entry
}
