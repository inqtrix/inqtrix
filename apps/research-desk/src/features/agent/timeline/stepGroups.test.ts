import { describe, expect, it } from 'vitest'

import type { AgentPlanTaskRecord, AgentRunRecord, AgentStepEntry, AgentTaskLiveState } from '../model'
import {
  groupStepEntries,
  isEvidenceReadEntry,
  isSectionLine,
  taskResultShownByRow,
  taskRowResultPreview,
} from './stepGroups'

const entry = (seq: number, kind: AgentStepEntry['kind'], narrationId?: string): AgentStepEntry =>
  ({ at: seq, kind, narrationId, seq, text: narrationId } as AgentStepEntry)

describe('groupStepEntries', () => {
  it('folds consecutive section lines of a memo pass into one disclosure row', () => {
    // Unfolded, four "Abschnitt ... geschrieben." rows stand between the
    // memo announcement and the task result — noise at the top level.
    const log = [
      entry(1, 'narration', 'n-synthesis'),
      entry(2, 'narration', 'n-section-0'),
      entry(3, 'narration', 'n-section-1'),
      entry(4, 'narration', 'n-section-2'),
      entry(5, 'narration', 'n-task-s'),
      entry(6, 'narration', 'n-section-0'),
    ]
    const items = groupStepEntries(log)
    expect(items.map((item) => item.kind)).toEqual(['entry', 'sections', 'entry', 'sections'])
    expect(items[1]).toMatchObject({ kind: 'sections', seq: 2 })
    expect((items[1] as { entries: AgentStepEntry[] }).entries.map((e) => e.seq)).toEqual([2, 3, 4])
  })

  it('only recognises the per-section narration ids', () => {
    expect(isSectionLine(entry(1, 'narration', 'n-section-12'))).toBe(true)
    expect(isSectionLine(entry(1, 'narration', 'n-synthesis'))).toBe(false)
    expect(isSectionLine(entry(1, 'activity', 'n-section-1'))).toBe(false)
  })

  it('groups only consecutive evidence invocations and retains failed reads', () => {
    const reads: AgentStepEntry[] = [
      { ...entry(1, 'activity'), label: 'read_evidence', status: 'completed' },
      { ...entry(2, 'activity'), activityOperation: 'evidence_read', status: 'failed', error: 'Origin unavailable' },
      entry(3, 'approval_decided'),
      { ...entry(4, 'activity'), activityOperationCode: 'read_evidence', status: 'running' },
      { ...entry(5, 'activity'), label: 'run_deep_mission' },
    ]
    const grouped = groupStepEntries(reads)
    expect(grouped.map((item) => item.kind)).toEqual(['evidence', 'entry', 'evidence', 'entry'])
    expect(grouped[0]).toEqual({ kind: 'evidence', seq: 1, entries: reads.slice(0, 2) })
    expect(grouped[2]).toEqual({ kind: 'evidence', seq: 4, entries: [reads[3]] })
    expect(isEvidenceReadEntry({ ...entry(6, 'narration'), label: 'read_evidence' })).toBe(false)
  })
})

describe('a finished task in the transcript', () => {
  const planTask = (taskId: string, overrides: Partial<AgentPlanTaskRecord> = {}): AgentPlanTaskRecord => ({
    taskId, ordinal: 0, title: 'Regulierung erfassen', toolKind: 'rag_query', objective: '', queries: [],
    gapIds: [], dependsOn: [], budget: {}, params: {}, expectedOutput: '', status: 'completed',
    childRunId: null, resultSummary: '', ...overrides,
  })
  const finished = (taskId: string, extra: Partial<AgentStepEntry> = {}): AgentStepEntry =>
    ({ seq: 10, at: 10, kind: 'task_finished', taskId, ...extra })
  const narration = (narrationId: string, detail = 'task'): AgentStepEntry =>
    ({ seq: 11, at: 11, kind: 'narration', narrationId, detail, text: 'Regulierung erfassen: EU-Leitlinien belegt.' })
  const run = (
    stepLog: AgentStepEntry[],
    live: Partial<AgentTaskLiveState> = { status: 'completed', resultSummary: 'EU-Leitlinien belegt.' },
    task: AgentPlanTaskRecord = planTask('t1'),
  ): Pick<AgentRunRecord, 'plan' | 'stepLog' | 'taskStates'> => ({
    plan: {
      planId: 'p', version: 1, status: 'approved', createdBy: 'agent', reason: '', createdAt: 0,
      summaryMarkdown: '', assumptions: [], successCriteria: [], tasks: [task], versions: [],
    },
    stepLog,
    taskStates: { t1: { attempt: 1, status: 'completed', ...live } as AgentTaskLiveState },
  })

  it('shows the result once: the row carries it and the narration folds', () => {
    // Otherwise every finished task stands twice, as its row and as the
    // "Titel: …" narration below it.
    const log = [finished('t1'), narration('n-task-t1')]
    expect(taskRowResultPreview(log[0], run(log))).toBe('EU-Leitlinien belegt.')
    expect(taskResultShownByRow(log[1], run(log))).toBe(true)
  })

  it('keeps the narration when the row shows no result of its own', () => {
    const withError = [finished('t1', { error: 'Anbieter nicht erreichbar' }), narration('n-task-t1')]
    expect(taskResultShownByRow(withError[1], run(withError))).toBe(false)
    const insufficient = [finished('t1'), narration('n-task-t1')]
    const thin = run(insufficient, { status: 'insufficient_evidence', resultSummary: 'EU-Leitlinien belegt.' })
    expect(taskRowResultPreview(insufficient[0], thin)).toBe('')
    expect(taskResultShownByRow(insufficient[1], thin)).toBe(false)
    const noRow = [narration('n-task-t1')]
    expect(taskResultShownByRow(noRow[0], run(noRow))).toBe(false)
    const noSummary = [finished('t1'), narration('n-task-t1')]
    expect(taskResultShownByRow(noSummary[1], run(noSummary, { status: 'completed', resultSummary: '' }))).toBe(false)
  })

  it('keeps the narration when the plan does not name the task', () => {
    // The row then shows only the raw id as its title; the narration is the
    // one line that names the task in words.
    const log = [finished('t1'), narration('n-task-t1')]
    const unplanned = run(log, undefined, planTask('t9'))
    expect(taskRowResultPreview(log[0], unplanned)).toBe('EU-Leitlinien belegt.')
    expect(taskResultShownByRow(log[1], unplanned)).toBe(false)
  })

  it('folds only the task narration of the same task', () => {
    const log = [finished('t1'), narration('n-t1'), narration('n-task-t1', 'plan'), narration('n-task-t2')]
    const record = run(log)
    expect(log.slice(1).map((entry) => taskResultShownByRow(entry, record))).toEqual([false, false, false])
  })
})
