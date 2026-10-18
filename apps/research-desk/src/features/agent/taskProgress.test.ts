import { describe, expect, it } from 'vitest'

import type { ResearchRunEvent } from '@/features/researchRuns/types'
import { translations } from '@/i18n/translations'
import {
  appendChildProgressMessage,
  childProgressMessage,
  meaningfulResearchSnapshot,
  researchNodePhase,
  snapshotWithResearchMetrics,
  type ChildProgressHistory,
} from './taskProgress'

const labels = translations.de.agent.canvas.child

describe('agent child progress', () => {
  it('never maps an empty or unknown child snapshot to answer', () => {
    expect(meaningfulResearchSnapshot({})).toBeUndefined()
    expect(researchNodePhase(undefined)).toBeNull()
    expect(researchNodePhase('unknown-node')).toBeNull()
    expect(researchNodePhase('search')).toBe('search')
  })

  it('surfaces progress warnings and terminal errors', () => {
    const progress: ResearchRunEvent = {
      created_at: 1,
      data: { message: 'Fallback auf reduzierte Suche' },
      run_id: 'child',
      sequence: 2,
      type: 'inqtrix.progress.message',
    }
    expect(childProgressMessage(progress, labels)?.severity).toBe('warning')
    expect(childProgressMessage({
      ...progress,
      data: { error: { message: 'Token-Budget erreicht' } },
      sequence: 3,
      type: 'inqtrix.run.failed',
    }, labels)).toMatchObject({ severity: 'error', text: 'Token-Budget erreicht' })
  })

  it('keeps one named query row when its finish event omits the query text', () => {
    const event: ResearchRunEvent = { created_at: 1, run_id: 'child', sequence: 1,
      type: 'inqtrix.research.query.started', data: { invocation_id: 'query-a', query: 'What does the source establish?', status: 'running' } }
    const start = childProgressMessage(event, labels)!
    expect(start).toMatchObject({ query: 'What does the source establish?', text: 'Suche läuft' })
    let history = appendChildProgressMessage({ messages: [], dropped: 0 }, start)
    const finish = childProgressMessage({ ...event, sequence: 2, type: 'inqtrix.research.query.finished', data: { invocation_id: 'query-a', status: 'completed' } }, labels)!
    history = appendChildProgressMessage(history, finish)
    expect(history.messages).toEqual([{ ...finish, query: 'What does the source establish?' }])
    const failed = childProgressMessage({ ...event, sequence: 3, type: 'inqtrix.research.query.finished', data: { invocation_id: 'query-a', status: 'failed' } }, labels)!
    expect(failed).toMatchObject({ severity: 'error', text: 'Suche fehlgeschlagen' })
    expect(appendChildProgressMessage(history, finish)).toBe(history)
  })

  it('counts older hidden messages at the existing history bound', () => {
    let history: ChildProgressHistory = { messages: [], dropped: 0 }
    for (let sequence = 1; sequence <= 103; sequence += 1) {
      history = appendChildProgressMessage(history, { sequence, text: 'Progress', severity: 'info' })
    }
    expect(history.messages).toHaveLength(100)
    expect(history.messages[0].sequence).toBe(4)
    expect(history.dropped).toBe(3)
  })

  it('maps durable local task metric keys without inventing counts', () => {
    expect(snapshotWithResearchMetrics(undefined, {
      reference_count: 7,
      claim_count: 4,
    }, 1)).toMatchObject({
      total_sources: 7,
      consolidated_claim_count: 4,
      total_queries: 1,
    })
    expect(snapshotWithResearchMetrics(undefined, undefined)).toBeUndefined()
  })
})
