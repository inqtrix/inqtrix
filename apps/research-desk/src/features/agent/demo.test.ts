import { readFileSync } from 'node:fs'

import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

import type { ResearchDeskAction } from '@/features/researchDesk/state'
import type { ResearchRunEvent } from '@/features/researchRuns/types'
import { attachedReportsMax } from './contextCategories'
import {
  createAgentDemo,
  DEMO_AGENT_ATTACHED_REPORTS,
  DEMO_AGENT_REPORT_REQUIREMENT,
  demoPlan,
  demoRunSummary,
  type AgentDemoActions,
} from './demo'
import { reportComposedCharsMax, reportGuidanceMaxChars, reportRuleIdsMax } from './reportRequirement'
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
      'agent_kernel',
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
      'workspace_agent',
      'canvas',
      { web: 'disabled', knowledge: 'available' },
    )

    expect(summary.mode).toBe('workspace_agent')
    expect(normalizeAgentExecutionSnapshot(summary.snapshot)).toMatchObject({
      consentReason: 'permission_policy',
      depth: 'deep',
      effectiveMode: 'workspace_agent',
      responseForm: 'canvas',
      sourcePolicy: { web: 'disabled', knowledge: 'available' },
    })
  })

  it('removes disabled sources from the plan and its dependency graph', () => {
    const knowledgeOnly = demoPlan(
      'RA-demo-knowledge',
      [],
      { web: 'disabled', knowledge: 'available' },
    )
    expect(knowledgeOnly.tasks.map((task) => task.tool_kind)).toEqual([
      'rag_query',
      'file_analysis',
      'synthesis',
    ])
    expect(knowledgeOnly.tasks[2].depends_on).toEqual(['t1', 't4'])

    const noSources = demoPlan(
      'RA-demo-none',
      [],
      { web: 'disabled', knowledge: 'disabled' },
    )
    expect(noSources.tasks.map((task) => task.tool_kind)).toEqual(['synthesis'])
    expect(noSources.tasks[0].depends_on).toEqual([])
  })

  it('speaks real plurals in the document analysis narration, like the server', () => {
    const source = readFileSync(new URL('./demo.ts', import.meta.url), 'utf8')
    const narration = source.split('\n').filter((line) => line.includes('Dokumentanalyse:'))
    expect(narration).toHaveLength(1)
    expect(narration[0]).not.toMatch(/\w\((e|n|en|s|er|es)\)/)
    expect(narration[0]).toContain('Dokumentanalyse: 1 von 1 Dokument bis zum Ende seines gespeicherten Textes gelesen.')
  })

  const demoTree = () => {
    const source = readFileSync(new URL('./demo.ts', import.meta.url), 'utf8')
    return ts.createSourceFile('demo.ts', source, ts.ScriptTarget.Latest, true)
  }

  /** The payload object of every `makeEvent(runId, next(), type, payload)`. */
  function eventPayloads(tree: ts.SourceFile, type: string): ts.ObjectLiteralExpression[] {
    const found: ts.ObjectLiteralExpression[] = []
    function visit(node: ts.Node) {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'makeEvent') {
        const [, , kind, payload] = node.arguments
        if (kind && ts.isStringLiteral(kind) && kind.text === type && payload && ts.isObjectLiteralExpression(payload)) {
          found.push(payload)
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return found
  }

  function property(payload: ts.ObjectLiteralExpression, name: string): ts.Expression | undefined {
    const match = payload.properties.find(
      (item): item is ts.PropertyAssignment => ts.isPropertyAssignment(item) && item.name.getText() === name,
    )
    return match?.initializer
  }

  it('hands every narrated task its result on the task event, as the server does', () => {
    // The server sends the result as `result_summary` on task.finished
    // and again behind the title in `n-task-<id>`; the row shows the
    // first, and the transcript folds the second only when the row has it.
    const tree = demoTree()
    const narrated = new Map(eventPayloads(tree, 'inqtrix.agent.narration').flatMap((payload) => {
      const id = property(payload, 'narration_id')
      const text = property(payload, 'text')
      if (!id || !ts.isStringLiteral(id) || !id.text.startsWith('n-task-') || !text) return []
      return [[id.text.slice('n-task-'.length), text] as const]
    }))
    expect([...narrated.keys()].sort()).toEqual(['t1', 't2', 't4'])
    for (const [taskId, text] of narrated) {
      const finished = eventPayloads(tree, 'inqtrix.agent.task.finished').find((payload) => {
        const id = property(payload, 'task_id')
        return id !== undefined && ts.isStringLiteral(id) && id.text === taskId
      })
      const summary = finished && property(finished, 'result_summary')
      expect(summary?.getText(), `result_summary of ${taskId}`).toMatch(/^DEMO_RESULT_T\d$/)
      // The narration repeats exactly that result behind the title.
      expect(ts.isTemplateExpression(text) && text.templateSpans.map((span) => span.expression.getText()))
        .toEqual([summary?.getText()])
    }
  })

  it('speaks the role previews as counted German, like the server', () => {
    const previews: string[] = []
    function visit(node: ts.Node) {
      if (ts.isPropertyAssignment(node) && node.name.getText() === 'result_preview' && ts.isStringLiteral(node.initializer)) {
        previews.push(node.initializer.text)
      }
      ts.forEachChild(node, visit)
    }
    visit(demoTree())
    expect(previews).toEqual(['2 Befunde gemeldet', '2 Belege nachgelesen'])
  })

  it('the demo sends outcome records and never says behoben', () => {
    // Every demo run that completes sends its record first, as the server
    // does, and a delegated mission's record crosses as its projection;
    // no demo text calls an unchecked edit "behoben".
    const { dispatched, events } = playDemo((demo, sent) => {
      const base = { question: 'Wie ist die Lage?', sessionId: 'session-demo-outcome' }
      demo.submit({ ...base, autonomy: 'autonomous', engineMode: 'agent_kernel' })
      demo.submit({ ...base, autonomy: 'autonomous', engineMode: 'workspace_agent', depth: 'deep' })
      demo.submit({ ...base, autonomy: 'autonomous', engineMode: 'agent_kernel', agentTier: 'tief' })
      demo.submit({ ...base, autonomy: 'balanced', engineMode: 'workspace_agent' })
      vi.advanceTimersByTime(120_000)
      const gated = sent.flatMap((action) => (
        action.type === 'setAgentRunApprovals' ? action.approvals.map((row) => row.approval_id) : []
      ))
      void demo.decideApproval(gated[0].replace(/^apr-/, ''), gated[0], { decision: 'reject' })
      vi.advanceTimersByTime(120_000)
    })
    const completed = events.filter((item) => item.type === 'inqtrix.run.completed').map((item) => item.run_id)
    const unrecorded = completed.filter((runId) => !events.some((item, index) => (
      item.run_id === runId
      && item.type === 'inqtrix.agent.outcome'
      && typeof item.data.summary === 'string'
      && index < events.findIndex((other) => other.run_id === runId && other.type === 'inqtrix.run.completed')
    )))
    const childRecords = events.filter((item) => (
      item.type === 'inqtrix.agent.child.progress' && item.data.event_type === 'inqtrix.agent.outcome'
    ))
    expect({
      completed: completed.length,
      unrecorded,
      childRecords: childRecords.length,
      behoben: JSON.stringify(dispatched).toLowerCase().includes('behoben'),
    }).toEqual({ completed: 4, unrecorded: [], childRecords: 1, behoben: false })
  })

  it('a Stufe that skips the review sends a waived record and enters no review phase', () => {
    // The server runs no review for a Stufe at verify level `labels` and
    // records it as waived, in both engines; the twin claims none either.
    const { events } = playDemo((demo) => {
      for (const engineMode of ['agent_kernel', 'workspace_agent']) {
        demo.submit({
          question: 'Wie ist die Lage?', sessionId: 'session-demo-waived',
          autonomy: 'autonomous', engineMode, agentTier: 'schnell',
        })
      }
      vi.advanceTimersByTime(120_000)
    })
    expect({
      reviews: events.filter((item) => item.type === 'inqtrix.agent.outcome').map((item) => item.data.review),
      critic: events.some((item) => item.type === 'inqtrix.agent.phase.changed' && item.data.phase === 'critic'),
    }).toEqual({ reviews: ['waived', 'waived'], critic: false })
  })
})

describe('the requirement limits in the demo', () => {
  // The demo has no capability manifest. Without its mirror the composer
  // and the plan gate said "the server checks the number when you send"
  // for a twin that checks nothing, and no cap state ever showed there.
  it('mirrors the numbers a current server publishes', () => {
    // The server's own constants (report_requirement.py), spelled here on
    // purpose: a test that read them from the mirror would be green by
    // construction.
    expect(DEMO_AGENT_REPORT_REQUIREMENT).toEqual({
      max_chars: 2_000,
      max_composed_chars: 20_000,
      max_rules: 3,
    })
  })

  it('gives the surfaces a cap and a ceiling to pre-check', () => {
    expect(reportRuleIdsMax(DEMO_AGENT_REPORT_REQUIREMENT)).toBe(3)
    expect(reportComposedCharsMax(DEMO_AGENT_REPORT_REQUIREMENT)).toBe(20_000)
    expect(reportGuidanceMaxChars(DEMO_AGENT_REPORT_REQUIREMENT)).toBe(2_000)
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
      play(createAgentDemo((action) => { dispatched.push(action) }, () => null, resolveRule), dispatched)
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
        agentTier: 'gruendlich', autonomy: 'autonomous', engineMode: 'agent_kernel',
        question: 'Halte dich an Executive Briefing.', ruleIds: ['rule-brief'], sessionId: 'session-demo-rules',
      })
      demo.submit({
        agentTier: 'schnell', autonomy: 'autonomous', engineMode: 'agent_kernel',
        question: 'Kurz.', ruleIds: ['rule-lektor'], sessionId: 'session-demo-rules',
      })
      demo.submit({
        autonomy: 'autonomous', engineMode: 'agent_kernel', question: 'Ohne Regel.', sessionId: 'session-demo-rules',
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

  it('opens the gate with the rules in force and decides with the parts, never a composed text', async () => {
    let decided: unknown = null
    const { dispatched } = playWithRules((demo, sent) => {
      demo.submit({
        autonomy: 'balanced', engineMode: 'workspace_agent',
        question: 'Analyse.', ruleIds: ['rule-brief'], sessionId: 'session-demo-gate',
      })
      vi.advanceTimersByTime(30_000)
      const gate = sent.flatMap((action) => (
        action.type === 'setAgentRunApprovals' ? action.approvals : []
      )).find((row) => row.status === 'pending')
      if (!gate) throw new Error('the demo opened no plan gate')
      void demo.decideApproval(gate.run_id, gate.approval_id, {
        decision: 'approve',
        report_guidance: 'Zielgruppe: Vorstand.',
        report_rule_ids: ['rule-brief', 'rule-lektor'],
      }).then(() => { decided = true })
      vi.advanceTimersByTime(1_000)
    })
    const rows = dispatched.flatMap((action) => (
      action.type === 'setAgentRunApprovals' ? action.approvals : []
    ))
    expect(rows.find((row) => row.status === 'pending')?.payload).toEqual({
      report_requirement: {
        free_text: '',
        rules: [{ label: 'briefing', revision: 1, template_id: 'rule-brief', title: 'Executive Briefing' }],
      },
    })
    expect(rows.find((row) => row.status === 'approved')?.decision_payload).toEqual({
      report_requirement: {
        free_text: 'Zielgruppe: Vorstand.',
        rules: [
          { label: 'briefing', revision: 1, template_id: 'rule-brief', title: 'Executive Briefing' },
          { label: 'lektor', revision: 1, template_id: 'rule-lektor' },
        ],
      },
    })
    await Promise.resolve()
    expect(decided).toBe(true)
  })

  it('refuses an unknown rule at the gate, as the server does', async () => {
    let refusal: unknown = null
    playWithRules((demo, sent) => {
      demo.submit({
        autonomy: 'balanced', engineMode: 'workspace_agent', question: 'Analyse.', sessionId: 'session-demo-gate',
      })
      vi.advanceTimersByTime(30_000)
      const gate = sent.flatMap((action) => (
        action.type === 'setAgentRunApprovals' ? action.approvals : []
      )).find((row) => row.status === 'pending')
      if (!gate) throw new Error('the demo opened no plan gate')
      demo.decideApproval(gate.run_id, gate.approval_id, {
        decision: 'approve', report_guidance: '', report_rule_ids: ['rule-gone'],
      }).catch((error: unknown) => { refusal = error })
    })
    await Promise.resolve()
    expect(String(refusal)).toContain('Unknown or non-visible rule: rule-gone')
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
