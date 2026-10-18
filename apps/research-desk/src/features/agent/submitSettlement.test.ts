import { describe, expect, it, vi } from 'vitest'

import { sendThenSettle } from './submitSettlement'

/** A send the test answers by hand, so "before" and "after" the server
 * answered are two observable moments. */
function heldSend<T>() {
  let answer: (value: T) => void = () => {}
  let fail: (error: Error) => void = () => {}
  const promise = new Promise<T>((resolve, reject) => {
    answer = resolve
    fail = reject
  })
  return { answer, fail, send: () => promise }
}

describe('what a submission leaves attached', () => {
  // Rules, skills, reports and the canvas pin hold for ONE task. Cleared
  // before the server answered, a refused send would lose everything the
  // user assembled for the retry.
  it('settles only after the server answered, as accepted', async () => {
    const held = heldSend<{ run_id: string } | null>()
    const settle = vi.fn()
    const pending = sendThenSettle(held.send, settle)
    await Promise.resolve()
    expect(settle).not.toHaveBeenCalled()
    held.answer({ run_id: 'run_1' })
    await expect(pending).resolves.toEqual({ run_id: 'run_1' })
    expect(settle).toHaveBeenCalledTimes(1)
    expect(settle).toHaveBeenCalledWith(true)
  })

  it('keeps everything when the server refuses', async () => {
    const settle = vi.fn()
    await expect(sendThenSettle(async () => null, settle)).resolves.toBeNull()
    expect(settle).toHaveBeenCalledWith(false)
  })

  it('reads a send that returned nothing as refused', async () => {
    // The caller returns false for it; clearing the draft at the same time
    // would say "sent" and "not sent" at once.
    const settle = vi.fn()
    await sendThenSettle(async () => undefined, settle)
    expect(settle).toHaveBeenCalledWith(false)
  })

  it('settles nothing when the send fails', async () => {
    const held = heldSend<{ run_id: string } | null>()
    const settle = vi.fn()
    const pending = sendThenSettle(held.send, settle)
    held.fail(new Error('network'))
    await expect(pending).rejects.toThrow('network')
    expect(settle).not.toHaveBeenCalled()
  })
})
