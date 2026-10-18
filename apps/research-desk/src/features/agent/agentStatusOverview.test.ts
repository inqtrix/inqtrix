import { describe, expect, it } from 'vitest'
import type {
  AgentLimitCapabilities,
  AgentPermissionModeEntry,
} from '@/features/researchRuns/types'
import {
  sourceAvailabilityFromOverviewSource,
  buildAgentOverview,
  countAgentToolUse,
  defaultEngineMode,
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
  plan_gate: true,
  web_replan_regate: true,
  patch_gate: true,
  kernel_gated_tools: ['load_skill', 'run_deep_mission', 'run_web_research', 'web_instant'],
  kernel_conditional_tools: ['search_project_knowledge'],
  kernel_always_gated: ['propose_editor_patch'],
}

const AUTONOMOUS: AgentPermissionModeEntry = {
  plan_gate: false,
  web_replan_regate: false,
  patch_gate: true,
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
  mission: {
    discovery_tool_calls: 15,
    plan_tasks: 8,
    replan_rounds: 2,
    clarification_rounds: 2,
    parallel_children: 6,
  },
  research: { rounds: 2 },
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
  it('describes the phase machine gates per mode', () => {
    const balanced = buildAgentOverview({
      agent: source(),
      autonomy: 'balanced',
      kernelSelectable: true,
      mode: 'workspace_agent',
    })
    expect(balanced.brain).toBe('workspace_agent')
    expect(balanced.kernel).toBe('selectable')
    expect(balanced.limits).toBe(LIMITS)
    expect(balanced.approvals).toEqual([
      { id: 'plan', state: 'asks' },
      { id: 'web_search', state: 'asks' },
      { id: 'editor_patch', state: 'always' },
    ])

    const auto = buildAgentOverview({
      agent: source(),
      autonomy: 'autonomous',
      kernelSelectable: false,
      mode: 'workspace_agent',
    })
    expect(auto.kernel).toBe('unavailable')
    expect(auto.approvals).toEqual([
      { id: 'plan', state: 'free' },
      { id: 'web_search', state: 'free' },
      { id: 'editor_patch', state: 'always' },
    ])
  })

  it('starts on the published default only when the feature flag confirms', () => {
    const agent = source({ default_mode: 'agent_kernel' })
    // default_mode alone is not enough — the feature flag must confirm.
    expect(defaultEngineMode(agent, null)).toBe('workspace_agent')
    expect(
      defaultEngineMode(agent, {
        agent_kernel: true, embedding_provider: true, knowledge: true, multi_stack: false, openapi: true,
      }),
    ).toBe('agent_kernel')
    expect(
      defaultEngineMode(source(), {
        agent_kernel: true, embedding_provider: true, knowledge: true, multi_stack: false, openapi: true,
      }),
    ).toBe('workspace_agent')
  })

  it('renders kernel approval rows for the selected kernel engine', () => {
    const kernel = buildAgentOverview({
      agent: source(),
      autonomy: 'balanced',
      kernelSelectable: true,
      mode: 'agent_kernel',
    })
    expect(kernel.brain).toBe('agent_kernel')
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
      mode: 'agent_kernel',
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
      mode: 'agent_kernel',
    })
    expect(kernel.approvals?.some((row) => row.id === 'summon_dragon' && row.state === 'asks')).toBe(true)
  })

  it('hides the approvals group when the server does not publish modes', () => {
    const overview = buildAgentOverview({
      agent: source({ permission_modes: undefined }),
      autonomy: 'balanced',
      kernelSelectable: false,
      mode: 'workspace_agent',
    })
    expect(overview.approvals).toBeNull()
  })

  it('hides limit claims when an older server does not publish them', () => {
    const overview = buildAgentOverview({
      agent: source({ limits: undefined }),
      autonomy: 'balanced',
      kernelSelectable: true,
      mode: 'agent_kernel',
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
      mode: 'workspace_agent',
    })
    expect(overview.tools).toEqual([
      { id: 'web_search', available: false },
      { id: 'knowledge_search', available: true },
      { id: 'editor_access', available: false },
    ])
  })
})

describe('countAgentToolUse', () => {
  it('counts only completed web and knowledge tasks', () => {
    expect(countAgentToolUse({
      tasks: [
        { taskId: 'w1', toolKind: 'web_instant' },
        { taskId: 'w2', toolKind: 'web_research' },
        { taskId: 'k1', toolKind: 'rag_query' },
        { taskId: 's1', toolKind: 'synthesis' },
      ],
      taskStates: {
        w1: { status: 'completed' },
        w2: { status: 'running' },
        k1: { status: 'completed' },
        s1: { status: 'completed' },
      },
    })).toEqual({ web: 1, knowledge: 1 })
  })
})
