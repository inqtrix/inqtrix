import type {
  AgentSourceControl,
  AgentLimitCapabilities,
  AgentPermissionModeEntry,
  AgentToolManifestEntry,
  InqtrixCapabilities,
} from '@/features/researchRuns/types'

/**
 * Pure derivation for the composer's run overview (AgentStatusMenu):
 * server-published facts in, typed row descriptors out. NOTHING here
 * re-derives gating semantics — the approvals group renders the
 * `permission_modes` block the server generates from its enforcing
 * policy code, and hides entirely when an older server does not
 * publish it (honest absence beats a guessed row).
 */

/** One approval line: what asks for consent in the selected mode. */
export type ApprovalRowState = 'asks' | 'free' | 'always'

export type ApprovalRow = {
  /** Known ids map to translated labels; unknown gated tools surface
   * under their raw name so a policy addition is never silently
   * missing from the overview. */
  id: 'plan' | 'web_search' | 'knowledge_search' | 'research' | 'skill_activation' | 'editor_patch' | string
  state: ApprovalRowState
  /** True when the gate applies only conditionally (e.g. only the
   * UN-scoped project-wide search asks). */
  conditional?: boolean
}

export type ToolAvailabilityRow = {
  id: 'web_search' | 'knowledge_search' | 'editor_access'
  available: boolean
}

export type AgentOverview = {
  brain: 'workspace_agent' | 'agent_kernel'
  /** Whether the SECOND engine exists on this deployment at all —
   * `active` (selected), `selectable` (offered, not selected), or
   * `unavailable` (the operator did not enable it). Always rendered:
   * an absent picker alone would hide that the feature exists. */
  kernel: 'active' | 'selectable' | 'unavailable'
  durable: boolean
  /** Null when the server does not publish permission_modes (or the
   * selected mode is unknown) — the group hides. */
  approvals: ApprovalRow[] | null
  tools: ToolAvailabilityRow[]
  /** Null on older servers. The UI hides the group instead of inventing
   * defaults that may disagree with operator policy. */
  limits: AgentLimitCapabilities | null
}

export type AgentToolUseCounts = {
  web: number
  knowledge: number
}

/** Counts completed source-bearing plan tasks. A direct kernel run may not
 * expose task rows; zero then means "not available", never "not used". */
export function countAgentToolUse({
  tasks,
  taskStates,
}: {
  tasks: Array<{ taskId: string; toolKind: string }>
  taskStates: Record<string, { status: string }>
}): AgentToolUseCounts {
  return tasks.reduce<AgentToolUseCounts>(
    (counts, task) => {
      if (taskStates[task.taskId]?.status !== 'completed') return counts
      if (task.toolKind === 'web_instant' || task.toolKind === 'web_research') {
        counts.web += 1
      } else if (task.toolKind === 'rag_query') {
        counts.knowledge += 1
      }
      return counts
    },
    { web: 0, knowledge: 0 },
  )
}

const KERNEL_ROW_BY_TOOL: Record<string, ApprovalRow['id']> = {
  web_instant: 'web_search',
  search_project_knowledge: 'knowledge_search',
  run_web_research: 'research',
  run_deep_mission: 'research',
  delegate_batch: 'research',
  load_skill: 'skill_activation',
  propose_editor_patch: 'editor_patch',
}

const TOOL_CAPABILITY_IDS: Record<ToolAvailabilityRow['id'], string> = {
  web_search: 'web.search.instant',
  knowledge_search: 'knowledge.search',
  editor_access: 'editor.patch.propose',
}

function kernelApprovalRows(entry: AgentPermissionModeEntry): ApprovalRow[] {
  const states = new Map<string, ApprovalRow>()
  const upsert = (row: ApprovalRow) => {
    const existing = states.get(row.id)
    // A row already marked as asking never downgrades to free (the
    // research pair maps two tools onto one line).
    if (!existing || (existing.state === 'free' && row.state !== 'free')) {
      states.set(row.id, row)
    }
  }
  for (const tool of entry.kernel_always_gated) {
    upsert({ id: KERNEL_ROW_BY_TOOL[tool] ?? tool, state: 'always' })
  }
  for (const tool of entry.kernel_gated_tools) {
    upsert({ id: KERNEL_ROW_BY_TOOL[tool] ?? tool, state: 'asks' })
  }
  for (const tool of entry.kernel_conditional_tools) {
    upsert({ id: KERNEL_ROW_BY_TOOL[tool] ?? tool, state: 'asks', conditional: true })
  }
  // Free rows appear explicitly so "runs without asking" is visible,
  // not just implied by absence.
  for (const id of ['web_search', 'knowledge_search', 'research', 'skill_activation'] as const) {
    if (!states.has(id)) upsert({ id, state: 'free' })
  }
  const order = ['web_search', 'knowledge_search', 'research', 'skill_activation', 'editor_patch']
  return [...states.values()].sort((a, b) => {
    const ia = order.indexOf(a.id)
    const ib = order.indexOf(b.id)
    return (ia === -1 ? order.length : ia) - (ib === -1 ? order.length : ib)
  })
}

function phaseMachineApprovalRows(entry: AgentPermissionModeEntry): ApprovalRow[] {
  return [
    { id: 'plan', state: entry.plan_gate ? 'asks' : 'free' },
    { id: 'web_search', state: entry.web_replan_regate ? 'asks' : 'free' },
    { id: 'editor_patch', state: entry.patch_gate ? 'always' : 'free' },
  ]
}

/** The structural subset of `capabilities.agent` the overview reads —
 * the demo provides this shape without faking the full block. */
export type AgentOverviewSource = {
  default_mode?: string
  durable: boolean
  tools: AgentToolManifestEntry[]
  permission_modes?: Record<string, AgentPermissionModeEntry>
  limits?: AgentLimitCapabilities
  source_controls?: AgentSourceControl[]
  /** The server accepts `knowledge_filters.scope = "all"` (session
   * knowledge switch); absent on older servers. */
  knowledge_scope_all?: boolean
}

/** Composer source availability, derived ONCE from the published
 * `source_controls` (older servers: the tool manifest) for the dock, the
 * overview rows and the demo twin alike. */
export type AgentSourceAvailability = {
  web: boolean
  knowledge: boolean
  /** Server reason while knowledge is unavailable; null when available or
   * unreported. */
  knowledgeReason: string | null
}

export function sourceAvailabilityFromOverviewSource(
  agent: AgentOverviewSource | null | undefined,
): AgentSourceAvailability {
  const published = agent?.source_controls
  if (published?.length) {
    const knowledge = published.find((entry) => entry.id === 'knowledge')
    return {
      web: published.find((entry) => entry.id === 'web')?.available === true,
      knowledge: knowledge?.available === true,
      knowledgeReason: knowledge?.reason_code ?? null,
    }
  }
  const manifest = new Set((agent?.tools ?? []).map((tool) => tool.id))
  return {
    web: manifest.has(TOOL_CAPABILITY_IDS.web_search),
    knowledge: manifest.has(TOOL_CAPABILITY_IDS.knowledge_search),
    knowledgeReason: null,
  }
}

export type AgentEngineMode = 'workspace_agent' | 'agent_kernel'
export type AgentDepth = 'normal' | 'deep'

/** Resolve one depth value for both composer display and run submission.
 * An explicit user choice wins; unknown/legacy capability values fail to the
 * backwards-compatible normal mode. */
export function effectiveAgentDepth(
  selected: AgentDepth | null | undefined,
  capabilityDefault: string | null | undefined,
): AgentDepth {
  if (selected) return selected
  return capabilityDefault === 'deep' ? 'deep' : 'normal'
}

/** The engine a fresh composer starts on: the server's published default,
 * but ONLY when the kernel feature flag confirms it is registered — a
 * default the server cannot run must never preselect. */
export function defaultEngineMode(
  agent: AgentOverviewSource | null,
  features: InqtrixCapabilities['features'] | null,
): AgentEngineMode {
  return agent?.default_mode === 'agent_kernel'
    && features?.agent_kernel === true
    ? 'agent_kernel'
    : 'workspace_agent'
}

export function buildAgentOverview({
  agent,
  autonomy,
  kernelSelectable,
  mode,
}: {
  agent: AgentOverviewSource | null
  autonomy: string
  /** True when the server registered the kernel (`features.agent_kernel`)
   * — feeds the availability row, independent of the selection. */
  kernelSelectable: boolean
  /** The SELECTED engine (composer state) — the caller gates
   * availability; the overview describes exactly what would run. */
  mode: AgentEngineMode
}): AgentOverview {
  const brain = mode
  const kernel: AgentOverview['kernel'] =
    mode === 'agent_kernel'
      ? 'active'
      : kernelSelectable
        ? 'selectable'
        : 'unavailable'
  const entry = agent?.permission_modes?.[autonomy] ?? null
  const approvals = entry
    ? brain === 'agent_kernel'
      ? kernelApprovalRows(entry)
      : phaseMachineApprovalRows(entry)
    : null
  const manifestIds = new Set(
    (agent?.tools ?? []).map((tool: AgentToolManifestEntry) => tool.id),
  )
  const sources = sourceAvailabilityFromOverviewSource(agent)
  const tools = (
    Object.keys(TOOL_CAPABILITY_IDS) as ToolAvailabilityRow['id'][]
  ).map((id) => ({
    id,
    available: id === 'web_search'
      ? sources.web
      : id === 'knowledge_search'
        ? sources.knowledge
        : manifestIds.has(TOOL_CAPABILITY_IDS[id]),
  }))
  return {
    brain,
    kernel,
    durable: agent?.durable === true,
    approvals,
    tools,
    limits: agent?.limits ?? null,
  }
}
