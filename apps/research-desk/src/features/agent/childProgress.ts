import { formatClockTime } from '@/lib/time'
import type { Locale, TranslationDictionary } from '../../i18n/translations'
import { countLabel } from '@/i18n/plural'
import { kernelToolLabel } from './activityPresentation'
import type { AgentChildProgressRecord } from './model'
import { shownOutcome } from './runPresentation'

/**
 * Every key a `child.progress` projection carries — the mirror of the
 * server's copy blocks (`runs/shared.py::build_child_progress_payload`).
 * The demo simulator's child events are pinned against this list, so a
 * demo line can never show a field the real projection would not carry.
 * Rows stored by older runs may carry more keys; the reducer reads them
 * without this list.
 */
export const CHILD_PROGRESS_PROJECTED_KEYS = new Set([
  // identity and lifecycle
  'task_id', 'child_run_id', 'run_status', 'event_type', 'attempt', 'updated_at',
  'snapshot', 'current_node', 'message', 'metrics', 'error',
  // a kernel role child's settled step (name and server count)
  'tool', 'tool_calls',
  // a knowledge child's grounding verdict
  'status', 'failure_code', 'marker', 'format_repaired',
  // a knowledge child's retrieval notices (bounded counters, no text)
  'reason', 'retrieval_mode', 'stage', 'requested_candidate_pool', 'returned_candidate_pool',
  'final_top_k', 'final_evidence_complete', 'requested_top_k', 'returned_hits', 'candidate_cap',
  'code', 'count', 'recommended_action',
])

/** The child fields a parent line is allowed to speak from. */
export type ChildProgressView = Pick<
  AgentChildProgressRecord,
  | 'checkedAnswers'
  | 'error'
  | 'snapshot'
  | 'runStatus'
  | 'message'
  | 'lastTool'
  | 'toolCalls'
  | 'startedAt'
  | 'updatedAt'
>

const BROKEN_OFF: Record<string, 'childFailed' | 'childCancelled'> = {
  cancelled: 'childCancelled',
  failed: 'childFailed',
}

/**
 * What a delegating parent can honestly say about its child RIGHT NOW.
 *
 * A delegation is one tool row that can own the run for many minutes; the
 * child reports its steps all along, and this line carries them across the
 * run boundary.
 *
 * The line speaks only from what actually arrived: a parent that invents
 * progress is worse than a parent that shows none.
 *
 * A child that COMPLETED gets no line — the delegation row's own check
 * already says so. A child that failed or was cancelled keeps one: the
 * tool call still returns normally in that case, so the row shows a
 * check either way, and without this line a failed subtask reads as a
 * successful one.
 */
export function childProgressLine(
  child: ChildProgressView,
  t: TranslationDictionary,
  locale: Locale,
): string | null {
  const status = child.runStatus ?? ''
  if (status === 'completed') return null
  const brokenOff = BROKEN_OFF[status]
  if (brokenOff) {
    const reason = child.error?.trim()
    const label = t.agent.timeline[brokenOff]
    return `${t.agent.timeline.childRun} · ${reason ? `${label}: ${reason}` : label}`
  }

  const parts: string[] = []
  if (status === 'waiting_for_approval' || status === 'waiting_for_input') {
    // The tray owns the decision; the line only says why nothing moves.
    parts.push(t.agent.timeline.childWaiting)
  } else if (child.toolCalls !== undefined) {
    // A kernel role child (a review, a section writer) reports no tasks:
    // its settled steps say what it does — the last one's tool and the
    // server's count, sampled every few calls. Its start is a server time
    // shown as a clock time: a duration up to the last sampled step stood
    // still between samples and read like a stalled review.
    const known = kernelToolLabel(child.lastTool ?? '', t)
    parts.push(
      !child.lastTool || known === child.lastTool ? t.agent.timeline.childToolStepUnknown : known,
      countLabel(child.toolCalls, t.agent.timeline.childToolStepsOne, t.agent.timeline.childToolSteps),
    )
    if (child.startedAt !== undefined) {
      parts.push(t.agent.timeline.childRunningSince.replace('{time}', () => formatClockTime(child.startedAt!, locale)))
    }
  } else {
    // A role child reports its phase before its first settled step:
    // `agent.activity` names what it does (`execution` -> "Führt
    // Aufgaben aus"). A research child reports no phase; its typed
    // snapshot speaks instead.
    const phases = t.agent.activity as Record<string, string | undefined>
    const phase = child.snapshot?.phase
    const phaseLabel = phase ? phases[phase] : undefined
    if (phaseLabel) parts.push(phaseLabel)
    if (!phase) parts.push(...researchChildFacts(child, t))
  }

  // The number that MOVES. Everything above can stand still for ten
  // minutes while the child works; this cannot, and it is the only part
  // of the line that answers "is it still alive".
  const checked = child.checkedAnswers ?? 0
  if (checked > 0) {
    parts.push(countLabel(
      checked,
      t.agent.timeline.childEvidenceOne,
      t.agent.timeline.childEvidence,
    ))
  }

  if (parts.length === 0) {
    // No typed fact: the line says the child works and since when — never
    // its raw progress message, which carried "done" and pipeline status
    // text ("Claims-Lage: …") verbatim into the parent. Without a start
    // either, there is nothing honest to say.
    if (child.startedAt === undefined) return null
    parts.push(
      t.agent.timeline.childWorking,
      t.agent.timeline.childRunningSince.replace('{time}', () => formatClockTime(child.startedAt!, locale)),
    )
  }
  return `${t.agent.timeline.childRun} · ${parts.join(' · ')}`
}

/** A research child's facts from its typed snapshot: the node it is on, the
 * section it writes (k of n in write order) or its search queries so far. */
function researchChildFacts(
  child: ChildProgressView,
  t: TranslationDictionary,
): string[] {
  const snapshot = child.snapshot
  if (!snapshot) return []
  const nodes = t.agent.timeline.childResearchNode as Record<string, string | undefined>
  const node = snapshot.current_node ? nodes[snapshot.current_node] : undefined
  if (!node) return []
  const facts = [node]
  const section = snapshot.writing_section
  const total = snapshot.sections_total
  if (snapshot.current_node === 'answer' && section && total) {
    facts.push(
      t.agent.timeline.childSectionOf
        .replace('{index}', String(section))
        .replace('{total}', String(total)),
    )
  } else if ((snapshot.total_queries ?? 0) > 0) {
    facts.push(countLabel(
      snapshot.total_queries ?? 0,
      t.agent.timeline.childSearchQueriesOne,
      t.agent.timeline.childSearchQueries,
    ))
  }
  return facts
}

/**
 * The one line a settled delegation of a stored kernel run keeps about the
 * retired mission it handed its task to: how the child ended and its
 * review's summary sentence. A completed mission without a record claims
 * no review ("Ohne Ausgang", as the recap says); a mission that broke off
 * says so with its reason. `null` while the child has not ended.
 */
export function childDelegationLine(
  child: Pick<AgentChildProgressRecord, 'error' | 'outcome' | 'runStatus'>,
  t: TranslationDictionary,
): string | null {
  const status = child.runStatus ?? ''
  const brokenOff = BROKEN_OFF[status]
  if (status !== 'completed' && !brokenOff) return null
  const reason = child.error?.trim()
  const ended = brokenOff
    ? reason ? `${t.agent.timeline[brokenOff]}: ${reason}` : t.agent.timeline[brokenOff]
    : t.agent.timeline.childCompleted
  const review = child.outcome
    ? shownOutcome(child.outcome)?.summary ?? ''
    : status === 'completed' ? t.agent.timeline.recapNoOutcome : ''
  return [t.agent.timeline.childRun, ended, review].filter(Boolean).join(' · ')
}
