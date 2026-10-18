import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { isRoleOperation, kernelToolLabel } from './activityPresentation'
import { isMissionChild } from './childMission'
import { CHILD_PROGRESS_PROJECTED_KEYS, childProgressLine } from './childProgress'
import { reviewChangedLines } from './answerDraft'
import { demoDelegationLane, demoRunSummary } from './demo'
import { applyAgentRunEvent } from './events'
import { agentRunFromSummary } from './model'
import type { ResearchRunEvent } from '@/features/researchRuns/types'

const t = translations.de

function lane() {
  let seq = 0
  const summary = demoRunSummary(
    'RA-demo-parity', 'session-parity', 'Was ist neu bei KI diese Woche?', 'balanced',
    'running', 'deep', 'agent_kernel', 'auto', undefined, undefined, 'tief',
  )
  return { steps: demoDelegationLane('RA-demo-parity', () => (seq += 1), 'Was ist neu bei KI diese Woche?', summary), summary }
}

function eventsOf(steps: ReturnType<typeof demoDelegationLane>): ResearchRunEvent[] {
  return steps
    .flatMap((step) => step.actions)
    .flatMap((action) => ('event' in action && action.type === 'appendApiRunEvent' ? [action.event] : []))
}

/**
 * The demo is the shop window: its delegated-mission lane must use only
 * the fields the REAL child projection carries, and must show the child
 * status lines the mission card renders — titled task starts, a failed
 * task, the search tool, section narration, per-task results, the plan
 * and the review's outcome record — through the same reducer and
 * presenter the live transcript uses.
 */
describe('demo delegation lane parity', () => {
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

  it('uses only projected child.progress keys', () => {
    const { steps } = lane()
    const child = eventsOf(steps).filter((event) => event.type === 'inqtrix.agent.child.progress')
    expect(child.length).toBeGreaterThan(10)
    for (const event of child) {
      for (const key of Object.keys(event.data)) {
        expect(CHILD_PROGRESS_PROJECTED_KEYS.has(key), `stray demo key ${key}`).toBe(true)
      }
    }
  })

  it('shows each of these line kinds at least once and stamps the tool row', () => {
    const { steps } = lane()
    const events = eventsOf(steps)
    const child = events.filter((event) => event.type === 'inqtrix.agent.child.progress').map((event) => event.data)
    const kinds = child.map((data) => String(data.event_type))
    expect(child.filter((d) => d.event_type === 'inqtrix.agent.task.started' && d.title).length).toBe(4)
    expect(kinds).toContain('inqtrix.agent.task.failed')
    // The review role's settled steps: name and count, never content.
    expect(child.filter((d) => d.event_type === 'inqtrix.agent.tool.finished').map((d) => [d.tool, d.tool_calls]))
      .toEqual([['read_evidence', 1], ['read_project_document', 5]])
    expect(child.filter((d) => d.operation === 'web.search.instant').map((d) => d.status)).toEqual(['started', 'completed'])
    expect(child.filter((d) => String(d.narration_id ?? '').startsWith('n-section-')).length).toBe(3)
    // The card's lines: the child's task ids on task events, one result
    // line per finished task, the plan line and the outcome record.
    expect(child.filter((d) => d.event_type === 'inqtrix.agent.task.started' && d.child_task_id).length).toBe(4)
    expect(child.filter((d) => d.kind === 'task' && String(d.narration_id ?? '').startsWith('n-task-')).length).toBe(3)
    expect(String(child.find((d) => d.kind === 'plan')?.text)).toContain('Mein Plan')
    expect(child.find((d) => d.event_type === 'inqtrix.agent.outcome')?.summary)
      .toBe('Geprüft · 6 Aussagen: 3 mit Quelltext, 3 vom Anbieter zugeschrieben')
    expect(child.at(-1)?.run_status).toBe('completed')
    const toolRow = events.find((event) => event.type === 'inqtrix.agent.tool.started')?.data
    expect(toolRow?.tool).toBe('run_deep_mission')
    expect(toolRow?.todo_index).toBe(1)
    expect(String(toolRow?.args_preview)).toContain('Was ist neu bei KI diese Woche?')
    expect(events.filter((event) => event.type === 'inqtrix.agent.narration' && event.data.kind === 'intent')).toEqual([])
  })

  it('renders the same child lines the live transcript would', () => {
    const { steps, summary } = lane()
    let record = agentRunFromSummary(summary)
    const perStep = steps.map((step) => eventsOf([step]))
    for (const event of [...perStep[0], ...perStep[1]]) record = applyAgentRunEvent(record, event)
    const child = record.children['RA-demo-parity-mission']
    expect(childProgressLine(child, t, 'de')).toBe(
      'Unterauftrag · 4 Aufgaben parallel · Suche: Welche Modellankündigungen gab es diese Woche?',
    )
    for (const event of perStep[2]) record = applyAgentRunEvent(record, event)
    // Every task settled: the phase word returns, the last search stays
    // visible while the child is still in execution, the failure counts.
    expect(childProgressLine(record.children['RA-demo-parity-mission'], t, 'de')).toBe(
      'Unterauftrag · Führt Aufgaben aus · Suche: Welche Modellankündigungen gab es diese Woche? · 1 Suche abgeschlossen · 1 Aufgabe fehlgeschlagen',
    )
    for (const event of [...perStep[3], ...perStep[4]]) record = applyAgentRunEvent(record, event)
    expect(childProgressLine(record.children['RA-demo-parity-mission'], t, 'de')).toBe(
      "Unterauftrag · Prüft das Ergebnis · Abschnitt 'Offene Punkte' geschrieben. · 1 Aufgabe fehlgeschlagen",
    )
    for (const event of perStep.slice(5).flat()) record = applyAgentRunEvent(record, event)
    expect(childProgressLine(record.children['RA-demo-parity-mission'], t, 'de')).toMatch(
      /^Unterauftrag abgeschlossen · 4 Aufgaben · 3 erledigt, 1 fehlgeschlagen · 3 Abschnitte · \d\d:\d\d:\d\d$/,
    )
    const phases = record.stepLog.filter((item) => item.kind === 'phase').map((item) => item.phase)
    expect(phases).toEqual(['execution', 'done'])
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
    // The child joins the row through the same link a delegated mission uses.
    expect(record.children['RA-demo-parity-review'].taskId).toBe('role-review')
    const row = record.stepLog.find((item) => item.activityKey === 'tool:role-review')
    expect(row?.status).toBe('completed')
    expect(kernelToolLabel(String(row?.activityOperationCode), t)).toBe('Prüfrolle (Unterauftrag)')
    // The role child reports phases like a mission does, so the child alone
    // would pass for one; the identity of its row keeps the mission card away.
    expect(isMissionChild(record.children['RA-demo-parity-review'])).toBe(true)
    expect(isRoleOperation(row?.activityOperationCode)).toBe(true)
    expect(isRoleOperation('run_deep_mission')).toBe(false)
    expect(kernelToolLabel('review_role', translations.en)).toBe('Review role (delegated)')
  })
})

describe('section role row', () => {
  it('is a role, labelled in both languages, never a mission card', () => {
    // A role child reports phases like a mission; classified as a role, its
    // row keeps the one-line presenter instead of drawing a mission card.
    expect(isRoleOperation('section_role')).toBe(true)
    expect(kernelToolLabel('section_role', translations.de)).toBe('Abschnitt mit Nachlesen (Unterauftrag)')
    expect(kernelToolLabel('section_role', translations.en)).toBe('Section with source reading (delegated)')
  })
})
