import { describe, expect, it } from 'vitest'
import type { UserEvent } from '@/api/inqtrixClient'
import {
  applyWake,
  confirmedUserEventCursor,
  createUserIdentityVerifier,
  noteWake,
  type PendingWake,
  type RefreshState,
  refreshTokenFor,
  userEventAction,
  userEventReconnectDelay,
  verifyLatestUserIdentity,
} from './useUserInvalidationEvents'

describe('userEventAction', () => {
  it('forces a reload before processing a ready frame for another user', () => {
    const event: UserEvent = {
      data: { cursor: '12', user_id: 'user-b' },
      id: null,
      type: 'ready',
    }
    expect(userEventAction(event, 'user-a')).toBe('reload')
    expect(userEventAction(event, 'user-b')).toBe('refetch')
  })

  it('treats invalidate and reset as content-free refetch signals', () => {
    expect(userEventAction({
      data: { resource_id: 'run-1', resource_type: 'run', scope: 'sharing' },
      id: '13',
      type: 'invalidate',
    }, 'user-a')).toBe('refetch')
    expect(userEventAction({
      data: {},
      id: null,
      type: 'reset',
    }, 'user-a')).toBe('refetch')
  })

  it('consumes comment outbox duplicates without waking every resource store', () => {
    for (const scope of [
      'collaboration_comment_changed',
      'collaboration_comment_mention',
    ]) {
      expect(userEventAction({
        data: {
          resource_id: 'ed_1',
          resource_type: 'editor_document',
          scope,
        },
        id: '13',
        type: 'invalidate',
      }, 'user-a')).toBe('consume')
    }
  })

  it('ignores unknown named events and bounds reconnect backoff', () => {
    expect(userEventAction({
      data: {},
      id: null,
      type: 'unknown',
    }, 'user-a')).toBe('ignore')
    expect(userEventReconnectDelay(-1)).toBe(1_000)
    expect(userEventReconnectDelay(20)).toBe(30_000)
  })
})

describe('verifyLatestUserIdentity', () => {
  const authenticated = (id: string) => ({
    authenticated: true as const,
    user: { display_name: null, email: null, id, role: 'user' },
  })

  it('authorizes a refetch only for the same canonical user', async () => {
    await expect(verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => true,
      probe: async () => authenticated('user-a'),
    })).resolves.toEqual({ action: 'refetch', error: null })
    await expect(verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => true,
      probe: async () => authenticated('user-b'),
    })).resolves.toEqual({ action: 'reload', error: null })
    await expect(verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => true,
      probe: async () => ({ authenticated: false }),
    })).resolves.toEqual({ action: 'reload', error: null })
  })

  it('retains without refetching on an explicit network failure', async () => {
    await expect(verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => true,
      probe: async () => {
        throw new TypeError('Failed to fetch')
      },
    })).resolves.toEqual({ action: 'retain', error: 'Failed to fetch' })
  })

  const httpFailure = (status: number, name = 'InqtrixRequestError') => {
    const error = new Error(`Inqtrix request failed with HTTP ${status}.`) as Error & {
      status: number
    }
    error.name = name
    error.status = status
    return error
  }
  const verifyFailure = (failure: unknown) => verifyLatestUserIdentity({
    expectedUserId: 'user-a',
    isCurrent: () => true,
    probe: async () => {
      throw failure
    },
  })

  it('reloads when the session answers 401 or 403', async () => {
    for (const status of [401, 403]) {
      await expect(verifyFailure(httpFailure(status)))
        .resolves.toEqual({ action: 'reload', error: null })
    }
  })

  it('reloads when the server reports another principal for this browser', async () => {
    await expect(verifyFailure(httpFailure(409, 'principal_changed')))
      .resolves.toEqual({ action: 'reload', error: null })
  })

  it('keeps the page when the session probe answers with a server error', async () => {
    for (const status of [500, 502, 503, 504]) {
      await expect(verifyFailure(httpFailure(status))).resolves.toEqual({
        action: 'retain',
        error: `Inqtrix request failed with HTTP ${status}.`,
      })
    }
  })

  it('keeps the page when the session probe times out or is rate limited', async () => {
    for (const status of [408, 429]) {
      await expect(verifyFailure(httpFailure(status))).resolves.toEqual({
        action: 'retain',
        error: `Inqtrix request failed with HTTP ${status}.`,
      })
    }
  })

  it('keeps the page when the session probe answers with something other than JSON', async () => {
    await expect(verifyFailure(new SyntaxError('Unexpected token < in JSON at position 0')))
      .resolves.toEqual({
        action: 'retain',
        error: 'Unexpected token < in JSON at position 0',
      })
  })

  it('keeps the page when the session answer has no recognizable identity', async () => {
    for (const answer of [
      {},
      { authenticated: true },
      { authenticated: true, user: {} },
      { authenticated: 'yes', user: { id: 'user-b' } },
    ]) {
      await expect(verifyLatestUserIdentity({
        expectedUserId: 'user-a',
        isCurrent: () => true,
        probe: async () => answer as never,
      })).resolves.toEqual({
        action: 'retain',
        error: 'The session probe answered without a recognizable identity.',
      })
    }
  })

  it('discards a delayed stale probe after a newer generation wins', async () => {
    let releaseFirst!: (value: ReturnType<typeof authenticated>) => void
    const firstProbe = new Promise<ReturnType<typeof authenticated>>((resolve) => {
      releaseFirst = resolve
    })
    let generation = 1
    const first = verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => generation === 1,
      probe: () => firstProbe,
    })

    generation = 2
    const second = verifyLatestUserIdentity({
      expectedUserId: 'user-a',
      isCurrent: () => generation === 2,
      probe: async () => authenticated('user-a'),
    })
    await expect(second).resolves.toEqual({ action: 'refetch', error: null })

    releaseFirst(authenticated('user-b'))
    await expect(first).resolves.toBeNull()
  })
})

describe('confirmedUserEventCursor', () => {
  const invalidate: UserEvent = {
    data: { scope: 'sharing' },
    id: '14',
    type: 'invalidate',
  }

  it('does not consume an invalidation while identity verification is offline', () => {
    expect(confirmedUserEventCursor('13', invalidate, {
      action: 'retain',
      error: 'Failed to fetch',
    })).toBe('13')
  })

  it('commits a confirmed event and clears a confirmed reset cursor', () => {
    const confirmed = { action: 'refetch' as const, error: null }
    expect(confirmedUserEventCursor('13', invalidate, confirmed)).toBe('14')
    expect(confirmedUserEventCursor('14', {
      data: {},
      id: null,
      type: 'reset',
    }, confirmed)).toBeUndefined()
  })
})

describe('createUserIdentityVerifier', () => {
  type Answer = () => Promise<never> | Promise<{
    authenticated: true
    user: { display_name: null; email: null; id: string; role: string }
  }>
  const harness = (answers: Answer[]) => {
    const timers: Array<{ callback: () => void; delayMs: number; id: number }> = []
    const cancelled: number[] = []
    const events: string[] = []
    let probes = 0
    const verifier = createUserIdentityVerifier({
      cancelTimer: (timer) => {
        cancelled.push(timer as unknown as number)
      },
      expectedUserId: 'user-a',
      onConfirmed: () => events.push('confirmed'),
      onUnverified: (reason) => events.push(`unverified:${reason}`),
      probe: () => {
        const answer = answers[Math.min(probes, answers.length - 1)]!
        probes += 1
        return answer() as never
      },
      reload: () => events.push('reload'),
      scheduleTimer: (callback, delayMs) => {
        const id = timers.length + 1
        timers.push({ callback, delayMs, id })
        return id as unknown as ReturnType<typeof setTimeout>
      },
    })
    const runTimer = async (index: number) => {
      timers[index]!.callback()
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
    return { cancelled, events, probes: () => probes, runTimer, timers, verifier }
  }
  const serverError = () => {
    const error = new Error('Inqtrix request failed with HTTP 503.') as Error & { status: number }
    error.status = 503
    return Promise.reject(error)
  }
  const sameUser = () => Promise.resolve({
    authenticated: true as const,
    user: { display_name: null, email: null, id: 'user-a', role: 'user' },
  })

  it('keeps the page on a server error and probes again with a growing delay until confirmed', async () => {
    const run = harness([serverError, serverError, sameUser])
    await run.verifier.verify()
    expect(run.events).toEqual(['unverified:Inqtrix request failed with HTTP 503.'])
    expect(run.timers.map((timer) => timer.delayMs)).toEqual([1_000])

    await run.runTimer(0)
    expect(run.timers.map((timer) => timer.delayMs)).toEqual([1_000, 2_000])

    await run.runTimer(1)
    expect(run.probes()).toBe(3)
    expect(run.events).toEqual([
      'unverified:Inqtrix request failed with HTTP 503.',
      'unverified:Inqtrix request failed with HTTP 503.',
      'confirmed',
    ])
    expect(run.timers).toHaveLength(2)
  })

  it('starts the delay again after a confirmed identity', async () => {
    const run = harness([serverError, sameUser, serverError])
    await run.verifier.verify()
    await run.runTimer(0)
    await run.verifier.verify()
    expect(run.timers.map((timer) => timer.delayMs)).toEqual([1_000, 1_000])
  })

  it('reloads once on a confirmed identity change and probes no more', async () => {
    const run = harness([() => Promise.resolve({
      authenticated: true as const,
      user: { display_name: null, email: null, id: 'user-b', role: 'user' },
    })])
    await run.verifier.verify()
    await run.verifier.verify()
    expect(run.events).toEqual(['reload'])
    expect(run.probes()).toBe(1)
    expect(run.timers).toHaveLength(0)
  })

  it('drops a scheduled retry on reset and on stop', async () => {
    const run = harness([serverError])
    await run.verifier.verify()
    run.verifier.reset()
    expect(run.cancelled).toEqual([1])
    await run.verifier.verify()
    run.verifier.stop()
    expect(run.cancelled).toEqual([1, 2])
    await run.verifier.verify()
    expect(run.probes()).toBe(2)
  })
})

describe('refreshing only what an event concerns', () => {
  const none = (): RefreshState => ({ all: 0, byScope: {} })
  const wake = (...scopes: Array<string | null>): PendingWake => {
    const pending: PendingWake = { all: false, scopes: new Set() }
    for (const scope of scopes) noteWake(pending, scope)
    return pending
  }

  it('lets a run event refresh only what lists runs', () => {
    // Every run start and finish refetched skills, templates, collections,
    // editor documents and folders, shares and the session as well.
    const state = applyWake(none(), wake('runs'))
    expect(refreshTokenFor(state, ['runs', 'sharing'])).toBe(1)
    expect(refreshTokenFor(state, ['skills'])).toBe(0)
    expect(refreshTokenFor(state, ['editor_documents', 'sharing'])).toBe(0)
    expect(refreshTokenFor(state, ['knowledge_collections', 'indexing'])).toBe(0)
  })

  it('refreshes everything after a reconnect, a reset or an unknown scope', () => {
    for (const pending of [wake(null), wake('account'), wake('runs', 'workspaces')]) {
      const state = applyWake(none(), pending)
      expect(refreshTokenFor(state, ['skills'])).toBe(1)
      expect(refreshTokenFor(state, ['runs'])).toBeGreaterThan(0)
    }
  })

  it('folds several events into one refresh per list', () => {
    const state = applyWake(none(), wake('runs', 'runs', 'sharing'))
    expect(refreshTokenFor(state, ['runs'])).toBe(1)
    expect(refreshTokenFor(state, ['runs', 'sharing'])).toBe(2)
    expect(refreshTokenFor(state, ['prompt_templates'])).toBe(0)
  })
})

