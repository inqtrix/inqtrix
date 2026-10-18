import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { isActiveAgentRun, isGateAgentRun, type AgentActivityRecord, type AgentRunRecord, type AgentStepEntry } from './model'

export type AgentOperation =
  | 'knowledge_collections'
  | 'knowledge_search'
  | 'web_instant'
  | 'discovery_summary'
  | 'model_turn'
  | 'evidence_read'
  | 'document_analysis'

export type AgentActivityIconKind = 'generic' | 'knowledge' | 'web'

export function agentActivityIconKind(
  operation: AgentOperation | undefined,
): AgentActivityIconKind {
  if (operation === 'web_instant') return 'web'
  if (
    operation === 'knowledge_collections'
    || operation === 'knowledge_search'
    || operation === 'evidence_read'
    || operation === 'document_analysis'
  ) {
    return 'knowledge'
  }
  return 'generic'
}

/** Normalize the stable activity vocabulary and the capability ids emitted by
 * older servers into one presentation contract. Unknown values deliberately
 * remain undefined so their raw label stays visible instead of being guessed. */
export function normalizeAgentOperation(value: unknown): AgentOperation | undefined {
  if (typeof value !== 'string') return undefined
  switch (value) {
    case 'knowledge_collections':
    case 'knowledge.collections':
    case 'knowledge.collections.list':
      return 'knowledge_collections'
    case 'knowledge_search':
    case 'knowledge.search':
    case 'search_project_knowledge':
      return 'knowledge_search'
    case 'web_instant':
    case 'web.search.instant':
      return 'web_instant'
    case 'discovery_summary':
      return 'discovery_summary'
    case 'agent.model.turn':
      return 'model_turn'
    case 'read_evidence':
    case 'evidence_read':
      return 'evidence_read'
    case 'document_analysis':
      return 'document_analysis'
    default:
      return undefined
  }
}

/** The title of an activity line — its operation or tool in reader words,
 * without the subject, progress or metrics behind it. The run line names
 * the work with this; the step row adds the rest (`activityDisplayText`). */
export function activityTitle(
  activity: Pick<
    AgentActivityRecord,
    'current' | 'detail' | 'kind' | 'label' | 'operation' | 'operationCode' | 'purpose' | 'status'
  >,
  t: TranslationDictionary,
): string {
  const operation = activity.operation
    ?? normalizeAgentOperation(activity.detail)
    ?? normalizeAgentOperation(activity.label)
  const humanDetail = cleanLegacyDetail(activity.detail || activity.purpose || '')
  return operation
    ? operation === 'evidence_read' && activity.status === 'failed'
      ? t.agent.timeline.evidenceReadGroupFailedOne
      : operation === 'evidence_read' && activity.status === 'completed'
        ? t.agent.activityOperations.evidenceReadCompleted
      // A reading that failed says so; the settled noun would claim a
      // finished analysis the row never delivered. Windows already read
      // make it a partial analysis, not an unread document.
      : operation === 'document_analysis' && activity.status === 'failed'
        ? (activity.current ?? 0) > 0
          ? t.agent.activityOperations.documentAnalysisPartial
          : t.agent.activityOperations.documentAnalysisFailed
      : operationLabel(operation, t, activity.status === 'completed' || activity.status === 'failed')
    : (activity.label && kernelToolLabel(activity.label, t))
      || humanDetail
      || activity.operationCode
      || fallbackKindLabel(activity.kind, t)
}

export function activityDisplayText(
  activity: Pick<
    AgentActivityRecord,
    'count' | 'current' | 'detail' | 'kind' | 'label' | 'metrics' | 'operation' | 'operationCode' | 'purpose' | 'status' | 'total'
  >,
  t: TranslationDictionary,
): string {
  const operation = activity.operation
    ?? normalizeAgentOperation(activity.detail)
    ?? normalizeAgentOperation(activity.label)
  const humanDetail = cleanLegacyDetail(activity.detail || activity.purpose || '')
  const title = activityTitle(activity, t)
  const detail = operation
    ? cleanOperationDetail(humanDetail, operation)
    : activity.label && humanDetail !== activity.label
      ? humanDetail
      : ''
  const progress = operation === 'evidence_read' ? '' : activityProgress(activity, t)
  const metrics = activityMetrics(activity.metrics, operation, t)
  return [title, detail, progress, metrics].filter(Boolean).join(' · ')
}

/** Both the live line and the disclosure describe the same invocation states. */
export function evidenceReadSummary(
  entries: readonly AgentStepEntry[],
  runStatus: AgentRunRecord['status'],
  t: TranslationDictionary,
) {
  const done = entries.filter((entry) => entry.status === 'completed').length
  const failed = entries.filter((entry) => entry.status === 'failed').length
  const settled = done + failed === entries.length
  const labels = t.agent.timeline
  const count = entries.length
  const template = failed > 0
    ? countLabel(count, labels.evidenceReadGroupFailedOne, labels.evidenceReadGroupFailed)
    : settled
      ? countLabel(count, labels.evidenceReadGroupDoneOne, labels.evidenceReadGroupDone)
      : !isActiveAgentRun(runStatus)
        ? countLabel(count, labels.evidenceReadGroupStoppedOne, labels.evidenceReadGroupStopped)
        : isGateAgentRun(runStatus)
          ? countLabel(count, labels.evidenceReadGroupPendingOne, labels.evidenceReadGroupPending)
          : countLabel(count, labels.evidenceReadGroupOne, labels.evidenceReadGroup)
  const summary = template
    .replace('{failed}', String(failed))
    .replace('{done}', String(done))
  return { done, failed, settled, summary }
}

/** A step row in reader words. A row that read a canvas document names it
 * by the title the caller resolves for its id; an unknown title leaves the
 * tool alone, never the id. */
export function activityStepDisplayText(
  entry: AgentStepEntry,
  t: TranslationDictionary,
  documentName?: (artifactId: string) => string | undefined,
): string {
  return activityDisplayText(
    {
      count: entry.activityCount,
      current: entry.current,
      detail: (entry.artifactId && documentName?.(entry.artifactId)) || entry.detail || '',
      kind: entry.activityKind ?? 'working',
      label: entry.label,
      operation: entry.activityOperation,
      operationCode: entry.activityOperationCode,
      metrics: entry.metrics,
      purpose: entry.purpose,
      status: entry.status,
      total: entry.total,
    },
    t,
  )
}

/** Return the one terminal activity error already rendered by task detail.
 * Earlier matching failures remain in the operation history as retry evidence. */
export function terminalActivityErrorIndex(
  history: readonly Pick<AgentActivityRecord, 'error' | 'status'>[],
  terminalError: string | undefined,
): number {
  const normalized = terminalError?.trim()
  if (!normalized) return -1
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const activity = history[index]
    if (
      activity?.status === 'failed'
      && activity.error?.trim() === normalized
    ) return index
  }
  return -1
}

export function discoveryProbeDisplay(
  probe: Record<string, unknown>,
  t: TranslationDictionary,
): { detail: string; title: string } {
  const rawKind = typeof probe.kind === 'string' ? probe.kind : ''
  const operation = normalizeAgentOperation(probe.operation) ?? normalizeAgentOperation(rawKind)
  const detail = typeof probe.query === 'string' ? probe.query.trim() : ''
  return {
    detail,
    title: operation ? operationLabel(operation, t) : rawKind || t.agent.activity.execution,
  }
}

const ROLE_OPERATIONS = new Set(['review_role', 'section_role'])

/**
 * Whether a delegation row belongs to an internal ROLE of the kernel (such as
 * the Deep review, or the writer of one memo section too large for a single
 * call). A role child reports phases like a mission does, but it has no
 * plan, no tasks and no sections: its row keeps the one-line presenter, and the
 * mission card is never drawn for it.
 */
export function isRoleOperation(tool: string | undefined): boolean {
  return tool !== undefined && ROLE_OPERATIONS.has(tool)
}

/** Human label of a kernel wire tool id for gate cards. Unknown ids stay
 * visible verbatim — an unmapped tool must never hide behind a guess. */
export function kernelToolLabel(
  tool: string,
  t: TranslationDictionary,
): string {
  switch (tool) {
    case 'web_instant':
      return t.agent.tools.webInstant
    case 'search_project_knowledge':
      return t.agent.tools.knowledgeSearch
    case 'run_web_research':
      return t.agent.tools.webResearch
    case 'run_deep_mission':
      return t.agent.tools.deepMission
    case 'review_role':
      // Not a model tool: the Deep review runs as a bounded role child, and
      // its delegation row rides the same tool-row protocol.
      return t.agent.tools.reviewRole
    case 'section_role':
      // Not a model tool either: one memo section written by a role that
      // reads its evidence, on the same tool-row protocol as the review.
      return t.agent.tools.sectionRole
    case 'delegate_batch':
      return t.agent.tools.delegateBatch
    case 'load_skill':
      return t.agent.tools.loadSkill
    case 'propose_editor_patch':
      return t.agent.tools.proposeEditorPatch
    case 'read_project_document':
      return t.agent.tools.readProjectDocument
    case 'cite_document_passages':
      return t.agent.tools.citeDocumentPassages
    case 'read_editor_document':
      return t.agent.tools.readEditorDocument
    case 'search_editor_document':
      return t.agent.tools.searchEditorDocument
    case 'read_canvas':
      return t.agent.tools.readCanvas
    case 'search_context':
      return t.agent.tools.searchContext
    case 'read_context':
      return t.agent.tools.readContext
    case 'read_evidence':
      return t.agent.tools.readEvidence
    case 'read_research_report':
      return t.agent.tools.readResearchReport
    case 'write_canvas':
      return t.agent.tools.writeCanvas
    case 'ask_user':
      return t.agent.tools.askUser
    default:
      return tool
  }
}

function operationLabel(
  operation: AgentOperation,
  t: TranslationDictionary,
  settled = false,
): string {
  switch (operation) {
    case 'evidence_read':
      return t.agent.activityOperations.evidenceRead
    case 'knowledge_collections':
      return settled ? t.agent.activityOperations.knowledgeCollectionsSummary : t.agent.activityOperations.knowledgeCollections
    case 'knowledge_search':
      return settled ? t.agent.tools.knowledgeSearch : t.agent.activityOperations.knowledgeSearch
    case 'web_instant':
      return settled ? t.agent.tools.webInstant : t.agent.activityOperations.webInstant
    case 'discovery_summary':
      return settled ? t.agent.activityOperations.discoverySummaryNoun : t.agent.activityOperations.discoverySummary
    case 'model_turn':
      // One word whether a turn runs or settled: every turn of the run
      // rides ONE row, so a tense pair flipped the row's word with each
      // turn. The glyph carries the state.
      return t.agent.activityOperations.modelTurnSummary
    case 'document_analysis':
      return settled
        ? t.agent.activityOperations.documentAnalysisSummary
        : t.agent.activityOperations.documentAnalysis
  }
}

function cleanOperationDetail(
  detail: string,
  operation: AgentOperation | undefined,
): string {
  const cleaned = cleanLegacyDetail(detail)
  if (!cleaned) return ''
  return normalizeAgentOperation(cleaned) === operation ? '' : cleaned
}

function cleanLegacyDetail(detail: string): string {
  return normalizeAgentOperation(detail) ? '' : detail.trim()
}

function activityProgress(
  activity: Pick<AgentActivityRecord, 'count' | 'current' | 'status' | 'total'>,
  t: TranslationDictionary,
): string {
  let progress = ''
  if (
    typeof activity.current === 'number'
    && typeof activity.total === 'number'
    && activity.total > 0
  ) {
    progress = t.agent.activityOperations.progress
      .replace('{current}', String(activity.current))
      .replace('{total}', String(activity.total))
  } else if ((activity.count ?? 0) > 1) {
    progress = t.agent.activityOperations.operations.replace(
      '{count}',
      String(activity.count),
    )
  }
  return [progress, activity.status === 'failed' ? t.status.failed : ''].filter(Boolean).join(' · ')
}

function fallbackKindLabel(kind: string, t: TranslationDictionary): string {
  if (kind === 'searching') return t.agent.activity.searching
  return t.agent.activity.execution
}

function activityMetrics(
  metrics: Record<string, unknown> | undefined,
  operation: AgentOperation | undefined,
  t: TranslationDictionary,
): string {
  if (!metrics) return ''
  const labels = t.agent.activityOperations
  const counts: string[] = []
  const results = finiteMetric(metrics.result_count)
  const references = finiteMetric(metrics.reference_count)
  const claims = finiteMetric(metrics.claim_count)
  if (results !== undefined) {
    // What a collection listing returns are collections, not results.
    counts.push(operation === 'knowledge_collections'
      ? countLabel(results, labels.collectionsOne, labels.collections)
      : countLabel(results, labels.resultsOne, labels.results))
  }
  if (references !== undefined) {
    counts.push(countLabel(references, labels.referencesOne, labels.references))
  }
  if (claims !== undefined) {
    counts.push(countLabel(claims, labels.claimsOne, labels.claims))
  }
  return counts.join(' · ')
}

function finiteMetric(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}
