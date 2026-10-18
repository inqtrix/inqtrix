import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'
import { agentRunFromSummary, type AgentRunOutcome } from '../model'
import { activityText, AgentRunTurn, EvidenceReadGroup, type AgentTimelineActions } from './AgentTimeline'

const localeState = vi.hoisted(() => ({ locale: 'de' as 'de' | 'en' }))
// The lane renders without a DOM, so no click can open a disclosure: a
// test that needs its content expands every disclosure through this flag.
const disclosureState = vi.hoisted(() => ({ expanded: false }))

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: localeState.locale, t: translations[localeState.locale] }),
}))

vi.mock('../AgentDisclosure', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../AgentDisclosure')>()
  return {
    AgentDisclosure: (props: Parameters<typeof actual.AgentDisclosure>[0]) => (
      disclosureState.expanded
        ? <div data-testid={props.testId}>{props.summary}{props.children}</div>
        : <actual.AgentDisclosure {...props} />
    ),
  }
})

function run() {
  return agentRunFromSummary({
    run_id: 'run_activity', status: 'running', queue_position: null,
    question: 'Vergleiche die vorhandenen Quellen.', stack: 'default',
    access: { mode: 'owner' },
    mode: 'agent_kernel', kind: 'agent', agent_overrides: { autonomy: 'balanced' },
    created_at: 1_700_000_000, started_at: 1_700_000_001, finished_at: null,
    elapsed_seconds: null, snapshot: { current_node: 'agent_kernel', last_message: '' },
    error: null, events_url: '/v1/runs/run_activity/events', result_url: '/v1/runs/run_activity/result',
  })
}

const actions = {} as AgentTimelineActions

function addAnswer(record: ReturnType<typeof run>, withSources = true, writing = false) {
  record.artifactOrder = ['answer']
  record.artifacts.answer = {
    artifactId: 'answer', kind: 'answer', title: 'Ergebnis',
    status: writing ? 'writing' : 'ready', revision: 1, updatedBy: 'agent',
    refsCount: withSources ? 1 : 0, createdAt: 1, updatedAt: 2,
    contentMarkdown: 'Abschließende Einordnung des Auftrags.',
    refs: withSources ? [{ label: 'W1', url: 'https://example.org/source', title: 'Beleg' }] : [],
  }
}

describe('run settings in the transcript', () => {
  const settingsRun = (status: 'completed' | 'failed') => {
    const record = agentRunFromSummary({
      run_id: 'run_settings', status, queue_position: null,
      question: 'Welche Pflichten gelten?', stack: 'default',
      access: { mode: 'owner' },
      mode: 'agent_kernel', kind: 'agent',
      agent_overrides: { autonomy: 'balanced', agent_tier: 'gruendlich' },
      created_at: 1_700_000_000, started_at: 1_700_000_001, finished_at: 1_700_000_090,
      elapsed_seconds: 89,
      snapshot: {
        current_node: 'agent_kernel', last_message: '',
        execution: {
          effective_mode: 'agent_kernel', response_form: 'chat', depth: 'normal',
          model: 'gpt-5.4', reasoning_effort: 'medium',
          source_policy: { web: 'disabled', knowledge: 'available' },
          knowledge_scope: 'all', knowledge_collection_count: 3,
          consent_reason: 'permission_policy',
          tool_use_counts: { web: 0, knowledge: 7 },
        },
      },
      error: status === 'failed' ? { message: 'boom', type: 'run_failed' } : null,
      events_url: '/v1/runs/run_settings/events', result_url: '/v1/runs/run_settings/result',
    })
    record.phase = status === 'completed' ? 'done' : 'failed'
    return record
  }

  it('states the conditions of a completed turn on the recap', () => {
    const record = settingsRun('completed')
    addAnswer(record)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    const line = html.match(/data-testid="agent-run-settings"[^>]*>([^<]*)</)
    expect(line?.[1]).toBe(
      'Automatisch · Standard · Gründlich · gpt-5.4 · Aufwand Med · Web aus · Wissen 7 Suchen (alle Sammlungen)',
    )
    expect(html.indexOf('data-testid="agent-run-settings"'))
      .toBeGreaterThan(html.indexOf(translations.de.agent.timeline.recapTitle))
    expect(html).toContain(`aria-label="${translations.de.agent.timeline.recapSettingsLabel}"`)
  })

  it('states them for a failed turn as well', () => {
    const html = renderToStaticMarkup(
      <AgentRunTurn actions={actions} run={settingsRun('failed')} historical />,
    )
    expect(html.match(/data-testid="agent-run-settings"/g)).toHaveLength(1)
    expect(html).toContain('Wissen 7 Suchen (alle Sammlungen)')
  })
})

describe('the rules a turn ran under', () => {
  const line = (html: string) =>
    html.match(/data-testid="agent-run-requirement"[^>]*>([^<]*)</)?.[1] ?? null

  function withRules(
    rules: { templateId: string; label: string; revision: number; title?: string }[],
    review = 'assignment',
    ruleTitles?: Record<string, string>,
  ) {
    const record = run()
    record.requirementMeta = { freeTextChars: 0, review, rules }
    return renderToStaticMarkup(
      <AgentRunTurn actions={actions} historical run={record} ruleTitles={ruleTitles} />,
    )
  }

  it('names each rule with its frozen revision under the question', () => {
    const html = withRules([{ label: 'Executive Briefing', revision: 3, templateId: 'pt_brief' }])
    // The review is OWED when the run starts — the line names the pass,
    // never a verdict the run has not reached yet.
    expect(line(html)).toBe('Regel: Executive Briefing (Rev. 3) · Prüfdurchgang gegen den Auftrag')
    expect(html.indexOf('data-testid="agent-run-requirement"'))
      .toBeGreaterThan(html.indexOf('inqtrix-user-bubble'))
  })

  it('names a rule by the title the pill showed, by its handle once the catalog lost it', () => {
    // The event carries the template handle; before sending the user saw
    // the title. The same rule must not read differently afterwards.
    const html = withRules(
      [
        { label: 'briefing', revision: 3, templateId: 'pt_brief' },
        { label: 'lektor', revision: 1, templateId: 'pt_gone' },
      ],
      'assignment',
      { pt_brief: 'Executive Briefing' },
    )
    expect(line(html)).toBe(
      'Regeln: Executive Briefing (Rev. 3) · lektor (Rev. 1) · Prüfdurchgang gegen den Auftrag',
    )
  })

  it('names a rule by the title it was attached under, before the catalog title and the handle', () => {
    // The library renamed the rule since: the run's line keeps the name the
    // rule had when it was attached, as its frozen revision does.
    const html = withRules(
      [
        { label: 'briefing', revision: 3, templateId: 'pt_brief', title: 'Executive Briefing' },
        { label: 'ton', revision: 1, templateId: 'pt_ton' },
        { label: 'lektor', revision: 2, templateId: 'pt_gone' },
      ],
      'assignment',
      { pt_brief: 'Neuer Name', pt_ton: 'Ton und Stil' },
    )
    expect(line(html)).toBe(
      'Regeln: Executive Briefing (Rev. 3) · Ton und Stil (Rev. 1) · lektor (Rev. 2) · Prüfdurchgang gegen den Auftrag',
    )
  })

  it('says when no review pass checks the result', () => {
    const html = withRules(
      [
        { label: 'Executive Briefing', revision: 3, templateId: 'pt_brief' },
        { label: 'Lektor', revision: 1, templateId: 'pt_lektor' },
      ],
      'none',
    )
    expect(line(html)).toBe(
      'Regeln: Executive Briefing (Rev. 3) · Lektor (Rev. 1) · angewendet, ohne Prüfdurchgang',
    )
  })

  it('renders a review value it does not know without claiming one', () => {
    expect(line(withRules([{ label: 'Lektor', revision: 2, templateId: 'pt_lektor' }], 'per_section')))
      .toBe('Regel: Lektor (Rev. 2)')
  })

  it('says when the review judges each rule on its own', () => {
    expect(line(withRules([{ label: 'Lektor', revision: 2, templateId: 'pt_lektor' }], 'per_rule')))
      .toBe('Regel: Lektor (Rev. 2) · Prüfung je Regel')
    localeState.locale = 'en'
    try {
      expect(line(withRules([{ label: 'Lektor', revision: 2, templateId: 'pt_lektor' }], 'per_rule')))
        .toBe('Rule: Lektor (rev. 2) · reviewed rule by rule')
    } finally {
      localeState.locale = 'de'
    }
  })

  it('speaks English for an English desk', () => {
    localeState.locale = 'en'
    try {
      expect(line(withRules([{ label: 'Lektor', revision: 2, templateId: 'pt_lektor' }], 'none')))
        .toBe('Rule: Lektor (rev. 2) · applied, no review pass')
      expect(line(withRules([{ label: 'Lektor', revision: 2, templateId: 'pt_lektor' }])))
        .toBe('Rule: Lektor (rev. 2) · review pass against the task')
    } finally {
      localeState.locale = 'de'
    }
  })

  it('shows no line for a turn without rules', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={run()} historical />)
    expect(line(html)).toBeNull()
  })
})

describe('a failed turn', () => {
  const failedTask = (taskId: string, ordinal: number) => ({
    taskId, ordinal, title: `Aufgabe ${taskId}`, toolKind: 'rag_query' as const, objective: '', queries: [],
    gapIds: [], dependsOn: [], budget: {}, params: {}, expectedOutput: '', status: 'failed' as const,
    childRunId: null, resultSummary: '',
  })

  it.each([
    ['de', 1, '0/1 abgeschlossen · 1 mit Aufmerksamkeit'],
    ['en', 1, '0/1 completed · 1 needs attention'],
    ['en', 2, '0/2 completed · 2 need attention'],
  ] as const)('counts the tasks that need attention (%s, %i)', (locale, count, line) => {
    // One failed task reads "1 needs attention", never "1 need attention".
    localeState.locale = locale
    try {
      const record = run()
      record.status = 'failed'
      record.phase = 'failed'
      record.error = 'boom'
      const tasks = Array.from({ length: count }, (_, index) => failedTask(`t${index + 1}`, index))
      record.plan = {
        planId: 'p', version: 1, status: 'approved', createdBy: 'agent', reason: '', createdAt: 0,
        summaryMarkdown: '', assumptions: [], successCriteria: [], tasks, versions: [],
      }
      const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
      expect(html).toContain(line)
    } finally {
      localeState.locale = 'de'
    }
  })
})

describe('agent activity transcript', () => {
  it.each([true, false])('places the finished work plan after the answer and before its sources and actions (sources=%s)', (withSources) => {
    const record = run()
    record.status = 'completed'
    record.phase = 'done'
    record.todos = [{ content: 'Auftrag bearbeiten', status: 'completed' }]
    addAnswer(record, withSources)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    const body = html.indexOf('Abschließende Einordnung des Auftrags.')
    const plan = html.indexOf('data-testid="agent-todo-list"')
    const copy = html.indexOf(`aria-label="${translations.de.agent.timeline.answerCopy}"`)
    expect(body).toBeGreaterThan(-1)
    expect(plan).toBeGreaterThan(body)
    expect(copy).toBeGreaterThan(plan)
    expect(html.match(/data-testid="agent-todo-list"/g)).toHaveLength(1)
    expect(html).toContain('aria-expanded="false"')
    const sources = html.indexOf('data-testid="agent-sources"')
    if (withSources) {
      expect(sources).toBeGreaterThan(plan)
      expect(copy).toBeGreaterThan(sources)
    } else {
      expect(sources).toBe(-1)
    }
  })

  it('keeps exactly one expanded work plan while the answer is writing', () => {
    const record = run()
    record.todos = [{ content: 'Ergebnis formulieren', status: 'in_progress' }]
    addAnswer(record, true, true)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html.match(/data-testid="agent-todo-list"/g)).toHaveLength(1)
    expect(html).toContain('Ergebnis formulieren')
    expect(html).not.toContain('data-testid="agent-todo-toggle"')
    expect(html).not.toContain('data-testid="agent-sources"')
  })

  it('names an unavailable quality review in the collapsed completion recap', () => {
    const record = run()
    record.status = 'completed'
    record.phase = 'done'
    record.lastSequence = 2
    record.stepLog = [{ kind: 'activity', activityKind: 'critic_unavailable', status: 'failed', seq: 1, at: 1 }]
    record.outcome = {
      outcomeId: 'run_activity:outcome', final: 'completed', review: 'unavailable',
      summary: 'Prüfung nicht auswertbar, Ergebnis ungeprüft', applied: 0, lines: [], marks: [],
    }
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html).toContain('Prüfung nicht auswertbar, Ergebnis ungeprüft')
  })
  it('keeps parallel evidence reads active until every invocation settles', () => {
    const record = run()
    record.activity = { kind: 'searching', detail: '', at: 1, operation: 'evidence_read', status: 'completed' }
    record.stepLog = [
      { seq: 1, at: 1, kind: 'activity', label: 'read_evidence', status: 'running' },
      { seq: 2, at: 1, kind: 'activity', label: 'read_evidence', status: 'completed' },
    ]
    expect(activityText(record, translations.de, 'de')).toBe('Belege nachlesen · 2 Abrufe · 1/2 abgeschlossen')
    record.stepLog[0].status = 'completed'
    expect(activityText(record, translations.de, 'de')).toBe('Vorhandene Belege aufgerufen · 2 Abrufe')
    record.status = 'waiting_for_approval'
    expect(activityText(record, translations.de, 'de')).toBe(translations.de.agent.activity.waitingApproval)
  })

  it('exposes long activity text as a keyboard-operable disclosure', () => {
    const record = run()
    record.stepLog = [{ seq: 1, at: 1, kind: 'activity', label: 'run_deep_mission', detail: 'Ein ausführlicher Auftrag', status: 'running' }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html).toMatch(/<button[^>]*aria-expanded="false"[^>]*data-testid="agent-activity-text"/)
  })

  it('summarizes retained evidence reads without filling the main transcript with ids', () => {
    const record = run()
    record.stepLog = Array.from({ length: 5 }, (_, index) => ({
      seq: index + 1, at: 1_700_000_010 + index, kind: 'activity' as const,
      label: 'read_evidence', activityKey: `tool:${index}`,
      detail: JSON.stringify({ reference_id: `ref_private_${index}` }), status: 'completed',
    }))
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html).toContain('Vorhandene Belege aufgerufen · 5 Abrufe')
    expect(html).toContain('aria-expanded="false"')
    expect(html).not.toContain('ref_private_')
    expect(html).not.toContain('read_evidence')
  })

  it('keeps a model-reported active todo static while the real activity has its own indicator', () => {
    const record = run()
    record.todos = [{ content: 'Recherche durchführen', status: 'in_progress' }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    const start = html.indexOf('data-testid="agent-todo-list"')
    expect(start).toBeGreaterThan(-1)
    const todo = html.slice(start, html.indexOf('</div>', start))
    expect(todo).toContain('Aufgabenplan')
    expect(todo).toContain('Recherche durchführen')
    expect(todo).not.toContain('inqtrix-running-dot')
    expect(html).toContain('role="status"')
  })

  it('labels completed model todos as reported when the run was cancelled', () => {
    const record = run()
    record.status = 'cancelled'
    record.todos = [{ content: 'Ergebnis aufbereiten', status: 'completed' }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html).toContain('Abgebrochen')
    expect(html).toContain('1/1 als erledigt gemeldet')
    expect(html).not.toContain('role="status"')
  })

  it.each([
    [{ evidenceLimit: true }, 'lucide-info', 'text-muted-foreground'],
    [{ fallback: true }, 'lucide-alert-triangle', 'text-warning'],
  ] as const)('marks an evidence note apart from a fallback row (%o)', (flag, icon, tone) => {
    const record = run()
    record.stepLog = [{
      seq: 1, at: 1, kind: 'activity', activityOperationCode: 'task.evidence_partial',
      detail: 'Teilaufgabe „Marktlage“: 3 Quellen nicht ausgewertet', status: 'completed', ...flag,
    }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    const start = html.indexOf('data-testid="agent-activity-item"')
    expect(start).toBeGreaterThan(-1)
    const rowStart = html.lastIndexOf('<p', start)
    const row = html.slice(rowStart, html.indexOf('</p>', start))
    expect(row).toContain('Teilaufgabe „Marktlage“: 3 Quellen nicht ausgewertet')
    expect(row).toContain(icon)
    expect(row).toContain(tone)
    expect(row).not.toContain('lucide-check')
  })

  it('keeps failed evidence reads visible in the collapsed summary', () => {
    const record = run()
    record.stepLog = [{
      seq: 1, at: 1_700_000_010, kind: 'activity', label: 'read_evidence',
      status: 'failed', error: 'Herkunft nicht erreichbar',
    }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect(html).toContain('Belegabruf fehlgeschlagen')
  })

  it.each([
    ['waiting_for_approval', 'Belegabruf noch offen'],
    ['cancelled', 'Belegabruf beendet · 0/1 abgeschlossen'],
  ] as const)('does not present unfinished reads as ongoing work in %s', (runStatus, expected) => {
    const html = renderToStaticMarkup(<EvidenceReadGroup
      entries={[{ seq: 1, at: 1, kind: 'activity', label: 'read_evidence', status: 'running' }]}
      historical
      runStatus={runStatus}
      t={translations.de}
    />)
    expect(html).toContain(expected)
    expect(html).not.toContain('Belege nachlesen ·')
  })
})

describe('a finished task in the transcript', () => {
  const RESULT = 'EU-Leitlinien vom 02.09. sind veröffentlicht.'
  function taskRun(summary: string) {
    const record = run()
    record.plan = {
      planId: 'plan_1', version: 1, status: 'approved', createdBy: 'agent', reason: '', createdAt: 1,
      summaryMarkdown: '', assumptions: [], successCriteria: [], versions: [],
      tasks: [{
        taskId: 't1', ordinal: 0, title: 'Regulierung erfassen', toolKind: 'rag_query', objective: '',
        queries: [], gapIds: [], dependsOn: [], budget: {}, params: {}, expectedOutput: '',
        status: 'completed', childRunId: null, resultSummary: summary,
      }],
    }
    record.taskStates = { t1: { attempt: 1, status: 'completed' } }
    record.stepLog = [
      { seq: 1, at: 1, kind: 'task_finished', taskId: 't1' },
      { seq: 2, at: 2, kind: 'narration', narrationId: 'n-task-t1', detail: 'task', text: `Regulierung erfassen: ${RESULT}` },
    ]
    return record
  }

  it('states its result once, on the task row', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={taskRun(RESULT)} historical />)
    expect(html.split(RESULT)).toHaveLength(2)
    expect(html).not.toContain(`Regulierung erfassen: ${RESULT}`)
  })

  it('keeps the narration when the row has no result to show', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={taskRun('')} historical />)
    expect(html).toContain(`Regulierung erfassen: ${RESULT}`)
  })
})

describe('the outcome line under the answer', () => {
  const LINES = [
    { kind: 'not_checked', text: 'Die Frist konnte nicht geprüft werden.', reasons: [] },
    { kind: 'limit', text: 'Ein Dokument hielt mehr Stellen, als die Analyse übernahm.', reasons: ['extraction_limit'] },
  ]
  const answered = (review?: string) => {
    const record = run()
    record.status = 'completed'
    record.phase = 'done'
    record.lastSequence = 9
    addAnswer(record)
    if (review) {
      record.outcome = {
        outcomeId: 'run_activity:outcome', final: 'completed', review,
        summary: 'Geprüft · 1 Punkt nicht geprüft · Lesegrenze', applied: 0, lines: LINES, marks: [],
      }
    }
    return record
  }

  it('shows the outcome line under the answer', () => {
    disclosureState.expanded = true
    try {
      const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={answered('completed')} historical />)
      const line = html.slice(html.indexOf('data-testid="agent-run-outcome"'))
      expect(LINES.map((item) => line.includes(item.text))).toEqual([true, true])
    } finally {
      disclosureState.expanded = false
    }
  })

  it('keeps the lines behind their disclosure until the reader opens it', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={answered('completed')} historical />)
    expect([
      html.includes('Geprüft · 1 Punkt nicht geprüft · Lesegrenze'),
      html.includes(translations.de.agent.timeline.outcomeLinesToggle),
      html.includes(LINES[0].text),
    ]).toEqual([true, true, false])
  })

  it.each(['waived', 'not_run'])('shows nothing when no review was owed (%s)', (review) => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={answered(review)} historical />)
    expect([html.includes('data-testid="agent-run-outcome"'), html.includes('Geprüft')]).toEqual([false, false])
  })

  it('drops the line under an answer the user edited and keeps the recap', () => {
    // The record describes the answer the run delivered, not the user's
    // edit: its summary stays in the recap (once) and leaves the answer.
    const record = answered('completed')
    record.artifacts.answer.updatedBy = 'user'
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect([
      html.includes('data-testid="agent-run-outcome"'),
      html.split('Geprüft · 1 Punkt nicht geprüft · Lesegrenze').length - 1,
    ]).toEqual([false, 1])
  })

  it('names a missing record in the recap only, never under the answer', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={answered()} historical />)
    expect([
      html.split(translations.de.agent.timeline.recapNoOutcome).length - 1,
      html.includes('data-testid="agent-run-outcome"'),
    ]).toEqual([1, false])
  })

  const DETAILS = 'ohne Befund · 0 von 1 zitierten Beleg nachgelesen'
  const NOTE = 'Nicht ausreichend belegt: W1 nennt keine Einordnung.'
  const reviewed = (marks: AgentRunOutcome['marks']) => {
    const record = answered('completed')
    record.outcome = {
      ...record.outcome!,
      summary: 'Geprüft · 1 Aussage: 1 nicht ausreichend belegt',
      details: DETAILS,
      marks,
    }
    return record
  }
  const MARK = { outputId: 'chat', quote: 'Abschließende Einordnung des Auftrags.', state: 'insufficient', note: NOTE }

  it('keeps what the review did besides the plaque behind its own disclosure', () => {
    const closed = renderToStaticMarkup(<AgentRunTurn actions={actions} run={reviewed([])} historical />)
    disclosureState.expanded = true
    try {
      const open = renderToStaticMarkup(<AgentRunTurn actions={actions} run={reviewed([])} historical />)
      expect([
        closed.includes(translations.de.agent.timeline.outcomeDetailsToggle),
        closed.includes(DETAILS),
        open.includes(DETAILS),
      ]).toEqual([true, false, true])
    } finally {
      disclosureState.expanded = false
    }
  })

  it("marks the answer's insufficiently supported statement and keeps a live region for its note", () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={reviewed([MARK])} historical />)
    expect([
      html.includes('data-claim-state="insufficient"'),
      html.includes(`data-claim-note="${NOTE}"`),
      html.includes('data-testid="agent-claim-note"'),
    ]).toEqual([true, true, true])
  })

  it('marks nothing while the answer is not published yet', () => {
    // The record arrives before the answer settles; the draft in between
    // is not the text the marks quote.
    const record = reviewed([MARK])
    record.artifacts.answer.status = 'writing'
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect([html.includes('data-claim-state'), html.includes('data-testid="agent-claim-note"')])
      .toEqual([false, false])
  })

  it('marks nothing of a canvas on the answer, and nothing on an answer the user edited', () => {
    const foreign = renderToStaticMarkup(
      <AgentRunTurn actions={actions} run={reviewed([{ ...MARK, outputId: 'art_doc' }])} historical />,
    )
    const edited = reviewed([MARK])
    edited.artifacts.answer.updatedBy = 'user'
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={edited} historical />)
    expect([
      foreign.includes('data-claim-state'),
      foreign.includes('data-testid="agent-claim-note"'),
      html.includes('data-claim-state'),
    ]).toEqual([false, false, false])
  })
})

describe('one review sentence', () => {
  it('states the review outcome once, under the answer', () => {
    // The regression: the review's sentence stood three times — in
    // the recap, as the review's closing narration row and under the answer.
    const record = run()
    record.status = 'completed'
    record.phase = 'done'
    record.elapsedSeconds = 304
    addAnswer(record)
    const summary = 'Geprüft · 5 Befunde überarbeitet'
    record.outcome = {
      outcomeId: 'run_activity:outcome', final: 'completed', review: 'completed',
      summary, applied: 5, lines: [], marks: [],
    }
    record.stepLog = [
      { kind: 'narration', narrationId: 'kernel_deep_review', seq: 1, at: 1,
        text: `Verifikations-Durchlauf: ${summary}` },
    ]
    const markup = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} />)
    expect(markup.split(summary).length - 1).toBe(1)
    expect(markup).toContain('data-testid="agent-run-outcome"')
  })

  it('keeps the sentence in the recap when there is no answer to carry it', () => {
    const record = run()
    record.status = 'completed'
    record.phase = 'done'
    const summary = 'Geprüft · ohne Befund'
    record.outcome = {
      outcomeId: 'run_activity:outcome', final: 'completed', review: 'completed',
      summary, applied: 0, lines: [], marks: [],
    }
    const markup = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} />)
    expect(markup.split(summary).length - 1).toBe(1)
  })
})

describe('the draft while the review runs', () => {
  const DRAFT = 'Die Frist gilt ab August 2026 [W1].'
  const FINAL = 'Die Frist gilt ab August 2026, sobald die Leitlinien veröffentlicht sind [W1].'
  const timeline = translations.de.agent.timeline

  function withDraft(status: 'running' | 'completed' | 'cancelled' = 'running') {
    const record = run()
    record.status = status
    if (status === 'completed') record.phase = 'done'
    record.answerDraft = {
      artifactId: 'answer', draftId: 'drf_answer', markdown: DRAFT,
      referenceLabels: ['W1'], createdAt: 5,
    }
    return record
  }

  function publish(record: ReturnType<typeof run>, body: string, writing = false) {
    addAnswer(record, true, writing)
    record.artifacts.answer.contentMarkdown = body
  }

  it('shows the draft as being reviewed, with no copy and no export', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={withDraft()} />)
    expect([
      html.includes('Die Frist gilt ab August 2026'),
      html.includes(timeline.answerDraftReviewing),
      html.includes('data-answer-state="draft-reviewing"'),
      html.includes(`aria-label="${timeline.answerCopy}"`),
      html.includes(`aria-label="${timeline.answerOpenCanvas}"`),
    ]).toEqual([true, true, true, false, false])
  })

  it('shows the work plan once beside the draft', () => {
    const record = withDraft()
    record.todos = [{ content: 'Kurzfazit formulieren', status: 'in_progress' }]
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} />)
    expect(html.split('Kurzfazit formulieren').length - 1).toBe(1)
  })

  it('keeps the draft while the reviewed answer is still being published', () => {
    const record = withDraft()
    publish(record, '## Neue Lage', true)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} />)
    expect([
      html.includes('Die Frist gilt ab August 2026'),
      html.includes('Neue Lage'),
      html.includes('data-answer-state="draft-reviewing"'),
    ]).toEqual([true, false, true])
  })

  it('replaces the draft by the reviewed answer and marks what the review changed', () => {
    const record = withDraft('completed')
    publish(record, FINAL)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect([
      html.includes('sobald die Leitlinien veröffentlicht sind'),
      html.includes('data-review-changed'),
      html.includes('data-answer-state="ready"'),
      html.includes(timeline.answerRevisedMarks),
      html.includes(timeline.answerShowDraft),
      html.split('Die Frist gilt ab August 2026').length - 1,
      html.includes(`aria-label="${timeline.answerCopy}"`),
    ]).toEqual([true, true, true, true, true, 1, true])
  })

  it('shows the draft text behind its disclosure', () => {
    disclosureState.expanded = true
    try {
      const record = withDraft('completed')
      publish(record, FINAL)
      const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
      const shown = html.slice(html.indexOf('data-testid="agent-answer-draft"'))
      expect([shown.includes('Die Frist gilt ab August 2026'), shown.includes('sobald')]).toEqual([true, false])
    } finally {
      disclosureState.expanded = false
    }
  })

  it('only changes the state when the review changed nothing', () => {
    const record = withDraft('completed')
    publish(record, DRAFT)
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={record} historical />)
    expect([
      html.includes('data-answer-state="ready"'),
      html.includes('data-review-changed'),
      html.includes(timeline.answerShowDraft),
      html.includes(timeline.answerRevisedMarks),
    ]).toEqual([true, false, false, false])
  })

  it('keeps the draft as unreviewed when the run was cancelled', () => {
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={withDraft('cancelled')} historical />)
    expect([
      html.includes('Die Frist gilt ab August 2026'),
      html.includes(timeline.answerDraftUnreviewed),
      html.includes('data-answer-state="draft-unreviewed"'),
      html.includes(`aria-label="${timeline.answerCopy}"`),
      html.includes(timeline.cancelled),
    ]).toEqual([true, true, true, false, true])
  })
})

describe('the review row', () => {
  const FINDINGS = [
    { quote: 'Der Markt wuchs um 12 %.', finding: 'Ohne Beleg.', target: 'chat' },
    { quote: 'Die Quelle ist eindeutig.', finding: 'Widerspruch zu K2.', target: 'art_1' },
  ]

  function reviewed() {
    const record = run()
    record.stepLog = [{
      kind: 'activity', seq: 1, at: 1, activityKind: 'deep_review_findings',
      label: 'Verifikation: 2 Befunde',
      detail: 'Chat: Der Markt wuchs um 12 %. Ohne Beleg.',
      status: 'completed', findings: FINDINGS,
    }]
    return record
  }

  it('is one short line with its count, the findings folded under it', () => {
    // The row stood eight lines tall: title, the full detail and every
    // finding. The count is the row; the passages are one click away.
    const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={reviewed()} />)
    expect([
      html.includes('Verifikation: 2 Befunde'),
      html.includes(translations.de.agent.timeline.reviewFindingsToggle),
      html.includes('Der Markt wuchs um 12 %.'),
    ]).toEqual([true, true, false])
  })

  it('shows the passages the review objected to once opened', () => {
    disclosureState.expanded = true
    try {
      const html = renderToStaticMarkup(<AgentRunTurn actions={actions} run={reviewed()} />)
      expect(FINDINGS.map((finding) => html.includes(finding.quote))).toEqual([true, true])
    } finally {
      disclosureState.expanded = false
    }
  })
})
