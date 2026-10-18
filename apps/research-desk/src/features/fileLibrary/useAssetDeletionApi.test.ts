import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createAssetDeletionPollingScope,
  resolveDeletionTransportOptions,
  waitForAssetDeletionPoll,
} from './useAssetDeletionApi'

describe('deletion transport selection', () => {
  it('keeps local assets local while Knowledge operations use their server', () => {
    const knowledge = { baseUrl: 'https://knowledge.example', workspaceId: 'workspace-a' }

    expect(resolveDeletionTransportOptions(null, knowledge)).toEqual({
      asset: null,
      knowledge,
      operations: knowledge,
    })
  })

  it('keeps asset operations available when Knowledge is unavailable', () => {
    const asset = { baseUrl: 'https://assets.example', workspaceId: 'workspace-a' }

    expect(resolveDeletionTransportOptions(asset, null)).toEqual({
      asset,
      knowledge: null,
      operations: asset,
    })
  })

  it('retains the shared transport contract for existing callers', () => {
    const shared = { workspaceId: 'workspace-a' }

    expect(resolveDeletionTransportOptions(shared)).toEqual({
      asset: shared,
      knowledge: shared,
      operations: shared,
    })
  })
})

describe('asset deletion polling scope', () => {
  it('aborts all old project requests and fences their late completions', () => {
    const scope = createAssetDeletionPollingScope()
    scope.reset('workspace-a:project-1')
    const first = scope.open('del-1', 'workspace-a:project-1')
    const second = scope.open('del-2', 'workspace-a:project-1')

    expect(first?.signal.aborted).toBe(false)
    expect(second?.signal.aborted).toBe(false)
    expect(scope.isCurrent('workspace-a:project-1')).toBe(true)

    scope.reset('workspace-b:project-2')

    expect(first?.signal.aborted).toBe(true)
    expect(second?.signal.aborted).toBe(true)
    expect(scope.isCurrent('workspace-a:project-1')).toBe(false)
    expect(scope.isCurrent('workspace-b:project-2')).toBe(true)
    expect(scope.open('del-late', 'workspace-a:project-1')).toBeNull()
    expect(scope.open('del-new', 'workspace-b:project-2')).not.toBeNull()
  })

  it('does not let an old release or stop detach a newer scoped controller', () => {
    const scope = createAssetDeletionPollingScope()
    scope.reset('old')
    const oldController = scope.open('del-same', 'old')
    expect(oldController).not.toBeNull()

    scope.reset('new')
    const newController = scope.open('del-same', 'new')
    expect(newController).not.toBeNull()
    scope.release('del-same', oldController!)
    scope.stop('del-same', 'old')

    expect(newController?.signal.aborted).toBe(false)
    expect(scope.open('del-same', 'new')).toBeNull()
    scope.stop('del-same', 'new')
    expect(newController?.signal.aborted).toBe(true)
  })
})

describe('asset deletion polling delay', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('removes its abort listener after the normal delay completes', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const remove = vi.spyOn(controller.signal, 'removeEventListener')
    const pending = waitForAssetDeletionPoll(250, controller.signal)

    await vi.advanceTimersByTimeAsync(250)
    await pending

    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
  })

  it('settles immediately on abort and clears the pending timer', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const remove = vi.spyOn(controller.signal, 'removeEventListener')
    const pending = waitForAssetDeletionPoll(5_000, controller.signal)

    controller.abort()
    await pending

    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
  })
})
