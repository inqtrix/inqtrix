/**
 * Pure SSE-event application for agent runs (the `applyKnowledgeRunEvent`
 * pattern): `(record, event) -> record`, no i18n, no clocks, no fetches —
 * fully unit-testable. Control-row payloads are NOT copied from events
 * (rows are the truth); events only flip the `*Stale` flags the
 * run hook turns into refetches, and carry the live task/phase state.
 */

import type {
  AgentChildOutcome,
  AgentClaimMark,
  AgentReviewFinding,
  AgentRunOutcome,
  AgentRunOutcomeLine,
  AgentRunRecord,
  AgentStepEntry,
  AgentTaskLiveState,
  AgentTouchedArtifact,
} from './model'
import {
  agentPhaseStation,
  isGateAgentRun,
  MAX_REVIEW_FINDINGS,
  MAX_STEP_LOG,
  TERMINAL_AGENT_TASK_STATUSES,
} from './model'
import type {
  ResearchRunEvent,
  ResearchRunSnapshot,
  ResearchRunStatus,
} from '@/features/researchRuns/types'
import { asFiniteNumber, asString, asStringArray } from '@/lib/coerce'
import { isoFromUnixSeconds, unixSecondsFromIso } from '@/lib/time'
import { normalizeAgentOperation } from './activityPresentation'
import { CHILD_DECISION_KINDS, stripTaskTitle } from './childMission'
import { mergeResearchSnapshot } from './taskProgress'

/** Apply the durable cancellation acknowledgement immediately.
 *
 * The HTTP response is authoritative even when SSE is degraded. Keeping this
 * projection beside the event reducer prevents a second, subtly different
 * task-state transition in the control hook.
 */
export function acknowledgeAgentTaskCancellation(
  record: AgentRunRecord,
  taskId: string,
  status: 'cancel_requested' | 'cancelled',
  childRunId: string | null,
): AgentRunRecord {
  const previous = record.taskStates[taskId]
  return {
    ...record,
    planStale: true,
    taskStates: {
      ...record.taskStates,
      [taskId]: taskCancellationState(previous, status, childRunId),
    },
  }
}


function taskCancellationState(
  previous: AgentTaskLiveState | undefined,
  status: 'cancel_requested' | 'cancelled',
  childRunId: string | null,
  finishedAt?: number,
): AgentTaskLiveState {
  const nextStatus = monotonicTaskStatus(previous?.status, status)
  if (previous && nextStatus === previous.status) return previous
  return {
    ...(previous ?? { attempt: 1, status: 'running' }),
    status: nextStatus,
    childRunId: childRunId ?? previous?.childRunId,
    ...(status === 'cancelled' && finishedAt !== undefined
      ? { finishedAt }
      : {}),
  }
}

function monotonicTaskStatus(
  current: AgentTaskLiveState['status'] | undefined,
  incoming: AgentTaskLiveState['status'],
  currentAttempt = 1,
  incomingAttempt = currentAttempt,
): AgentTaskLiveState['status'] {
  if (!current) return incoming
  if (
    current === 'failed'
    && incoming === 'running'
    && incomingAttempt > currentAttempt
  ) return incoming
  if (TERMINAL_AGENT_TASK_STATUSES.has(current)) return current
  if (current === 'cancel_requested' && incoming !== 'cancelled') {
    return current
  }
  return incoming
}

/** Operation codes of the evidence-limit notes. Events stored by earlier
 * versions marked them with ``fallback: true``; the reducer reads that
 * stored flag as the evidence limit it stood for, so a reopened run keeps
 * a completed task in its own status group. */
const EVIDENCE_LIMIT_OPERATION_CODES: ReadonlySet<string> = new Set([
  'task.evidence_needs_review',
  'task.evidence_partial',
])

/** The in-place key of a run's one sufficiency row (`appendStep` updates
 * an entry whose key is already logged). Distinct from every narration id
 * the server sends (`n-…`). */
const SUFFICIENCY_NOTICE_ID = 'notice:sufficiency_gap'

export function applyAgentRunEvent(
  record: AgentRunRecord,
  event: ResearchRunEvent,
  options?: { arrivedLive: boolean; receivedAt?: number },
): AgentRunRecord {
  // Replayed/duplicate frames must be no-ops (reconnects replay history).
  if (event.sequence <= record.lastSequence) return record
  const next: AgentRunRecord = {
    ...record,
    lastSequence: event.sequence,
    // Every run event is progress — the stream carries no heartbeats — and
    // a child's work reaches the parent as its own events. The sequence
    // guard above keeps this monotonic.
    lastProgressAt: event.created_at,
  }
  if (options?.arrivedLive && options.receivedAt !== undefined) {
    // The smallest (arrival - server time) seen is the clock skew plus the
    // fastest delivery: close enough for a seconds display.
    const offset = options.receivedAt - event.created_at
    next.clockOffsetSeconds = Math.min(record.clockOffsetSeconds ?? offset, offset)
  }
  const data = event.data ?? {}

  switch (event.type) {
    case 'inqtrix.run.queued':
      next.status = 'queued'
      return next
    case 'inqtrix.run.started':
      next.status = 'running'
      next.startedAt ??= isoFromUnixSeconds(event.created_at)
      return next
    case 'inqtrix.run.resumed':
      // A resume starts another execution segment; the original start remains
      // the wall-clock anchor for the user's complete wait time.
      next.status = 'running'
      return next
    case 'inqtrix.run.waiting': {
      const status = stringField(data, 'status')
      next.status =
        status === 'waiting_for_input'
          ? 'waiting_for_input'
          : status === 'waiting_for_children'
            ? 'waiting_for_children'
            : 'waiting_for_approval'
      return next
    }
    case 'inqtrix.run.completed':
      next.status = 'completed'
      next.artifactsStale = true
      applyTerminalRunTime(next, event)
      settleOpenKernelToolRows(next, record, 'completed')
      settleOpenTodoOnCompletion(next)
      return next
    case 'inqtrix.run.failed': {
      next.status = 'failed'
      next.artifactsStale = true
      // Structured error first (message + stable code), the legacy
      // bare-message field only as fallback — a typed failure like
      // iteration_limit must not degrade to an empty generic line.
      const parsedFailure = errorInfo(data.error)
      next.errorCode = parsedFailure.code ?? record.errorCode
      next.error =
        parsedFailure.message
        || stringField(data, 'message')
        || record.error
      applyTerminalRunTime(next, event)
      settleOpenKernelToolRows(next, record, 'failed')
      return next
    }
    case 'inqtrix.run.cancelled':
      next.status = 'cancelled'
      next.artifactsStale = true
      applyTerminalRunTime(next, event)
      settleOpenKernelToolRows(next, record, 'failed')
      return next
    case 'inqtrix.run.snapshot': {
      const snapshot = data.snapshot as ResearchRunSnapshot | undefined
      if (snapshot) next.snapshot = snapshot
      return next
    }

    case 'inqtrix.agent.phase.changed': {
      const phase = stringField(data, 'phase')
      if (phase) {
        next.phase = phase
        const station = agentPhaseStation(phase)
        if (station) next.station = station
        // A segment restart re-enters the phase it left; a second
        // AUSFÜHRUNG heading under the first says nothing new.
        const lastPhaseEntry = [...record.stepLog]
          .reverse()
          .find((item) => item.kind === 'phase')
        if (lastPhaseEntry?.phase !== phase) {
          appendStep(next, { kind: 'phase', phase })
        }
      }
      return next
    }

    case 'inqtrix.agent.plan.proposed':
    case 'inqtrix.agent.plan.revised': {
      next.planStale = true
      const version = numberField(data, 'version')
      // A revised plan can arrive auto-approved (balanced replans) — the
      // timeline shows that as a note instead of an approval card.
      const autoApproved =
        event.type === 'inqtrix.agent.plan.revised'
        && Boolean(data.auto_approved)
      if (autoApproved && version !== undefined) {
        next.lastAutoApprovedVersion = version
      }
      appendStep(next, { autoApproved, kind: 'plan', version })
      return next
    }

    case 'inqtrix.agent.approval.requested': {
      next.approvalsStale = true
      // The transcript remembers WHAT was asked, not only the decision —
      // history shows the gate after it settled. Requests can re-emit
      // across segments, so the marker dedupes on its subject id.
      const approvalId = stringField(data, 'approval_id')
      if (
        approvalId
        && !record.stepLog.some(
          (item) =>
            item.kind === 'gate_requested' && item.approvalId === approvalId,
        )
      ) {
        appendStep(next, {
          approvalId,
          detail: stringField(data, 'kind') || undefined,
          kind: 'gate_requested',
        })
      }
      return next
    }
    case 'inqtrix.agent.approval.decided': {
      next.approvalsStale = true
      const approvalId = stringField(data, 'approval_id')
      // Settle the LOCAL row immediately: a decision made elsewhere
      // (second tab, server auto-decide) otherwise keeps the gate card
      // mounted for the whole refetch round trip — on a degraded
      // transport that read as a stuck gate. The
      // refetched rows stay the truth; the merge never regresses.
      const decidedStatus = stringField(data, 'status')
      if (
        approvalId
        && (decidedStatus === 'approved'
          || decidedStatus === 'rejected'
          || decidedStatus === 'edited')
      ) {
        next.approvals = record.approvals.map((item) =>
          item.approvalId === approvalId && item.status === 'pending'
            ? { ...item, decidedAt: event.created_at, status: decidedStatus }
            : item)
      }
      const approvalKind =
        stringField(data, 'kind')
        || stringField(data, 'approval_kind')
        || record.approvals.find(
          (approval) => approval.approvalId === approvalId,
        )?.kind
      if (approvalKind === 'plan' || approvalKind === 'replan') {
        next.planStale = true
      }
      // Decision marker for the transcript; the rows carry the content.
      appendStep(next, {
        approvalId: approvalId || undefined,
        kind: 'approval_decided',
        detail: stringField(data, 'status') || undefined,
      })
      return next
    }

    case 'inqtrix.agent.clarification.requested': {
      next.clarificationsStale = true
      const clarificationId = stringField(data, 'clarification_id')
      if (
        clarificationId
        && !record.stepLog.some(
          (item) =>
            item.kind === 'gate_requested'
            && item.clarificationId === clarificationId,
        )
      ) {
        appendStep(next, {
          clarificationId,
          kind: 'gate_requested',
        })
      }
      return next
    }
    case 'inqtrix.node.model_resolution': {
      // Only the ANSWER-shaped nodes drive the chip — the
      // resolution of intake/critic/... is provenance noise here.
      const node = stringField(data, 'node')
      if (
        node !== 'agent_answer'
        && node !== 'agent_answer_light'
        && node !== 'agent_synthesis'
        && node !== 'agent_kernel'
      ) {
        return next
      }
      next.modelResolution = {
        model: stringField(data, 'model'),
        effort: stringField(data, 'effort'),
        tier: stringField(data, 'tier'),
        modelSource: stringField(data, 'model_source'),
      }
      return next
    }
    case 'inqtrix.agent.clarification.answered': {
      next.clarificationsStale = true
      const clarificationId = stringField(data, 'clarification_id')
      // Same immediate local settle as approval.decided.
      if (clarificationId) {
        next.clarifications = record.clarifications.map((item) =>
          item.clarificationId === clarificationId
          && item.status === 'pending'
            ? { ...item, answeredAt: event.created_at, status: 'answered' }
            : item)
      }
      appendStep(next, {
        clarificationId: clarificationId || undefined,
        kind: 'clarification_answered',
      })
      return next
    }

    case 'inqtrix.agent.task.started': {
      const taskId = stringField(data, 'task_id')
      if (!taskId) return next
      const previous = record.taskStates[taskId]
      const incomingAttempt = numberField(data, 'attempt') ?? 1
      const status = monotonicTaskStatus(
        previous?.status,
        'running',
        previous?.attempt ?? 1,
        incomingAttempt,
      )
      const state: AgentTaskLiveState = {
        ...(previous ?? {}),
        status,
        attempt: Math.max(
          previous?.attempt ?? 1,
          incomingAttempt,
        ),
        childRunId:
          stringField(data, 'child_run_id')
          || previous?.childRunId,
        activity: previous?.activity,
        activityHistory: previous?.activityHistory,
        fallback: previous?.fallback,
        metrics: previous?.metrics,
        resultSummary: previous?.resultSummary,
        startedAt: previous?.startedAt ?? event.created_at,
        ...(status === 'running' ? { finishedAt: undefined } : {}),
      }
      next.taskStates = { ...record.taskStates, [taskId]: state }
      appendStep(next, { kind: 'task_started', taskId })
      return next
    }
    case 'inqtrix.agent.task.cancel_requested': {
      const taskId = stringField(data, 'task_id')
      if (!taskId) return next
      const status = stringField(data, 'status')
      const previous = record.taskStates[taskId]
      next.taskStates = {
        ...record.taskStates,
        [taskId]: taskCancellationState(
          previous,
          status === 'cancelled' ? 'cancelled' : 'cancel_requested',
          stringField(data, 'child_run_id') || null,
          event.created_at,
        ),
      }
      next.planStale = true
      return next
    }
    case 'inqtrix.agent.task.finished':
    case 'inqtrix.agent.task.failed': {
      const taskId = stringField(data, 'task_id')
      if (!taskId) return next
      const outcome = stringField(data, 'status')
      const parsedError = errorInfo(data.error)
      const failed =
        event.type === 'inqtrix.agent.task.failed' || outcome === 'failed'
      const insufficient = outcome === 'insufficient_evidence'
      const cancelled = outcome === 'cancelled'
      const previousTask = record.taskStates[taskId]
      const incomingStatus: AgentTaskLiveState['status'] = cancelled
          ? 'cancelled'
          : insufficient
          ? 'insufficient_evidence'
          : failed
            ? 'failed'
            : 'completed'
      const status = monotonicTaskStatus(
        previousTask?.status,
        incomingStatus,
      )
      const lifecycleAccepted = status === incomingStatus
      const state: AgentTaskLiveState = {
        status,
        outcome: lifecycleAccepted
          ? outcome || (failed ? 'failed' : 'completed')
          : previousTask?.outcome,
        attempt: previousTask?.attempt ?? 1,
        childRunId:
          stringField(data, 'child_run_id') ||
          previousTask?.childRunId,
        error: lifecycleAccepted ? parsedError.message : previousTask?.error,
        errorCode: lifecycleAccepted ? parsedError.code : previousTask?.errorCode,
        activity: previousTask?.activity,
        activityHistory: previousTask?.activityHistory,
        metrics: recordField(data, 'metrics') ?? previousTask?.metrics,
        resultSummary:
          lifecycleAccepted
            ? stringField(data, 'result_summary') || previousTask?.resultSummary
            : previousTask?.resultSummary,
        fallback: previousTask?.fallback,
        evidenceLimit: previousTask?.evidenceLimit,
        startedAt: previousTask?.startedAt,
        finishedAt: lifecycleAccepted
          ? previousTask?.finishedAt ?? event.created_at
          : previousTask?.finishedAt,
      }
      next.taskStates = { ...record.taskStates, [taskId]: state }
      if (lifecycleAccepted) {
        // Settle any still-open activity rows of the task: a retry
        // notice or hard-aborted invocation must not stay "running"
        // forever in the protocol once the task itself concluded.
        const settled = status === 'completed' ? 'completed' : 'failed'
        const openRows = record.stepLog.some(
          (item) =>
            item.kind === 'activity'
            && item.taskId === taskId
            && item.status !== 'completed'
            && item.status !== 'failed',
        )
        if (openRows) {
          next.stepLog = (
            next.stepLog === record.stepLog
              ? [...record.stepLog]
              : next.stepLog
          ).map((item) =>
            item.kind === 'activity'
            && item.taskId === taskId
            && item.status !== 'completed'
            && item.status !== 'failed'
              ? { ...item, status: settled }
              : item,
          )
        }
      }
      // Task outcomes update the plan rows (status/result_summary) too.
      next.planStale = true
      appendStep(next, {
        error: state.error,
        kind: failed || insufficient ? 'task_failed' : 'task_finished',
        taskId,
      })
      return next
    }

    case 'inqtrix.agent.child.progress': {
      const childRunId = stringField(data, 'child_run_id')
      const taskId = stringField(data, 'task_id')
      if (!childRunId) return next
      const previousChild = record.children[childRunId]
      const snapshot = mergeResearchSnapshot(
        previousChild?.snapshot,
        data.snapshot,
      )
      const runStatus = stringField(data, 'run_status') || previousChild?.runStatus
      const currentNode = stringField(data, 'current_node') || previousChild?.currentNode
      const message = stringField(data, 'message') || previousChild?.message
      const parsedError = errorInfo(data.error)
      const error = parsedError.message || previousChild?.error
      const errorCode = parsedError.code || previousChild?.errorCode
      const metrics = recordField(data, 'metrics') ?? previousChild?.metrics
      const attempt = numberField(data, 'attempt') ?? previousChild?.attempt
      const updatedAt = numberField(data, 'updated_at') ?? event.created_at
      // Which unit of work a delegated MISSION is on. The status comes
      // from the EVENT, not from the payload's carried-forward value: a
      // task that just started must not inherit the previous task's
      // `completed`, which would freeze the line on a finished step.
      // Which units of work a delegated MISSION has open. A mission
      // starts its whole parallel wave in ONE burst — five tasks in the
      // same millisecond, in the run that motivated this — so a single
      // "current ordinal" named one of five and read as if the other
      // four did not exist. The SET is the honest answer.
      const childEvent = stringField(data, 'event_type')
      const ordinal = numberField(data, 'ordinal')
      const openTasks = nextOpenChildTasks(
        previousChild?.openTasks,
        childEvent,
        ordinal,
      )
      const failedTasks = nextFailedChildTasks(
        previousChild?.failedTasks,
        childEvent,
        ordinal,
      )
      const taskTitle = stringField(data, 'title')
      const taskTitles = childEvent === 'inqtrix.agent.task.started'
        && ordinal !== undefined && taskTitle
        ? { ...(previousChild?.taskTitles ?? {}), [ordinal]: taskTitle }
        : previousChild?.taskTitles
      // A task's searches cross as bracketed activity (started ->
      // completed): the line shows the latest query and counts the
      // completed ones — the number that moves during a long wave.
      const operation = stringField(data, 'operation')
      const isChildSearch = childEvent === 'inqtrix.agent.activity'
        && (operation === 'web.search.instant' || operation === 'knowledge.search')
      const activityStatus = isChildSearch ? stringField(data, 'status') : ''
      const lastQuery = isChildSearch && activityStatus === 'started'
        ? (stringField(data, 'query') || previousChild?.lastQuery)
        : previousChild?.lastQuery
      const searchesDone = (previousChild?.searchesDone ?? 0)
        + (isChildSearch && activityStatus === 'completed' ? 1 : 0)
      // Only the DERIVED synthesis lines cross (outline, section done);
      // the reducer mirrors the server gate so a stray kind never lands.
      const section = childEvent === 'inqtrix.agent.narration'
        && stringField(data, 'kind') === 'synthesis'
        ? (stringField(data, 'text') || previousChild?.section)
        : previousChild?.section
      const sectionsDone = (previousChild?.sectionsDone ?? 0)
        + (childEvent === 'inqtrix.agent.narration'
          && stringField(data, 'narration_id').startsWith('n-section-') ? 1 : 0)
      const childStartedAt = previousChild?.startedAt ?? event.created_at
      // The card's per-task facts, joined by the child's own plan-task id
      // (`child_task_id` on task events and searches, `n-task-<id>` on the
      // result line). Timestamps are the projection's arrival.
      const childTaskId = stringField(data, 'child_task_id')
      const isTaskStart = childEvent === 'inqtrix.agent.task.started'
      const isTaskSettled = childEvent === 'inqtrix.agent.task.finished'
        || childEvent === 'inqtrix.agent.task.failed'
      const taskIds = isTaskStart && ordinal !== undefined && childTaskId
        ? { ...(previousChild?.taskIds ?? {}), [childTaskId]: ordinal }
        : previousChild?.taskIds
      const taskStartedAt = isTaskStart && ordinal !== undefined
        ? { ...(previousChild?.taskStartedAt ?? {}), [ordinal]: event.created_at }
        : previousChild?.taskStartedAt
      const taskKind = stringField(data, 'tool_kind')
      const taskKinds = isTaskStart && ordinal !== undefined && taskKind
        ? { ...(previousChild?.taskKinds ?? {}), [ordinal]: taskKind }
        : previousChild?.taskKinds
      const taskSettledAt = isTaskSettled && ordinal !== undefined
        ? { ...(previousChild?.taskSettledAt ?? {}), [ordinal]: event.created_at }
        : previousChild?.taskSettledAt
      const searchTask = isChildSearch && childTaskId ? taskIds?.[childTaskId] : undefined
      const taskSearches = searchTask !== undefined && activityStatus === 'completed'
        ? {
          ...(previousChild?.taskSearches ?? {}),
          [searchTask]: (previousChild?.taskSearches?.[searchTask] ?? 0) + 1,
        }
        : previousChild?.taskSearches
      const lastQueryTask = isChildSearch && activityStatus === 'started' && searchTask !== undefined
        ? searchTask
        : previousChild?.lastQueryTask
      const narrationKind = childEvent === 'inqtrix.agent.narration' ? stringField(data, 'kind') : ''
      const narrationId = stringField(data, 'narration_id')
      const narrationText = stringField(data, 'text')
      const resultTask = narrationKind === 'task' && narrationId.startsWith('n-task-')
        ? taskIds?.[narrationId.slice('n-task-'.length)]
        : undefined
      const taskResults = resultTask !== undefined && narrationText
        ? {
          ...(previousChild?.taskResults ?? {}),
          [resultTask]: stripTaskTitle(narrationText, taskTitles?.[resultTask]),
        }
        : previousChild?.taskResults
      const planLine = narrationKind === 'plan' && narrationText
        ? narrationText
        : previousChild?.planLine
      const decisionKind = childEvent === 'inqtrix.agent.activity' ? stringField(data, 'kind') : ''
      const decisionDetail = stringField(data, 'detail')
      const decision = CHILD_DECISION_KINDS.has(decisionKind)
        ? {
          kind: decisionKind,
          label: stringField(data, 'label') || stringField(data, 'message'),
          ...(decisionDetail ? { detail: decisionDetail } : {}),
          at: event.created_at,
        }
        : previousChild?.decision
      const researchRounds = (previousChild?.researchRounds ?? 0)
        + (decisionKind === 'critic_research' ? 1 : 0)
      const outcome = childEvent === 'inqtrix.agent.outcome'
        ? childOutcome(data) ?? previousChild?.outcome
        : previousChild?.outcome
      const taskToolKind = stringField(data, 'tool_kind') || previousChild?.taskToolKind
      // A grounded knowledge answer is the child's unit of visible
      // progress: one arrives every 20-60 s during execution. Counting
      // them turns "the same line for twenty minutes" into a number
      // that moves — the difference between "working" and "hung".
      const checkedAnswers = (previousChild?.checkedAnswers ?? 0)
        + (childEvent === 'inqtrix.knowledge.grounding.checked' ? 1 : 0)
      // A kernel role child's settled step: its tool's name and the
      // server's call count. Only that event carries them; every other
      // projection keeps the last ones.
      const isToolStep = childEvent === 'inqtrix.agent.tool.finished'
      const lastTool = isToolStep
        ? (stringField(data, 'tool') || previousChild?.lastTool)
        : previousChild?.lastTool
      const toolCalls = isToolStep
        ? (numberField(data, 'tool_calls') ?? previousChild?.toolCalls)
        : previousChild?.toolCalls
      next.children = {
        ...record.children,
        [childRunId]: {
          childRunId,
          taskId: taskId || previousChild?.taskId || '',
          ...(snapshot ? { snapshot } : {}),
          ...(runStatus ? { runStatus } : {}),
          ...(currentNode ? { currentNode } : {}),
          ...(message ? { message } : {}),
          ...(metrics ? { metrics } : {}),
          ...(attempt !== undefined ? { attempt } : {}),
          ...(openTasks ? { openTasks } : {}),
          ...(failedTasks ? { failedTasks } : {}),
          ...(taskTitles ? { taskTitles } : {}),
          ...(lastQuery ? { lastQuery } : {}),
          ...(searchesDone > 0 ? { searchesDone } : {}),
          ...(lastTool ? { lastTool } : {}),
          ...(toolCalls !== undefined ? { toolCalls } : {}),
          ...(section ? { section } : {}),
          ...(sectionsDone > 0 ? { sectionsDone } : {}),
          startedAt: childStartedAt,
          ...(taskIds ? { taskIds } : {}),
          ...(taskStartedAt ? { taskStartedAt } : {}),
          ...(taskSettledAt ? { taskSettledAt } : {}),
          ...(taskSearches ? { taskSearches } : {}),
          ...(taskKinds ? { taskKinds } : {}),
          ...(lastQueryTask !== undefined ? { lastQueryTask } : {}),
          ...(taskResults ? { taskResults } : {}),
          ...(planLine ? { planLine } : {}),
          ...(decision ? { decision } : {}),
          ...(researchRounds > 0 ? { researchRounds } : {}),
          ...(outcome ? { outcome } : {}),
          ...(taskToolKind ? { taskToolKind } : {}),
          ...(checkedAnswers > 0 ? { checkedAnswers } : {}),
          ...(error ? { error } : {}),
          ...(errorCode ? { errorCode } : {}),
          updatedAt,
        },
      }
      // A child parking on a human decision blocks the whole delegation,
      // and its approval/clarification rows live under the CHILD run id —
      // flag them for the parent's control loop so the composer tray can
      // offer the gate. Only the transition flags: while
      // the child stays parked, further progress events must not refetch.
      if (
        runStatus !== undefined
        && isGateAgentRun(runStatus as ResearchRunStatus)
        && previousChild?.runStatus !== runStatus
      ) {
        const gates = record.childGates[childRunId]
        next.childGates = {
          ...record.childGates,
          [childRunId]: {
            approvals: gates?.approvals ?? [],
            clarifications: gates?.clarifications ?? [],
            ...(gates?.question ? { question: gates.question } : {}),
            stale: true,
          },
        }
      }
      if (taskId) {
        const previousTask = record.taskStates[taskId]
        const projectedStatus = monotonicTaskStatus(
          previousTask?.status,
          childTaskStatus(runStatus, previousTask?.status),
          previousTask?.attempt ?? 1,
          attempt ?? previousTask?.attempt ?? 1,
        )
        const childTerminal =
          projectedStatus === 'completed'
          || projectedStatus === 'failed'
          || projectedStatus === 'insufficient_evidence'
        next.taskStates = {
          ...next.taskStates,
          [taskId]: {
            ...(previousTask ?? { status: 'running', attempt: 1 }),
            status: projectedStatus,
            childRunId,
            attempt:
              attempt
              ?? previousTask?.attempt
              ?? 1,
            ...(error ? { error } : {}),
            ...(errorCode ? { errorCode } : {}),
            startedAt: previousTask?.startedAt ?? event.created_at,
            ...(childTerminal
              ? { finishedAt: previousTask?.finishedAt ?? updatedAt }
              : { finishedAt: undefined }),
          },
        }
      }
      return next
    }

    case 'inqtrix.answer.draft': {
      // Run state, not an artifact row: the answer artifact begins with the
      // published answer, and the list reconcile replaces artifact rows.
      // A repeated draft (a redelivered segment) is the newer one.
      const draftId = stringField(data, 'draft_id')
      const artifactId = stringField(data, 'artifact_id')
      const markdown = stringField(data, 'markdown')
      if (!draftId || !artifactId || !markdown) return next
      next.answerDraft = {
        draftId,
        artifactId,
        markdown,
        referenceLabels: stringListField(data, 'reference_labels') ?? [],
        createdAt: event.created_at,
      }
      return next
    }

    case 'inqtrix.answer.started': {
      const artifactId = stringField(data, 'artifact_id')
      const publicationId = stringField(data, 'publication_id')
      if (!artifactId || !publicationId) return next
      const existing = record.artifacts[artifactId]
      next.artifacts = {
        ...record.artifacts,
        [artifactId]: {
          artifactId,
          kind: 'answer',
          title: existing?.title ?? 'Antwort',
          status: 'writing',
          revision: existing?.revision ?? 1,
          updatedBy: 'agent',
          refsCount: existing?.refsCount ?? 0,
          createdAt: existing?.createdAt ?? event.created_at,
          updatedAt: event.created_at,
          contentMarkdown: '',
          refs: existing?.refs,
          revisions: existing?.revisions,
          publicationId,
          publicationOffset: 0,
          publicationNeedsReconcile: true,
          // The labels the finished answer cites, known to the server
          // before the first delta. With them the streamed text can
          // render its citations immediately; without them the whole
          // body was rewritten the moment the answer settled, which
          // reads as the message being re-inserted.
          publicationRefLabels: stringListField(data, 'reference_labels'),
        },
      }
      next.artifactOrder = record.artifactOrder.includes(artifactId)
        ? record.artifactOrder
        : [...record.artifactOrder, artifactId]
      next.artifactsStale = false
      return next
    }

    case 'inqtrix.output_text.delta': {
      const artifactId = stringField(data, 'artifact_id')
      const publicationId = stringField(data, 'publication_id')
      const delta = stringField(data, 'delta')
      const offset = numberField(data, 'offset')
      const existing = artifactId ? record.artifacts[artifactId] : undefined
      if (
        !artifactId
        || !publicationId
        || !existing
        || existing.status !== 'writing'
        || existing.publicationId !== publicationId
        || offset === undefined
      ) {
        return next
      }
      if (offset !== (existing.publicationOffset ?? 0)) {
        // A gap or out-of-order frame is repaired from the authoritative
        // artifact; never concatenate guessed Markdown, and never let the
        // truncated body pass as the finished answer at settle.
        next.artifacts = {
          ...record.artifacts,
          [artifactId]: { ...existing, publicationGap: true },
        }
        next.artifactsStale = true
        return next
      }
      next.artifacts = {
        ...record.artifacts,
        [artifactId]: {
          ...existing,
          contentMarkdown: `${existing.contentMarkdown ?? ''}${delta}`,
          publicationOffset: offset + new TextEncoder().encode(delta).byteLength,
          updatedAt: event.created_at,
        },
      }
      return next
    }

    case 'inqtrix.answer.ready':
    case 'inqtrix.answer.interrupted': {
      const artifactId = stringField(data, 'artifact_id')
      const publicationId = stringField(data, 'publication_id')
      const existing = artifactId ? record.artifacts[artifactId] : undefined
      if (!artifactId || !existing || existing.publicationId !== publicationId) {
        return next
      }
      next.artifacts = {
        ...record.artifacts,
        [artifactId]: {
          ...existing,
          status: event.type === 'inqtrix.answer.ready' ? 'ready' : 'interrupted',
          publicationId: undefined,
          publicationOffset: undefined,
          publicationNeedsReconcile: true,
          updatedAt: event.created_at,
        },
      }
      // Ready refetches the canonical body + references. Interrupted also
      // refetches so a checkpointed partial artifact can be recovered.
      next.artifactsStale = true
      return next
    }

    case 'inqtrix.agent.artifact.created':
    case 'inqtrix.agent.artifact.updated':
    case 'inqtrix.agent.artifact.edit_conflict':
      // Kernel answer persistence happens immediately before the common
      // answer publication. Fetching its already-complete row here would
      // flash the full body and then clear it again on answer.started.
      // Terminal events still force a reconciliation if publication fails.
      if (
        stringField(data, 'kind') === 'answer'
        && !['cancelled', 'completed', 'failed'].includes(record.status)
      ) {
        return next
      }
      // Signal only: the cached row must NOT adopt the event's
      // revision — a bumped revision over the OLD body would defeat the
      // list-refetch staleness check and let a user edit of stale text
      // pass the server's expected_revision gate, silently overwriting
      // the newer body.
      // edit_conflict rides the same refetch: the agent preserved the
      // user's text and appended its update, so the canvas reloads the
      // reconciled memo instead of the user's stale local copy.
      // Document writes also land as the turn's file chip. The
      // conflict marker is excluded — its resolving write emits its own
      // updated event; a user PUT carries no kind and never chips.
      if (event.type !== 'inqtrix.agent.artifact.edit_conflict') {
        const touchedKind = stringField(data, 'kind')
        const touchedId = stringField(data, 'artifact_id')
        if (touchedId && (touchedKind === 'memo' || touchedKind === 'deliverable')) {
          const touched = [...record.touchedArtifacts]
          const existing = touched.findIndex(
            (item) => item.artifactId === touchedId,
          )
          const previous = existing === -1 ? undefined : touched[existing]
          const touchedTitle = stringField(data, 'title') || previous?.title
          const touchedArrived = previous?.arrivedLive ?? options?.arrivedLive
          const touchedRevision =
            numberField(data, 'revision') ?? previous?.revision ?? 1
          // The turn diff's `from` side is the revision BEFORE the
          // run's FIRST touch; revisions increment by one, so the
          // event's from_revision (or revision-1 when the event does
          // not carry that field) is exact on the first touch and
          // sticky afterwards.
          const touchedFrom =
            previous?.fromRevision
            ?? numberField(data, 'from_revision')
            ?? touchedRevision - 1
          // Accumulate the server-counted delta; one number-less
          // contributor makes the whole sum honestly unknown.
          const eventAdded = numberField(data, 'lines_added')
          const eventRemoved = numberField(data, 'lines_removed')
          const summable =
            eventAdded !== undefined
            && eventRemoved !== undefined
            && (previous === undefined
              || (previous.linesAdded !== undefined
                && previous.linesRemoved !== undefined))
          const entry: AgentTouchedArtifact = {
            artifactId: touchedId,
            kind: touchedKind,
            revision: touchedRevision,
            fromRevision: touchedFrom,
            created:
              previous?.created
              ?? (event.type === 'inqtrix.agent.artifact.created'),
            at: event.created_at,
            ...(summable
              ? {
                linesAdded: (previous?.linesAdded ?? 0) + eventAdded,
                linesRemoved: (previous?.linesRemoved ?? 0) + eventRemoved,
              }
              : {}),
            ...(touchedTitle !== undefined ? { title: touchedTitle } : {}),
            ...(touchedArrived !== undefined
              ? { arrivedLive: touchedArrived }
              : {}),
          }
          if (existing === -1) touched.push(entry)
          else touched[existing] = entry
          next.touchedArtifacts = touched
        }
      }
      next.artifactsStale = true
      return next

    case 'inqtrix.agent.canvas_context.attached': {
      // The durable record of the submission's canvas comments —
      // rebuilt identically on live delivery and replay.
      const contextArtifactId = stringField(data, 'artifact_id')
      if (!contextArtifactId) return next
      const rawComments = Array.isArray(data.comments) ? data.comments : []
      next.canvasContextMeta = {
        artifactId: contextArtifactId,
        revision: numberField(data, 'revision') ?? 1,
        comments: rawComments.flatMap((item) => {
          if (typeof item !== 'object' || item === null) return []
          const record = item as Record<string, unknown>
          return [
            {
              comment:
                typeof record.comment === 'string' ? record.comment : '',
              quotePreview:
                typeof record.quote_preview === 'string'
                  ? record.quote_preview
                  : '',
            },
          ]
        }),
      }
      return next
    }

    case 'inqtrix.agent.requirement.attached': {
      // Emitted once, when the run's first segment starts — rebuilt
      // identically on live delivery and replay. A part that is not
      // whole is not shown: half a rule name is no record. Dropping it
      // is said once per event, so a server sending broken parts shows.
      const rawRules = Array.isArray(data.rules) ? data.rules : []
      const rules = rawRules.flatMap((item) => {
        if (typeof item !== 'object' || item === null) return []
        const part = item as Record<string, unknown>
        const templateId = asString(part.template_id)
        const label = asString(part.label)
        const revision = asFiniteNumber(part.revision)
        // Optional: a part stored before titles were kept carries none.
        const title = asString(part.title)
        return templateId && label && revision !== undefined
          ? [{ label, revision, templateId, ...(title ? { title } : {}) }]
          : []
      })
      if (rules.length < rawRules.length) {
        console.warn(
          `Run ${record.runId}: requirement.attached carries ${rawRules.length - rules.length}`
            + ' rule part(s) that are not whole; the transcript line leaves them out.',
          rawRules,
        )
      }
      next.requirementMeta = {
        freeTextChars: numberField(data, 'free_text_chars') ?? null,
        review: stringField(data, 'review'),
        rules,
      }
      return next
    }

    case 'inqtrix.agent.patch.proposed': {
      const patchId = stringField(data, 'patch_id')
      if (patchId) {
        next.patchId = patchId
        next.patchStale = true
      }
      next.artifactsStale = true
      return next
    }

    case 'inqtrix.agent.activity': {
      const rawProbe = stringField(data, 'probe')
      const operationCode = stringField(data, 'operation') || rawProbe
      const operation =
        normalizeAgentOperation(operationCode)
        ?? normalizeAgentOperation(rawProbe)
      const taskId = stringField(data, 'task_id') || undefined
      const status = stringField(data, 'status') || undefined
      const parsedError = errorInfo(data.error)
      // An evidence limit is a note about the task, never a fallback: it
      // leaves the task in its own status group. A stored ``fallback`` flag
      // on an evidence-limit code is read as that note.
      const storedEvidenceLimit =
        data.fallback === true
        && EVIDENCE_LIMIT_OPERATION_CODES.has(operationCode)
      const fallback =
        (data.fallback === true && !storedEvidenceLimit)
        || status === 'fallback'
      const evidenceLimit = data.evidence_limit === true || storedEvidenceLimit
      const metrics = recordField(data, 'metrics')
      const purpose = stringField(data, 'purpose') || undefined
      const attempt = numberField(data, 'attempt')
      const detail =
        stringField(data, 'detail')
        || stringField(data, 'query')
        || (operation ? '' : rawProbe)
      const previousTaskActivity = taskId
        ? record.taskStates[taskId]?.activity
        : undefined
      const effectiveDetail = detail || (
        status !== undefined
        && (
          (operation !== undefined
            && previousTaskActivity?.operation === operation)
          || previousTaskActivity?.operationCode === operationCode
        )
          ? previousTaskActivity.detail
          : ''
      )
      // The transcript is an append-only protocol. Contract per event
      // shape:
      //   started (new invocation)   -> append one row
      //   started (same invocation)  -> upsert row (retry/progress text)
      //   completed/failed           -> settle THAT row in place
      //   task.finished/failed       -> settle any still-open rows of
      //                                 the task (see terminal branch)
      // Every INVOCATION (one query of a multi-query task) gets its own
      // row, so "search 6 of 6" never hides inside a counter on row 1.
      // Identity: activity id, else the bare query (stable across retry
      // notices whose detail text changes), else the detail.
      const invocationKey =
        stringField(data, 'activity_id')
        || stringField(data, 'operation_id')
        || stringField(data, 'query')
        || effectiveDetail
      const activityKey = operation
        ? `${record.phase}:${operation}:${taskId ?? ''}:${invocationKey}`
        : undefined
      const previousStep = activityKey
        ? record.stepLog.find((item) => item.activityKey === activityKey)
        : undefined
      const startsInvocation = status === undefined || status === 'started'
      const count = previousStep
        ? startsInvocation
          ? (previousStep.activityCount ?? 1) + 1
          : previousStep.activityCount ?? 1
        : 1
      next.activity = {
        activityId:
          stringField(data, 'activity_id')
          || stringField(data, 'operation_id')
          || undefined,
        kind: stringField(data, 'kind') || 'working',
        detail: effectiveDetail,
        label: stringField(data, 'label') || undefined,
        status,
        operation,
        operationCode: operationCode || undefined,
        current: numberField(data, 'current'),
        total: numberField(data, 'total'),
        count,
        taskId,
        purpose,
        metrics,
        attempt,
        error: parsedError.message,
        errorCode: parsedError.code,
        fallback,
        evidenceLimit,
        at: event.created_at,
      }
      if (taskId) {
        const previousTask = record.taskStates[taskId]
        const activityHistory = updateTaskActivityHistory(
          previousTask?.activityHistory,
          next.activity,
        )
        const activityTerminal = status === 'completed' || status === 'failed'
        const lifecycleLocked = previousTask !== undefined && (
          TERMINAL_AGENT_TASK_STATUSES.has(previousTask.status)
          || previousTask.status === 'cancel_requested'
        )
        next.taskStates = {
          ...next.taskStates,
          [taskId]: {
            ...(previousTask ?? { status: 'running', attempt: 1 }),
            activity: next.activity,
            activityHistory,
            ...(parsedError.message
              ? { error: parsedError.message }
              : {}),
            ...(parsedError.code ? { errorCode: parsedError.code } : {}),
            ...(fallback ? { fallback: true } : {}),
            ...(evidenceLimit ? { evidenceLimit: true } : {}),
            startedAt: previousTask?.startedAt ?? event.created_at,
            ...(activityTerminal && !lifecycleLocked
              ? { finishedAt: event.created_at }
              : {}),
            ...(status === 'started' && !lifecycleLocked
              ? { finishedAt: undefined }
              : {}),
          },
        }
      }
      appendActivityStep(next, {
        activityKey,
        activityKind: next.activity.kind,
        findings: reviewFindings(data),
        activityOperation: operation,
        activityOperationCode: next.activity.operationCode,
        detail: next.activity.detail || undefined,
        kind: 'activity',
        label: next.activity.label,
        status: next.activity.status,
        fallback: next.activity.fallback,
        evidenceLimit: next.activity.evidenceLimit,
        error: next.activity.error,
        current: next.activity.current,
        total: next.activity.total,
        metrics: next.activity.metrics,
        purpose: next.activity.purpose,
        attempt: next.activity.attempt,
        activityCount: count,
        taskId,
      })
      return next
    }

    case 'inqtrix.agent.tool.started':
    case 'inqtrix.agent.tool.finished': {
      // Kernel ReAct-loop tool events ride the ONE activity-step
      // protocol: started appends a running row,
      // finished settles THAT row via the shared activityKey upsert.
      // write_todos travels as todo.updated -> run.todos (the checklist
      // block), so its tool row would duplicate the list; ask_user
      // parks the run (clarification rows are the story there).
      const started = event.type === 'inqtrix.agent.tool.started'
      const callId = stringField(data, 'invocation_id')
        || stringField(data, 'tool_call_id')
      const previousStep = callId
        ? record.stepLog.find((item) => item.activityKey === `tool:${callId}`)
        : undefined
      const tool = stringField(data, 'tool')
        || (!started ? previousStep?.activityOperationCode ?? '' : '')
      // finished settles by call id even when the ToolMessage lost its
      // name. An anonymous orphan has no display identity; in particular,
      // a suppressed workplan update must not become a new search row.
      if (!tool) {
        return next
      }
      if (tool === 'write_todos' || tool === 'ask_user') {
        return next
      }
      const operation = normalizeAgentOperation(tool)
      // Phase-free key: the call id IS the identity — a phase flip
      // between started and finished must still settle the same row.
      const activityKey = `tool:${callId || tool}`
      const detail = started
        ? toolArgsPreviewText(stringField(data, 'args_preview'))
        : record.stepLog.find((item) => item.activityKey === activityKey)
          ?.detail
      const status = started
        ? 'running'
        : stringField(data, 'status') === 'error'
          ? 'failed'
          : 'completed'
      next.activity = {
        kind: 'searching',
        detail: detail || '',
        label: operation ? undefined : tool,
        status,
        operation,
        operationCode: tool,
        count: 1,
        at: event.created_at,
      }
      const todoIndex = started ? numberField(data, 'todo_index') : undefined
      const artifactId = started ? stringField(data, 'artifact_id') : ''
      appendActivityStep(next, {
        activityKey,
        activityKind: 'searching',
        activityOperation: operation,
        activityOperationCode: tool,
        detail: detail || undefined,
        kind: 'activity',
        label: operation ? undefined : tool,
        status,
        ...(todoIndex !== undefined ? { todoIndex } : {}),
        ...(artifactId ? { artifactId } : {}),
      })
      return next
    }

    case 'inqtrix.agent.todo.updated': {
      // The kernel's task list replaces itself wholesale per event; the
      // checklist block renders run.todos, so no step row is appended.
      const todos = Array.isArray(data.todos) ? data.todos : []
      next.todos = todos
        .filter((item): item is Record<string, unknown> =>
          typeof item === 'object' && item !== null)
        .map((item) => ({
          content: asString(item.content) ?? '',
          status: asString(item.status) ?? '',
        }))
        .filter((item) => item.content !== '')
      // A todo list is a REPORT, valid as of when the model wrote it —
      // never a live signal. Keeping its timestamp lets the surface say
      // so instead of presenting a stale entry as the present.
      next.todosAt = event.created_at
      return next
    }

    case 'inqtrix.agent.tool_limit.reached': {
      // The one silent hard stop: the backend rejects the batch without
      // any narration — without this notice the run just "ends".
      appendStep(next, {
        current: numberField(data, 'attempted'),
        kind: 'notice',
        noticeCode: 'tool_limit',
        total: numberField(data, 'limit'),
      })
      return next
    }

    case 'inqtrix.agent.citation.validation': {
      // Unknown labels and unreadable brackets are both what was marked.
      const labels = [data.unknown_labels, data.malformed_brackets].flatMap((list) => (
        Array.isArray(list)
          ? list.map((item) => asString(item) ?? '').filter(Boolean)
          : []
      ))
      appendStep(next, {
        detail: labels.join(', ') || undefined,
        kind: 'notice',
        noticeCode: 'citation_validation',
        status: stringField(data, 'status') || undefined,
      })
      return next
    }

    case 'inqtrix.agent.sufficiency.judged': {
      // Only the NEGATIVE verdict is user-relevant (the gaps steer the
      // next searches); a covered verdict is followed by the answer
      // itself, and error markers are internal.
      const coverage = stringField(data, 'coverage')
      const nudged = data.nudge === true
      if (!nudged || !coverage || coverage === 'covered') return next
      // `missing` is the judge's own steering text for the model's next
      // searches — English, with internal ids and character ranges; the
      // row carries the server's fact only (how many sources the model
      // started and has not finished; absent from older servers: none).
      // Every verdict of the run updates ONE row in place.
      appendStep(next, {
        current: numberField(data, 'open_continuations') ?? 0,
        kind: 'notice',
        narrationId: SUFFICIENCY_NOTICE_ID,
        noticeCode: 'sufficiency_gap',
      })
      return next
    }

    case 'inqtrix.agent.outcome': {
      // The run's own account of how its review ended. The last record
      // wins: a redelivered segment repeats it under the same id. It is
      // run state, never a step row — the step log is bounded, and the
      // recap must not lose the record to that bound.
      const outcome = runOutcome(data)
      if (outcome) next.outcome = outcome
      return next
    }

    case 'inqtrix.agent.narration': {
      const text = stringField(data, 'text')
      if (text) {
        appendStep(next, {
          detail: stringField(data, 'kind') || undefined,
          kind: 'narration',
          narrationId: stringField(data, 'narration_id') || undefined,
          // Still being written: the run line names what this step does.
          open: data.final === false ? true : undefined,
          phase: stringField(data, 'phase') || undefined,
          text,
        })
      }
      return next
    }

    default:
      return next
  }

  /** Bounded ordered append with narration UPSERT: a line carrying a
   * ``narrationId`` already in the log replaces that entry in place
   * (keeping its position and React key) rather than appending a
   * duplicate — the backend re-emits stable narration ids on node
   * re-execution (critic replan loop) with fresh sequences, which the
   * sequence guard does not suppress. seq/at ride the event envelope. */
  function appendStep(
    target: AgentRunRecord,
    entry: Omit<AgentStepEntry, 'at' | 'seq'>,
  ): void {
    const arrival = options
      ? { arrivedLive: options.arrivedLive }
      : undefined
    const log =
      target.stepLog === record.stepLog
        ? [...record.stepLog]
        : target.stepLog
    if (entry.narrationId) {
      const existing = log.findIndex(
        (item) => item.narrationId === entry.narrationId,
      )
      if (existing !== -1) {
        // Update text/at in place; keep the original seq so the React
        // key is stable and the line does not jump to the bottom.
        log[existing] = {
          ...log[existing],
          ...entry,
          ...arrival,
          at: event.created_at,
          seq: log[existing].seq,
        }
        target.stepLog = log
        return
      }
    }
    log.push({
      ...entry,
      ...arrival,
      at: event.created_at,
      seq: event.sequence,
    })
    target.stepLog =
      log.length > MAX_STEP_LOG ? log.slice(log.length - MAX_STEP_LOG) : log
  }

  /** Consecutive probes/calls of the same semantic operation update one
   * transcript row. The plan retains every literal query; the timeline stays
   * a readable status story rather than four identical capability ids. */
  function appendActivityStep(
    target: AgentRunRecord,
    entry: Omit<AgentStepEntry, 'at' | 'seq'>,
  ): void {
    const operation = entry.activityOperation
    // An explicit activityKey upserts even without a normalized
    // operation (kernel tool rows of tools outside the vocabulary) —
    // otherwise tool.finished would append a SECOND line instead of
    // settling the running one.
    if (!operation && !entry.activityKey) {
      appendStep(target, entry)
      return
    }
    // The caller passes the per-invocation key; the phase-scoped
    // fallback only serves legacy entries without one.
    const activityKey =
      entry.activityKey
      ?? `${record.phase}:${operation}:${entry.taskId ?? ''}`
    const log =
      target.stepLog === record.stepLog
        ? [...record.stepLog]
        : target.stepLog
    const existing = log.findIndex((item) => item.activityKey === activityKey)
    if (existing === -1) {
      appendStep(target, {
        ...entry,
        activityCount: entry.activityCount ?? 1,
        activityKey,
        // `at` moves to the settle time on finish; the creation time
        // stays here so a row's duration can be measured.
        startedAt: event.created_at,
      })
      return
    }
    log[existing] = {
      ...log[existing],
      ...entry,
      ...(options ? { arrivedLive: options.arrivedLive } : undefined),
      detail: entry.detail ?? log[existing].detail,
      label: entry.label ?? log[existing].label,
      activityOperation:
        entry.activityOperation ?? log[existing].activityOperation,
      activityOperationCode:
        entry.activityOperationCode ?? log[existing].activityOperationCode,
      activityCount:
          entry.activityCount ?? log[existing].activityCount ?? 1,
      activityKey,
      at: event.created_at,
      seq: log[existing].seq,
    }
    target.stepLog = log
  }
}

/** Preserve the server summary when present; otherwise derive the terminal
 * stopwatch from the same event boundaries already used by live replay. */
function applyTerminalRunTime(
  record: AgentRunRecord,
  event: ResearchRunEvent,
): void {
  record.finishedAt ??= isoFromUnixSeconds(event.created_at)
  const wireElapsed = asFiniteNumber(event.data?.elapsed_seconds)
  if (wireElapsed !== undefined) {
    record.elapsedSeconds = Math.max(0, wireElapsed)
    return
  }
  if (record.elapsedSeconds !== undefined) return
  if (!record.startedAt) return
  record.elapsedSeconds = Math.max(
    0,
    event.created_at - unixSecondsFromIso(record.startedAt),
  )
}

/** Settle still-running kernel tool rows at a terminal run event: a run
 * that dies mid-call never emits ``tool.finished``, and the task-terminal
 * sweep only covers rows WITH a ``taskId`` — a kernel tool row has none
 * and would show a running glyph forever. */
function settleOpenKernelToolRows(
  next: AgentRunRecord,
  record: AgentRunRecord,
  status: 'completed' | 'failed',
): void {
  const isOpenToolRow = (item: AgentStepEntry) =>
    item.kind === 'activity'
    && item.activityKey !== undefined
    && item.activityKey.startsWith('tool:')
    && item.status === 'running'
  if (!next.stepLog.some(isOpenToolRow)) return
  const log =
    next.stepLog === record.stepLog ? [...next.stepLog] : next.stepLog
  for (let index = 0; index < log.length; index += 1) {
    const item = log[index]
    if (item && isOpenToolRow(item)) {
      log[index] = { ...item, status }
    }
  }
  next.stepLog = log
}

/**
 * Close the checklist entry the model was on when the run SUCCEEDED.
 *
 * The list is written by a tool call, so every state change costs the
 * model one more turn — and the last entry is always the one it finishes
 * by answering, after which there is no turn left to tick it. The run
 * then ends with a checklist that says "2/3" and draws a live dot beside
 * a step that is over: the same false-readout the open tool rows above
 * have, settled the same way and in the same place.
 *
 * Only `completed` settles, and only `in_progress`. A cancelled or
 * failed run really did stop mid-step, and an entry the model never
 * started is not finished just because the run is — both keep what they
 * say.
 */
function settleOpenTodoOnCompletion(next: AgentRunRecord): void {
  const todos = next.todos
  if (!todos?.some((todo) => todo.status === 'in_progress')) return
  // `map` builds a new array, so the record this one was copied from
  // keeps the list the model actually reported.
  next.todos = todos.map((todo) => (
    todo.status === 'in_progress' ? { ...todo, status: 'completed' } : todo
  ))
}

/** ``(data, key)`` accessor with the '' fallback this module's call sites
 * expect; the coercion itself is the shared {@link asString}. */
function stringField(data: Record<string, unknown>, key: string): string {
  return asString(data[key]) ?? ''
}

/**
 * The flagged passages of a review activity, or nothing.
 *
 * Every element is validated field by field: the payload comes off the
 * wire, and a row that is half a finding would render as a quotation
 * mark around nothing. The cap bounds one pathological verdict; the
 * review's own label keeps carrying the full count.
 */
function reviewFindings(
  data: Record<string, unknown>,
): readonly AgentReviewFinding[] | undefined {
  const raw = data.findings
  if (!Array.isArray(raw)) return undefined
  const findings: AgentReviewFinding[] = []
  for (const item of raw.slice(0, MAX_REVIEW_FINDINGS)) {
    if (typeof item !== 'object' || item === null) continue
    const entry = item as Record<string, unknown>
    const quote = asString(entry.quote) ?? ''
    const finding = asString(entry.finding) ?? ''
    const target = asString(entry.target) ?? ''
    if (!quote || !finding || !target) continue
    findings.push({ quote, finding, target })
  }
  return findings.length ? findings : undefined
}

/**
 * An outcome record off the wire, or nothing.
 *
 * The summary is the words a surface shows; a record without one carries
 * nothing to show and is not read at all, so the run reads as having no
 * record. A line that is not a whole line (kind, text, reasons), or a mark
 * that is not a whole mark (output, passage, state, note), is dropped
 * rather than rendered as half a sentence.
 */
function runOutcome(data: Record<string, unknown>): AgentRunOutcome | undefined {
  const summary = asString(data.summary)
  if (summary === undefined) return undefined
  const lines: AgentRunOutcomeLine[] = []
  for (const item of Array.isArray(data.lines) ? data.lines : []) {
    if (typeof item !== 'object' || item === null) continue
    const entry = item as Record<string, unknown>
    const kind = asString(entry.kind)
    const text = asString(entry.text)
    if (!kind || !text || !Array.isArray(entry.reasons)) continue
    lines.push({ kind, text, reasons: asStringArray(entry.reasons) })
  }
  const marks: AgentClaimMark[] = []
  for (const item of Array.isArray(data.marks) ? data.marks : []) {
    if (typeof item !== 'object' || item === null) continue
    const entry = item as Record<string, unknown>
    const outputId = asString(entry.output_id)
    const quote = asString(entry.quote)
    const state = asString(entry.state)
    const note = asString(entry.note)
    if (!outputId || !quote || !state || !note) continue
    marks.push({ outputId, quote, state, note })
  }
  const details = asString(data.details)
  return {
    outcomeId: stringField(data, 'outcome_id'),
    final: stringField(data, 'final'),
    review: stringField(data, 'review'),
    summary,
    ...(details ? { details } : {}),
    applied: numberField(data, 'applied') ?? 0,
    lines,
    marks,
  }
}

/** The projection of a delegated mission's outcome record, or nothing —
 * read by the same rule as a run's own record. */
function childOutcome(data: Record<string, unknown>): AgentChildOutcome | undefined {
  const summary = asString(data.summary)
  if (summary === undefined) return undefined
  return {
    final: stringField(data, 'final'),
    review: stringField(data, 'review'),
    summary,
    applied: numberField(data, 'applied') ?? 0,
  }
}

/** ``(data, key)`` accessor over the shared {@link asFiniteNumber}. */
function numberField(
  data: Record<string, unknown>,
  key: string,
): number | undefined {
  return asFiniteNumber(data[key])
}

/**
 * The ordinals a delegated mission currently has open.
 *
 * A mission starts its whole parallel wave in one burst, so "the current
 * task" is not a single number. Keeping the SET means the surface can
 * say how many run at once instead of naming one of five as if it were
 * the only one. A `finished` event closes exactly its own ordinal, so a
 * straggler stays visible after its siblings settle — which is the case
 * a reader most needs to see.
 */
export function nextOpenChildTasks(
  current: readonly number[] | undefined,
  eventType: string,
  ordinal: number | undefined,
): number[] | undefined {
  if (ordinal === undefined) return current ? [...current] : undefined
  const open = new Set(current ?? [])
  if (eventType === 'inqtrix.agent.task.started') open.add(ordinal)
  else if (
    eventType === 'inqtrix.agent.task.finished'
    || eventType === 'inqtrix.agent.task.failed'
  ) open.delete(ordinal)
  else return current ? [...current] : undefined
  return [...open].sort((left, right) => left - right)
}

/** Ordinals of a delegated mission's FAILED tasks: a failure settles a
 * task (it leaves `openTasks`) and is counted here, so the line can say
 * "2 Aufgaben fehlgeschlagen" instead of pretending they still run. */
export function nextFailedChildTasks(
  current: readonly number[] | undefined,
  eventType: string,
  ordinal: number | undefined,
): number[] | undefined {
  if (eventType !== 'inqtrix.agent.task.failed' || ordinal === undefined) {
    return current ? [...current] : undefined
  }
  const failed = new Set(current ?? [])
  failed.add(ordinal)
  return [...failed].sort((left, right) => left - right)
}

/** A list of non-empty strings from an event payload, else undefined. */
function stringListField(
  data: Record<string, unknown>,
  key: string,
): string[] | undefined {
  const value = data[key]
  if (!Array.isArray(value)) return undefined
  const items = value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
  return items.length > 0 ? items : undefined
}

function recordField(
  data: Record<string, unknown>,
  key: string,
): Record<string, unknown> | undefined {
  const value = data[key]
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

/** args_preview arrives as words from current servers (the query, a title,
 * or nothing for an id-only call); older segments may still carry the raw
 * JSON dump of the tool args. Its query stays readable; anything else in it
 * is identifiers, never words for the reader, so it shows nothing. */
function toolArgsPreviewText(preview: string): string {
  const trimmed = preview.trim()
  if (!trimmed.startsWith('{')) return trimmed
  try {
    const parsed = JSON.parse(trimmed) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const query = (parsed as Record<string, unknown>).query
      if (typeof query === 'string' && query.trim()) return query.trim()
    }
  } catch {
    // A truncated dump (older servers capped previews) has no words either.
  }
  return ''
}

function errorInfo(value: unknown): { code?: string; message?: string } {
  if (typeof value === 'string' && value.trim()) {
    return { message: value.trim() }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const error = value as Record<string, unknown>
  const message = asString(error.message)
  const code = asString(error.code) ?? asString(error.type)
  return {
    ...(message ? { message } : {}),
    ...(code ? { code } : {}),
  }
}

const MAX_TASK_ACTIVITY_HISTORY = 100

function updateTaskActivityHistory(
  current: AgentTaskLiveState['activityHistory'],
  activity: NonNullable<AgentTaskLiveState['activity']>,
): NonNullable<AgentTaskLiveState['activityHistory']> {
  const history = [...(current ?? [])]
  const updatesInvocation =
    activity.status !== undefined && activity.status !== 'started'
  let index = -1
  if (updatesInvocation) {
    if (activity.activityId) {
      index = history.findIndex(
        (item) => item.activityId === activity.activityId,
      )
    }
    if (index === -1) {
      for (let cursor = history.length - 1; cursor >= 0; cursor -= 1) {
        const item = history[cursor]
        if (
          item.operation === activity.operation
          && item.kind === activity.kind
          && item.status !== 'completed'
          && item.status !== 'failed'
          && item.status !== 'fallback'
        ) {
          index = cursor
          break
        }
      }
    }
  }
  if (index === -1) history.push(activity)
  else {
    history[index] = {
      ...history[index],
      ...activity,
      detail: activity.detail || history[index].detail,
      label: activity.label ?? history[index].label,
      operation: activity.operation ?? history[index].operation,
      operationCode: activity.operationCode ?? history[index].operationCode,
      purpose: activity.purpose ?? history[index].purpose,
      metrics: activity.metrics ?? history[index].metrics,
      attempt: activity.attempt ?? history[index].attempt,
    }
  }
  return history.length > MAX_TASK_ACTIVITY_HISTORY
    ? history.slice(history.length - MAX_TASK_ACTIVITY_HISTORY)
    : history
}

function childTaskStatus(
  runStatus: string | undefined,
  current: AgentTaskLiveState['status'] | undefined,
): AgentTaskLiveState['status'] {
  if (runStatus === 'cancelled' && current === 'cancel_requested') {
    return 'cancel_requested'
  }
  if (runStatus === 'failed' || runStatus === 'cancelled' || runStatus === 'expired') {
    return 'failed'
  }
  if (runStatus === 'completed') return 'completed'
  if (runStatus === 'insufficient_evidence') return 'insufficient_evidence'
  if (
    runStatus === 'queued'
    || runStatus === 'running'
    || runStatus === 'waiting_for_approval'
    || runStatus === 'waiting_for_children'
    || runStatus === 'waiting_for_input'
  ) return 'running'
  return current ?? 'running'
}
