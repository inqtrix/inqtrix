import { describe, expect, it } from 'vitest'
import type {
  AgentLimitCapabilities,
  AgentPermissionModeEntry,
} from '@/features/researchRuns/types'
import {
  sourceAvailabilityFromOverviewSource,
  buildAgentOverview,
  agentDeskVisible,
  agentRunsAvailable,
  effectiveAgentAutonomy,
  effectiveAgentDepth,
  type AgentOverviewSource,
} from './agentStatusOverview'

describe('sourceAvailabilityFromOverviewSource', () => {
  it('reads the published controls with their reason, else the tool manifest', () => {
    expect(sourceAvailabilityFromOverviewSource({
      durable: true,
      tools: [],
      source_controls: [
        { id: 'web', default: 'available', available: true },
        { id: 'knowledge', default: 'available', available: false, reason_code: 'store_unreachable' },
      ],
    })).toEqual({ web: true, knowledge: false, knowledgeReason: 'store_unreachable' })
    expect(sourceAvailabilityFromOverviewSource({
      durable: true,
      tools: [{ id: 'knowledge.search', effect: 'read', read_only: true, idempotent: true }] as never,
    })).toEqual({ web: false, knowledge: true, knowledgeReason: null })
    expect(sourceAvailabilityFromOverviewSource(null)).toEqual({ web: false, knowledge: false, knowledgeReason: null })
  })
})

describe('effectiveAgentAutonomy', () => {
  const standardOnly = { autonomy_modes: ['strict', 'balanced'], default_autonomy: 'balanced' }

  it('keeps a selection the server offers', () => {
    expect(effectiveAgentAutonomy('strict', standardOnly)).toBe('strict')
  })

  it('takes the default for a selection the server stopped offering', () => {
    expect(effectiveAgentAutonomy('autonomous', standardOnly)).toBe('balanced')
  })

  it('takes the default without a selection', () => {
    expect(effectiveAgentAutonomy(null, { autonomy_modes: ['balanced'], default_autonomy: 'balanced' }))
      .toBe('balanced')
  })

  it('keeps a selection while nothing says what the server offers', () => {
    expect(effectiveAgentAutonomy('autonomous', undefined)).toBe('autonomous')
  })

  it('falls back to Standard when the server names no default', () => {
    expect(effectiveAgentAutonomy(null, undefined)).toBe('balanced')
  })
})

describe('effectiveAgentDepth', () => {
  it('uses the capability default until the user makes an explicit choice', () => {
    expect(effectiveAgentDepth(null, 'deep')).toBe('deep')
    expect(effectiveAgentDepth('normal', 'deep')).toBe('normal')
    expect(effectiveAgentDepth('deep', 'normal')).toBe('deep')
  })

  it('falls back to normal for older or malformed capability manifests', () => {
    expect(effectiveAgentDepth(null, undefined)).toBe('normal')
    expect(effectiveAgentDepth(null, 'ultra')).toBe('normal')
  })
})

const BALANCED: AgentPermissionModeEntry = {
  kernel_gated_tools: ['load_skill', 'run_web_research', 'web_instant'],
  kernel_conditional_tools: ['search_project_knowledge'],
  kernel_always_gated: ['propose_editor_patch'],
}

const AUTONOMOUS: AgentPermissionModeEntry = {
  kernel_gated_tools: [],
  kernel_conditional_tools: [],
  kernel_always_gated: ['propose_editor_patch'],
}

const LIMITS: AgentLimitCapabilities = {
  tokens: {
    enabled: true,
    limit: 20_000,
    ceiling: 20_000,
    recoverable: false,
    extendable: false,
    reason: 'operator_ceiling_exactly_once_required',
  },
  kernel: {
    schnell: { tool_calls: 30, tool_calls_ceiling: 30, steps: 57, steps_ceiling: 57 },
    normal: { tool_calls: 30, tool_calls_ceiling: 60, steps: 73, steps_ceiling: 145 },
    deep: { tool_calls: 60, tool_calls_ceiling: 120, steps: 121, steps_ceiling: 241 },
  },
}

function source(overrides: Partial<AgentOverviewSource> = {}): AgentOverviewSource {
  return {
    durable: true,
    tools: [
      { id: 'web.search.instant', summary: '', effect: 'read', read_only: true, idempotent: true },
      { id: 'knowledge.search', summary: '', effect: 'read', read_only: true, idempotent: true },
      { id: 'editor.patch.propose', summary: '', effect: 'write', read_only: false, idempotent: false },
    ],
    permission_modes: { autonomous: AUTONOMOUS, balanced: BALANCED },
    limits: LIMITS,
    ...overrides,
  }
}

describe('buildAgentOverview', () => {
  it('names the kernel active, and drops a reason the server sent for an unregistered one', () => {
    const active = buildAgentOverview({
      agent: source(),
      autonomy: 'balanced',
      kernelReason: 'tool_calls_required',
      kernelSelectable: true,
    })
    expect(active.kernel).toBe('active')
    expect(active.kernelReason).toBeNull()
    expect(active.limits).toBe(LIMITS)
  })

  it('names the kernel unavailable with its reason', () => {
    const blocked = buildAgentOverview({
      agent: source(),
      autonomy: 'balanced',
      kernelReason: 'tool_calls_required',
      kernelSelectable: false,
    })
    expect(blocked.kernel).toBe('unavailable')
    expect(blocked.kernelReason).toBe('tool_calls_required')
  })

  it('renders the kernel approval rows of the selected mode', () => {
    const kernel = buildAgentOverview({
      agent: source(),
      autonomy: 'balanced',
      kernelSelectable: true,
    })
    expect(kernel.kernel).toBe('active')
    expect(kernel.approvals).toEqual([
      { id: 'web_search', state: 'asks' },
      { id: 'knowledge_search', state: 'asks', conditional: true },
      { id: 'research', state: 'asks' },
      { id: 'skill_activation', state: 'asks' },
      { id: 'editor_patch', state: 'always' },
    ])
  })

  it('shows explicit free rows in the autonomous kernel mode', () => {
    const kernel = buildAgentOverview({
      agent: source(),
      autonomy: 'autonomous',
      kernelSelectable: true,
    })
    expect(kernel.approvals).toEqual([
      { id: 'web_search', state: 'free' },
      { id: 'knowledge_search', state: 'free' },
      { id: 'research', state: 'free' },
      { id: 'skill_activation', state: 'free' },
      { id: 'editor_patch', state: 'always' },
    ])
  })

  it('surfaces unknown gated tools under their raw name', () => {
    const agent = source({
      permission_modes: {
        balanced: { ...BALANCED, kernel_gated_tools: [...BALANCED.kernel_gated_tools, 'summon_dragon'] },
      },
    })
    const kernel = buildAgentOverview({
      agent,
      autonomy: 'balanced',
      kernelSelectable: true,
    })
    expect(kernel.approvals?.some((row) => row.id === 'summon_dragon' && row.state === 'asks')).toBe(true)
  })

  it('hides the approvals group when the server does not publish modes', () => {
    const overview = buildAgentOverview({
      agent: source({ permission_modes: undefined }),
      autonomy: 'balanced',
      kernelSelectable: false,
    })
    expect(overview.approvals).toBeNull()
  })

  it('hides limit claims when an older server does not publish them', () => {
    const overview = buildAgentOverview({
      agent: source({ limits: undefined }),
      autonomy: 'balanced',
      kernelSelectable: true,
    })
    expect(overview.limits).toBeNull()
  })

  it('reports tool availability from the manifest, missing ids as unavailable', () => {
    const overview = buildAgentOverview({
      agent: source({
        tools: [
          { id: 'knowledge.search', summary: '', effect: 'read', read_only: true, idempotent: true },
        ],
      }),
      autonomy: 'balanced',
      kernelSelectable: false,
    })
    expect(overview.tools).toEqual([
      { id: 'web_search', available: false },
      { id: 'knowledge_search', available: true },
      { id: 'editor_access', available: false },
    ])
  })
})

describe('agent desk and agent runs', () => {
  const features = (extra: Record<string, boolean>) => ({
    embedding_provider: true, knowledge: true, multi_stack: false, openapi: true, ...extra,
  })

  it('shows the desk where the server set up agent runs, whatever else it reports', () => {
    expect(agentDeskVisible(features({ agent_desk: true, workspace_agent: false }), false)).toBe(true)
    expect(agentDeskVisible(features({ agent_desk: false, workspace_agent: true }), false)).toBe(false)
    expect(agentDeskVisible(null, false)).toBe(false)
    expect(agentDeskVisible(null, true)).toBe(true)
  })

  it('starts a run only on the registered kernel', () => {
    expect(agentRunsAvailable(features({ agent_desk: true, agent_kernel: true }), true)).toBe(true)
    expect(agentRunsAvailable(features({ agent_desk: true, agent_kernel: false, workspace_agent: true }), true)).toBe(false)
    expect(agentRunsAvailable(null, false)).toBe(true)
  })
})
