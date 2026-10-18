/**
 * Pure builders for user-added plan tasks at the approval gate.
 *
 * Tier budgets shown here MIRROR the backend `TIER_POLICIES` table
 * (like the `MAX_TASK_QUERIES` mirror in PlanReviewBody): the gate must
 * not OFFER what the validator will reject, but the server validator
 * remains the enforcement. An unset tier keeps the legacy depth
 * semantics (no ceiling).
 */

import type { AgentPlanDraft, AgentPlanTaskDraft } from './usePlanApproval'

const ALL_WEB_PROFILES = ['schnell', 'compact', 'deep'] as const

const TIER_WEB_OPTIONS: Record<string, readonly string[]> = {
  schnell: [],
  gruendlich: ['schnell', 'compact'],
  tief: ALL_WEB_PROFILES,
}

const TIER_WEB_DEFAULT: Record<string, string> = {
  gruendlich: 'schnell',
  tief: 'compact',
}

const TIER_RAG_DEFAULT: Record<string, string> = {
  schnell: 'schnell',
  gruendlich: 'standard',
  tief: 'gruendlich',
}

/** Suchtiefe options the gate may OFFER for a web_research task. A
 * tier publishes a ceiling ladder; a tier-LESS run mirrors the legacy
 * server behavior, which pins the profile EXACTLY (depth-derived) —
 * offering more would let the validator reject the edit. */
export function webProfileOptionsForTier(
  tier: string | undefined,
  depth?: string,
): readonly string[] {
  if (!tier) return [depth === 'deep' ? 'deep' : 'compact']
  return TIER_WEB_OPTIONS[tier] ?? ALL_WEB_PROFILES
}

/** Tier-policy-driven params of one user-added task. */
export function newTaskParams(
  kind: 'rag_query' | 'web_instant' | 'web_research',
  { tier, depth }: { tier?: string; depth?: string },
): Record<string, unknown> {
  if (kind === 'rag_query') {
    return { profile: (tier && TIER_RAG_DEFAULT[tier]) || 'standard' }
  }
  if (kind === 'web_research') {
    const fallback = depth === 'deep' ? 'deep' : 'compact'
    return { profile: (tier && TIER_WEB_DEFAULT[tier]) || fallback }
  }
  return {}
}

/** One truthful user-added execution unit for the plan draft. */
export function buildUserPlanTask({
  kind,
  text,
  collectionIds,
  tier,
  depth,
  taskId,
}: {
  kind: 'rag_query' | 'web_instant' | 'web_research'
  text: string
  collectionIds: string[]
  tier?: string
  depth?: string
  taskId: string
}): AgentPlanTaskDraft {
  const params = newTaskParams(kind, { depth, tier })
  return {
    taskId,
    title: text.slice(0, 80),
    toolKind: kind,
    objective: text,
    queries: [text],
    gapIds: [],
    dependsOn: [],
    budget: {},
    params:
      kind === 'rag_query' && collectionIds.length > 0
        ? { ...params, collection_ids: collectionIds }
        : params,
    expectedOutput: '',
  }
}

/** Insert before the synthesis task and extend its dependencies. */
export function withUserPlanTask(
  draft: AgentPlanDraft,
  task: AgentPlanTaskDraft,
): AgentPlanDraft {
  const synthesisIndex = draft.tasks.findIndex(
    (item) => item.toolKind === 'synthesis',
  )
  const tasks = [...draft.tasks]
  if (synthesisIndex === -1) {
    tasks.push(task)
  } else {
    tasks.splice(synthesisIndex, 0, task)
  }
  return {
    ...draft,
    tasks: tasks.map((item) =>
      item.toolKind === 'synthesis'
        ? { ...item, dependsOn: [...item.dependsOn, task.taskId] }
        : item,
    ),
  }
}
