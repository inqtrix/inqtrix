import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { activityStepDisplayText } from './activityPresentation'
import { childProgressLine } from './childProgress'
import type { AgentStepEntry } from './model'
import { runHeadline } from './runHeadline'

const t = translations.de

type HeadlineInput = Parameters<typeof runHeadline>[0]

function run(overrides: Partial<HeadlineInput> = {}): HeadlineInput {
  return {
    activity: undefined,
    artifactOrder: [],
    artifacts: {},
    phase: 'execution',
    queuePosition: null,
    status: 'running',
    stepLog: [],
    ...overrides,
  } as HeadlineInput
}

function step(seq: number, entry: Partial<AgentStepEntry>): AgentStepEntry {
  return { at: 1_700_000_000 + seq, seq, kind: 'activity', ...entry } as AgentStepEntry
}

describe('runHeadline', () => {
  it('names a running delegation by its tool, never by the child line under it', () => {
    // The regression: the head showed the child's line, and the same
    // line stood under the delegation row — one sentence twice on screen.
    const delegation = step(1, {
      activityKind: 'searching',
      activityOperationCode: 'run_web_research',
      detail: 'Omnibus-Stand prüfen',
      label: 'run_web_research',
      status: 'running',
    })
    const child = {
      runStatus: 'running',
      snapshot: { current_node: 'search', total_queries: 6 },
    }
    const headline = runHeadline(run({ status: 'waiting_for_children', stepLog: [delegation] }), t)
    expect(headline).toBe(t.agent.tools.webResearch)
    expect(headline).not.toBe(childProgressLine(child, t, 'de'))
    expect(headline).not.toBe(activityStepDisplayText(delegation, t))
    expect(headline).not.toContain('Omnibus')
  })

  it('says a subtask works when no row of the parent is open', () => {
    expect(runHeadline(run({ status: 'waiting_for_children' }), t)).toBe('Teilauftrag arbeitet')
  })

  it('names the review while its narration line is still being written', () => {
    const review = step(2, {
      kind: 'narration',
      narrationId: 'kernel_deep_review',
      open: true,
      text: 'Verifikations-Durchlauf: Prüfung läuft.',
    })
    expect(runHeadline(run({ stepLog: [review] }), t)).toBe('Prüft die Antwort')
    // Settled: the review no longer runs.
    expect(runHeadline(run({ stepLog: [{ ...review, open: undefined }] }), t)).not.toBe(
      'Prüft die Antwort',
    )
  })

  it('names the answer while it is being written', () => {
    const headline = runHeadline(
      run({
        artifactOrder: ['answer_1'],
        artifacts: { answer_1: { kind: 'answer', status: 'writing' } } as unknown as HeadlineInput['artifacts'],
      }),
      t,
    )
    expect(headline).toBe('Schreibt die Antwort')
  })

  it('names evidence reads without their count', () => {
    const read = step(3, {
      activityOperation: 'evidence_read',
      activityOperationCode: 'read_evidence',
      status: 'running',
    })
    expect(runHeadline(run({ stepLog: [read] }), t)).toBe(t.agent.activityOperations.evidenceRead)
  })

  it('names a search by its operation, not by its query', () => {
    const search = step(4, {
      activityKind: 'searching',
      activityOperation: 'web_instant',
      detail: 'EU AI Act Artikel 50 Fristen',
      status: 'running',
    })
    const headline = runHeadline(run({ stepLog: [search] }), t)
    expect(headline).toBe(t.agent.activityOperations.webInstant)
    expect(headline).not.toContain('Artikel 50')
  })

  it('names an open model turn in its own words, which the server reports as started', () => {
    // A model turn stays `started` until it settles; it is never `running`.
    // Read as closed, the head fell back to the phase word for the whole
    // turn (41.6 s in one measured run). The turn's row keeps one word for
    // every turn of the run; a head that took that word put it on screen
    // twice, so the head says what the run does, as for review and answer.
    const turn = step(5, {
      activityOperation: 'model_turn',
      activityOperationCode: 'agent.model.turn',
      status: 'started',
    })
    const headline = runHeadline(run({ stepLog: [turn] }), t)
    expect(headline).toBe('Denkt nach')
    expect(headline).not.toBe(activityStepDisplayText(turn, t))
    expect(runHeadline(run({ stepLog: [turn] }), translations.en)).toBe('Thinking')
  })

  it('names the answer while a done run still publishes it', () => {
    // `done` comes before the answer's publication: the run is not
    // finished, so neither the phase fallback nor "Abgeschlossen" is true.
    expect(runHeadline(run({ phase: 'done' }), t)).toBe(t.agent.activity.writingAnswer)
  })

  it('names waits as waits', () => {
    expect(runHeadline(run({ status: 'waiting_for_approval' }), t)).toBe('Wartet auf deine Freigabe')
    expect(runHeadline(run({ status: 'waiting_for_input' }), t)).toBe('Wartet auf deine Antwort')
    expect(runHeadline(run({ status: 'queued' }), t)).toBe(t.agent.activity.starting)
  })

  it('speaks English for an English desk', () => {
    expect(runHeadline(run({ status: 'waiting_for_children' }), translations.en)).toBe('Subtask at work')
  })
})
