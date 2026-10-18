import type { AgentPlanTaskRecord, AgentRunRecord } from './model'
import { TERMINAL_AGENT_TASK_STATUSES } from './model'
import { effectiveAgentTaskStatus } from './plan/taskPresentation'

/**
 * Terminal tasks of the open run view worth prefetching, capped so an
 * idle callback never fans out across a large plan. Order follows the
 * plan (the order the overview renders), synthesis excluded like the
 * overview itself — pure so the cap/terminal rules stay testable.
 */
export function prefetchableTaskResultIds(
  tasks: readonly AgentPlanTaskRecord[],
  taskStates: AgentRunRecord['taskStates'],
  cap: number,
): string[] {
  const ids: string[] = []
  for (const task of tasks) {
    if (ids.length >= cap) break
    if (task.toolKind === 'synthesis') continue
    const status = effectiveAgentTaskStatus(task, taskStates[task.taskId])
    if (TERMINAL_AGENT_TASK_STATUSES.has(status)) ids.push(task.taskId)
  }
  return ids
}
