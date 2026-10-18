import { describe, expect, it } from 'vitest'

import { clearScrollMemory, readScrollMemory, writeScrollMemory } from './scrollMemory'

describe('scroll memory', () => {
  it('round-trips a remembered position and clears it', () => {
    writeScrollMemory('chat:t1', { distanceFromBottom: 420, pinnedToBottom: false })
    expect(readScrollMemory('chat:t1')).toEqual({ distanceFromBottom: 420, pinnedToBottom: false })

    clearScrollMemory('chat:t1')
    expect(readScrollMemory('chat:t1')).toBeUndefined()
  })

  it('keeps namespaced keys independent so chat and knowledge never collide', () => {
    writeScrollMemory('chat:shared', { distanceFromBottom: 10, pinnedToBottom: false })
    writeScrollMemory('knowledge:shared', { distanceFromBottom: 0, pinnedToBottom: true })

    expect(readScrollMemory('chat:shared')).toEqual({ distanceFromBottom: 10, pinnedToBottom: false })
    expect(readScrollMemory('knowledge:shared')).toEqual({ distanceFromBottom: 0, pinnedToBottom: true })

    clearScrollMemory('chat:shared')
    clearScrollMemory('knowledge:shared')
  })
})
