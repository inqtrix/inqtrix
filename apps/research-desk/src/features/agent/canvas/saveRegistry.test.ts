import { describe, expect, it } from 'vitest'

import { createCanvasSaveRegistry } from './saveRegistry'

describe('createCanvasSaveRegistry', () => {
  it('flushes every registered entry', async () => {
    const registry = createCanvasSaveRegistry()
    const flushed: string[] = []
    registry.register('art_a', async () => {
      flushed.push('a')
    })
    registry.register('art_b', async () => {
      flushed.push('b')
    })
    await registry.flushAll()
    expect(flushed.sort()).toEqual(['a', 'b'])
  })

  it('survives the tab-transition race: a late cleanup removes only its OWN entry', async () => {
    // AnimatePresence unmounts the OLD document view AFTER the new one
    // mounted; with the previous single slot the old cleanup nulled the
    // new view's registration and the submit flush protected nothing.
    const registry = createCanvasSaveRegistry()
    const flushed: string[] = []
    const oldFlush = async () => {
      flushed.push('old')
    }
    const newFlush = async () => {
      flushed.push('new')
    }
    const deregisterOld = registry.register('art_x', oldFlush)
    registry.register('art_x', newFlush)
    // The old mount's cleanup fires LAST — it must not evict newFlush.
    deregisterOld()
    expect(registry.size()).toBe(1)
    await registry.flushAll()
    expect(flushed).toEqual(['new'])
  })

  it('deregisters its own live entry normally', async () => {
    const registry = createCanvasSaveRegistry()
    const deregister = registry.register('art_y', async () => {
      throw new Error('must not run')
    })
    deregister()
    expect(registry.size()).toBe(0)
    await registry.flushAll()
  })
})
