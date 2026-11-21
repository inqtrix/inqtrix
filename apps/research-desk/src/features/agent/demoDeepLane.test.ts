import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { kernelToolLabel } from './activityPresentation'
import { CHILD_PROGRESS_PROJECTED_KEYS } from './childProgress'
import { reviewChangedLines } from './answerDraft'
import { demoDeepLane, demoRunSummary } from './demo'
import { applyAgentRunEvent } from './events'
import { agentRunFromSummary } from './model'
import type { ResearchRunEvent } from '@/features/researchRuns/types'

const t = translations.de

function lane() {
  let seq = 0
  const summary = demoRunSummary(
    'RA-demo-parity', 'session-parity', 'Was ist neu bei KI diese Woche?', 'balanced',
    'running', 'deep', 'auto', undefined, undefined, 'tief',
  )
  return { steps: demoDeepLane('RA-demo-parity', () => (seq += 1), summary), summary }
}

function eventsOf(steps: ReturnType<typeof demoDeepLane>): ResearchRunEvent[] {
  return steps
    .flatMap((step) => step.actions)
    .flatMap((action) => ('event' in action && action.type === 'appendApiRunEvent' ? [action.event] : []))
}

/**
 * The demo is the shop window: its deep lane (Stufe Tief) shows what the
 * kernel does there — the report in the canvas, the answer read while the
 * review role re-reads the evidence, and the review's outcome — through the
 * same reducer and presenter the live transcript uses, with only the fields
 * the real child projection carries.
 */
describe('demo deep lane parity', () => {
  it('shows the answer while the review runs and marks what the review changed', () => {
    const { steps } = lane()
    const events = eventsOf(steps)
    const kinds = events.map((event) => event.type)
    const drafts = events.filter((event) => event.type === 'inqtrix.answer.draft')
    const review = events.findIndex((event) => event.data.narration_id === 'kernel_deep_review')
    const answers = steps
      .flatMap((step) => step.actions)
      .flatMap((action) => (action.type === 'setAgentRunArtifactDetail' && action.artifact.kind === 'answer'
        ? [action.artifact.content_markdown ?? '']
        : []))
    expect([
      drafts.length,
      kinds.indexOf('inqtrix.answer.draft') < review,
      [...reviewChangedLines(String(drafts[0]?.data.markdown), answers.at(-1) ?? '')],
    ]).toEqual([1, true, [1]])
  })

  it('writes its report into the canvas before the review, and delegates no mission', () => {
    const { steps } = lane()
    const events = eventsOf(steps)
    const order = events.map((event) => `${event.type}:${String(event.data.tool ?? '')}`)
    expect(events.filter((event) => event.type.startsWith('inqtrix.agent.tool.') && event.data.tool === 'write_canvas')
      .map((event) => event.type.split('.').at(-1))).toEqual(['started', 'finished'])
    expect(order.indexOf('inqtrix.agent.tool.finished:write_canvas'))
      .toBeLessThan(order.indexOf('inqtrix.agent.tool.started:review_role'))
    const report = steps
      .flatMap((step) => step.actions)
      .flatMap((action) => (action.type === 'setAgentRunArtifactDetail' && action.artifact.kind === 'deliverable'
        ? [action.artifact]
        : []))
      .at(-1)
    expect([report?.title, report?.status, report?.content_markdown?.startsWith('# Markteinschätzung')])
      .toEqual(['Markteinschätzung: EU AI Act und der KI-Mittelstand', 'ready', true])
    expect(events.some((event) => event.data.tool === 'run_deep_mission')).toBe(false)
  })

  it('uses only projected child.progress keys for its review role', () => {
    const { steps } = lane()
    const child = eventsOf(steps).filter((event) => event.type === 'inqtrix.agent.child.progress')
    // The role's phases and its settled steps: name and count, never content.
    expect(child.map((event) => [event.data.event_type, event.data.tool ?? null])).toEqual([
      ['inqtrix.agent.phase.changed', null],
      ['inqtrix.agent.tool.finished', 'read_evidence'],
      ['inqtrix.agent.tool.finished', 'read_project_document'],
      ['inqtrix.agent.phase.changed', null],
    ])
    for (const event of child) {
      for (const key of Object.keys(event.data)) {
        expect(CHILD_PROGRESS_PROJECTED_KEYS.has(key), `stray demo key ${key}`).toBe(true)
      }
    }
  })

  it('shows the Deep review as one delegation row joined to its role child', () => {
    const { steps, summary } = lane()
    const events = eventsOf(steps)
    const rows = events
      .filter((event) => event.type.startsWith('inqtrix.agent.tool.') && event.data.tool === 'review_role')
      .map((event) => [event.type.split('.').at(-1), event.data.invocation_id])
    // One row, started once and finished once, under the role's origin key.
    expect(rows).toEqual([['started', 'role-review'], ['finished', 'role-review']])
    let record = agentRunFromSummary(summary)
    for (const event of events) record = applyAgentRunEvent(record, event)
    expect(record.children['RA-demo-parity-review'].taskId).toBe('role-review')
    const row = record.stepLog.find((item) => item.activityKey === 'tool:role-review')
    expect(row?.status).toBe('completed')
    expect(kernelToolLabel(String(row?.activityOperationCode), t)).toBe('Prüfrolle (Unterauftrag)')
    expect(kernelToolLabel('review_role', translations.en)).toBe('Review role (delegated)')
    const phases = record.stepLog.filter((item) => item.kind === 'phase').map((item) => item.phase)
    expect(phases).toEqual(['execution', 'done'])
  })
})

describe('section role row', () => {
  it('is labelled in both languages', () => {
    expect(kernelToolLabel('section_role', translations.de)).toBe('Abschnitt mit Nachlesen (Unterauftrag)')
    expect(kernelToolLabel('section_role', translations.en)).toBe('Section with source reading (delegated)')
  })
})
