import { formatClockTime, formatDuration } from '@/lib/time'
import type { Locale, TranslationDictionary } from '../../i18n/translations'
import { countLabel } from '@/i18n/plural'
import { kernelToolLabel } from './activityPresentation'
import type { AgentChildProgressRecord } from './model'

/**
 * Every key a `child.progress` projection may carry — the mirror of the
 * server's copy blocks (`runs/shared.py::build_child_progress_payload`).
 * The demo simulator's child events are pinned against this list, so a
 * demo line can never show a field the real projection would not carry.
 */
export const CHILD_PROGRESS_PROJECTED_KEYS = new Set([
  // identity and lifecycle
  'task_id', 'child_run_id', 'run_status', 'event_type', 'attempt', 'updated_at',
  'snapshot', 'current_node', 'message', 'metrics', 'error',
  // mission task events
  'ordinal', 'tool_kind', 'status', 'title',
  // a kernel role child's settled step (name and server count)
  'tool', 'tool_calls',
  // a task's searches
  'operation', 'query', 'current', 'total', 'child_task_id',
  // derived narration lines (synthesis, task result head, plan, discovery)
  'narration_id', 'kind', 'text',
  // a mission's decisions (kind shared with narration; label and detail)
  'label', 'detail',
  // a mission's outcome record (numbers and the bounded summary sentence;
  // its lines and codes stay in the child)
  'final', 'review', 'round', 'revision', 'accounting', 'applied', 'open', 'summary',
])

/** The child fields a parent line is allowed to speak from. */
export type ChildProgressView = Pick<
  AgentChildProgressRecord,
  | 'checkedAnswers'
  | 'error'
  | 'snapshot'
  | 'runStatus'
  | 'openTasks'
  | 'message'
  | 'taskTitles'
  | 'failedTasks'
  | 'lastQuery'
  | 'searchesDone'
  | 'lastTool'
  | 'toolCalls'
  | 'section'
  | 'sectionsDone'
  | 'startedAt'
  | 'updatedAt'
>


/** A query is shown as a phrase, not as the whole 240-character line;
 * the cut is visible. */
const QUERY_PREVIEW_CHARS = 80

export function queryPreview(query: string): string {
  return query.length <= QUERY_PREVIEW_CHARS
    ? query
    : `${query.slice(0, QUERY_PREVIEW_CHARS - 1).trimEnd()}…`
}

const BROKEN_OFF: Record<string, 'childFailed' | 'childCancelled'> = {
  cancelled: 'childCancelled',
  failed: 'childFailed',
}

/**
 * What a delegating parent can honestly say about its child RIGHT NOW.
 *
 * A kernel run that hands a deep mission to a child would otherwise show
 * one unchanging line for the child's entire lifetime. The child reports
 * its phase and its tasks all along; this line carries them across the
 * run boundary and renders them.
 *
 * The line speaks only from what actually arrived. No phase and no task
 * means no line: a parent that invents progress is worse than a parent
 * that shows none.
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
  if (status === 'completed') {
    // The delegation row's check says "done"; this line says what the
    // mission did — counts from the projections, nothing invented. A
    // research child (no task projections) keeps no summary line.
    const started = Object.keys(child.taskTitles ?? {}).length
    if (started === 0) return null
    const failed = child.failedTasks?.length ?? 0
    const open = child.openTasks?.length ?? 0
    const done = Math.max(0, started - failed - open)
    const elapsed = child.startedAt !== undefined && child.updatedAt !== undefined
      ? formatDuration(child.updatedAt - child.startedAt)
      : ''
    return t.agent.timeline.childSummary
      .replace('{tasks}', countLabel(started, t.agent.timeline.taskCountOne, t.agent.timeline.taskCount))
      .replace('{done}', String(done))
      .replace('{failed}', String(failed))
      .replace('{sections}', countLabel(
        child.sectionsDone ?? 0,
        t.agent.timeline.sectionCountOne,
        t.agent.timeline.sectionCount,
      ))
      .replace('{elapsed}', elapsed)
      .replace(/ · $/, '')
  }
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
    // A kernel role child (a review, a section writer) has no mission
    // phase or tasks: its settled steps say what it does — the last
    // one's tool and the server's count, sampled every few calls. Its
    // start is a server time shown as a clock time: a duration up to the
    // last sampled step stood still between samples and read like a
    // stalled review.
    const known = kernelToolLabel(child.lastTool ?? '', t)
    parts.push(
      !child.lastTool || known === child.lastTool ? t.agent.timeline.childToolStepUnknown : known,
      countLabel(child.toolCalls, t.agent.timeline.childToolStepsOne, t.agent.timeline.childToolSteps),
    )
    if (child.startedAt !== undefined) {
      parts.push(t.agent.timeline.childRunningSince.replace('{time}', formatClockTime(child.startedAt, locale)))
    }
  } else {
    // `agent.activity`, not `agent.stations`: the line says what the
    // child is DOING ("Führt Aufgaben aus"), and it is the only map that
    // covers every mission phase — `stations` has no `evidence` entry.
    // The phase word only when no task is open: "Führt Aufgaben aus ·
    // Aufgabe 5 „…"" would say the same thing twice, and the task names the
    // work more precisely than the phase does.
    const phases = t.agent.activity as Record<string, string | undefined>
    const phase = child.snapshot?.phase
    const phaseLabel = phase ? phases[phase] : undefined
    if (phaseLabel && (child.openTasks ?? []).length === 0) parts.push(phaseLabel)
    if (!phase) parts.push(...researchChildFacts(child, t))
  }

  const open = child.openTasks ?? []
  if (open.length > 1) {
    // A mission runs its wave in parallel. Naming ONE of five as "the"
    // current task read as if the other four did not exist — and the
    // phase hung on the slowest of them for thirteen minutes.
    parts.push(
      t.agent.activity.parallelTasks.replace('{count}', String(open.length)),
    )
  } else if (open.length === 1) {
    // Ordinals are zero-based on the wire and one-based for a reader.
    // The plan task's title names the unit of work when it crossed.
    const title = child.taskTitles?.[open[0]]?.trim()
    parts.push(
      title
        ? t.agent.timeline.childTaskTitled
          .replace('{number}', String(open[0] + 1))
          .replace('{title}', title)
        : t.agent.timeline.childTask.replace('{number}', String(open[0] + 1)),
    )
  }

  // What the child is doing INSIDE the task: during execution the
  // latest search and the count of completed ones; during synthesis
  // the derived line (outline, section written). Searches are not
  // shown once the phase moved on — a stale query would read as if
  // the writer were still searching.
  const phaseNow = child.snapshot?.phase
  if (!phaseNow || phaseNow === 'execution' || phaseNow === 'evidence') {
    const query = child.lastQuery?.trim()
    if (query) {
      parts.push(
        t.agent.timeline.childSearch.replace('{query}', queryPreview(query)),
      )
    }
    const done = child.searchesDone ?? 0
    if (done > 0) {
      parts.push(countLabel(
        done,
        t.agent.timeline.childSearchDoneOne,
        t.agent.timeline.childSearchesDone,
      ))
    }
  } else if (phaseNow === 'synthesis' || phaseNow === 'critic') {
    const section = child.section?.trim()
    if (section) parts.push(section)
  }

  // Settled failures stay visible: a mission that lost two tasks and
  // went on planning must not read as two tasks still running.
  const failed = child.failedTasks?.length ?? 0
  if (failed > 0) {
    parts.push(countLabel(
      failed,
      t.agent.timeline.childTaskFailedOne,
      t.agent.timeline.childTasksFailed,
    ))
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
      t.agent.timeline.childRunningSince.replace('{time}', formatClockTime(child.startedAt, locale)),
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
