import type { AgentPlanTaskRecord, AgentTaskLiveState } from '../model'
import type { AgentTaskResultWire } from '../types'
import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'

export type AgentTaskEffectiveStatus =
  | 'pending'
  | 'running'
  | 'cancel_requested'
  | 'cancelled'
  | 'completed'
  | 'failed'
  | 'insufficient_evidence'
  | 'skipped'

export type AgentTaskGroup = 'active' | 'attention' | 'completed'

export type AgentPlanExecutionWave = {
  taskCount: number
  toolCounts: Partial<Record<AgentPlanTaskRecord['toolKind'], number>>
}

export type AgentTaskExecutionSemantics = {
  kind: 'instant' | 'research' | 'knowledge' | 'file' | 'synthesis'
  profile: string
  queryCount: number
  requestCount: number
}

export type AgentTaskMetricSnapshot = {
  consolidated_claim_count?: number
  total_queries?: number
  total_sources?: number
}

export type AgentTaskMetric = {
  kind: 'sources' | 'queries' | 'claims'
  value: number
}

/** Tool-aware execution semantics for plan and live cards. The generic
 * `queries` array means literal calls for local retrieval tools, but guidance
 * questions inside one delegated research run — keeping that distinction in
 * one helper prevents UI labels from drifting. */
export function agentTaskExecutionSemantics(
  task: Pick<AgentPlanTaskRecord, 'params' | 'queries' | 'toolKind'>,
): AgentTaskExecutionSemantics {
  const queryCount = task.queries.filter((query) => query.trim()).length
  const profile = typeof task.params.profile === 'string' ? task.params.profile : ''
  switch (task.toolKind) {
    case 'web_instant':
      return {
        kind: 'instant',
        profile: '',
        queryCount,
        requestCount: Math.max(1, queryCount),
      }
    case 'web_research':
      return {
        kind: 'research',
        profile: profile || 'compact',
        queryCount,
        requestCount: 1,
      }
    case 'rag_query':
      return {
        kind: 'knowledge',
        profile,
        queryCount,
        requestCount: Math.max(1, queryCount),
      }
    case 'file_analysis':
      return {
        kind: 'file',
        profile: '',
        queryCount,
        requestCount: Math.max(1, queryCount),
      }
    case 'synthesis':
      return {
        kind: 'synthesis',
        profile: '',
        queryCount: 0,
        requestCount: 1,
      }
  }
}

/** Live events win while connected; the durable plan row is the reload-safe
 * fallback once task status/result rows have been reconciled server-side. */
export function effectiveAgentTaskStatus(
  task: Pick<AgentPlanTaskRecord, 'status'>,
  live: AgentTaskLiveState | undefined,
): AgentTaskEffectiveStatus {
  if (live?.status) return live.status
  if (
    task.status === 'running'
    || task.status === 'cancel_requested'
    || task.status === 'cancelled'
    || task.status === 'completed'
    || task.status === 'failed'
    || task.status === 'insufficient_evidence'
    || task.status === 'skipped'
  ) {
    return task.status
  }
  return 'pending'
}

export function agentTaskExecutionLabel(
  task: Pick<AgentPlanTaskRecord, 'params' | 'queries' | 'toolKind'>,
  t: TranslationDictionary,
): string {
  const semantics = agentTaskExecutionSemantics(task)
  switch (semantics.kind) {
    case 'instant':
      return countLabel(
        semantics.requestCount,
        t.agent.task.instantOne,
        t.agent.task.instantMany,
      )
    case 'research':
      return countLabel(
        semantics.queryCount,
        t.agent.task.researchOne,
        t.agent.task.research,
      ).replace('{profile}', researchProfileLabel(semantics.profile, t))
    case 'knowledge':
      return countLabel(
        semantics.requestCount,
        t.agent.task.knowledgeOne,
        t.agent.task.knowledgeMany,
      )
    case 'file':
      return countLabel(
        semantics.requestCount,
        t.agent.task.fileOne,
        t.agent.task.file,
      )
    case 'synthesis':
      return t.agent.task.synthesis
  }
}

/** Plain type name for the tool icon's tooltip — the icon alone leaves the
 * task KIND implicit at the approval gate. */
export function agentTaskTypeLabel(
  toolKind: AgentPlanTaskRecord['toolKind'],
  t: TranslationDictionary,
): string {
  return t.agent.task.typeLabels[toolKind]
}

export function agentTaskQueryLabel(
  task: Pick<AgentPlanTaskRecord, 'params' | 'queries' | 'toolKind'>,
  t: TranslationDictionary,
): string {
  const semantics = agentTaskExecutionSemantics(task)
  if (semantics.kind === 'research') return t.agent.task.guidingQuestions
  if (semantics.kind === 'instant') return t.agent.task.searchRequests
  return t.agent.plan.queries
}

export function agentTaskStatusLabel(
  status: AgentTaskEffectiveStatus,
  t: TranslationDictionary,
): string {
  if (status === 'running') return t.agent.task.statusRunning
  if (status === 'cancel_requested') return t.agent.task.statusCancelRequested
  if (status === 'cancelled') return t.agent.task.statusCancelled
  if (status === 'completed') return t.agent.task.statusCompleted
  if (status === 'failed') return t.agent.task.statusFailed
  if (status === 'insufficient_evidence') {
    return t.agent.task.statusInsufficientEvidence
  }
  if (status === 'skipped') return t.agent.task.statusSkipped
  return t.agent.task.statusPending
}

export function agentTaskGroup(
  status: AgentTaskEffectiveStatus,
  fallback = false,
): AgentTaskGroup {
  if (fallback) return 'attention'
  if (status === 'completed') return 'completed'
  if (
    status === 'failed'
    || status === 'cancelled'
    || status === 'insufficient_evidence'
    || status === 'skipped'
  ) return 'attention'
  return 'active'
}

/** The run view's task groups, in display order, empty groups left out.
 * Only a visible fallback path moves a task under attention; an evidence
 * limit is a note and leaves the task in its own status group. */
export function agentTaskGroups(
  tasks: AgentPlanTaskRecord[],
  taskStates: Readonly<Record<string, AgentTaskLiveState | undefined>>,
): Array<{ kind: AgentTaskGroup; tasks: AgentPlanTaskRecord[] }> {
  const groups: Array<{ kind: AgentTaskGroup; tasks: AgentPlanTaskRecord[] }> = [
    { kind: 'active', tasks: [] },
    { kind: 'attention', tasks: [] },
    { kind: 'completed', tasks: [] },
  ]
  for (const task of tasks) {
    const live = taskStates[task.taskId]
    const kind = agentTaskGroup(
      effectiveAgentTaskStatus(task, live),
      live?.fallback === true,
    )
    groups.find((group) => group.kind === kind)?.tasks.push(task)
  }
  return groups.filter((group) => group.tasks.length > 0)
}

/** The task's truthful elapsed seconds at ``now``. Terminal operation time wins
 * over a later wave-level terminal fold, which is essential for parallel work. */
export function agentTaskElapsedSeconds(
  live: Pick<AgentTaskLiveState, 'finishedAt' | 'startedAt'> | undefined,
  now: number,
): number | undefined {
  if (live?.startedAt === undefined) return undefined
  return Math.max(0, (live.finishedAt ?? now) - live.startedAt)
}

/** Metrics available by execution contract. Local instant/knowledge lanes do
 * not claim-extract, so a synthetic zero would imply a quality failure. */
export function agentTaskMetrics(
  snapshot: AgentTaskMetricSnapshot | undefined,
  includeZeroClaims: boolean,
): AgentTaskMetric[] {
  if (!snapshot) return []
  const metrics: AgentTaskMetric[] = []
  if (snapshot.total_sources !== undefined) {
    metrics.push({ kind: 'sources', value: snapshot.total_sources })
  }
  if (snapshot.total_queries !== undefined) {
    metrics.push({ kind: 'queries', value: snapshot.total_queries })
  }
  if (
    snapshot.consolidated_claim_count !== undefined
    && (includeZeroClaims || snapshot.consolidated_claim_count > 0)
  ) {
    metrics.push({ kind: 'claims', value: snapshot.consolidated_claim_count })
  }
  return metrics
}

/** Compact plain-text task evidence for transcript/card previews. The raw
 * Markdown remains untouched for the detail renderer and copy surface. */
export function agentTaskResultPreview(title: string, markdown: string): string {
  let text = markdown
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~>#|]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  const normalizedTitle = title.trim()
  if (
    normalizedTitle
    && text.toLocaleLowerCase().startsWith(
      `${normalizedTitle.toLocaleLowerCase()}:`,
    )
  ) {
    text = text.slice(normalizedTitle.length + 1).trimStart()
  }
  return text
}

export function agentTaskResultContent(
  result: AgentTaskResultWire | null,
  preview: string,
  loadError: string | null,
): { markdown: string; previewOnly: boolean } {
  const complete = result?.answer_markdown || result?.result_summary || ''
  const markdown = complete || preview
  return {
    markdown,
    previewOnly: Boolean(markdown) && (
      Boolean(loadError) || result?.legacy_summary_only === true
    ),
  }
}

/** Topological execution waves derived only from explicit dependencies. Tasks
 * within a wave may run in parallel; later waves start after prior dependencies.
 * Invalid cycles return no summary rather than inventing an order. */
export function agentPlanExecutionWaves(
  tasks: Array<Pick<
    AgentPlanTaskRecord,
    'dependsOn' | 'ordinal' | 'taskId' | 'toolKind'
  >>,
): AgentPlanExecutionWave[] {
  const remaining = new Map(tasks.map((task) => [task.taskId, task]))
  const completed = new Set<string>()
  const waves: AgentPlanExecutionWave[] = []
  while (remaining.size > 0) {
    const ready = [...remaining.values()]
      .filter((task) => task.dependsOn.every((id) => completed.has(id)))
      .sort((a, b) => a.ordinal - b.ordinal)
    if (ready.length === 0) return []
    const toolCounts: AgentPlanExecutionWave['toolCounts'] = {}
    for (const task of ready) {
      toolCounts[task.toolKind] = (toolCounts[task.toolKind] ?? 0) + 1
      remaining.delete(task.taskId)
      completed.add(task.taskId)
    }
    waves.push({ taskCount: ready.length, toolCounts })
  }
  return waves
}

function researchProfileLabel(
  profile: string,
  t: TranslationDictionary,
): string {
  if (profile === 'deep') return t.agent.task.profileDeep
  if (profile === 'compact') return t.agent.task.profileCompact
  if (profile === 'schnell') return t.agent.task.profileFast
  return profile || t.agent.task.profileCompact
}
