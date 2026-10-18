import type { TranslationDictionary } from '../../i18n/translations'
import { activityTitle } from './activityPresentation'
import type { AgentRunRecord, AgentStepEntry } from './model'
import { KERNEL_REVIEW_NARRATION_ID } from './runPresentation'
import { isEvidenceReadEntry } from './timeline/stepGroups'

type HeadlineRun = Pick<
  AgentRunRecord,
  'activity' | 'artifactOrder' | 'artifacts' | 'phase' | 'queuePosition' | 'status' | 'stepLog'
>

/**
 * The run line's headline: WHAT kind of work runs now — never its detail.
 *
 * The step rows below already say what exactly runs (the query, the child's
 * search count, the section). A headline that repeated the newest row put
 * the same sentence on screen twice (the child line in the head and under
 * its delegation row). So the head names the category — the tool or
 * operation title, the review, the answer being written — and the rows keep
 * the detail. A model turn's row is one word with no detail, so the head
 * says what the run does instead of that word. Waiting states are named as
 * such: a wait is no idle run.
 */
export function runHeadline(run: HeadlineRun, t: TranslationDictionary): string {
  if (run.status === 'queued') {
    return run.queuePosition && run.queuePosition > 0
      ? t.agent.activity.queued
      : t.agent.activity.starting
  }
  if (run.status === 'waiting_for_approval') return t.agent.activity.waitingApproval
  if (run.status === 'waiting_for_input') return t.agent.activity.waitingInput
  if (answerBeingWritten(run)) return t.agent.activity.writingAnswer
  // `done` comes before the answer's publication: a run still active in
  // it is publishing its answer, not finished and not executing tasks.
  if (run.phase === 'done') return t.agent.activity.writingAnswer
  if (openNarration(run.stepLog)?.narrationId === KERNEL_REVIEW_NARRATION_ID) {
    return t.agent.activity.reviewing
  }
  const open = newestOpenActivity(run.stepLog)
  if (open) {
    if (isEvidenceReadEntry(open)) return t.agent.activityOperations.evidenceRead
    if (open.activityOperation === 'model_turn') return t.agent.activity.thinking
    return activityTitle(
      {
        current: open.current,
        detail: open.detail ?? '',
        kind: open.activityKind ?? 'working',
        label: open.label,
        operation: open.activityOperation,
        operationCode: open.activityOperationCode,
        purpose: open.purpose,
        status: open.status,
      },
      t,
    )
  }
  if (run.status === 'waiting_for_children') return t.agent.activity.childWorking
  const phases = t.agent.activity as Record<string, string | undefined>
  return (run.phase && phases[run.phase]) || t.agent.activity.execution
}

function answerBeingWritten(run: Pick<HeadlineRun, 'artifactOrder' | 'artifacts'>): boolean {
  return run.artifactOrder.some((artifactId) => {
    const artifact = run.artifacts[artifactId]
    return artifact?.kind === 'answer' && artifact.status === 'writing'
  })
}

/** The newest narration line still being written, if it is the newest step. */
function openNarration(stepLog: readonly AgentStepEntry[]): AgentStepEntry | undefined {
  for (let index = stepLog.length - 1; index >= 0; index -= 1) {
    const entry = stepLog[index]
    if (entry.kind === 'narration') return entry.open ? entry : undefined
  }
  return undefined
}

/** The newest activity row that has not settled yet. A row keeps its
 * event's own status: a tool row runs as `running`, a model turn as
 * `started` until it settles. */
function newestOpenActivity(stepLog: readonly AgentStepEntry[]): AgentStepEntry | undefined {
  for (let index = stepLog.length - 1; index >= 0; index -= 1) {
    const entry = stepLog[index]
    if (entry.kind === 'activity' && (entry.status === 'running' || entry.status === 'started')) {
      return entry
    }
  }
  return undefined
}
