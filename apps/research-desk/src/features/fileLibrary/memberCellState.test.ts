import { describe, expect, it } from 'vitest'
import { isMemberInRun, memberCellState, memberHasNoEmbeddableText, rangeBetween } from './helpers'
import type { IndexingJobLive } from '@/features/project/types'

function liveJob(overrides: Partial<IndexingJobLive>): IndexingJobLive {
  return {
    completedDocuments: 0,
    jobId: 'j1',
    percent: 0,
    runningFileIds: [],
    source: 'server',
    startedAt: '2026-06-25T00:00:00.000Z',
    totalDocuments: 0,
    ...overrides,
  }
}

describe('isMemberInRun', () => {
  it('is true only for a file in the actively running job', () => {
    expect(isMemberInRun(liveJob({ runningFileIds: ['a', 'b'] }), 'a')).toBe(true)
    expect(isMemberInRun(liveJob({ runningFileIds: ['a', 'b'] }), 'c')).toBe(false)
  })

  it('is false while the job is still queued (no row pulses "läuft")', () => {
    // Regression: a queued re-embed must not contradict the "In Warteschlange"
    // header by pulsing its member rows before anything is processed.
    const queued = liveJob({ runningFileIds: ['a', 'b'], queuePosition: 1 })
    expect(isMemberInRun(queued, 'a')).toBe(false)
  })

  it('is false when there is no live job', () => {
    expect(isMemberInRun(null, 'a')).toBe(false)
    expect(isMemberInRun(undefined, 'a')).toBe(false)
  })
})

// Regression guard for the "alles läuft" bug: the per-file running label must be
// driven by whether THIS file is in the current run's working set (`inRun`), not
// by the whole index being in an `indexing` state.
describe('memberCellState', () => {
  it('keeps an embedded file outside the run reading "embedded" (the bug)', () => {
    // An already-embedded member that is NOT part of the running job must never
    // read "running" just because the index is indexing another document.
    expect(memberCellState('embedded', false, undefined)).toBe('embedded')
  })

  it('shows "running" for an in-run file not yet confirmed', () => {
    expect(memberCellState('pending', true, undefined)).toBe('running')
  })

  it('lets a confirmed live outcome win over the run state', () => {
    expect(memberCellState('pending', true, 'embedded')).toBe('embedded')
    expect(memberCellState('pending', true, 'skipped')).toBe('skipped')
  })

  it('shows the persisted state for a file outside the run', () => {
    expect(memberCellState('pending', false, undefined)).toBe('pending')
    expect(memberCellState('skipped', false, undefined)).toBe('skipped')
  })
})

describe('memberHasNoEmbeddableText', () => {
  const base = { extractedText: '', parsePending: false, serverFileId: null, uploadPending: false }

  it('is true only for a settled, text-less, server-less member', () => {
    expect(memberHasNoEmbeddableText(base)).toBe(true)
  })

  it('treats in-flight placeholders as queued, never as no-text', () => {
    expect(memberHasNoEmbeddableText({ ...base, uploadPending: true })).toBe(false)
    expect(memberHasNoEmbeddableText({ ...base, parsePending: true })).toBe(false)
  })

  it('is false once text or a re-parseable server original exists', () => {
    expect(memberHasNoEmbeddableText({ ...base, extractedText: 'body' })).toBe(false)
    expect(memberHasNoEmbeddableText({ ...base, serverFileId: 'fl_1' })).toBe(false)
  })
})

describe('rangeBetween (shift-click ranges over the visual order)', () => {
  const order = ['a', 'b', 'c', 'd', 'e']

  it('spans anchor..target inclusively in either direction', () => {
    expect(rangeBetween(order, 'b', 'd')).toEqual(['b', 'c', 'd'])
    expect(rangeBetween(order, 'd', 'b')).toEqual(['b', 'c', 'd'])
  })

  it('degrades to a single toggle without an anchor or with a stale anchor', () => {
    expect(rangeBetween(order, null, 'c')).toEqual(['c'])
    expect(rangeBetween(order, 'gone', 'c')).toEqual(['c'])
  })

  it('returns nothing for a target outside the visible order', () => {
    expect(rangeBetween(order, 'a', 'hidden')).toEqual([])
  })
})
