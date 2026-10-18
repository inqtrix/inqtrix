import { describe, expect, it } from 'vitest'

import { createReadyGate } from './readyGate'

describe('a send that waits until its thread or session is ready', () => {
  it('lets a send through at once when it is ready', async () => {
    const gate = createReadyGate()
    gate.update({ failed: false, key: 'thread-a', pending: false })
    await expect(gate.wait()).resolves.toBe('ready')
  })

  it('holds a send until the same key is ready', async () => {
    const gate = createReadyGate()
    gate.update({ failed: false, key: 'thread-a', pending: true })
    let outcome: string | null = null
    const waiting = gate.wait().then((value) => { outcome = value })
    await Promise.resolve()
    expect(outcome).toBeNull()
    gate.update({ failed: false, key: 'thread-a', pending: true })
    await Promise.resolve()
    expect(outcome).toBeNull()
    gate.update({ failed: false, key: 'thread-a', pending: false })
    await waiting
    expect(outcome).toBe('ready')
  })

  it('gives up without sending when the load fails', async () => {
    const gate = createReadyGate()
    gate.update({ failed: false, key: 'thread-a', pending: true })
    const waiting = gate.wait()
    gate.update({ failed: true, key: 'thread-a', pending: false })
    await expect(waiting).resolves.toBe('failed')
  })

  it('gives up without sending when another key is opened while it waits', async () => {
    const gate = createReadyGate()
    gate.update({ failed: false, key: 'thread-a', pending: true })
    const waiting = gate.wait()
    gate.update({ failed: false, key: 'thread-b', pending: false })
    await expect(waiting).resolves.toBe('cancelled')
  })
})
