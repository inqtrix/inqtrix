import { afterEach, describe, expect, it, vi } from 'vitest'
import { logoutAndReload } from './useAuthSession'

describe('logoutAndReload', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reloads only after the server confirms logout', async () => {
    const destroySession = vi.fn(async () => undefined)
    const reload = vi.fn()
    const awaitDurability = vi.fn(async () => undefined)

    await expect(
      logoutAndReload(destroySession, reload, undefined, awaitDurability),
    ).resolves.toBe(true)
    expect(awaitDurability).toHaveBeenCalledOnce()
    expect(awaitDurability.mock.invocationCallOrder[0]).toBeLessThan(
      destroySession.mock.invocationCallOrder[0],
    )
    expect(destroySession).toHaveBeenCalledOnce()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('preserves the session when collaboration durability cannot be reached', async () => {
    const failure = new Error('collaboration durability unavailable')
    const awaitDurability = vi.fn(async () => {
      throw failure
    })
    const destroySession = vi.fn(async () => undefined)
    const reload = vi.fn()
    const onError = vi.fn()
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    await expect(
      logoutAndReload(destroySession, reload, onError, awaitDurability),
    ).resolves.toBe(false)
    expect(destroySession).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledWith(failure)
  })

  it('preserves the rendered session when server logout fails', async () => {
    const failure = new Error('offline')
    const destroySession = vi.fn(async () => {
      throw failure
    })
    const reload = vi.fn()
    const onError = vi.fn()
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    await expect(logoutAndReload(destroySession, reload, onError)).resolves.toBe(false)
    expect(reload).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(
      'Logout failed; the current session remains active.',
      failure,
    )
    expect(onError).toHaveBeenCalledWith(failure)
  })
})
