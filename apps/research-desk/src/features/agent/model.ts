/**
 * Agent Desk record model — separate from ResearchRunRecord.
 *
 * Rows are the truth: the control surfaces (plan, approvals,
 * clarifications, artifacts) are FETCHED rows converted here; SSE events only
 * flip `*Stale` flags and carry live task/phase state. All converters are
 * pure so they stay unit-testable without React.
 */

import type {
  AgentApprovalWire,
  AgentArtifactDetailWire,
  AgentArtifactMetaWire,
  AgentClarificationWire,
  AgentPatchWire,
  AgentPlanTaskWire,
  AgentPlanWire,
} from './types'
import type {
  ResearchRunAccess,
  ResearchRunSnapshot,
  ResearchRunStatus,
  ResearchRunSummary,
} from '@/features/researchRuns/types'
import type {
  AgentKnowledgeSwitch,
  AgentSessionModelSelection,
  AgentSourcePolicy,
} from './executionPolicy'

// --- Pulse-track stations (the signature progress line) ---------------------

/**
 * The metro-line stations of the pulse track. Deliberately fewer than the
 * backend phases: `clarification` is a GATE overlay (not a station) and
 * `evidence` folds into synthesis — six stations keep the track compact.
 */
export const AGENT_PHASE_STATIONS = [
  'intake',
  'discovery',
  'planning',
  'execution',
  'synthesis',
  'critic',
] as const

export type AgentPhaseStation = (typeof AGENT_PHASE_STATIONS)[number]

/**
 * The stations of the AGENT KERNEL — measured, not assumed.
 *
 * Across every kernel run in the store the engine reports exactly two
 * phases: `execution` and `done`. It has no discovery, no planning, no
 * synthesis and no critic. Painting the mission's six stations for it
 * would describe a flow that never happens; `result` is the station the
 * finished run is standing on.
 */
export const AGENT_KERNEL_STATIONS = [
  'intake',
  'execution',
  'result',
] as const

/** Which line to draw. Absent metadata keeps the mission line, which is
 * the older and broader vocabulary — an unknown engine must not lose
 * stations it may well have visited. */
export function agentStationsFor(
  effectiveMode: string | undefined,
): readonly string[] {
  return effectiveMode === 'agent_kernel'
    ? AGENT_KERNEL_STATIONS
    : AGENT_PHASE_STATIONS
}

/**
 * How far the pulse track is filled: every station BEFORE this index is
 * done, the station AT it is the active one while the run is alive.
 *
 * A finished run fills up to the station it actually reached and no
 * further. Filling the whole line on completion makes the track lie about
 * the kernel, which only ever reports `intake` and `execution`: it would
 * claim a discovery, a plan and a verification pass that never ran. The
 * line is read as a record of what happened, so an engine that visits
 * four of six stations must show four.
 */
export function agentPulseActiveIndex(
  station: AgentPhaseStation,
  completed: boolean,
  stations: readonly string[] = AGENT_PHASE_STATIONS,
): number {
  const reached = Math.max(0, stations.indexOf(station))
  if (!completed) return reached
  // The two lines end differently. The kernel's ends in the RESULT a
  // finished run holds, so completing it means reaching the last
  // station. The mission's ends in the critic, which a completed run may
  // well have skipped — there, completion only closes the station the
  // run actually reached, and the rest stay pale.
  return stations === AGENT_KERNEL_STATIONS ? stations.length : reached + 1
}

/**
 * Backend phase -> station index; `null` keeps the previous station (gates
 * and unknown phases must never bounce the track backwards).
 */
export function agentPhaseStation(phase: string): AgentPhaseStation | null {
  switch (phase) {
    case 'intake':
      return 'intake'
    case 'discovery':
      return 'discovery'
    case 'planning':
      return 'planning'
    case 'execution':
      return 'execution'
    case 'evidence':
    case 'synthesis':
      return 'synthesis'
    case 'critic':
      return 'critic'
    default:
      return null
  }
}

export function isTerminalAgentPhase(phase: string): boolean {
  return phase === 'done' || phase === 'cancelled'
}

// --- Records -----------------------------------------------------------------

export type AgentPlanTaskRecord = {
  taskId: string
  ordinal: number
  title: string
  toolKind: AgentPlanTaskWire['tool_kind']
  objective: string
  queries: string[]
  gapIds: string[]
  dependsOn: string[]
  budget: Record<string, unknown>
  params: Record<string, unknown>
  expectedOutput: string
  status: string
  childRunId: string | null
  resultSummary: string
}

export type AgentPlanVersionRecord = {
  planId: string
  version: number
  status: string
  createdBy: 'agent' | 'user'
  reason: string
  createdAt: number
}

export type AgentPlanRecord = AgentPlanVersionRecord & {
  summaryMarkdown: string
  assumptions: string[]
  successCriteria: string[]
  tasks: AgentPlanTaskRecord[]
  versions: AgentPlanVersionRecord[]
}

export type AgentApprovalRecord = {
  approvalId: string
  kind: AgentApprovalWire['kind']
  status: AgentApprovalWire['status']
  subjectType: string
  subjectId: string
  payload: Record<string, unknown>
  decision: string
  /** Decision-scoped keys (`approval_scope: 'run'` marks a run-wide
   * tool grant); empty for a plain approve. Optional so older fixtures
   * and stores omit it — the wire mapper always fills it. */
  decisionPayload?: Record<string, unknown>
  note: string
  createdAt: number
  decidedAt: number | null
}

export type AgentClarificationOption = {
  id: string
  label: string
  description: string
}

export type AgentClarificationQuestion = {
  id: string
  prompt: string
  options: AgentClarificationOption[]
  multiSelect: boolean
}

export type AgentClarificationRecord = {
  clarificationId: string
  question: string
  options: AgentClarificationOption[]
  /** Structured round questions; empty = legacy single-text round. */
  questions: AgentClarificationQuestion[]
  /** Structured answers by question id (empty until answered). */
  answers: Record<string, { optionIds: string[]; text: string }>
  /** The run's proposed default assumption, shown up front. A round has no
   * expiry and no dismiss path: it stays open until answered or the run ends. */
  defaultAssumption: string
  status: AgentClarificationWire['status']
  answer: string
  optionId: string
  createdAt: number
  answeredAt: number | null
}

export type AgentArtifactRecord = {
  artifactId: string
  kind: AgentArtifactMetaWire['kind']
  title: string
  status: AgentArtifactMetaWire['status'] | 'interrupted'
  revision: number
  updatedBy: 'agent' | 'user'
  refsCount: number
  createdAt: number
  updatedAt: number
  /** Body + refs, present only after a detail fetch (list stays lean). */
  contentMarkdown?: string
  /** Kind-specific detail payload. Undefined means the detail has not been
   * fetched; an empty object is a fetched legacy artifact without payload. */
  payload?: Record<string, unknown>
  refs?: Record<string, unknown>[]
  revisions?: { revision: number; createdBy: string; createdAt: number; status: string }[]
  /** Live answer publication identity/UTF-8 cursor. Wire artifact fetches do
   * not carry these; they exist only while SSE deltas are authoritative. */
  publicationId?: string
  publicationOffset?: number
  /** The streamed body remains visible until the authoritative artifact
   * detail (including references) replaces it after publication. */
  publicationNeedsReconcile?: boolean
  /** Citation labels the answer will use, delivered with
   * `answer.started` so the STREAMED text can render them right away.
   * Cleared when the artifact settles and the real refs arrive. */
  publicationRefLabels?: string[]
  /** A delta arrived out of order: the streamed body is incomplete and
   * must never be kept as the finished answer (the authoritative detail
   * replaces it). */
  publicationGap?: boolean
}

/** One editor patch proposed by the run; mirrors the wire detail. */
export type AgentPatchRecord = {
  patchId: string
  documentId: string
  source: AgentPatchWire['source']
  status: AgentPatchWire['status']
  summary: string
  edits: AgentPatchWire['edits']
  warnings: string[]
  revisionBefore: number
  appliedRevision: number | null
  appliedEditIds: string[] | null
  note: string
  documentRevision: number
}

export function agentPatchFromWire(wire: AgentPatchWire): AgentPatchRecord {
  return {
    patchId: wire.patch_id,
    documentId: wire.document_id,
    source: wire.source,
    status: wire.status,
    summary: wire.summary,
    edits: wire.edits.map((edit) => ({ ...edit })),
    warnings: [...wire.warnings],
    revisionBefore: wire.revision_before,
    appliedRevision: wire.applied_revision,
    appliedEditIds: wire.applied_edit_ids ? [...wire.applied_edit_ids] : null,
    note: wire.note,
    documentRevision: wire.document_revision,
  }
}

/** THE settled-task vocabulary: statuses whose result rows exist and no
 * longer change. One definition for event reduction, result caching and
 * prefetch eligibility — no surface may keep its own copy of this set. */
export const TERMINAL_AGENT_TASK_STATUSES: ReadonlySet<string> = new Set([
  'cancelled',
  'completed',
  'failed',
  'insufficient_evidence',
  'skipped',
])

/** Live per-task state derived from `inqtrix.agent.task.*` events. */
export type AgentTaskLiveState = {
  status:
    | 'running'
    | 'cancel_requested'
    | 'cancelled'
    | 'completed'
    | 'failed'
    | 'insufficient_evidence'
    | 'skipped'
  /** Terminal task outcome as reported (completed|failed|insufficient_evidence…). */
  outcome?: string
  attempt: number
  childRunId?: string
  error?: string
  /** Latest task-scoped operation for local Instant/Knowledge work. */
  activity?: AgentActivityRecord
  /** Bounded invocation-level history for the selected task detail. */
  activityHistory?: AgentActivityRecord[]
  /** Truthful terminal counters emitted by the task executor. */
  metrics?: Record<string, unknown>
  /** Immediate task outcome from the terminal event. The authoritative plan
   * row replaces it on refresh, but live cards must not wait for that fetch. */
  resultSummary?: string
  errorCode?: string
  /** The task took a visible fallback path (it belongs under attention). */
  fallback?: boolean
  /** The task's evidence has a limit the run names as an open point. A
   * note, not a fallback: the task keeps its own status group. */
  evidenceLimit?: boolean
  /** First task/activity boundary (unix seconds). Retries retain the original
   * start so the card reports the complete work-unit duration. */
  startedAt?: number
  /** Earliest truthful terminal operation boundary. A later wave-level
   * task.finished event must not inflate parallel task durations. */
  finishedAt?: number
}

/** Latest child-run snapshot (throttled `child.progress` events). */
export type AgentChildProgressRecord = {
  childRunId: string
  taskId: string
  /** Identity-only child-start events carry no snapshot. The first real
   * research snapshot fills this field; absence means "preparing", never
   * "answer". */
  snapshot?: ResearchRunSnapshot
  /** Durable parent-stream projection. These fields keep the parent work-unit
   * useful without opening a second child subscription. */
  runStatus?: string
  currentNode?: string
  message?: string
  metrics?: Record<string, unknown>
  attempt?: number
  error?: string
  errorCode?: string
  /** A kernel role child's last settled tool (its wire name) and the
   * server's count of its calls — the steps that cross its projection.
   * Absent for every other child. */
  lastTool?: string
  toolCalls?: number
  updatedAt?: number
  /** Zero-based ordinals a delegated MISSION currently has OPEN. A
   * mission starts its whole parallel wave at once, so this is a set,
   * not a single "current task". Empty means every started task has
   * settled. A research child reports none of this. */
  openTasks?: number[]
  taskToolKind?: string
  /** Grounded evidence answers the child has completed so far — the
   * unit of progress that actually moves during a long execution. */
  checkedAnswers?: number
  /** Plan task titles by zero-based ordinal, from the mission's
   * `task.started` projections — what the line names a task by. */
  taskTitles?: Record<number, string>
  /** Zero-based ordinals of tasks that FAILED (settled, not open). The
   * line says how many, so a mission that lost two tasks and went on
   * planning is not read as two tasks still running. */
  failedTasks?: number[]
  /** The search a mission task started most recently (projected
   * `agent.activity`, `scope="task"`); the line shows it while the
   * child executes. */
  lastQuery?: string
  /** Searches the mission's tasks completed so far — with the parallel
   * wave this is the honest count next to the one query shown. */
  searchesDone?: number
  /** The latest derived synthesis line of the mission (outline title,
   * section written) — what the child is writing right now. */
  section?: string
  /** Memo sections the mission has written (n-section-* narrations). */
  sectionsDone?: number
  /** First projection seen for this child — the summary line's clock
   * runs from here to the terminal update. */
  startedAt?: number
  /** The child's own plan-task ids by ordinal — the join key between a
   * task row, its searches (`child_task_id`) and its result line
   * (`n-task-<id>`). */
  taskIds?: Record<string, number>
  /** Arrival time (epoch seconds) of a task's start / settle projection. */
  taskStartedAt?: Record<number, number>
  taskSettledAt?: Record<number, number>
  /** Completed searches per task ordinal — only the mission's OWN
   * searches (instant/knowledge tasks) cross; a `web_research` task
   * delegates to a grandchild whose searches stay in its own stream. */
  taskSearches?: Record<number, number>
  /** Tool kind per task ordinal (`web_research`, `web_instant`, ...). */
  taskKinds?: Record<number, string>
  /** Ordinal of the task whose search `lastQuery` belongs to. */
  lastQueryTask?: number
  /** Head of a finished task's result (narration kind `task`, its title
   * prefix stripped) — the mission's intermediate results, one line each. */
  taskResults?: Record<number, string>
  /** The mission's derived plan line ("Mein Plan (N Aufgaben): …"). */
  planLine?: string
  /** The latest decision after a check (`critic_unavailable`,
   * `critic_revise`, `critic_research`, `critic_research_exhausted`,
   * `replan_invalid`): why the mission goes on or stops. */
  decision?: { kind: string; label: string; detail?: string; at: number }
  /** Critic research rounds the mission opened so far. */
  researchRounds?: number
  /** How the mission's review ended, as its projected outcome record: the
   * states and the server's bounded summary sentence, never its lines. */
  outcome?: AgentChildOutcome
}

/** The part of a child's outcome record that crosses to its parent. */
export type AgentChildOutcome = {
  final: string
  review: string
  summary: string
  applied: number
}

/**
 * Gate rows of one delegated child run, held on the PARENT record. A child
 * parked on a human decision (`child.progress` reports `waiting_for_approval`
 * or `waiting_for_input`) blocks the whole delegation, but child runs never
 * materialize as desk records — so the parent's control loop fetches the
 * child's approval/clarification rows and the composer tray offers them
 * with child context. Same row/staleness contract as the root surfaces.
 */
export type AgentChildGateRecord = {
  approvals: AgentApprovalRecord[]
  clarifications: AgentClarificationRecord[]
  /** The child's FULL delegated question (its run row's `question`) —
   * the gate context must be completely readable, and every other
   * parent-side source (args preview, progress message) is truncated
   * or transient. Fetched with the rows. */
  question?: string
  /** SSE refetch signal: set when child.progress reports a gate park. */
  stale: boolean
}

/**
 * Anchor-independent artifact meta per session. The server moves a
 * session artifact's ``run_id`` to the newest updating run, so run-scoped
 * records lose it from older runs — this index keys on the SESSION and
 * carries each artifact's CURRENT anchor for follow-up reads.
 */
export type AgentSessionArtifactMeta = {
  artifactId: string
  /** CURRENT run anchor — the id every artifact API call must use. */
  runId: string
  kind: AgentArtifactMetaWire['kind']
  title: string
  status: AgentArtifactMetaWire['status']
  revision: number
  updatedAt: number
  /** Body fetched under an anchor run that is not a desk record (a
   * delegated child's memo): desk runs keep their body on the run
   * record, this row is the only place a child-anchored body can live. */
  record?: AgentArtifactRecord
}

export type AgentSessionArtifactIndex = {
  order: string[]
  byId: Record<string, AgentSessionArtifactMeta>
  /** Refetch signal, flipped alongside the anchor run's artifactsStale. */
  stale: boolean
  /** Last fetch failure — rendered where the index feeds a menu, so a
   * broken listing never hides silently. */
  error?: string
}

export function sessionArtifactMetaFromWire(
  wire: AgentArtifactMetaWire,
): AgentSessionArtifactMeta {
  return {
    artifactId: wire.artifact_id,
    runId: wire.run_id,
    kind: wire.kind,
    title: wire.title,
    status: wire.status,
    revision: wire.revision,
    updatedAt: wire.updated_at,
  }
}

/**
 * The canvas comments that rode this run's submission — rebuilt
 * from the durable `canvas_context.attached` event on every replay
 * (run summaries deliberately never carry the canvas context).
 */
export type AgentCanvasContextMeta = {
  artifactId: string
  revision: number
  comments: { comment: string; quotePreview: string }[]
}

/**
 * The result requirement a run started under — rebuilt from the durable
 * `requirement.attached` event on every replay. The labels are what the
 * question already shows; the revision each rule is frozen at is not.
 */
export type AgentRequirementMeta = {
  /** `title` is the display title the rule had when it was attached;
   * absent for a part stored before titles were kept. */
  rules: { templateId: string; label: string; revision: number; title?: string }[]
  /** Length of the typed half; null when the server could not split the
   * stored requirement into its parts. */
  freeTextChars: number | null
  /** `per_rule` = the review judges each attached rule on its own (the
   * kernel); `assignment` = a review pass checks the result against the
   * task, requirement included; `none` = the tier runs no review pass. Kept as
   * sent: an unknown value is shown neutrally, never mapped onto one. */
  review: string
}

/**
 * One document this run touched (`artifact.created/updated` with kind
 * memo|deliverable) — the turn's file-chip row. Rows stay the truth: the
 * chip is a navigation handle, the artifact ROW stays the content
 * truth (title may lag until the row refetch lands).
 */
export type AgentTouchedArtifact = {
  artifactId: string
  kind: 'memo' | 'deliverable'
  /** `write_canvas` events carry the title; the memo event does not —
   * display falls back to the fetched row / generic document label. */
  title?: string
  revision: number
  /** Revision BEFORE this run's first touch — the turn diff's
   * `from` side; 0 when the run created the artifact. */
  fromRevision: number
  /** Server-counted line delta, accumulated across this run's touches.
   * Absent = honestly unknown: at least one contributing event carried
   * no numbers (rows predating the counters, size guard) — the badge
   * then stays away instead of showing a partial sum. */
  linesAdded?: number
  linesRemoved?: number
  /** First touch in this run was the artifact's creation. */
  created: boolean
  /** Live-boundary provenance (same contract as step entries). */
  arrivedLive?: boolean
  at: number
}

/**
 * Resolve an artifact reference whose descriptor may carry a STALE run
 * anchor: the hinted run first, then any loaded run of the
 * same session holding the artifact, then the session index's current
 * anchor. The returned runId is the one artifact API calls must use.
 */
export function resolveAgentArtifact(
  runs: Record<string, AgentRunRecord>,
  sessionArtifacts: Record<string, AgentSessionArtifactIndex>,
  reference: { artifactId: string; runId: string },
): { artifact: AgentArtifactRecord | undefined; runId: string } {
  const hinted = runs[reference.runId]
  const sessionId = hinted?.sessionId
  // The index meta names the CURRENT anchor and revision. A cached copy
  // on a loaded run only wins while it is not OLDER than that — an old
  // turn keeps a frozen pre-update copy after a re-anchor, and serving
  // it would show a stale revision without any hint.
  const indexes = sessionId
    ? [sessionArtifacts[sessionId]]
    : Object.values(sessionArtifacts)
  const meta = indexes
    .map((index) => index?.byId[reference.artifactId])
    .find(Boolean)
  const fresh = (artifact: AgentArtifactRecord | undefined) =>
    artifact !== undefined && (!meta || artifact.revision >= meta.revision)
  const direct = hinted?.artifacts[reference.artifactId]
  if (fresh(direct)) return { artifact: direct, runId: reference.runId }
  for (const run of Object.values(runs)) {
    if (sessionId && run.sessionId !== sessionId) continue
    const artifact = run.artifacts[reference.artifactId]
    if (fresh(artifact)) return { artifact, runId: run.runId }
  }
  if (meta) {
    return {
      artifact: runs[meta.runId]?.artifacts[reference.artifactId] ?? meta.record,
      runId: meta.runId,
    }
  }
  // No index row: any loaded copy beats nothing (the load effect
  // refetches bodies; revisions cannot be compared without the meta).
  if (direct) return { artifact: direct, runId: reference.runId }
  for (const run of Object.values(runs)) {
    if (sessionId && run.sessionId !== sessionId) continue
    const artifact = run.artifacts[reference.artifactId]
    if (artifact) return { artifact, runId: run.runId }
  }
  return { artifact: undefined, runId: reference.runId }
}

/** One open point of an outcome record, in the reader's language. */
export type AgentRunOutcomeLine = {
  kind: string
  text: string
  reasons: string[]
}

/**
 * A statement of a delivered text that the review marked as
 * insufficiently supported: `quote` is its passage as the text holds it,
 * `outputId` the text (`chat` for the answer, else the canvas's artifact
 * id), `note` why, in the reader's language.
 */
export type AgentClaimMark = {
  outputId: string
  quote: string
  state: string
  note: string
}

/**
 * A run's outcome record (`inqtrix.agent.outcome`): how its review ended,
 * in the server's own words. `summary` is one closed sentence built from
 * the record's states and counts — the delivered statements by state when
 * the review counted them, with what it did besides in `details`; `lines`
 * name what stayed open; `marks` the statements the review marked. A
 * surface shows these words and never builds its own from the counts.
 */
export type AgentRunOutcome = {
  outcomeId: string
  final: string
  review: string
  summary: string
  details?: string
  applied: number
  lines: AgentRunOutcomeLine[]
  marks: AgentClaimMark[]
}

/**
 * The answer a reviewed kernel run shows while its review runs
 * (`inqtrix.answer.draft`): the draft after the citation check the
 * published answer passes too, whole. The published answer replaces it;
 * the draft keeps its own text, so a surface can show what the review
 * changed.
 */
export type AgentAnswerDraft = {
  draftId: string
  /** The answer artifact the published answer will use. */
  artifactId: string
  markdown: string
  /** The labels the draft cites, for its citations before sources exist. */
  referenceLabels: string[]
  createdAt: number
}

/** The one-line live activity readout (signature activity line). */
/** One flagged passage of a review, as a reader sees it. */
export type AgentReviewFinding = {
  /** The verbatim passage the review objected to. */
  quote: string
  /** What is wrong with it. */
  finding: string
  /** Which output it sits in: `chat` or a canvas artifact id. */
  target: string
}

export type AgentActivityRecord = {
  activityId?: string
  kind: string
  detail: string
  label?: string
  status?: string
  operation?: import('./activityPresentation').AgentOperation
  operationCode?: string
  current?: number
  total?: number
  count?: number
  taskId?: string
  purpose?: string
  metrics?: Record<string, unknown>
  attempt?: number
  error?: string
  errorCode?: string
  fallback?: boolean
  evidenceLimit?: boolean
  at: number
}

/**
 * One transcript line of the main-window step stream, appended by
 * `applyAgentRunEvent` in event order (replay-safe via the record's
 * `lastSequence` guard) and bounded to {@link MAX_STEP_LOG}. Decision
 * markers (`clarification_answered`, `approval_decided`) carry only ids;
 * the stream joins them with the control ROWS for display (rows are the
 * truth for content, the log only orders the story).
 */
export type AgentStepEntry = {
  /** Event sequence — stable identity and ordering across replays. */
  seq: number
  /** Event timestamp (seconds). */
  at: number
  /** Whether the transport delivered this row after its live boundary.
   * `false` marks persisted replay/catch-up and suppresses presentation
   * effects; absent keeps locally produced/demo entries backwards compatible. */
  arrivedLive?: boolean
  kind:
    | 'phase'
    | 'activity'
    | 'plan'
    | 'task_started'
    | 'task_finished'
    | 'task_failed'
    | 'clarification_answered'
    | 'approval_decided'
    | 'narration'
    | 'notice'
    | 'gate_requested'
  phase?: string
  taskId?: string
  activityKind?: string
  /** The passages a review flagged, with what is wrong about each. The
   * prose of a review activity carries its count; this carries the text
   * a reader can check for themselves. Reader-facing only: the server
   * strips the offsets and internal ids that drive the edits. */
  findings?: readonly AgentReviewFinding[]
  activityOperation?: import('./activityPresentation').AgentOperation
  activityOperationCode?: string
  activityKey?: string
  activityCount?: number
  detail?: string
  label?: string
  status?: string
  fallback?: boolean
  evidenceLimit?: boolean
  current?: number
  total?: number
  metrics?: Record<string, unknown>
  purpose?: string
  attempt?: number
  version?: number
  autoApproved?: boolean
  clarificationId?: string
  approvalId?: string
  /** The todo (zero-based index into the last reported list) a kernel
   * tool row served — `tool.started.todo_index`. */
  todoIndex?: number
  /** The canvas document a kernel tool row read — `tool.started.artifact_id`.
   * The row names it by the title the reader knows, never by this id. */
  artifactId?: string
  /** When an activity row was CREATED; `at` moves to the settle time,
   * so this is what a duration is measured from. */
  startedAt?: number
  error?: string
  /** Narration prose. */
  text?: string
  /** Stable narration slot id (e.g. `n-synthesis`, `n-section-0`). The
   * backend re-emits the SAME id on node re-execution (critic replan
   * loop) with a fresh sequence — the reducer upserts on this id so the
   * line updates in place instead of multiplying. */
  narrationId?: string
  /** A narration line the server is still writing (`final: false`): the
   * next emission with the same id replaces it. */
  open?: boolean
  /** Which runtime fact a `notice` row reports (tool_limit,
   * citation_validation, sufficiency_gap) — the
   * label is rendered from i18n, the row carries only the code. */
  noticeCode?: string
}

/** Step-log bound; the tail is the recent story, the plan tab the map. */
export const MAX_STEP_LOG = 400

/** Flagged passages kept per review row — a bound, never a silent cap on
 * what was found: the review's own label carries the full count. */
export const MAX_REVIEW_FINDINGS = 20

/**
 * Non-terminal run statuses: the run still occupies its session — the
 * composer shows stop, pills/canvas stay live, the rail marks activity.
 * `waiting_for_children` is a NORMAL mid-execution state (the parent is
 * parked while its child research runs work), never idleness. THE one
 * predicate for every "is something going on?" check — surfaces must
 * not hand-roll their own status lists, which drift apart.
 */
export function isActiveAgentRun(status: ResearchRunStatus): boolean {
  return (
    status === 'running'
    || status === 'queued'
    || status === 'waiting_for_approval'
    || status === 'waiting_for_input'
    || status === 'waiting_for_children'
  )
}

/**
 * Parked on a HUMAN decision (approval or answer): surfaces switch from
 * the working pulse to the calm warning breathe, and the gate tray has
 * something to show. A children-wait is deliberately NOT a gate — the
 * children are doing the work, so "working" = active && !gate.
 */
export function isGateAgentRun(status: ResearchRunStatus): boolean {
  return status === 'waiting_for_approval' || status === 'waiting_for_input'
}

/** The single Agent-UI permission gate. A view share is strictly read-only;
 * edit shares and owned/unscoped/local runs keep the existing status gates. */
export function canEditAgentRun(
  run: Pick<AgentRunRecord, 'access'> | null | undefined,
): boolean {
  return Boolean(
    run
    && (run.access?.mode !== 'shared' || run.access.permission === 'edit'),
  )
}

export type AgentRunRecord = {
  runId: string
  sessionId?: string
  kind: 'agent' | 'agent_child' | 'standard'
  /** Durable run-tree links used to retain loaded children while their root
   * remains visible and prune the whole tree when the root disappears. */
  parentRunId?: string
  rootRunId?: string
  question: string
  /** The question above is a clipped prefix; the full text is one
   * single-run read away. Drives the transcript bubble's "show more". */
  questionClipped?: boolean
  autonomy?: string
  /** Thoroughness: 'deep' shows the badge; unset = normal. */
  depth?: string
  /** Selected Stufe: schnell|gruendlich|tief; unset = legacy depth
   * semantics. Drives the gate's Suchtiefe options and add-task
   * defaults (the server validator stays the enforcement). */
  agentTier?: string
  /** Latest ANSWER-node model resolution from the live event stream.
   * Live-only by design: agent runs are not project-persisted, so after
   * a reload the chip is honestly absent instead of guessed. */
  modelResolution?: {
    model: string
    effort: string
    tier: string
    modelSource: string
  }
  status: ResearchRunStatus
  /** Backend phase string (intake…critic|done|cancelled). */
  phase: string
  /** Last station the pulse track reached (never moves backwards). */
  station: AgentPhaseStation
  access?: ResearchRunAccess
  queuePosition?: number | null
  createdAt: string
  startedAt?: string
  finishedAt?: string
  elapsedSeconds?: number
  /** Server time (s) of the newest event the run's stream delivered — its
   * own and its children's: the run line's "last update" reads this. */
  lastProgressAt?: number
  /** Client clock minus server clock (s), the smallest seen over live
   * events: the run line ticks in server time, so a skewed client clock
   * neither invents nor hides elapsed time. */
  clockOffsetSeconds?: number
  timing?: ResearchRunSummary['timing']
  error?: string
  errorCode?: string
  snapshot?: ResearchRunSnapshot
  /** Highest event sequence applied (drives `?after=` replay filtering). */
  lastSequence: number
  // Control rows (fetched; `*Stale` flags are the SSE refetch signals).
  plan?: AgentPlanRecord
  planStale: boolean
  approvals: AgentApprovalRecord[]
  approvalsStale: boolean
  clarifications: AgentClarificationRecord[]
  clarificationsStale: boolean
  artifactOrder: string[]
  artifacts: Record<string, AgentArtifactRecord>
  artifactsStale: boolean
  // Live progress.
  taskStates: Record<string, AgentTaskLiveState>
  children: Record<string, AgentChildProgressRecord>
  /** Fetched gate rows of parked children, keyed by child run id. */
  childGates: Record<string, AgentChildGateRecord>
  /** The kernel's live task list (todo.updated) — replaced wholesale per
   * event, rendered as ONE checklist block while the run is active. */
  todos?: { content: string; status: string }[]
  /** When that list was reported (event timestamp, seconds). */
  todosAt?: number
  activity?: AgentActivityRecord
  /** Ordered transcript lines for the step stream (bounded). */
  stepLog: AgentStepEntry[]
  /** The run's latest outcome record. Run state, not a transcript line:
   * the step log's bound must never drop it. */
  outcome?: AgentRunOutcome
  /** The answer the run showed while its review ran
   * (`inqtrix.answer.draft`). Run state beside the artifacts, never an
   * artifact row: the artifact list reconcile replaces those rows. */
  answerDraft?: AgentAnswerDraft
  /** Documents this run touched, in first-touch order (deduped by
   * artifact — an update refreshes the existing chip in place). */
  touchedArtifacts: AgentTouchedArtifact[]
  /** Canvas comments that rode the submission (replay-durable). */
  canvasContextMeta?: AgentCanvasContextMeta
  /** Rules the run started under (replay-durable). */
  requirementMeta?: AgentRequirementMeta
  /** Auto-approved replan note (plan.revised{auto_approved}). */
  lastAutoApprovedVersion?: number
  /** Proposed editor patch: id from the patch.proposed event, the
   * detail fetched row-first like every other control surface. */
  patchId?: string
  patch?: AgentPatchRecord
  patchStale: boolean
}

/**
 * `updatedAt` of a session record FABRICATED from a run summary:
 * hydration delivers run summaries newest-first, often
 * before the session listing, and the fabricated row must never carry
 * merge authority — with the epoch stamp the real server row always
 * wins `local-newer-wins`, and the autosave never pushes an untouched
 * fabrication (persistableAgentSessionsInOrder skips it). Any USER
 * mutation stamps a real `updatedAt` and thereby lifts the marker —
 * no clearing discipline to drift. Only the fabrication branch in
 * `withAgentRunSummary` ever writes this value.
 */
export const DERIVED_AGENT_SESSION_UPDATED_AT = '1970-01-01T00:00:00.000Z'

export type AgentSessionRecord = import('@/features/sessionTitle/model').SessionTitleFields & {
  id: string
  title: string
  groupId: string | null
  createdAt: string
  updatedAt: string
  /** Run ids of this session's turns, oldest first. */
  runIds: string[]
  /** Persistent source availability for runs started in this session. */
  sourcePolicy: AgentSourcePolicy
  /** The session knowledge switch (`/wissen` / the knowledge glyph): absent
   * until the session first asked for project knowledge. Rides `items_json`
   * beside the source policy. */
  knowledgeSwitch?: AgentKnowledgeSwitch
  /** The model picked in THIS session, kept so a reload returns to it rather
   * than to the account preference. Rides `items_json` beside the source
   * policy — no schema change. Absent means nothing was picked here and the
   * account preference seeds the composer. */
  modelSelection?: AgentSessionModelSelection
  /** Client-only: true once a server response for this session carried
   * `items_json`. A record built from a run summary exists before any such
   * response, so until the listing or the detail fetch lands, "no pick
   * stored" and "pick not loaded yet" are indistinguishable — and preference
   * seeding must wait. This flag is what it waits for. Never serialized. */
  metadataHydrated?: boolean
  /** `false` only for recipient-side views derived from shared runs. Such
   * sessions exist solely to render the Agent Desk and must never cross the
   * agent-session persistence API. Absent means persistable for backwards
   * compatibility with existing local state. */
  persistable?: boolean
  /** Durable server-owned deletion lifecycle. The row remains visible until
   * the terminal receipt removes it. */
  deletion?: import('@/features/project/sessionDeletion').SessionDeletionState
}

export type AgentSessionGroupRecord = {
  id: string
  title: string
  createdAt: string
  updatedAt: string
}

/**
 * History timestamp of a session row: the latest turn's `createdAt`,
 * falling back to `updatedAt`. The session record only bumps on rename /
 * source-policy edits (run membership is server-derived and unstamped),
 * so `updatedAt` alone would show a stale age for active conversations.
 */
export function agentSessionHistoryTimeIso(
  session: AgentSessionRecord,
  runs: Record<string, AgentRunRecord>,
): string {
  let latest = ''
  for (const runId of session.runIds) {
    const createdAt = runs[runId]?.createdAt ?? ''
    if (createdAt > latest) latest = createdAt
  }
  return latest || session.updatedAt
}

/**
 * The session to re-open after server hydration: the persisted intent
 * when it still exists, else the most recent session, else none. Pure —
 * the workspace effect dispatches the actual selection exactly once.
 */
export function restoredAgentSessionId(
  persistedId: string | null,
  sessionOrder: readonly string[],
  sessions: Record<string, AgentSessionRecord>,
  runs: Record<string, AgentRunRecord>,
): string | null {
  if (persistedId && sessions[persistedId] && !sessions[persistedId].deletion) {
    return persistedId
  }
  // Fallback by CONVERSATION recency (latest turn, not `updatedAt` —
  // which only bumps on rename/source-policy edits) and never by list
  // position: hydration appends in server list order, which is not a
  // guaranteed newest-first contract.
  let latestId: string | null = null
  let latestTime = ''
  for (const id of sessionOrder) {
    const session = sessions[id]
    if (!session || session.deletion) continue
    const time = agentSessionHistoryTimeIso(session, runs)
    if (!latestId || time > latestTime) {
      latestId = session.id
      latestTime = time
    }
  }
  return latestId
}

/**
 * What the desk's center column shows. Pure so the anti-flash contract is
 * pinnable offline: once the sessions are KNOWN (first listing settled for
 * this hydration identity — a view switch does not reset that), an empty
 * desk is a real welcome state and never the loading skeleton. The skeleton
 * remains for the genuine unknowns: the first listing of a fresh identity,
 * and a selected session whose runs are still paging in.
 */
export function agentCenterScreen({
  hasRuns,
  hasSelectedSession,
  runsHydrated,
  serverEnabled,
  sessionsKnown,
}: {
  hasRuns: boolean
  hasSelectedSession: boolean
  runsHydrated: boolean
  serverEnabled: boolean
  sessionsKnown: boolean
}): 'skeleton' | 'transcript' | 'welcome' {
  if (hasRuns) return 'transcript'
  if (!serverEnabled) return 'welcome'
  if (!sessionsKnown || (hasSelectedSession && !runsHydrated)) return 'skeleton'
  return 'welcome'
}

// --- Wire -> record converters (pure) ---------------------------------------

export function agentPlanFromWire(wire: AgentPlanWire): AgentPlanRecord {
  return {
    planId: wire.plan_id,
    version: wire.version,
    status: wire.status,
    createdBy: wire.created_by,
    reason: wire.reason,
    createdAt: wire.created_at,
    summaryMarkdown: wire.summary_markdown,
    assumptions: [...wire.assumptions],
    successCriteria: [...wire.success_criteria],
    tasks: wire.tasks.map(agentPlanTaskFromWire),
    versions: wire.versions.map((version) => ({
      planId: version.plan_id,
      version: version.version,
      status: version.status,
      createdBy: version.created_by,
      reason: version.reason,
      createdAt: version.created_at,
    })),
  }
}

export function agentPlanTaskFromWire(
  wire: AgentPlanTaskWire,
): AgentPlanTaskRecord {
  return {
    taskId: wire.task_id,
    ordinal: wire.ordinal,
    title: wire.title,
    toolKind: wire.tool_kind,
    objective: wire.objective,
    queries: [...wire.queries],
    gapIds: [...wire.gap_ids],
    dependsOn: [...wire.depends_on],
    budget: { ...wire.budget },
    params: { ...wire.params },
    expectedOutput: wire.expected_output,
    status: wire.status,
    childRunId: wire.child_run_id,
    resultSummary: wire.result_summary,
  }
}

export function agentApprovalFromWire(
  wire: AgentApprovalWire,
): AgentApprovalRecord {
  return {
    approvalId: wire.approval_id,
    kind: wire.kind,
    status: wire.status,
    subjectType: wire.subject_type,
    subjectId: wire.subject_id,
    payload: { ...wire.payload },
    decision: wire.decision,
    decisionPayload: { ...(wire.decision_payload ?? {}) },
    note: wire.note,
    createdAt: wire.created_at,
    decidedAt: wire.decided_at,
  }
}

/**
 * Reconcile an approvals write into the existing rows. Approval decisions
 * are immutable server-side (a new gate is a new approval_id), so a row
 * that is already decided locally must never regress to ``pending`` — the
 * decided-event refetch can race the decision commit and observe a stale
 * pending snapshot, which a blind replace would turn into a re-opened
 * gate tray.
 * Incoming rows otherwise win; unknown ids append in arrival order.
 */
export function mergeAgentRunApprovals(
  existing: AgentApprovalRecord[],
  incoming: AgentApprovalRecord[],
): AgentApprovalRecord[] {
  const byId = new Map(existing.map((row) => [row.approvalId, row]))
  for (const row of incoming) {
    const current = byId.get(row.approvalId)
    if (current && current.status !== 'pending' && row.status === 'pending') {
      continue
    }
    byId.set(row.approvalId, row)
  }
  return [...byId.values()]
}

/**
 * Reconcile a clarifications write into the existing rows — the exact
 * sibling of :func:`mergeAgentRunApprovals`: an answered round must
 * never regress to ``pending`` when the answered-event refetch races
 * the answer commit (a regressed row re-opens the composer gate tray).
 */
export function mergeAgentRunClarifications(
  existing: AgentClarificationRecord[],
  incoming: AgentClarificationRecord[],
): AgentClarificationRecord[] {
  const byId = new Map(existing.map((row) => [row.clarificationId, row]))
  for (const row of incoming) {
    const current = byId.get(row.clarificationId)
    if (current && current.status !== 'pending' && row.status === 'pending') {
      continue
    }
    byId.set(row.clarificationId, row)
  }
  return [...byId.values()]
}


export function agentClarificationFromWire(
  wire: AgentClarificationWire,
): AgentClarificationRecord {
  return {
    clarificationId: wire.clarification_id,
    question: wire.question,
    options: wire.options.map(clarificationOptionFromWire),
    questions: (wire.questions ?? []).map((question) => ({
      id: question.id,
      prompt: question.prompt,
      options: question.options.map(clarificationOptionFromWire),
      multiSelect: question.multi_select,
    })),
    answers: Object.fromEntries(
      Object.entries(wire.answers ?? {}).map(([questionId, entry]) => [
        questionId,
        { optionIds: [...entry.option_ids], text: entry.text },
      ]),
    ),
    defaultAssumption: wire.default_assumption,
    status: wire.status,
    answer: wire.answer,
    optionId: wire.option_id,
    createdAt: wire.created_at,
    answeredAt: wire.answered_at,
  }
}

function clarificationOptionFromWire(option: {
  id: string
  label: string
  description?: string
}): AgentClarificationOption {
  return {
    id: option.id,
    label: option.label,
    description: option.description ?? '',
  }
}

export function agentArtifactFromWire(
  wire: AgentArtifactMetaWire | AgentArtifactDetailWire,
): AgentArtifactRecord {
  const record: AgentArtifactRecord = {
    artifactId: wire.artifact_id,
    kind: wire.kind,
    title: wire.title,
    status: wire.status,
    revision: wire.revision,
    updatedBy: wire.updated_by,
    refsCount: wire.refs_count,
    createdAt: wire.created_at,
    updatedAt: wire.updated_at,
  }
  if ('content_markdown' in wire) {
    record.contentMarkdown = wire.content_markdown
    record.payload = wire.payload ? { ...wire.payload } : {}
    record.refs = wire.refs.map((ref) => ({ ...ref }))
    record.revisions = wire.revisions.map((revision) => ({
      revision: revision.revision,
      createdBy: revision.created_by,
      createdAt: revision.created_at,
      status: revision.status,
    }))
  }
  return record
}

/** Whether a run summary belongs on the Agent Desk (root agent runs only).
 * Both engines count — the FE twin of the backend's AGENT_MODE_IDS. */
export function isAgentRunSummary(summary: ResearchRunSummary): boolean {
  return (
    (summary.mode === 'workspace_agent' || summary.mode === 'agent_kernel')
    && summary.kind !== 'agent_child'
  )
}

/**
 * Session identity for one agent summary. Shared runs must not materialize the
 * owner's `session_id` in the recipient's syncable session namespace. A view
 * session is therefore derived from the server-generated root run id; children
 * resolve to the same id and the whole tree prunes together.
 */
export function agentSessionIdFromSummary(
  summary: ResearchRunSummary,
): string | undefined {
  if (summary.access?.mode === 'shared') {
    return `shared-agent-view:${summary.root_run_id || summary.run_id}`
  }
  return summary.session_id || undefined
}

export function agentRunFromSummary(
  summary: ResearchRunSummary,
): AgentRunRecord {
  const phase = phaseFromSnapshot(summary.snapshot)
  return {
    runId: summary.run_id,
    // '' and null both mean "sessionless" on the wire — normalize to
    // undefined so merges can never stomp a known session with emptiness.
    sessionId: agentSessionIdFromSummary(summary),
    kind: summary.kind ?? 'standard',
    parentRunId: summary.parent_run_id,
    rootRunId: summary.root_run_id,
    question: summary.question,
    questionClipped: summary.question_clipped === true ? true : undefined,
    autonomy: autonomyFromOverrides(summary.agent_overrides),
    depth: depthFromOverrides(summary.agent_overrides),
    agentTier: tierFromOverrides(summary.agent_overrides),
    status: summary.status,
    phase,
    station: agentPhaseStation(phase) ?? 'intake',
    access: summary.access,
    queuePosition: summary.queue_position,
    createdAt: isoFromSeconds(summary.created_at) ?? new Date().toISOString(),
    startedAt: isoFromSeconds(summary.started_at),
    finishedAt: isoFromSeconds(summary.finished_at),
    elapsedSeconds: summary.elapsed_seconds ?? undefined,
    timing: summary.timing,
    error: summary.error?.message,
    errorCode: summary.error?.type,
    snapshot: summary.snapshot,
    lastSequence: 0,
    // The kernel has no plan phase (its /plan is a guaranteed 404), so
    // seeding it stale would fire one doomed fetch per desk open. The FE
    // twin of the backend's AGENT_MODE_IDS split — a kernel that ever
    // gains plans must flip this together with isAgentRunSummary.
    planStale: summary.mode !== 'agent_kernel',
    approvals: [],
    approvalsStale: true,
    clarifications: [],
    clarificationsStale: true,
    artifactOrder: [],
    artifacts: {},
    artifactsStale: true,
    taskStates: {},
    children: {},
    childGates: {},
    stepLog: [],
    touchedArtifacts: [],
    patchStale: false,
  }
}

/** Merge a fresh summary into an existing record, keeping fetched rows. */
export function mergeAgentRunSummary(
  current: AgentRunRecord | undefined,
  summary: ResearchRunSummary,
): AgentRunRecord {
  const next = agentRunFromSummary(summary)
  if (!current) return next
  // A summary is a snapshot of the run row; the event stream is the same
  // run still moving. Where both describe the run's LIFE (status, phase,
  // queue position, snapshot), the newer of the two wins — and a summary
  // read before an event this record has already consumed is the older
  // one. The decision endpoints make that the normal case, not a race:
  // their summary is read inside the transaction that re-queues the run,
  // so it says `queued` while the worker is already running it. Writing
  // that over the streamed state left the desk claiming a queue position
  // for a run that was working, until the next lifecycle event happened
  // to arrive — which can be minutes. Fields the stream does not carry
  // (session, access, timing) merge either way.
  const behindTheStream =
    summary.last_event_sequence !== undefined
    && summary.last_event_sequence <= current.lastSequence
  const phase = behindTheStream
    ? current.phase
    : next.phase || current.phase
  return {
    ...current,
    sessionId: next.sessionId || current.sessionId,
    parentRunId: next.parentRunId ?? current.parentRunId,
    rootRunId: next.rootRunId ?? current.rootRunId,
    status: behindTheStream ? current.status : next.status,
    phase,
    station: agentPhaseStation(phase) ?? current.station,
    queuePosition: behindTheStream ? current.queuePosition : next.queuePosition,
    startedAt: next.startedAt ?? current.startedAt,
    finishedAt: next.finishedAt ?? current.finishedAt,
    elapsedSeconds: next.elapsedSeconds ?? current.elapsedSeconds,
    timing: next.timing ?? current.timing,
    error: next.error ?? current.error,
    errorCode: next.errorCode ?? current.errorCode,
    snapshot: behindTheStream
      ? current.snapshot
      : next.snapshot ?? current.snapshot,
    access: next.access ?? current.access,
  }
}

function phaseFromSnapshot(snapshot: ResearchRunSnapshot | undefined): string {
  const raw = (snapshot as Record<string, unknown> | undefined)?.phase
  return typeof raw === 'string' && raw ? raw : 'intake'
}

function autonomyFromOverrides(
  overrides: Record<string, unknown>,
): string | undefined {
  const raw = overrides.autonomy
  return typeof raw === 'string' && raw ? raw : undefined
}

function depthFromOverrides(
  overrides: Record<string, unknown>,
): string | undefined {
  return overrides.depth === 'deep' ? 'deep' : undefined
}

function tierFromOverrides(
  overrides: Record<string, unknown>,
): string | undefined {
  const raw = overrides.agent_tier
  return typeof raw === 'string' && raw ? raw : undefined
}

function isoFromSeconds(value: number | null | undefined): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return new Date(value * 1000).toISOString()
}
