import { describe, expect, it } from 'vitest'

import {
  clearsLastErrorOnAdmissionLoss,
  type DemandQueue,
  demandQueued,
  loadTerminalResult,
  queueUntilDemanded,
  replayTerminalEventPages,
  routeResultError,
  runResultIsUsed,
  shareResultLoad,
  shouldReplayTerminalAgentEvents,
  terminalRunAction,
  waitForRunTerminal,
} from './useResearchRunApi'
import type { ResearchRunResult, ResearchRunStatus, ResearchRunSummary } from './types'

describe('shouldReplayTerminalAgentEvents', () => {
  it('replays terminal root agent runs for both engines', () => {
    expect(shouldReplayTerminalAgentEvents({
      kind: 'agent',
      mode: 'workspace_agent',
      status: 'completed',
    })).toBe(true)
    expect(shouldReplayTerminalAgentEvents({
      kind: 'agent',
      mode: 'agent_kernel',
      status: 'failed',
    })).toBe(true)
  })

  it('does not replay children, active agents, or standard runs', () => {
    expect(shouldReplayTerminalAgentEvents({
      kind: 'agent_child',
      mode: 'workspace_agent',
      status: 'completed',
    })).toBe(false)
    expect(shouldReplayTerminalAgentEvents({
      kind: 'agent',
      mode: 'workspace_agent',
      status: 'running',
    })).toBe(false)
    expect(shouldReplayTerminalAgentEvents({
      kind: 'standard',
      mode: 'research',
      status: 'completed',
    })).toBe(false)
  })
})

describe('replayTerminalEventPages', () => {
  it('replays every page in sequence until the durable terminal page', async () => {
    const afterValues: Array<number | null> = []
    const delivered: number[] = []
    await replayTerminalEventPages({
      fetchPage: async (after) => {
        afterValues.push(after)
        return after === null
          ? {
            data: [{
              created_at: 1,
              data: {},
              run_id: 'r1',
              sequence: 1,
              type: 'inqtrix.agent.phase.changed',
            }],
            terminal: false,
          }
          : {
            data: [{
              created_at: 2,
              data: {},
              run_id: 'r1',
              sequence: 2,
              type: 'inqtrix.run.completed',
            }],
            terminal: true,
          }
      },
      onEvent: (event) => delivered.push(event.sequence),
    })
    expect(afterValues).toEqual([null, 1])
    expect(delivered).toEqual([1, 2])
  })

  it('fails visibly when a non-terminal replay cannot advance', async () => {
    await expect(replayTerminalEventPages({
      fetchPage: async () => ({ data: [], terminal: false }),
      onEvent: () => undefined,
    })).rejects.toThrow('before a terminal page')
  })
})

function summaryWithStatus(status: ResearchRunStatus): ResearchRunSummary {
  return { run_id: 'r1', status } as ResearchRunSummary
}

describe('waitForRunTerminal', () => {
  it('resolves on the first summary when it is already terminal', async () => {
    let sleeps = 0
    const summary = await waitForRunTerminal({
      fetchSummary: async () => summaryWithStatus('cancelled'),
      sleep: async () => {
        sleeps += 1
      },
    })
    expect(summary?.status).toBe('cancelled')
    expect(sleeps).toBe(0)
  })

  it('polls with the injected sleep until the run terminalizes', async () => {
    const statuses: ResearchRunStatus[] = ['running', 'running', 'cancelled']
    let fetches = 0
    let sleptMs = 0
    const summary = await waitForRunTerminal({
      fetchSummary: async () => summaryWithStatus(statuses[fetches++]),
      pollMs: 100,
      maxWaitMs: 1000,
      sleep: async (ms) => {
        sleptMs += ms
      },
    })
    expect(summary?.status).toBe('cancelled')
    expect(fetches).toBe(3)
    expect(sleptMs).toBe(200)
  })

  it('returns null once the wait bound is exhausted', async () => {
    let fetches = 0
    const summary = await waitForRunTerminal({
      fetchSummary: async () => {
        fetches += 1
        return summaryWithStatus('running')
      },
      pollMs: 100,
      maxWaitMs: 250,
      sleep: async () => undefined,
    })
    expect(summary).toBeNull()
    // 0ms, 100ms, 200ms waited -> fourth fetch sees waited >= bound.
    expect(fetches).toBe(4)
  })

  it('propagates fetch errors to the caller', async () => {
    await expect(waitForRunTerminal({
      fetchSummary: async () => {
        throw new Error('summary fetch failed')
      },
      sleep: async () => undefined,
    })).rejects.toThrow('summary fetch failed')
  })
})


describe('clearsLastErrorOnAdmissionLoss', () => {
  it('clears the error when the session leaves the server for the demo', () => {
    // Otherwise, after a failed server request the demo composer keeps
    // saying "request rejected" for the whole demo session, on two
    // surfaces — the demo path never clears it again.
    expect(
      clearsLastErrorOnAdmissionLoss({ enabled: false, canList: true }),
    ).toBe(true)
    expect(
      clearsLastErrorOnAdmissionLoss({ enabled: false, canList: false }),
    ).toBe(true)
  })

  it('keeps the error when only the session was lost', () => {
    // A real run's error is exactly what the user needs to see; losing the
    // session must not hide it.
    expect(
      clearsLastErrorOnAdmissionLoss({ enabled: true, canList: false }),
    ).toBe(false)
  })
})

describe('finished runs on a refresh pulse', () => {
  // Every refresh pulse (focus, online, sign-in, user events) lists all runs
  // again. A finished result does not change, so this view fetches it once.
  const standard = (status: ResearchRunStatus) => ({
    kind: 'standard' as ResearchRunSummary['kind'],
    mode: 'research' as ResearchRunSummary['mode'],
    run_id: 'run-1',
    status,
  })

  it('loads a completed result until this view holds it, and again for a waiting caller', () => {
    const loaded = new Set<string>()
    expect(terminalRunAction(standard('completed'), loaded, false)).toBe('load_result')
    loaded.add('run-1')
    expect(terminalRunAction(standard('completed'), loaded, false)).toBe('none')
    expect(terminalRunAction(standard('completed'), loaded, true)).toBe('load_result')
    expect(terminalRunAction(standard('failed'), new Set<string>(), false)).toBe('none')
    expect(terminalRunAction({
      kind: 'agent',
      mode: 'agent_kernel',
      run_id: 'run-agent',
      status: 'completed',
    }, loaded, false)).toBe('replay_events')
  })

  it('leaves a knowledge result until its question is in view, unless a caller waits on it', () => {
    // A knowledge result attaches to its question; with the question not yet
    // loaded, the result fetched at start was dropped and still counted as
    // loaded (34 finished runs, 20 stored questions).
    const knowledge = { ...standard('completed'), mode: 'knowledge' as ResearchRunSummary['mode'] }
    expect(terminalRunAction(knowledge, new Set<string>(), false)).toBe('defer_result')
    expect(terminalRunAction(knowledge, new Set<string>(), true)).toBe('load_result')
    expect(terminalRunAction(knowledge, new Set(['run-1']), false)).toBe('none')
  })

  it('hands on the result with the run finish and remembers the run once it arrived', async () => {
    const loaded = new Set<string>()
    const delivered: Array<[string, string | undefined]> = []
    await loadTerminalResult({
      fetchResult: async (runId) => ({
        answer: 'Antwort.',
        run_id: runId,
        status: 'completed',
        top_claims: [],
        top_sources: [],
      }) as unknown as ResearchRunResult,
      finishedAt: '2026-01-02T00:03:00.000Z',
      loaded,
      onError: () => { throw new Error('no error expected') },
      onResult: (result, finishedAt) => { delivered.push([result.run_id, finishedAt]) },
      runId: 'run-1',
    })
    expect(delivered).toEqual([['run-1', '2026-01-02T00:03:00.000Z']])
    expect(loaded.has('run-1')).toBe(true)
  })

  it('keeps a failed fetch for the next pulse', async () => {
    const loaded = new Set<string>()
    const errors: string[] = []
    await loadTerminalResult({
      fetchResult: async () => { throw new Error('offline') },
      loaded,
      onError: (runId, message) => { errors.push(`${runId}: ${message}`) },
      onResult: () => { throw new Error('no result expected') },
      runId: 'run-2',
    })
    expect(errors).toEqual(['run-2: offline'])
    expect(loaded.has('run-2')).toBe(false)
  })
})

describe('a finished result that failed to load', () => {
  it('reaches the consumer as a result error, not as a failed run', () => {
    const seen: string[] = []
    routeResultError({
      onResultError: (runId, message) => { seen.push(`result:${runId}:${message}`) },
      onRunError: (runId, message) => { seen.push(`run:${runId}:${message}`) },
    }, 'run-done', 'timeout')
    expect(seen).toEqual(['result:run-done:timeout'])
  })

  it('fails the run for a consumer that keeps nothing but the result', () => {
    const seen: string[] = []
    routeResultError({
      onRunError: (runId, message) => { seen.push(`run:${runId}:${message}`) },
    }, 'run-done', 'timeout')
    expect(seen).toEqual(['run:run-done:timeout'])
  })
})

describe('loading only what an open view shows', () => {
  const agentSummary = (runId: string) => ({
    kind: 'agent', mode: 'agent_kernel', run_id: runId, status: 'completed',
  } as ResearchRunSummary)
  const emptyQueue = (): DemandQueue => ({ demanded: new Set(), pending: new Map() })

  it('keeps a finished agent run waiting until its session asks for it', () => {
    // Every reload replayed the whole event history of every finished agent
    // run (58 requests), though only the open session shows any of them.
    const queue = emptyQueue()
    expect(queueUntilDemanded(queue, agentSummary('run-a'))).toBe(false)
    expect(queueUntilDemanded(queue, agentSummary('run-b'))).toBe(false)
    expect(demandQueued(queue, ['run-a']).map((summary) => summary.run_id)).toEqual(['run-a'])
    expect(demandQueued(queue, ['run-a'])).toEqual([])
    expect([...queue.pending.keys()]).toEqual(['run-b'])
  })

  it('replays at once a run its session asked for before the listing arrived', () => {
    const queue = emptyQueue()
    expect(demandQueued(queue, ['run-a'])).toEqual([])
    expect(queueUntilDemanded(queue, agentSummary('run-a'))).toBe(true)
    expect(queue.pending.size).toBe(0)
  })

  it('does not load results that the agent desk never reads', () => {
    // An agent run keeps its answer as an artifact; its result payload was
    // fetched for every finished agent run and thrown away.
    expect(runResultIsUsed({ kind: 'agent', mode: 'agent_kernel' })).toBe(false)
    expect(runResultIsUsed({ kind: 'agent', mode: 'workspace_agent' })).toBe(false)
    expect(runResultIsUsed({ kind: 'agent_child', mode: 'workspace_agent' })).toBe(false)
    expect(runResultIsUsed({ kind: 'standard', mode: 'research' })).toBe(true)
    expect(runResultIsUsed({ kind: 'standard', mode: 'knowledge' })).toBe(true)
  })

  it('asks for one result once while it is on its way', async () => {
    const loads = new Map<string, Promise<void>>()
    let started = 0
    let finish: () => void = () => undefined
    const start = () => {
      started += 1
      return new Promise<void>((resolve) => { finish = resolve })
    }
    const first = shareResultLoad(loads, 'run-1', start)
    const second = shareResultLoad(loads, 'run-1', start)
    expect(started).toBe(1)
    finish()
    await Promise.all([first, second])
    await shareResultLoad(loads, 'run-1', async () => { started += 1 })
    expect(started).toBe(2)
  })
})

