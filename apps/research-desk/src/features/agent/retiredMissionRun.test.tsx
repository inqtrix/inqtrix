import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { ResearchRunEvent, ResearchRunSummary } from '@/features/researchRuns/types'
import { translations } from '@/i18n/translations'
import { applyAgentRunEvent } from './events'
import { agentRunFromSummary, type AgentApprovalRecord, type AgentRunRecord } from './model'
import { RETIRED_MISSION_RUN_EVENTS } from './retiredMissionRun.fixture'
import { AgentRunTurn, type AgentTimelineActions } from './timeline/AgentTimeline'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

// The question's time stamp follows the clock and the zone of the machine
// the test runs on; every other line of the turn is the run's own.
vi.mock('@/lib/time', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/time')>(),
  formatMessageTimestamp: () => 'TS',
}))

const QUESTION = 'Wie hat sich die Zahl öffentlicher Ladepunkte in Deutschland seit Anfang 2024 entwickelt?'

function storedRun(
  runId: string,
  mode: ResearchRunSummary['mode'],
  execution: Record<string, unknown>,
): AgentRunRecord {
  const summary: ResearchRunSummary = {
    run_id: runId,
    status: 'completed',
    queue_position: null,
    question: QUESTION,
    stack: 'default',
    access: { mode: 'owner' },
    mode,
    kind: 'agent',
    agent_overrides: { autonomy: 'balanced' },
    created_at: 1_794_500_000,
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: { current_node: 'agent', last_message: '', execution },
    error: null,
    events_url: `/v1/runs/${runId}/events`,
  }
  return agentRunFromSummary(summary)
}

function replay(record: AgentRunRecord, events: readonly ResearchRunEvent[]): AgentRunRecord {
  return events.reduce(
    (current, event) => applyAgentRunEvent(current, event, { arrivedLive: false }),
    record,
  )
}

const ENTITIES: Record<string, string> = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': '\'', '&#39;': '\'', '&nbsp;': ' ',
}

/** Elements that start a line of their own on the page. */
const BLOCK_ELEMENTS = new Set([
  'button', 'div', 'h1', 'h2', 'h3', 'li', 'ol', 'p', 'section', 'ul',
])

/**
 * The text lines a reader sees, in page order: every block element starts a
 * new line, a span stands apart from its neighbour, and the markup between
 * is dropped. Collapsed parts of the page are read as well — the step feed
 * of a settled turn is one click away, and its lines are what it shows then.
 */
function visibleLines(html: string): string[] {
  const lines: string[] = []
  let current = ''
  const flush = () => {
    const text = current.replace(/\s+/g, ' ').trim()
    if (text) lines.push(text)
    current = ''
  }
  for (const token of html.split(/(<[^>]+>)/)) {
    if (token.startsWith('<')) {
      const name = /^<\/?([a-z0-9]+)/i.exec(token)?.[1]?.toLowerCase() ?? ''
      if (BLOCK_ELEMENTS.has(name)) flush()
      else if (name === 'span') current += ' '
      continue
    }
    current += token.replace(/&[#a-z0-9]+;/gi, (entity) => ENTITIES[entity] ?? entity)
  }
  flush()
  return lines
}

/** The text of every button of the page, in page order. */
function buttonTexts(html: string): string[] {
  return [...html.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)]
    .map((match) => visibleLines(match[1]).join(' '))
}

const actions = {} as AgentTimelineActions

const MISSION_EXECUTION = {
  effective_mode: 'workspace_agent',
  model: 'gpt-5.4',
  reasoning_effort: 'medium',
  source_policy: { web: 'available', knowledge: 'disabled' },
  tool_use_counts: { web: 3, knowledge: 0 },
}

describe('a stored run of the retired mission engine', () => {
  const run = () => replay(
    storedRun('run-retired-mission', 'workspace_agent', MISSION_EXECUTION),
    RETIRED_MISSION_RUN_EVENTS,
  )

  it('keeps every line it showed, as plain text', () => {
    // The lines are spelled out, not read from the dictionary: a deleted
    // decision text, a deleted station name or an event type the reducer no
    // longer reads as retired changes them.
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={run()} />)
    expect(visibleLines(html)).toEqual([
      'Sie TS Standard',
      QUESTION,
      'Arbeit abgeschlossen',
      '00:07:33 Laufzeit ·',
      '32 Schritte',
      'Gegen Anbieterauswertungen geprüft, Originalseiten nicht gelesen · 6 Aussagen: 6 vom Anbieter zugeschrieben',
      'Standard · gpt-5.4 · Aufwand Med · Web 3 Suchen · Wissen aus',
      'Auftrag',
      'Plan',
      'Plan v1 vorgeschlagen',
      'Mein Plan (5 Aufgaben): Öffentliche Ladepunkte zur Jahresmitte 2026 und historische Vergleichswerte recherchieren, fachlich benannte Ausbaulücken ermitteln und die Ergebnisse kurz mit Quellen zusammenfassen.',
      'Plan-Freigabe angefragt',
      'Plan freigegeben',
      'Ausführung',
      'Instant-Websuche · Wie viele öffentlich zugängliche Ladepunkte für Elektroautos gab es in Deutschland um den 1. Juli 2026 laut Bundesnetzagentur oder einer anderen nachvollziehbaren Fachquelle, und auf welches Datum bezieht sich die Zahl? · 1/1',
      'Ladepunktzahl Mitte 2026 ermitteln',
      'Laut Bundesnetzagentur gab es 157.762 Normalladepunkte und 56.572 Schnellladepunkte in Deutschland, also zusammen 214.334 öffentlich zugängliche Ladepunkte. Die Bundesnetzagentur bezieht diese Zahl auf den …',
      'Entwicklung seit Anfang 2024 erfassen',
      'Laut Bundesnetzagentur gab es in Deutschland: - am 1. Januar 2024: 123.449 öffentlich zugängliche Ladepunkte. ([energieagentur.rlp.de](https://www.energieagentur.rlp.de/info/die-energieagentur-informiert/aktue …',
      'Synthese',
      'Ich schreibe jetzt das Memo \'Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken\' mit 2 Abschnitten.',
      'Bericht zur Ladeinfrastruktur verfassen',
      'Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken Bestand und Entwicklung seit Anfang 2024 Laut gespeicherten Suchzusammenfassungen gab es in Deutschland am 1. Januar 2024 123.449 öffentlich …',
      'Prüfung',
      'Plan',
      'Plan v2 aktualisiert',
      'Mein Plan (7 Aufgaben): Ergänzend werden ein belastbarer Stichtagswert für Mitte 2026 und die Originalaussagen zu Ausbauücken geprüft.',
      'Plan-Freigabe angefragt',
      'Plan freigegeben',
      'Ausführung',
      'Ladepunktzahlen und Vergleichbarkeit prüfen',
      'Kurzfazit Für Mitte 2026 liegt kein geprüfter Originalwert der Bundesnetzagentur vor; auch die Veränderung seit Anfang 2024 ist daher nicht belastbar bestimmbar. Als späteren Anhaltspunkt schreibt die Suchauswe …',
      'Vorgaben zu IT-Sicherheit und Reisekosten auswerten',
      'Fehlgeschlagen: \'Qdrant/bm25\'',
      'Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken Erstellt',
    ])
  })

  it('offers no control on a line the retired engine wrote', () => {
    // A plan, task or decision line that opens nothing must not be a
    // button: a focus stop that does nothing is a dead end for a keyboard.
    // What stays a control belongs to every run: the step toggle, the
    // expandable search row and the document.
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={run()} />)
    expect(buttonTexts(html)).toEqual([
      '32 Schritte',
      'Instant-Websuche · Wie viele öffentlich zugängliche Ladepunkte für Elektroautos gab es in Deutschland um den 1. Juli 2026 laut Bundesnetzagentur oder einer anderen nachvollziehbaren Fachquelle, und auf welches Datum bezieht sich die Zahl? · 1/1',
      'Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken Erstellt',
    ])
  })

  it('names its decisions by the rows the server keeps, with the requirement in force', () => {
    // The approvals list is what the desk loads for every run; the decided
    // plan gate carried the requirement the report was written under.
    const approval = (approvalId: string, kind: string, payload: Record<string, unknown>): AgentApprovalRecord => ({
      approvalId,
      kind,
      status: 'approved',
      subjectType: 'plan',
      subjectId: 'plan-retired',
      payload: {},
      decision: 'approve',
      decisionPayload: payload,
      note: '',
      createdAt: 1_794_500_024,
      decidedAt: 1_794_500_030,
    })
    const record = {
      ...run(),
      approvals: [
        approval('apr-retired-plan-0', 'plan', {
          report_requirement: { free_text: 'Mit Tabelle der Stichtage.', rules: [] },
        }),
        approval('apr-retired-plan-1', 'replan', {}),
      ],
      approvalsStale: false,
    }
    const lines = visibleLines(renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={record} />))
    const first = lines.indexOf('Plan-Freigabe angefragt')
    expect(lines.slice(first, first + 3)).toEqual([
      'Plan-Freigabe angefragt',
      'Deine Vorgabe für dieses Ergebnis',
      'Plan freigegeben',
    ])
    expect(lines.filter((line) => line === 'Plan freigegeben')).toHaveLength(2)
  })

  it('keeps the heading of its discovery and the lines of its discovery and patch gates', () => {
    // The stored run above never had a discovery or a patch gate; older
    // runs did. Their words are spelled out here as well.
    const at = (sequence: number, type: string, data: Record<string, unknown>): ResearchRunEvent => ({
      type, run_id: 'run-retired-gates', sequence, created_at: 1_794_500_000 + sequence, data,
    })
    const record = replay(storedRun('run-retired-gates', 'workspace_agent', MISSION_EXECUTION), [
      at(1, 'inqtrix.agent.phase.changed', { phase: 'discovery', previous_phase: 'intake' }),
      at(2, 'inqtrix.agent.approval.requested', { approval_id: 'apr-retired-discovery', kind: 'discovery' }),
      at(3, 'inqtrix.agent.approval.decided', { approval_id: 'apr-retired-discovery', status: 'approved' }),
      at(4, 'inqtrix.agent.approval.requested', { approval_id: 'apr-retired-patch', kind: 'patch' }),
      at(5, 'inqtrix.agent.approval.decided', { approval_id: 'apr-retired-patch', status: 'rejected' }),
    ])
    const lines = visibleLines(renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={record} />))
    const wanted = [
      'Erkundung',
      'Erkundungs-Freigabe angefragt',
      'Erkundung freigegeben',
      'Patch-Freigabe angefragt',
      'Patch abgelehnt',
    ]
    expect(lines.filter((line) => wanted.includes(line))).toEqual(wanted)
  })

  it('reads an approval of a kind it does not know as a plain receipt, never as a plan', () => {
    const events: ResearchRunEvent[] = [
      { type: 'inqtrix.agent.approval.requested', run_id: 'run-unknown-kind', sequence: 1, created_at: 1, data: { approval_id: 'apr-x', kind: 'calendar' } },
      { type: 'inqtrix.agent.approval.decided', run_id: 'run-unknown-kind', sequence: 2, created_at: 2, data: { approval_id: 'apr-x', status: 'rejected' } },
    ]
    const record = replay(storedRun('run-unknown-kind', 'agent_kernel', { effective_mode: 'agent_kernel' }), events)
    const lines = visibleLines(renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={record} />))
    expect(lines).toContain('Freigabe angefragt')
    expect(lines).toContain('Freigabe abgelehnt')
    expect(lines).not.toContain('Plan abgelehnt')
  })
})

describe('a stored kernel run that delegated a deep mission', () => {
  const at = (sequence: number) => 1_794_600_000 + sequence
  const event = (sequence: number, type: string, data: Record<string, unknown>): ResearchRunEvent => ({
    type, run_id: 'run-kernel-deep', sequence, created_at: at(sequence), data,
  })
  const child = (sequence: number, data: Record<string, unknown>) => event(sequence, 'inqtrix.agent.child.progress', {
    child_run_id: 'run-mission-child', task_id: 'call-deep', ...data,
  })
  const events: ResearchRunEvent[] = [
    event(1, 'inqtrix.run.started', { status: 'running' }),
    event(2, 'inqtrix.agent.phase.changed', { phase: 'execution', previous_phase: 'intake' }),
    event(3, 'inqtrix.agent.tool.started', {
      tool: 'run_deep_mission', invocation_id: 'call-deep', args_preview: 'Ausbau der Ladeinfrastruktur bewerten',
    }),
    child(4, {
      run_status: 'running', event_type: 'inqtrix.agent.task.started',
      ordinal: 0, title: 'Bestand erfassen', tool_kind: 'web_instant', child_task_id: 't1',
    }),
    child(5, {
      run_status: 'running', event_type: 'inqtrix.agent.outcome',
      final: 'completed', review: 'completed', applied: 0,
      summary: 'Gegen Anbieterauswertungen geprüft · 4 Aussagen belegt',
    }),
    child(6, { run_status: 'completed', event_type: 'inqtrix.run.completed' }),
    event(7, 'inqtrix.agent.tool.finished', { tool: 'run_deep_mission', invocation_id: 'call-deep', status: 'ok' }),
    event(8, 'inqtrix.run.completed', { status: 'completed' }),
  ]
  const run = () => replay(
    storedRun('run-kernel-deep', 'agent_kernel', { effective_mode: 'agent_kernel' }),
    events,
  )

  it('keeps one line of how the delegation ended under its row', () => {
    const lines = visibleLines(renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={run()} />))
    const row = lines.indexOf('Tiefen-Recherche (Unterauftrag) · Ausbau der Ladeinfrastruktur bewerten')
    expect(row).toBeGreaterThan(-1)
    expect(lines[row + 1]).toBe('Unterauftrag · abgeschlossen · Gegen Anbieterauswertungen geprüft · 4 Aussagen belegt')
    expect(lines).not.toContain('Verlauf des Unterauftrags ansehen')
  })

  it('says how a broken-off delegation ended', () => {
    const failed = replay(run(), [])
    failed.children = {
      ...failed.children,
      'run-mission-child': {
        ...failed.children['run-mission-child'],
        error: 'Zeitlimit erreicht',
        outcome: undefined,
        runStatus: 'failed',
      },
    }
    const lines = visibleLines(renderToStaticMarkup(<AgentRunTurn actions={actions} historical run={failed} />))
    expect(lines).toContain('Unterauftrag · fehlgeschlagen: Zeitlimit erreicht')
  })
})
