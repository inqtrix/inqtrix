import { describe, expect, it } from 'vitest'

import { runBindingGroups, serverContracts } from './runBindingsModel'

describe('open runs judged against the version this server carries', () => {
  const bindings = {
    contracts: { research: 1, agent_kernel: 2 },
    counts: [
      { mode: 'agent_kernel', status: 'queued', workflow_contract: 3, runs: 2 },
      { mode: 'agent_kernel', status: 'running', workflow_contract: 2, runs: 4 },
      { mode: 'agent_kernel', status: 'waiting_for_approval', workflow_contract: 1, runs: 1 },
      { mode: 'agent_kernel', status: 'waiting_for_approval', workflow_contract: 3, runs: 1 },
      { mode: 'knowledge', status: 'queued', workflow_contract: 1, runs: 5 },
      { mode: 'research', status: 'queued', workflow_contract: 1, runs: 7 },
    ],
  }

  it('counts as outside what the cancel ends: above the version, or a mode not registered', () => {
    const groups = runBindingGroups(bindings)

    expect(groups.map((group) => [group.mode, group.serverContract, group.outsideRuns])).toEqual([
      ['agent_kernel', 2, 3],
      ['knowledge', null, 5],
      ['research', 1, 0],
    ])
  })

  it('leaves a run bound below the version inside, because it ends on its own', () => {
    const kernel = runBindingGroups(bindings)[0]

    expect(kernel.rows).toEqual([
      { status: 'waiting_for_approval', contract: 1, runs: 1, outside: false, asked: false },
      { status: 'running', contract: 2, runs: 4, outside: false, asked: false },
      { status: 'queued', contract: 3, runs: 2, outside: true, asked: false },
      { status: 'waiting_for_approval', contract: 3, runs: 1, outside: true, asked: false },
    ])
  })

  it('does not offer again a running run already asked to stop', () => {
    const [kernel] = runBindingGroups({
      contracts: { agent_kernel: 2 },
      counts: [
        { mode: 'agent_kernel', status: 'cancel_requested', workflow_contract: 3, runs: 2 },
        { mode: 'agent_kernel', status: 'queued', workflow_contract: 3, runs: 1 },
      ],
    })

    expect(kernel.outsideRuns).toBe(1)
    expect(kernel.rows).toContainEqual(
      { status: 'cancel_requested', contract: 3, runs: 2, outside: true, asked: true },
    )
  })

  it('reads a mode named like an object property as not registered', () => {
    const groups = runBindingGroups({
      contracts: {},
      counts: [{ mode: 'constructor', status: 'queued', workflow_contract: 1, runs: 1 }],
    })

    expect(groups[0].serverContract).toBeNull()
    expect(groups[0].outsideRuns).toBe(1)
  })

  it('names the versions of this server by mode', () => {
    expect(serverContracts(bindings)).toEqual([
      ['agent_kernel', 2],
      ['research', 1],
    ])
  })
})
