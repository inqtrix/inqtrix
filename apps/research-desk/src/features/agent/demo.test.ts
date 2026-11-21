import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

import type { ResearchDeskAction } from '@/features/researchDesk/state'
import type { ResearchRunEvent } from '@/features/researchRuns/types'
import { translations } from '@/i18n/translations'
import { attachedReportsMax } from './contextCategories'
import {
  createAgentDemo,
  DEMO_AGENT_ATTACHED_REPORTS,
  DEMO_AGENT_REPORT_REQUIREMENT,
  DEMO_ANALYSIS_CALL,
  DEMO_ANALYSIS_OBJECTIVE,
  DEMO_BATCH_ACTION,
  DEMO_BATCH_KNOWLEDGE_SCOPE,
  DEMO_QUICK_WEB_ACTION,
  demoRunSummary,
  type AgentDemoActions,
} from './demo'
import { CHILD_PROGRESS_PROJECTED_KEYS } from './childProgress'
import { gateActionRow, gateKnowledgeScope, toolGateHeadline } from './gateSummary'
import { reportComposedCharsMax, reportRuleIdsMax } from './reportRequirement'
import { normalizeAgentExecutionSnapshot } from './executionPolicy'

describe('Agent Desk demo contract twin', () => {
  it('publishes the selected run contract in the canonical execution block', () => {
    const summary = demoRunSummary(
      'RA-demo-test',
      'session-test',
      'Question',
      'balanced',
      'running',
      'deep',
      'auto',
      { web: 'disabled', knowledge: 'available' },
      { model: 'model-test', effort: 'high', tier: null },
    )

    expect(summary.mode).toBe('agent_kernel')
    expect(summary.agent_overrides).toMatchObject({
      autonomy: 'balanced',
      depth: 'deep',
      effort: 'high',
      model: 'model-test',
      source_policy: { web: 'disabled', knowledge: 'available' },
    })
    expect(normalizeAgentExecutionSnapshot(summary.snapshot)).toMatchObject({
      consentReason: 'permission_policy',
      depth: 'deep',
      effectiveMode: 'agent_kernel',
      model: 'model-test',
      reasoningEffort: 'high',
      responseForm: 'chat',
      sourcePolicy: { web: 'disabled', knowledge: 'available' },
      toolUseCounts: { web: 0, knowledge: 0 },
    })
  })

  it('applies a strict quick-web directive exactly like the accepted server route', () => {
    const summary = demoRunSummary(
      'RA-demo-web',
      'session-test',
      'Current question',
      'strict',
      'running',
      'deep',
      'canvas',
      { web: 'disabled', knowledge: 'available' },
    )

    expect(summary.mode).toBe('agent_kernel')
    expect(normalizeAgentExecutionSnapshot(summary.snapshot)).toMatchObject({
      consentReason: 'permission_policy',
      depth: 'deep',
      effectiveMode: 'agent_kernel',
      responseForm: 'canvas',
      sourcePolicy: { web: 'disabled', knowledge: 'available' },
    })
  })

  const demoTree = () => {
    const source = readFileSync(new URL('./demo.ts', import.meta.url), 'utf8')
    return ts.createSourceFile('demo.ts', source, ts.ScriptTarget.Latest, true)
  }

  it('speaks the role previews as counted German, like the server', () => {
    const previews: string[] = []
    function visit(node: ts.Node) {
      if (ts.isPropertyAssignment(node) && node.name.getText() === 'result_preview' && ts.isStringLiteral(node.initializer)) {
        previews.push(node.initializer.text)
      }
      ts.forEachChild(node, visit)
    }
    visit(demoTree())
    expect(previews).toEqual(['2 Befunde gemeldet'])
  })

  it('the demo sends outcome records and never says behoben', () => {
    // Every demo run that completes sends its record first, as the server
    // does, also a run whose web search was rejected at its gate; no demo
    // text calls an unchecked edit "behoben".
    const { dispatched, events } = playDemo((demo, sent) => {
      const base = { question: 'Wie ist die Lage?', sessionId: 'session-demo-outcome' }
      demo.submit({ ...base, autonomy: 'autonomous', agentTier: 'gruendlich' })
      demo.submit({ ...base, autonomy: 'autonomous', depth: 'deep' })
      demo.submit({ ...base, autonomy: 'autonomous', agentTier: 'tief' })
      demo.submit({ ...base, autonomy: 'balanced', agentTier: 'schnell' })
      vi.advanceTimersByTime(120_000)
      const gated = sent.flatMap((action) => (
        action.type === 'setAgentRunApprovals' ? action.approvals : []
      ))
      void demo.decideApproval(gated[0].run_id, gated[0].approval_id, { decision: 'reject' })
      vi.advanceTimersByTime(120_000)
    })
    const completed = events.filter((item) => item.type === 'inqtrix.run.completed').map((item) => item.run_id)
    const unrecorded = completed.filter((runId) => !events.some((item, index) => (
      item.run_id === runId
      && item.type === 'inqtrix.agent.outcome'
      && typeof item.data.summary === 'string'
      && index < events.findIndex((other) => other.run_id === runId && other.type === 'inqtrix.run.completed')
    )))
    expect({
      completed: completed.length,
      unrecorded,
      behoben: JSON.stringify(dispatched).toLowerCase().includes('behoben'),
    }).toEqual({ completed: 4, unrecorded: [], behoben: false })
  })

  it('a Stufe that skips the review sends a waived record and enters no review phase', () => {
    // The server runs no review for a Stufe at verify level `labels` and
    // records it as waived; the twin claims none either.
    const { events } = playDemo((demo) => {
      demo.submit({
        question: 'Wie ist die Lage?', sessionId: 'session-demo-waived',
        autonomy: 'autonomous', agentTier: 'schnell',
      })
      vi.advanceTimersByTime(120_000)
    })
    expect({
      reviews: events.filter((item) => item.type === 'inqtrix.agent.outcome').map((item) => item.data.review),
      critic: events.some((item) => item.type === 'inqtrix.agent.phase.changed' && item.data.phase === 'critic'),
    }).toEqual({ reviews: ['waived'], critic: false })
  })
})

describe('the kernel arcs of the demo', () => {
  // The demo plays only what the kernel does: no part of the retired
  // mission reaches the desk, in no Stufe and in no mode, also not as the
  // event of a child.
  const MISSION_EVENTS = new Set([
    'inqtrix.agent.plan.proposed',
    'inqtrix.agent.plan.revised',
    'inqtrix.agent.patch.proposed',
    'inqtrix.agent.task.started',
    'inqtrix.agent.task.cancel_requested',
    'inqtrix.agent.task.finished',
    'inqtrix.agent.task.failed',
  ])
  const MISSION_TOOLS = new Set(['run_deep_mission', 'section_role'])
  const MISSION_STATIONS = new Set(['discovery', 'planning', 'synthesis', 'critic', 'patch', 'clarification', 'evidence'])

  function playEveryArc() {
    return playDemo((demo, sent) => {
      const base = { question: 'Wie ist die Lage?', sessionId: 'session-demo-kernel' }
      for (const agentTier of ['schnell', 'gruendlich', 'tief']) {
        for (const autonomy of ['balanced', 'autonomous']) {
          demo.submit({ ...base, agentTier, autonomy })
          demo.submit({ ...base, agentTier, autonomy, responseForm: 'canvas' })
          demo.submit({ ...base, agentTier, autonomy, responseForm: 'chat' })
        }
      }
      // Answer every question and approve every gate until nothing waits.
      const answered = new Set<string>()
      const decided = new Set<string>()
      for (let round = 0; round < 6; round += 1) {
        vi.advanceTimersByTime(60_000)
        for (const action of sent) {
          if (action.type === 'setAgentRunClarifications') {
            for (const row of action.clarifications) {
              if (row.status !== 'pending' || answered.has(row.clarification_id)) continue
              answered.add(row.clarification_id)
              void demo.answerClarification(row.run_id, row.clarification_id, { answer: 'Nur EU' })
            }
          }
          if (action.type === 'setAgentRunApprovals') {
            for (const row of action.approvals) {
              if (row.status !== 'pending' || decided.has(row.approval_id)) continue
              decided.add(row.approval_id)
              void demo.decideApproval(row.run_id, row.approval_id, { decision: 'approve' })
            }
          }
        }
      }
      vi.advanceTimersByTime(180_000)
    })
  }

  it('sends no mission event, tool, gate, station or plan in any Stufe', () => {
    const { dispatched, events } = playEveryArc()
    const childTypes = events
      .filter((item) => item.type === 'inqtrix.agent.child.progress')
      .map((item) => String(item.data.event_type))
    expect({
      events: events.filter((item) => MISSION_EVENTS.has(item.type)).map((item) => item.type),
      children: childTypes.filter((type) => MISSION_EVENTS.has(type)),
      tools: events.filter((item) => MISSION_TOOLS.has(String(item.data.tool ?? ''))).map((item) => item.data.tool),
      stations: events
        .filter((item) => item.type === 'inqtrix.agent.phase.changed' && MISSION_STATIONS.has(String(item.data.phase)))
        .map((item) => item.data.phase),
      gates: dispatched.flatMap((action) => (
        action.type === 'setAgentRunApprovals' ? action.approvals.map((row) => row.kind) : []
      )).filter((kind) => kind !== 'tool'),
    }).toEqual({ events: [], children: [], tools: [], stations: [], gates: [] })
  })

  it('ends every run with an answer, and a run that asks for a canvas with its report there', () => {
    const { dispatched, events } = playEveryArc()
    const runs = [...new Set(events.map((item) => item.run_id))]
    const completed = new Set(events.filter((item) => item.type === 'inqtrix.run.completed').map((item) => item.run_id))
    const details = dispatched.flatMap((action) => (
      action.type === 'setAgentRunArtifactDetail' ? [{ runId: action.runId, kind: action.artifact.kind }] : []
    ))
    const answered = new Set(details.filter((item) => item.kind === 'answer').map((item) => item.runId))
    const reported = new Set(details.filter((item) => item.kind === 'deliverable').map((item) => item.runId))
    expect(runs).toHaveLength(18)
    expect(runs.filter((runId) => !completed.has(runId) || !answered.has(runId))).toEqual([])
    // Tief writes its report unless the run asks for the chat; Schnell and
    // Gründlich only on request.
    expect(reported.size).toBe(8)
  })
})

describe('the requirement limits in the demo', () => {
  // The demo has no capability manifest. Without its mirror the composer
  // said "the server checks the number when you send" for a twin that
  // checks nothing, and no cap state ever showed there.
  it('mirrors the numbers a current server publishes', () => {
    // The server's own constants (report_requirement.py), spelled here on
    // purpose: a test that read them from the mirror would be green by
    // construction.
    expect(DEMO_AGENT_REPORT_REQUIREMENT).toEqual({
      max_composed_chars: 20_000,
      max_rules: 3,
    })
  })

  it('gives the surfaces a cap and a ceiling to pre-check', () => {
    expect(reportRuleIdsMax(DEMO_AGENT_REPORT_REQUIREMENT)).toBe(3)
    expect(reportComposedCharsMax(DEMO_AGENT_REPORT_REQUIREMENT)).toBe(20_000)
  })
})

describe('the report cap in the demo', () => {
  // The demo has no capability manifest. Without its mirror the report
  // category said the server checks the number for a twin that checks
  // nothing, and the cap state never showed there.
  it('mirrors the number a current server publishes', () => {
    // The server's own constant (MAX_ATTACHED_REPORTS), spelled here on
    // purpose: a test that read it from the mirror would be green by
    // construction.
    expect(DEMO_AGENT_ATTACHED_REPORTS).toEqual({ max_reports: 3 })
    expect(attachedReportsMax(DEMO_AGENT_ATTACHED_REPORTS)).toBe(3)
  })

  it('is what the workspace reads in the demo, with no number of its own', () => {
    // Source pin (the workspace is not rendered in this lane): the cap is
    // the server's, or in the demo its mirror — never a literal fallback.
    const source = readFileSync(new URL('./AgentWorkspace.tsx', import.meta.url), 'utf8')
    const tree = ts.createSourceFile('AgentWorkspace.tsx', source, ts.ScriptTarget.Latest, true)
    const initializers = (name: string): string[] => {
      const found: string[] = []
      function visit(node: ts.Node) {
        if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) {
          found.push(node.initializer.getText(tree).replace(/\s+/g, ' '))
        }
        ts.forEachChild(node, visit)
      }
      visit(tree)
      return found
    }
    expect(initializers('publishedAttachedReports')).toEqual([
      'agentBlock?.attached_reports ?? (demo ? DEMO_AGENT_ATTACHED_REPORTS : null)',
    ])
    expect(initializers('reportLimit')).toEqual(['attachedReportsMax(publishedAttachedReports)'])
  })
})

describe('rules in the demo', () => {
  // The demo has no server catalog: it resolves rule ids against the
  // project's own rules, exactly where the server would read its catalog.
  const catalog: Record<string, { label: string; revision: number; title?: string }> = {
    'rule-brief': { label: 'briefing', revision: 1, title: 'Executive\n  Briefing' },
    'rule-lektor': { label: 'lektor', revision: 1 },
  }
  const resolveRule = (id: string) => catalog[id] ?? null

  function playWithRules(
    play: (demo: AgentDemoActions, dispatched: ResearchDeskAction[]) => void,
  ): { dispatched: ResearchDeskAction[]; events: ResearchRunEvent[] } {
    const dispatched: ResearchDeskAction[] = []
    vi.useFakeTimers()
    vi.stubGlobal('window', globalThis)
    try {
      play(createAgentDemo((action) => { dispatched.push(action) }, resolveRule), dispatched)
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
    const events = dispatched.flatMap((action) => (
      action.type === 'appendApiRunEvent' ? [action.event] : []
    ))
    return { dispatched, events }
  }

  const attachedOf = (events: ResearchRunEvent[]) => events
    .filter((item) => item.type === 'inqtrix.agent.requirement.attached')
    .map((item) => item.data)

  it('records the rules a run starts under, like the server', () => {
    const { events } = playWithRules((demo) => {
      demo.submit({
        agentTier: 'gruendlich', autonomy: 'autonomous',
        question: 'Halte dich an Executive Briefing.', ruleIds: ['rule-brief'], sessionId: 'session-demo-rules',
      })
      demo.submit({
        agentTier: 'schnell', autonomy: 'autonomous',
        question: 'Kurz.', ruleIds: ['rule-lektor'], sessionId: 'session-demo-rules',
      })
      demo.submit({
        autonomy: 'autonomous', question: 'Ohne Regel.', sessionId: 'session-demo-rules',
      })
      vi.advanceTimersByTime(1_000)
    })
    expect(attachedOf(events)).toEqual([
      {
        free_text_chars: 0,
        // The kernel's review judges each attached rule on its own.
        review: 'per_rule',
        // Like the server: the title on one line, and none invented.
        rules: [{ label: 'briefing', revision: 1, template_id: 'rule-brief', title: 'Executive Briefing' }],
      },
      {
        free_text_chars: 0,
        review: 'none',
        rules: [{ label: 'lektor', revision: 1, template_id: 'rule-lektor' }],
      },
    ])
  })

})

describe('the tool gate of the quick web search in the demo', () => {
  type Decision = Parameters<AgentDemoActions['decideApproval']>[2]
  const approvalsOf = (dispatched: ResearchDeskAction[]) => dispatched.flatMap((action) => (
    action.type === 'setAgentRunApprovals' ? action.approvals : []
  ))
  const quickWebTask = (events: ResearchRunEvent[]) => events
    .filter((item) => item.type.startsWith('inqtrix.agent.tool.') && item.data.tool === 'web_instant')
    .map((item) => `${item.type.replace('inqtrix.agent.tool.', '')} ${String(item.data.status ?? '')}`.trim())

  /** A run that asks at its gates, played past its question (`ask_user`)
   * and then through the decision at the gate of its web search. */
  function playPastTheClarification(
    decision: Decision | null,
    sourcePolicy: { web: 'available' | 'disabled'; knowledge: 'available' | 'disabled' } = {
      web: 'available', knowledge: 'available',
    },
  ) {
    return playDemo((demo, sent) => {
      demo.submit({
        autonomy: 'balanced', question: 'Analyse.', sessionId: 'session-demo-tool-gate', sourcePolicy,
      })
      vi.advanceTimersByTime(30_000)
      const clarification = sent.flatMap((action) => (
        action.type === 'setAgentRunClarifications' ? action.clarifications : []
      )).find((row) => row.status === 'pending')
      if (!clarification) throw new Error('the demo asked no clarification')
      void demo.answerClarification(clarification.run_id, clarification.clarification_id, { answer: 'Nur EU' })
      vi.advanceTimersByTime(30_000)
      if (decision) {
        void demo.decideApproval(clarification.run_id, `apr-tool-${clarification.run_id}`, decision)
        vi.advanceTimersByTime(120_000)
      }
    })
  }

  it('parks the run before the search and shows the form the search is asked for', () => {
    const { dispatched, events } = playPastTheClarification(null)
    const gate = approvalsOf(dispatched).filter((row) => row.kind === 'tool')
    expect(gate.map((row) => [row.status, row.payload])).toEqual([
      ['pending', { actions: [DEMO_QUICK_WEB_ACTION] }],
    ])
    expect(events.at(-1)).toMatchObject({
      type: 'inqtrix.run.waiting', data: { status: 'waiting_for_approval' },
    })
    expect(quickWebTask(events)).toEqual([])
    // The seed names a form, so the card has its line and the edit its choice.
    const row = gateActionRow(DEMO_QUICK_WEB_ACTION, translations.de)
    expect([row.text, row.note]).toEqual([
      DEMO_QUICK_WEB_ACTION.args.query, 'Gewünschte Form der Suchantwort: Zeitleiste',
    ])
  })

  it.each<[string, Decision, string, Record<string, unknown>]>([
    ['approve', { decision: 'approve' }, 'approved', {}],
    // A run-wide approval keeps its scope in the stored decision, as on the server.
    ['approve for the run', { decision: 'approve', approval_scope: 'run' }, 'approved', { approval_scope: 'run' }],
    [
      'edit',
      { decision: 'edit', actions: [{ tool: 'web_instant', args: { query: 'neu', answer_form: 'table' } }] },
      'edited',
      { actions: [{ tool: 'web_instant', args: { query: 'neu', answer_form: 'table' } }] },
    ],
  ])('runs the search after an %s and finishes the run', (_case, decision, status, decided) => {
    const { dispatched, events } = playPastTheClarification(decision)
    const row = approvalsOf(dispatched).filter((item) => item.kind === 'tool').at(-1)
    expect([row?.status, row?.decision_payload]).toEqual([status, decided])
    expect(quickWebTask(events)).toEqual(['started', 'finished success'])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('skips a rejected search and finishes the run without it', () => {
    const { dispatched, events } = playPastTheClarification({ decision: 'reject' })
    expect(approvalsOf(dispatched).filter((item) => item.kind === 'tool').at(-1)?.status).toBe('rejected')
    expect(quickWebTask(events)).toEqual([])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('takes the run up once, before the gate, and not again after the decision', () => {
    const { events } = playPastTheClarification({ decision: 'approve' })
    expect(events.filter((item) => item.type === 'inqtrix.run.started')).toHaveLength(1)
  })

  it('asks for no search where the run has no web', () => {
    const { dispatched, events } = playPastTheClarification(null, { web: 'disabled', knowledge: 'available' })
    expect(approvalsOf(dispatched).filter((row) => row.kind === 'tool')).toEqual([])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('asks nothing of a run that decides for itself', () => {
    const { dispatched, events } = playDemo((demo) => {
      demo.submit({ autonomy: 'autonomous', question: 'Analyse.', sessionId: 'session-demo-tool-gate' })
      vi.advanceTimersByTime(120_000)
    })
    expect(approvalsOf(dispatched)).toEqual([])
    expect(quickWebTask(events)).toEqual(['started', 'finished success'])
  })
})

describe('the batch of subtasks a deep demo run delegates first', () => {
  type Decision = { decision: string }
  const approvalsOf = (dispatched: ResearchDeskAction[]) => dispatched.flatMap((action) => (
    action.type === 'setAgentRunApprovals' ? action.approvals : []
  ))
  const toolSteps = (events: ResearchRunEvent[], tool: string) => events
    .filter((item) => item.type.startsWith('inqtrix.agent.tool.') && item.data.tool === tool)
    .map((item) => `${item.type.replace('inqtrix.agent.tool.', '')} ${String(item.data.status ?? '')}`.trim())
  const batchChildren = (events: ResearchRunEvent[]) => events
    .filter((item) => item.type === 'inqtrix.agent.child.progress' && String(item.data.task_id).startsWith('demo-batch-1:'))

  /** A deep kernel run, played to its first stop and then through the
   * decision at the gate of its batch. */
  type Switch = 'available' | 'disabled'
  function playDeep(
    autonomy: 'balanced' | 'autonomous',
    decision: Decision | null,
    sourcePolicy: { web: Switch; knowledge: Switch } = { web: 'available', knowledge: 'available' },
  ) {
    return playDemo((demo, sent) => {
      demo.submit({
        agentTier: 'tief', autonomy, question: 'Was gilt für uns?',
        sessionId: 'session-demo-batch', sourcePolicy,
      })
      vi.advanceTimersByTime(30_000)
      if (!decision) return
      const gate = approvalsOf(sent).find((row) => row.status === 'pending')
      if (!gate) throw new Error('the demo opened no gate')
      void demo.decideApproval(gate.run_id, gate.approval_id, decision)
      vi.advanceTimersByTime(180_000)
    })
  }

  it('analyses a whole document between the batch and the report when project knowledge is on', () => {
    const { events } = playDeep('autonomous', null)
    expect(toolSteps(events, 'analyze_document')).toEqual(['started', 'finished success'])
    const started = events.find((item) => item.type === 'inqtrix.agent.tool.started' && item.data.tool === 'analyze_document')
    expect(started?.data.args_preview).toBe(DEMO_ANALYSIS_OBJECTIVE)
    // One live row for the document, keyed by the call, counting its windows:
    // three of the rows the server sends (the first and every fifth), then the settled row.
    const rows = events.filter((item) => item.type === 'inqtrix.agent.activity' && item.data.operation === 'document_analysis')
    expect(rows.map((item) => [item.data.status, item.data.current, item.data.total])).toEqual([
      ['started', 1, 32], ['started', 15, 32], ['started', 30, 32], ['completed', 32, 32],
    ])
    expect(new Set(rows.map((item) => String(item.data.activity_id))))
      .toEqual(new Set([`${DEMO_ANALYSIS_CALL}:document-analysis:kd-demo-ki-verordnung`]))
    expect(rows.every((item) => item.data.scope === 'run' && !('task_id' in item.data))).toBe(true)
    const order = events.map((item) => `${item.type}:${String(item.data.tool ?? '')}`)
    expect(order.indexOf('inqtrix.agent.tool.finished:delegate_batch'))
      .toBeLessThan(order.indexOf('inqtrix.agent.tool.started:analyze_document'))
    expect(order.indexOf('inqtrix.agent.tool.finished:analyze_document'))
      .toBeLessThan(order.indexOf('inqtrix.agent.tool.started:write_canvas'))
    expect(toolGateHeadline([{ tool: 'analyze_document', args: { objective: DEMO_ANALYSIS_OBJECTIVE } }], translations.de))
      .toBe('Freigabe: Dokumente auswerten')
  })

  it('analyses no document when project knowledge is off', () => {
    const { events } = playDeep('autonomous', null, { web: 'available', knowledge: 'disabled' })
    expect(toolSteps(events, 'analyze_document')).toEqual([])
    expect(events.some((item) => item.data.operation === 'document_analysis')).toBe(false)
  })

  it('parks at the approval of the batch, which names the collections and the time window', () => {
    const { dispatched, events } = playDeep('balanced', null)
    const gate = approvalsOf(dispatched)
    expect(gate.map((row) => [row.kind, row.status, row.payload])).toEqual([
      ['tool', 'pending', { actions: [DEMO_BATCH_ACTION], knowledge_scope: DEMO_BATCH_KNOWLEDGE_SCOPE }],
    ])
    expect(events.at(-1)).toMatchObject({ type: 'inqtrix.run.waiting', data: { status: 'waiting_for_approval' } })
    expect([toolSteps(events, 'delegate_batch'), toolSteps(events, 'write_canvas'), batchChildren(events)])
      .toEqual([['started'], [], []])
    // What the card says from this seed: both lines a batch can carry.
    const row = gateActionRow(DEMO_BATCH_ACTION, translations.de)
    expect(toolGateHeadline([DEMO_BATCH_ACTION], translations.de)).toBe('Freigabe: Fan-out — 2 parallele Unteraufträge')
    expect(gateKnowledgeScope(gate[0].payload)).toEqual(['EU-Recht'])
    expect(row.items.map((item) => item.replace(/^.*\?/, '').trim())).toEqual([
      '(knowledge)', '(research) · Bevorzugter Zeitraum der Quellen: letzte 30 Tage',
    ])
  })

  it.each([
    ['approve', 'approved'],
    ['approve for the run', 'approved'],
  ])('runs both subtasks after an %s and goes on to the report', (kind, status) => {
    const decision = kind === 'approve' ? { decision: 'approve' } : { decision: 'approve', approval_scope: 'run' }
    const { dispatched, events } = playDeep('balanced', decision)
    expect(approvalsOf(dispatched).at(-1)?.status).toBe(status)
    expect(approvalsOf(dispatched).at(-1)?.decision_payload).toEqual(kind === 'approve' ? {} : { approval_scope: 'run' })
    const children = batchChildren(events)
    // One child per assignment, joined to the row by the call and its
    // place; each is queued, runs and completes.
    expect([...new Set(children.map((item) => String(item.data.task_id)))]).toEqual(['demo-batch-1:0', 'demo-batch-1:1'])
    expect(children.filter((item) => item.data.run_status === 'completed').map((item) => String(item.data.task_id)).sort())
      .toEqual(['demo-batch-1:0', 'demo-batch-1:1'])
    for (const child of children) {
      for (const key of Object.keys(child.data)) {
        expect(CHILD_PROGRESS_PROJECTED_KEYS.has(key), `stray demo key ${key}`).toBe(true)
      }
    }
    expect(toolSteps(events, 'delegate_batch')).toEqual(['started', 'finished success'])
    // The batch is settled before the report starts, and the run ends.
    const order = events.map((item) => `${item.type}:${String(item.data.tool ?? '')}`)
    expect(order.indexOf('inqtrix.agent.tool.finished:delegate_batch'))
      .toBeLessThan(order.indexOf('inqtrix.agent.tool.started:write_canvas'))
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('says what each subtask found on a line of its own', () => {
    const { events } = playDeep('balanced', { decision: 'approve' })
    const lines = events
      .filter((item) => item.type === 'inqtrix.agent.narration' && String(item.data.narration_id).startsWith('n-child-'))
      .map((item) => String(item.data.text))
    expect(lines).toHaveLength(2)
    expect(lines.map((line) => line.includes(': '))).toEqual([true, true])
  })

  it('settles a rejected batch as a failed step, starts none of its subtasks and goes on', () => {
    const { dispatched, events } = playDeep('balanced', { decision: 'reject' })
    expect(approvalsOf(dispatched).at(-1)?.status).toBe('rejected')
    expect([toolSteps(events, 'delegate_batch'), batchChildren(events)]).toEqual([['started', 'finished error'], []])
    expect(toolSteps(events, 'write_canvas')).toEqual(['started', 'finished success'])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('takes the run up once before the gate and once after the decision', () => {
    const { events } = playDeep('balanced', { decision: 'approve' })
    expect(events.filter((item) => item.type === 'inqtrix.run.started')).toHaveLength(2)
    expect(events.filter((item) => item.type === 'inqtrix.agent.phase.changed' && item.data.phase === 'execution'))
      .toHaveLength(1)
  })

  it.each([
    ['project knowledge', { web: 'available', knowledge: 'disabled' }, 'research', false],
    ['the web', { web: 'disabled', knowledge: 'available' }, 'knowledge', true],
  ] as const)('delegates no assignment to %s where the run has it switched off', (_source, sourcePolicy, mode, named) => {
    // A run without project knowledge that showed a knowledge assignment
    // would show what the server refuses.
    const { dispatched, events } = playDeep('balanced', { decision: 'approve' }, sourcePolicy)
    const gate = approvalsOf(dispatched)[0]
    const actions = gate.payload.actions as (typeof DEMO_BATCH_ACTION)[]
    expect(actions[0].args.assignments.map((assignment) => assignment.mode)).toEqual([mode])
    expect(gateKnowledgeScope(gate.payload)).toEqual(named ? ['EU-Recht'] : [])
    expect(toolGateHeadline(actions, translations.de)).toBe('Freigabe: Fan-out — 1 Unterauftrag')
    expect([...new Set(batchChildren(events).map((item) => String(item.data.task_id)))]).toEqual(['demo-batch-1:0'])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('delegates no batch where the run has both sources switched off, and goes straight to the report', () => {
    const { dispatched, events } = playDeep('balanced', null, { web: 'disabled', knowledge: 'disabled' })
    expect(approvalsOf(dispatched)).toEqual([])
    expect([toolSteps(events, 'delegate_batch'), toolSteps(events, 'write_canvas')])
      .toEqual([[], ['started', 'finished success']])
    expect(events.at(-1)?.type).toBe('inqtrix.run.completed')
  })

  it('asks nothing of a run that decides for itself and delegates the batch all the same', () => {
    const { dispatched, events } = playDeep('autonomous', null)
    expect(approvalsOf(dispatched)).toEqual([])
    expect(toolSteps(events, 'delegate_batch')).toEqual(['started', 'finished success'])
  })
})

describe('the search record behind the web citations of a demo run', () => {
  it('reaches the run with its answer, so a web citation opens on its search', () => {
    const { dispatched } = playDemo((demo) => {
      demo.submit({ autonomy: 'autonomous', question: 'Analyse.', sessionId: 'session-demo-evidence' })
      vi.advanceTimersByTime(120_000)
    })
    const details = dispatched.flatMap((action) => (
      action.type === 'setAgentRunArtifactDetail' ? [action.artifact] : []
    ))
    const bundle = details.find((artifact) => artifact.kind === 'evidence_bundle')
    const searches = (bundle?.payload?.web_search_ledger as { searches?: Record<string, unknown> })?.searches ?? {}
    // The run's deliverable, whichever form it takes, cites the web source.
    const cited = details
      .flatMap((artifact) => artifact.refs)
      .find((reference) => reference.label === 'W1')
    expect(Object.keys(searches)).toEqual([cited?.query_id])
    // The source the demo search cites at three places.
    expect((cited?.citation_ids as string[] | undefined)?.length).toBe(3)
  })
})

/** Play the demo under fake timers; what it dispatched, and its run events. */
function playDemo(
  play: (demo: AgentDemoActions, dispatched: ResearchDeskAction[]) => void,
): { dispatched: ResearchDeskAction[]; events: ResearchRunEvent[] } {
  const dispatched: ResearchDeskAction[] = []
  vi.useFakeTimers()
  vi.stubGlobal('window', globalThis)
  try {
    play(createAgentDemo((action) => { dispatched.push(action) }), dispatched)
  } finally {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  }
  const events = dispatched.flatMap((action) => (
    action.type === 'appendApiRunEvent' ? [action.event] : []
  ))
  return { dispatched, events }
}
