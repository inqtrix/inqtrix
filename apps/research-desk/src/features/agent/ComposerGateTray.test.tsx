import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { ResearchRunSummary } from '@/features/researchRuns/types'
import { translations } from '@/i18n/translations'
import {
  ComposerGateTray,
  StructuredClarificationForm,
  ToolEditFields,
} from './ComposerGateTray'
import { applyAgentRunEvent } from './events'
import { initialEditDraft } from './gateSummary'
import { agentRunFromSummary } from './model'
import type {
  AgentApprovalRecord,
  AgentClarificationRecord,
  AgentRunRecord,
} from './model'
import type { AgentTimelineActions } from './timeline/AgentTimeline'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

const longLabel = 'Nur Quellen aus der Sammlung zum EU AI Act, ohne Websuche und ohne Anhänge'

function round(): AgentClarificationRecord {
  return {
    answer: '',
    answeredAt: null,
    answers: {},
    clarificationId: 'clr_long',
    createdAt: 1,
    defaultAssumption: '',
    optionId: '',
    options: [],
    question: 'Welche Quellen?',
    questions: [{
      id: 'q1',
      multiSelect: false,
      options: [
        { description: '', id: 'q1_o1', label: 'Alle Quellen' },
        { description: '', id: 'q1_o2', label: longLabel },
      ],
      prompt: 'Welche Quellen?',
    }],
    status: 'pending',
  }
}

describe('answer options of a clarification', () => {
  it('shows a long option in full, wrapped, where the user picks it', () => {
    // The chosen option's wording is the user's answer: a chip held to one
    // line would hide part of it at the moment of choice, and touch devices
    // have no hover to reveal it.
    const html = renderToStaticMarkup(
      <StructuredClarificationForm
        canEdit
        clarification={round()}
        onAnswer={async () => undefined}
        t={translations.de}
      />,
    )
    const chip = html.match(/<button[^>]*>([^<]*Nur Quellen[^<]*)<\/button>/)
    expect(chip?.[1]).toBe(longLabel)
    const classes = chip?.[0].match(/class="([^"]*)"/)?.[1].split(/\s+/) ?? []
    expect(classes).toContain('whitespace-normal')
    expect(classes).not.toContain('h-6')
  })
})

describe('the default assumption of a clarification', () => {
  it('shows the model text as written, also where it reads like a replacement pattern', () => {
    // A string replacement reads `$&` and `$'` in the inserted text as
    // patterns: the assumption would show the template instead of itself.
    const assumption = "Preise in $& und $' bleiben wie im Auftrag"
    const html = renderToStaticMarkup(
      <StructuredClarificationForm
        canEdit
        clarification={{ ...round(), defaultAssumption: assumption }}
        onAnswer={async () => undefined}
        t={translations.de}
      />,
    )
    // As React escapes text: `&` and `'` become entities.
    const escaped = assumption.replaceAll('&', '&amp;').replaceAll("'", '&#x27;')
    expect(html).toContain(`Standardannahme des Laufs: ${escaped}`)
  })
})

function summary(status: ResearchRunSummary['status']): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    run_id: 'run_parent_1',
    status,
    queue_position: null,
    question: 'Recherchiere die Lage.',
    stack: 'default',
    mode: 'agent_kernel',
    kind: 'agent',
    agent_overrides: { autonomy: 'balanced' },
    created_at: 1_700_000_000,
    started_at: 1_700_000_001,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: { current_node: 'agent_kernel', last_message: '' },
    error: null,
    events_url: '/v1/runs/run_parent_1/events',
  }
}

const researchApproval: AgentApprovalRecord = {
  approvalId: 'apr_tool_1',
  kind: 'tool',
  status: 'pending',
  subjectType: 'tool',
  subjectId: 'call_1',
  payload: {
    actions: [{
      tool: 'run_web_research',
      args: { question: 'Was hat sich geändert?', recency: 'week' },
    }],
  },
  decision: '',
  note: '',
  createdAt: 1_700_000_050,
  decidedAt: null,
}

function tray(run: AgentRunRecord): string {
  // A static render calls none of the actions.
  return renderToStaticMarkup(
    <ComposerGateTray actions={{} as AgentTimelineActions} run={run} />,
  )
}

describe('the period a research prefers for its sources', () => {
  // Who decides reads the question and, right under it, the period.
  const underTheQuestion =
    /Was hat sich geändert\?<\/span><span[^>]*>Bevorzugter Zeitraum der Quellen: letzte 7 Tage<\/span>/

  it('stands under the question where the run asks for its own approval', () => {
    const html = tray({
      ...agentRunFromSummary(summary('waiting_for_approval')),
      approvals: [researchApproval],
    })
    expect(html).toMatch(underTheQuestion)
  })

  it('stands under the question where a delegated run asks for approval', () => {
    const parked = applyAgentRunEvent(
      agentRunFromSummary(summary('waiting_for_children')),
      {
        type: 'inqtrix.agent.child.progress',
        run_id: 'run_parent_1',
        sequence: 1,
        created_at: 1_700_000_101,
        data: {
          child_run_id: 'run_child_1',
          run_status: 'waiting_for_approval',
          task_id: 'call_abc',
        },
      },
    )
    const html = tray({
      ...parked,
      childGates: {
        run_child_1: {
          approvals: [researchApproval],
          clarifications: [],
          stale: false,
        },
      },
    })
    expect(html).toMatch(underTheQuestion)
  })
})

describe('the form a web search is asked to answer in', () => {
  const searchApproval: AgentApprovalRecord = {
    ...researchApproval,
    payload: {
      actions: [{
        tool: 'web_instant',
        args: { query: 'Wie hoch war die Inflation?', answer_form: 'table' },
      }],
    },
  }

  it('stands under the question as what the call asks for', () => {
    // The card knows the call, not the search provider of the run: it says
    // what is requested and promises no table.
    const html = tray({
      ...agentRunFromSummary(summary('waiting_for_approval')),
      approvals: [searchApproval],
    })
    expect(html).toMatch(
      /Wie hoch war die Inflation\?<\/span><span[^>]*>Gewünschte Form der Suchantwort: Tabelle<\/span>/,
    )
  })
})

describe('the fields of an edited tool call', () => {
  function fields(action: Record<string, unknown>): string {
    // A static render changes no field.
    return renderToStaticMarkup(
      <ToolEditFields
        action={action}
        draft={initialEditDraft(action)}
        onChange={() => undefined}
        t={translations.de}
      />,
    )
  }
  const textFields = (html: string) =>
    [...html.matchAll(/<span[^>]*>([^<]*)<\/span><input[^>]*value="([^"]*)"/g)].map(
      (field) => [field[1], field[2]],
    )
  const formChoice = /<button[^>]*aria-label="Form der Suchantwort"[^>]*>/

  it.each([
    ['names a form', { query: 'Frage', answer_form: 'table' }],
    ['names none', { query: 'Frage' }],
  ])('offers the form of a web search as a choice where the call %s', (_case, args) => {
    // Free text would let an approver type a name the tool refuses.
    const html = fields({ tool: 'web_instant', args })
    expect(textFields(html)).toEqual([['query', 'Frage']])
    expect(html.match(formChoice)?.[0]).toContain('role="combobox"')
  })

  it('offers no such choice for a call of another tool', () => {
    const html = fields({
      tool: 'run_web_research',
      args: { question: 'Frage', answer_form: 'table' },
    })
    expect(textFields(html)).toEqual([['question', 'Frage'], ['answer_form', 'table']])
    expect(html).not.toMatch(formChoice)
  })
})
