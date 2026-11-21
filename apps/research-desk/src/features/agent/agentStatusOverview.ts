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
  id: 'web_search' | 'knowledge_search' | 'research' | 'skill_activation' | 'editor_patch' | string
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
  /** Whether the kernel runs on this deployment — `active`, or
   * `unavailable` when the server did not register it. Always rendered:
   * an absent row would hide that the engine exists. */
  kernel: 'active' | 'unavailable'
  /** Why the kernel is unavailable, as the server names it
   * (`feature_status.agent_kernel.reason_code`); null otherwise. */
  kernelReason: string | null
  durable: boolean
  /** Null when the server does not publish permission_modes (or the
   * selected mode is unknown) — the group hides. */
  approvals: ApprovalRow[] | null
  tools: ToolAvailabilityRow[]
  /** Null on older servers. The UI hides the group instead of inventing
   * defaults that may disagree with operator policy. */
  limits: AgentLimitCapabilities | null
}

const KERNEL_ROW_BY_TOOL: Record<string, ApprovalRow['id']> = {
  web_instant: 'web_search',
  search_project_knowledge: 'knowledge_search',
  run_web_research: 'research',
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

/** The structural subset of `capabilities.agent` the overview reads —
 * the demo provides this shape without faking the full block. */
export type AgentOverviewSource = {
  default_mode?: string | null
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

/** The permission mode a run starts in: the selection while the server still
 * offers it, otherwise the server's default. A selection can outlive the offer
 * when an operator turns a mode off while the desk stays open. */
export function effectiveAgentAutonomy(
  selected: string | null | undefined,
  agent: { autonomy_modes?: string[]; default_autonomy?: string } | null | undefined,
): string {
  const fallback = agent?.default_autonomy ?? 'balanced'
  if (!selected) return fallback
  return !agent?.autonomy_modes || agent.autonomy_modes.includes(selected) ? selected : fallback
}

/** The Agent Desk shows where the server set up agent runs
 * (`features.agent_desk`), or in demo mode. Without the kernel it stays
 * and names the reason; it never falls back to another engine. */
export function agentDeskVisible(
  features: InqtrixCapabilities['features'] | null | undefined,
  demo: boolean,
): boolean {
  return demo || features?.agent_desk === true
}

/** Whether a run can start: on a server only with the registered kernel;
 * without it the composer shows the server's reason and sends nothing. The
 * demo always can. */
export function agentRunsAvailable(
  features: InqtrixCapabilities['features'] | null | undefined,
  serverEnabled: boolean,
): boolean {
  return !serverEnabled || features?.agent_kernel === true
}

export function buildAgentOverview({
  agent,
  autonomy,
  kernelReason = null,
  kernelSelectable,
}: {
  agent: AgentOverviewSource | null
  autonomy: string
  /** The server's reason while the kernel is not registered. */
  kernelReason?: string | null
  /** True when the server registered the kernel (`features.agent_kernel`)
   * — feeds the availability row. */
  kernelSelectable: boolean
}): AgentOverview {
  // A kernel the server did not register is unavailable, so its reason
  // shows.
  const kernel: AgentOverview['kernel'] = kernelSelectable ? 'active' : 'unavailable'
  const entry = agent?.permission_modes?.[autonomy] ?? null
  const approvals = entry ? kernelApprovalRows(entry) : null
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
    kernel,
    kernelReason: kernel === 'unavailable' ? kernelReason : null,
    durable: agent?.durable === true,
    approvals,
    tools,
    limits: agent?.limits ?? null,
  }
}
