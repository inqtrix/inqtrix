import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { AgentStatusMenu } from './AgentStatusMenu'
import type { AgentOverview } from './agentStatusOverview'
import { DEMO_AGENT_OVERVIEW_SOURCE } from './demo'
import { normalizeAgentExecutionSnapshot } from './executionPolicy'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

// The menu body renders into a portal only while open; inline it so the
// static markup carries the rows.
vi.mock('@/components/ui/dropdown-menu', () => {
  const Passthrough = ({ children }: { children?: ReactNode }) => <>{children}</>
  return {
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: Passthrough,
    DropdownMenuSeparator: () => null,
    DropdownMenuTrigger: Passthrough,
  }
})

const overview: AgentOverview = {
  kernel: 'active',
  kernelReason: null,
  durable: true,
  approvals: null,
  tools: [],
  limits: DEMO_AGENT_OVERVIEW_SOURCE.limits ?? null,
}

describe('agent status menu limits', () => {
  const o = translations.de.agent.overview
  function limits(props: Partial<Parameters<typeof AgentStatusMenu>[0]>) {
    const html = renderToStaticMarkup(
      <TooltipProvider>
        <AgentStatusMenu
          autonomyHint=""
          autonomyLabel="Standard"
          disabled={false}
          memoryEnabled={false}
          overview={overview}
          responseForm="auto"
          responseFormValue="Automatisch"
          sourcePolicy={{ web: 'available', knowledge: 'available' }}
          {...props}
        />
      </TooltipProvider>,
    )
    const row = (label: string) => {
      const match = html.match(new RegExp(
        `${label}</span><span[^>]*>([^<]*)</span></div>(?:<p[^>]*>([^<]*)</p>)?`,
      ))
      return match ? { hint: match[2] ?? null, value: match[1] } : null
    }
    return { steps: row(o.rowSteps), tokens: row(o.rowTokens), toolCalls: row(o.rowToolCalls) }
  }

  it('keeps a row readable: the numbers in the pill, the words under the row', () => {
    // The regression: "5 / 30 verwendet · nach Bestätigung bis 60" was cut
    // in the pill, so the ceiling a limit can be extended to was unreadable.
    const running = limits({
      execution: normalizeAgentExecutionSnapshot({
        execution: {
          effective_mode: 'agent_kernel',
          source_policy: { web: 'available', knowledge: 'available' },
          limits: {
            tool_calls: { used: 5, limit: 30, ceiling: 60, recoverable: true, extendable: true },
            steps: { used: 33, limit: 121, ceiling: 241, recoverable: true, extendable: true },
            tokens: { used: 113701, limit: 900000, ceiling: 900000, recoverable: false, extendable: false },
          },
        },
      } as never),
      overview: {
        ...overview,
        limits: overview.limits && {
          ...overview.limits,
          tokens: { ...overview.limits.tokens, enabled: true, limit: 900000, ceiling: 900000 },
        },
      },
    })
    expect(running).toEqual({
      steps: { hint: 'verwendet · nach Bestätigung bis 241', value: '33 / 121' },
      tokens: { hint: 'verwendet · Betreibergrenze', value: '113701 / 900000' },
      toolCalls: { hint: 'verwendet · nach Bestätigung bis 60', value: '5 / 30' },
    })
  })

  it('names the kind of a limit before a run has spent anything', () => {
    // Published limits of the normal depth: 30 calls up to 60, 120 steps up
    // to 240; the token limit is off in this deployment.
    expect(limits({})).toEqual({
      steps: { hint: 'nach Bestätigung bis 240', value: '120' },
      tokens: { hint: null, value: o.limitDisabled },
      toolCalls: { hint: 'nach Bestätigung bis 60', value: '30' },
    })
    // A limit without room above it is the operator's ceiling.
    expect(limits({ tier: 'schnell' }).toolCalls).toEqual({ hint: 'Betreibergrenze', value: '30' })
  })
})


describe('agent status menu source use', () => {
  const o = translations.de.agent.overview
  function used(
    execution: Record<string, unknown>,
    childRuns: Record<string, unknown> = {},
  ): Record<string, string | null> {
    const html = renderToStaticMarkup(
      <TooltipProvider>
        <AgentStatusMenu
          autonomyHint=""
          autonomyLabel="Standard"
          childRuns={childRuns as never}
          disabled={false}
          execution={normalizeAgentExecutionSnapshot({
            execution: {
              effective_mode: 'agent_kernel',
              source_policy: { web: 'available', knowledge: 'available' },
              ...execution,
            },
          } as never)}
          memoryEnabled={false}
          overview={overview}
          responseForm="auto"
          responseFormValue="Automatisch"
          sourcePolicy={{ web: 'available', knowledge: 'available' }}
        />
      </TooltipProvider>,
    )
    const value = (label: string) =>
      html.match(new RegExp(`${label}</span><span[^>]*>([^<]*)</span>`))?.[1] ?? null
    // The line under a row, where the row has one.
    const hint = (label: string) =>
      html.match(new RegExp(
        `${label}</span><span[^>]*>[^<]*</span></div><p[^>]*>([^<]*)</p>`,
      ))?.[1] ?? null
    return {
      knowledge: value(o.toolKnowledgeUsed),
      knowledgeHint: hint(o.toolKnowledgeUsed),
      web: value(o.toolWebUsed),
      webHint: hint(o.toolWebUsed),
    }
  }

  it('names searches where it says searches, for both sources', () => {
    // Two document reads and one search are three uses of the knowledge
    // source and one search; one delegated research is one web use whose
    // child ran six searches. The pill holds the searches alone: it is
    // too narrow for both counts, so the research stands under the row.
    expect(used(
      { tool_use_counts: { web: 1, knowledge: 3 }, web_searches: 0, knowledge_searches: 1 },
      { child_a: { childRunId: 'child_a', taskId: 'call_a', snapshot: { total_queries: 8, executed_queries: 6 } } },
    )).toEqual({
      knowledge: '1 Suche',
      knowledgeHint: null,
      web: '6 Suchen',
      webHint: '1 Recherche delegiert',
    })
  })

  it('says that a run searched nothing itself where it read documents or its subtasks asked the knowledge', () => {
    expect(used({
      tool_use_counts: { web: 0, knowledge: 2 }, web_searches: 0, knowledge_searches: 0,
    })).toEqual({
      knowledge: 'ohne eigene Suche', knowledgeHint: null, web: null, webHint: null,
    })
  })

  it('keeps the counter of source uses for a run that recorded no search count', () => {
    expect(used({ tool_use_counts: { web: 2, knowledge: 2 } })).toEqual({
      knowledge: '2 Suchen', knowledgeHint: null, web: '2 Suchen', webHint: null,
    })
  })
})


describe('agent status menu after a run of the retired mission engine', () => {
  const o = translations.de.agent.overview
  // What a server publishes for the run overview: the kernel's limits and
  // the token budget — no mission limits.
  const published = DEMO_AGENT_OVERVIEW_SOURCE.limits!
  const kernelOverview: AgentOverview = {
    kernel: 'active',
    kernelReason: null,
    durable: true,
    approvals: null,
    tools: [],
    limits: { kernel: published.kernel, tokens: published.tokens },
  }
  // The latest run of the session ran on the retired engine: its stored
  // snapshot names that engine, its model, its depth and what it spent.
  const missionRun = normalizeAgentExecutionSnapshot({
    execution: {
      effective_mode: 'workspace_agent',
      model: 'mission-model',
      reasoning_effort: 'high',
      depth: 'deep',
      source_policy: { web: 'disabled', knowledge: 'available' },
      tool_use_counts: { web: 4, knowledge: 2 },
      limits: {
        tool_calls: { used: 7, limit: 30, ceiling: 60, recoverable: true, extendable: true },
      },
    },
  } as never)

  function markup(): string {
    return renderToStaticMarkup(
      <TooltipProvider>
        <AgentStatusMenu
          autonomyHint=""
          autonomyLabel="Standard"
          disabled={false}
          execution={missionRun}
          memoryEnabled={false}
          overview={kernelOverview}
          responseForm="auto"
          responseFormValue="Automatisch"
          sourcePolicy={{ web: 'available', knowledge: 'available' }}
        />
      </TooltipProvider>,
    )
  }

  it('renders without failing', () => {
    expect(() => markup()).not.toThrow()
  })

  it('shows what the next kernel run would do, never the stored run as a kernel run', () => {
    const html = markup()
    const value = (label: string) =>
      html.match(new RegExp(`${label}</span><span[^>]*>([^<]*)</span>`))?.[1] ?? null
    // The composer's selection, not the stored run's model, depth or spend.
    expect(html).not.toContain('mission-model')
    expect(value(o.rowDepth)).toBe(o.depthNormal)
    expect(value(o.rowToolCalls)).toBe('30')
    expect(value(o.toolWebUsed)).toBeNull()
    expect(value(o.toolKnowledgeUsed)).toBeNull()
  })
})


describe('agent status menu kernel availability', () => {
  function markup(over: Partial<AgentOverview>) {
    return renderToStaticMarkup(
      <TooltipProvider>
        <AgentStatusMenu
          autonomyHint=""
          autonomyLabel="Standard"
          disabled={false}
          memoryEnabled={false}
          overview={{ ...overview, ...over }}
          responseForm="auto"
          responseFormValue="Automatisch"
          sourcePolicy={{ web: 'available', knowledge: 'available' }}
        />
      </TooltipProvider>,
    )
  }

  it('says why the kernel is unavailable when the server names the reason', () => {
    const reason = translations.de.agent.overview.kernelReasons.tool_calls_required
    expect(markup({ kernel: 'unavailable', kernelReason: 'tool_calls_required' })).toContain(reason)
  })

  it.each(['operator_disabled', 'unknown'] as const)(
    'says why the kernel is unavailable when the server reports %s',
    (code) => {
      const reasons = translations.de.agent.overview.kernelReasons as Record<string, string>
      expect(reasons[code]).toBeTruthy()
      expect(markup({ kernel: 'unavailable', kernelReason: code })).toContain(reasons[code])
    },
  )

})
