import { describe, expect, it } from 'vitest'

import type { AgentRunRecord, AgentStepEntry } from '../model'
import {
  groupStepEntries,
  isEvidenceReadEntry,
  isSectionLine,
  retiredTaskRow,
  taskResultShownByRow,
  taskStartConcluded,
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

describe('a task of a stored mission run in the transcript', () => {
  const finished = (extra: Partial<AgentStepEntry> = {}): AgentStepEntry => ({
    seq: 10, at: 10, kind: 'task_finished', taskId: 't1', status: 'completed',
    title: 'Regulierung erfassen', result: '**EU-Leitlinien** belegt.', ...extra,
  })
  const narration = (narrationId: string, detail = 'task'): AgentStepEntry =>
    ({ seq: 11, at: 11, kind: 'narration', narrationId, detail, text: 'Regulierung erfassen: EU-Leitlinien belegt.' })
  const run = (stepLog: AgentStepEntry[]): Pick<AgentRunRecord, 'stepLog'> => ({ stepLog })

  it('names the task by the title and shows the result head as plain text', () => {
    expect(retiredTaskRow(finished(), [finished()])).toEqual({
      title: 'Regulierung erfassen', result: 'EU-Leitlinien belegt.',
    })
    // A result that repeats the title says it once.
    expect(retiredTaskRow(finished({ result: 'Regulierung erfassen: belegt.' }), []).result).toBe('belegt.')
  })

  it('names a task stored without its title by its result line, else by its id', () => {
    const untitled = finished({ title: undefined })
    expect(retiredTaskRow(untitled, [untitled, narration('n-task-t1')])).toEqual({
      title: 'Regulierung erfassen: EU-Leitlinien belegt.', result: '',
    })
    expect(retiredTaskRow(untitled, [untitled])).toEqual({ title: 't1', result: 'EU-Leitlinien belegt.' })
    // A start line has no result yet.
    expect(retiredTaskRow({ seq: 1, at: 1, kind: 'task_started', taskId: 't1' }, [])).toEqual({ title: 't1', result: '' })
  })

  it('shows the result once: the row carries it and the narration folds', () => {
    const log = [finished(), narration('n-task-t1')]
    expect(taskResultShownByRow(log[1], run(log))).toBe(true)
    // The row named by the narration itself folds it as well.
    const untitled = [finished({ title: undefined }), narration('n-task-t1')]
    expect(taskResultShownByRow(untitled[1], run(untitled))).toBe(true)
  })

  it('keeps the narration when the row shows no result of its own', () => {
    for (const row of [
      finished({ error: 'Anbieter nicht erreichbar' }),
      finished({ status: 'insufficient_evidence' }),
      finished({ result: undefined }),
    ]) {
      const log = [row, narration('n-task-t1')]
      expect(taskResultShownByRow(log[1], run(log))).toBe(false)
    }
    expect(taskResultShownByRow(narration('n-task-t1'), run([narration('n-task-t1')]))).toBe(false)
  })

  it('folds only the task narration of the same task', () => {
    const log = [finished(), narration('n-t1'), narration('n-task-t1', 'plan'), narration('n-task-t2')]
    expect(log.slice(1).map((entry) => taskResultShownByRow(entry, run(log)))).toEqual([false, false, false])
  })

  it('answers a start line by the outcome line that follows it', () => {
    const started: AgentStepEntry = { seq: 5, at: 5, kind: 'task_started', taskId: 't1' }
    expect(taskStartConcluded(started, [started])).toBe(false)
    expect(taskStartConcluded(started, [started, finished()])).toBe(true)
    // An outcome before a retry's start does not answer it.
    expect(taskStartConcluded({ ...started, seq: 20 }, [finished(), { ...started, seq: 20 }])).toBe(false)
    expect(taskStartConcluded(started, [started, finished({ taskId: 't2' })])).toBe(false)
  })
})
