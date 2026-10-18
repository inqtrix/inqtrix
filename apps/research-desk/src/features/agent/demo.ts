/**
 * Agent Desk demo simulator (the `buildDemoAskScript` pattern): plays the
 * complete assignment arc through the REAL reducer pipeline — synthetic
 * wire events via `appendApiRunEvent` for live state (pulse track, task
 * dots) plus control-row actions for plans/approvals/artifacts (rows are
 * the truth). Interactive: phase A runs to the plan gate and
 * PARKS; the user's approval plays phase B (execution -> memo -> done).
 */

import type { Dispatch } from 'react'

import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { countLabel } from '@/i18n/plural'
import type {
  AgentTierCapability,
  ResearchRunEvent,
  ResearchRunSummary,
} from '@/features/researchRuns/types'
import type {
  AgentApprovalWire,
  AgentArtifactMetaWire,
  AgentClarificationAnswerRequest,
  AgentClarificationWire,
  AgentPatchWire,
  AgentPlanWire,
} from './types'
import type { AgentOverviewSource } from './agentStatusOverview'
import type { AttachedReportsCapability } from './contextCategories'
import type { ReportRequirementCapability } from './reportRequirement'
import type {
  AgentSourcePolicy,
} from './executionPolicy'

export const DEMO_AGENT_RUN_PREFIX = 'RA-demo-'

// The results of the finished demo tasks, as the server sends them: once
// as `result_summary` on the task event (the row shows its head) and once
// behind the task title in the `n-task-<id>` narration.
const DEMO_RESULT_T1 = 'Der Bestand belegt die Anforderungen für Hochrisiko-Systeme; Konformitätsbewertung bindet 15-20% der Entwicklungskapazität.'
const DEMO_RESULT_T2 = '9 externe Quellen bestätigen eine Konsolidierungswelle; kleinere Anbieter suchen Compliance-Partnerschaften.'
const DEMO_RESULT_T4 = 'Dokumentanalyse: 1 von 1 Dokument bis zum Ende seines gespeicherten Textes gelesen. 3 Stellen wörtlich belegt.'

/**
 * The demo's mirror of `capabilities.agent` for the composer's run
 * overview — same values a CURRENT server publishes (the new UI must be
 * demo-visible). Kept literal on purpose: the demo simulates a server,
 * it never imports server code.
 */
/** Demo mirror of `capabilities.agent.tiers` (the Stufen ladder must
 * be visible in the demo — memory rule: new UI shows in the demo). */
export const DEMO_AGENT_TIERS: AgentTierCapability[] = [
  {
    id: 'schnell',
    clarification_rounds: 0,
    plan_gate: 'skip_unless_strict',
    web_research: false,
    web_child_profile: null,
    web_child_ceiling: null,
    mission: false,
    rag_default_profile: 'schnell',
    verify: 'labels',
    response_form: 'chat',
  },
  {
    id: 'gruendlich',
    clarification_rounds: 1,
    plan_gate: 'per_autonomy',
    web_research: true,
    web_child_profile: 'schnell',
    web_child_ceiling: 'compact',
    mission: false,
    rag_default_profile: 'standard',
    verify: 'standard',
    response_form: 'auto',
  },
  {
    id: 'tief',
    clarification_rounds: 2,
    plan_gate: 'per_autonomy',
    web_research: true,
    web_child_profile: 'compact',
    web_child_ceiling: 'deep',
    mission: true,
    rag_default_profile: 'gruendlich',
    verify: 'escalating',
    response_form: 'canvas',
  },
]

/** Demo mirror of `capabilities.agent.report_requirement`: without it the
 * demo has no rule cap and no composed ceiling, so the composer and the
 * plan gate said the server checks on sending while the twin checks
 * nothing, and no cap state ever showed. Kept literal on purpose, like the
 * tiers. Demo rules carry no composed size, so the size check stays
 * unchecked there — and the rule menu says so. */
export const DEMO_AGENT_REPORT_REQUIREMENT: ReportRequirementCapability = {
  max_chars: 2_000,
  max_composed_chars: 20_000,
  max_rules: 3,
}

/** Demo mirror of `capabilities.agent.attached_reports`: without it the
 * demo has no report cap, so the report category said the server checks
 * the number while the twin checks nothing, and the cap state never
 * showed. Kept literal on purpose, like the requirement mirror. */
export const DEMO_AGENT_ATTACHED_REPORTS: AttachedReportsCapability = {
  max_reports: 3,
}

export const DEMO_AGENT_OVERVIEW_SOURCE: AgentOverviewSource = {
  default_mode: 'agent_kernel',
  durable: true,
  limits: {
    tokens: {
      enabled: false,
      limit: 0,
      ceiling: 0,
      recoverable: false,
      extendable: false,
      reason: 'operator_ceiling_exactly_once_required',
    },
    kernel: {
      schnell: { tool_calls: 30, tool_calls_ceiling: 30, steps: 57, steps_ceiling: 57 },
      normal: { tool_calls: 30, tool_calls_ceiling: 60, steps: 73, steps_ceiling: 145 },
      deep: { tool_calls: 60, tool_calls_ceiling: 120, steps: 121, steps_ceiling: 241 },
    },
    mission: {
      discovery_tool_calls: 15,
      plan_tasks: 8,
      replan_rounds: 2,
      clarification_rounds: 2,
      parallel_children: 6,
    },
    research: { rounds: 2 },
  },
  tools: [
    {
      id: 'web.search.instant',
      summary: 'Run one grounded web search (no research graph).',
      effect: 'read',
      read_only: true,
      idempotent: true,
    },
    {
      id: 'knowledge.search',
      summary: 'Hybrid retrieval over the knowledge base.',
      effect: 'read',
      read_only: true,
      idempotent: true,
    },
    {
      id: 'editor.patch.propose',
      summary: 'Propose anchored edits to an editor document.',
      effect: 'write',
      read_only: false,
      idempotent: false,
    },
  ],
  knowledge_scope_all: true,
  source_controls: [
    { id: 'web', default: 'available', available: true, reason_code: null },
    { id: 'knowledge', default: 'available', available: true, reason_code: null },
  ],
  permission_modes: {
    balanced: {
      plan_gate: true,
      web_replan_regate: true,
      patch_gate: true,
      kernel_gated_tools: [
        'delegate_batch',
        'load_skill',
        'run_deep_mission',
        'run_web_research',
        'web_instant',
      ],
      kernel_conditional_tools: ['search_project_knowledge'],
      kernel_always_gated: ['propose_editor_patch'],
    },
    autonomous: {
      plan_gate: false,
      web_replan_regate: false,
      patch_gate: true,
      kernel_gated_tools: [],
      kernel_conditional_tools: [],
      kernel_always_gated: ['propose_editor_patch'],
    },
  },
}

type DemoStep = { delayMs: number; actions: ResearchDeskAction[] }

type DemoRuntime = {
  dispatch: Dispatch<ResearchDeskAction>
  timeouts: Set<number>
}

let demoRunCounter = 0

function playSteps(runtime: DemoRuntime, steps: DemoStep[]) {
  let at = 0
  for (const step of steps) {
    at += step.delayMs
    const id = window.setTimeout(() => {
      runtime.timeouts.delete(id)
      for (const action of step.actions) runtime.dispatch(action)
    }, at)
    runtime.timeouts.add(id)
  }
}

/** Fabricated resolution mirroring the backend shape: an explicit pick
 * reports `explicit_request`, otherwise the tier-map default. */
function demoModelResolution(
  node: string,
  selection?: {
    model: string | null
    effort: string | null
    tier: string | null
  },
) {
  if (selection?.model) {
    return {
      node,
      model: selection.model,
      effort: selection.effort ?? '',
      tier: selection.tier ?? 'high',
      model_source: 'explicit_request',
      effort_source: selection.effort ? 'explicit_request' : 'provider_default',
      requested_tier: '',
    }
  }
  return {
    node,
    model: 'claude-sonnet-4-6',
    effort: '',
    tier: selection?.tier ?? 'high',
    model_source: selection?.tier ? `tier:${selection.tier}` : 'tier:high',
    effort_source: 'provider_default',
    requested_tier: selection?.tier ?? '',
  }
}

export function demoRunSummary(
  runId: string,
  sessionId: string,
  question: string,
  autonomy: string,
  status: ResearchRunSummary['status'],
  depth: string = 'normal',
  engineMode: string = 'agent_kernel',
  responseForm: string = 'auto',
  sourcePolicy: AgentSourcePolicy = {
    web: 'available',
    knowledge: 'available',
  },
  modelSelection?: DemoModelSelection,
  agentTier: string = '',
): ResearchRunSummary {
  const effectiveSourcePolicy = { ...sourcePolicy }
  const effectiveMode = engineMode
  const effectiveResponseForm = responseForm === 'chat' || responseForm === 'canvas'
      ? responseForm
      : effectiveMode === 'agent_kernel'
        ? 'chat'
        : 'canvas'
  return {
    access: { mode: 'owner' },
    run_id: runId,
    status,
    queue_position: null,
    question,
    stack: 'demo',
    mode: effectiveMode as ResearchRunSummary['mode'],
    kind: 'agent',
    session_id: sessionId,
    agent_overrides: {
      autonomy,
      ...(agentTier ? { agent_tier: agentTier } : {}),
      ...(depth === 'deep' ? { depth } : {}),
      ...(responseForm !== 'auto' ? { response_form: responseForm } : {}),
      source_policy: effectiveSourcePolicy,
      ...(modelSelection?.model ? { model: modelSelection.model } : {}),
      ...(modelSelection?.effort ? { effort: modelSelection.effort } : {}),
      ...(!modelSelection?.model && modelSelection?.tier
        ? { model_tier: modelSelection.tier }
        : {}),
    },
    created_at: Date.now() / 1000,
    started_at: Date.now() / 1000,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: {
      execution: demoExecutionBlock({
        autonomy,
        depth,
        effectiveMode,
        modelSelection,
        responseForm: effectiveResponseForm,
        sourcePolicy: effectiveSourcePolicy,
        toolUseCounts: { web: 0, knowledge: 0 },
      }),
    },
    error: null,
    events_url: `/v1/runs/${runId}/events`,
    result_url: `/v1/runs/${runId}/result`,
  }
}

type DemoModelSelection = {
  model: string | null
  effort: string | null
  tier: string | null
}

type DemoExecutionBlockInput = {
  autonomy: string
  depth: string
  effectiveMode: string
  modelSelection?: DemoModelSelection
  responseForm: string
  sourcePolicy: AgentSourcePolicy
  toolUseCounts: { web: number; knowledge: number }
  /** Web searches the run's own provider calls executed. */
  webSearches?: number
}

/** Canonical server-shape projection used by every demo run state. */
export function demoExecutionBlock({
  autonomy,
  depth,
  effectiveMode,
  modelSelection,
  responseForm,
  sourcePolicy,
  toolUseCounts,
  webSearches = 0,
}: DemoExecutionBlockInput): Record<string, unknown> {
  return {
    effective_mode: effectiveMode,
    response_form: responseForm,
    depth,
    model: modelSelection?.model ?? 'claude-sonnet-4-6',
    reasoning_effort: modelSelection?.effort ?? '',
    source_policy: sourcePolicy,
    // The simulator models no knowledge boundary of its own: it reports the
    // session's inherited scope, so the recap line names no collection set.
    knowledge_scope: 'inherited',
    knowledge_collection_count: 0,
    consent_reason: autonomy === 'autonomous'
      ? 'autonomous_policy'
      : 'permission_policy',
    tool_use_counts: toolUseCounts,
    web_searches: webSearches,
  }
}

function makeEvent(
  runId: string,
  sequence: number,
  type: string,
  data: Record<string, unknown>,
): ResearchDeskAction {
  const event: ResearchRunEvent = {
    type,
    run_id: runId,
    sequence,
    created_at: Date.now() / 1000,
    data,
  }
  return { event, type: 'appendApiRunEvent' }
}

/** Demo collection the rag task narrows to when the user picked none —
 * matches the seeded vector index ("EU-Recht"), so the source line
 * demonstrates the id -> title mapping. */
const DEMO_RAG_COLLECTION = 'vector-index-eu-recht'

export function demoPlan(
  runId: string,
  collectionIds: string[] = [],
  sourcePolicy: AgentSourcePolicy = {
    web: 'available',
    knowledge: 'available',
  },
): AgentPlanWire {
  const ragScope =
    collectionIds.length > 0 ? collectionIds : [DEMO_RAG_COLLECTION]
  const sourceTasks = [
    {
      task_id: 't1',
      ordinal: 0,
      title: 'Interne Berichte zu KI-Regulierung auswerten',
      tool_kind: 'rag_query' as const,
      objective: 'Bestand zu EU-AI-Act-Anforderungen konsolidieren',
      queries: [
        'Welche Anforderungen stellt der EU AI Act an Hochrisiko-Systeme?',
        'Welche internen Analysen bewerten die Compliance-Last für Anbieter?',
      ],
      gap_ids: ['g1'],
      depends_on: [],
      budget: {},
      params: { profile: 'standard', collection_ids: ragScope },
      expected_output: 'Belegte Kernaussagen aus dem Bestand',
      status: 'pending',
      child_run_id: null,
      result_summary: '',
    },
    {
      task_id: 't2',
      ordinal: 1,
      title: 'Aktuelle Marktlage recherchieren',
      tool_kind: 'web_research' as const,
      objective: 'Aktuelle Entwicklungen seit Q1 2026',
      queries: [
        'EU AI Act Mittelstand Auswirkungen 2026',
        'EU AI Act Umsetzungsstand Leitlinien 2026 offiziell',
      ],
      gap_ids: ['g2'],
      depends_on: [],
      budget: {},
      params: { profile: 'schnell', recency: 'month' },
      expected_output: 'Aktuelle externe Quellenlage',
      status: 'pending',
      child_run_id: null,
      result_summary: '',
    },
    {
      task_id: 't3',
      ordinal: 2,
      title: 'Gegenposition prüfen',
      tool_kind: 'web_instant' as const,
      objective: 'Kritische Stimmen zur Compliance-Last finden',
      queries: ['AI Act Kritik Compliance Kosten Mittelstand'],
      gap_ids: ['g2'],
      depends_on: [],
      budget: {},
      params: {},
      expected_output: 'Gegenläufige Einschätzungen',
      status: 'pending',
      child_run_id: null,
      result_summary: '',
    },
    {
      task_id: 't4',
      ordinal: 3,
      title: 'Verordnungstext vollständig prüfen',
      tool_kind: 'file_analysis' as const,
      objective: 'Alle Fristen für Anbieter von Hochrisiko-Systemen wörtlich belegen',
      queries: ['Welche Fristen setzt die KI-Verordnung Anbietern von Hochrisiko-Systemen?'],
      gap_ids: ['g1'],
      depends_on: [],
      budget: {},
      params: { collection_ids: ragScope },
      expected_output: 'Wörtlich belegte Fristen mit Seitenangabe',
      status: 'pending',
      child_run_id: null,
      result_summary: '',
    },
  ].filter((task) => {
    if (task.tool_kind === 'rag_query' || task.tool_kind === 'file_analysis') {
      return sourcePolicy.knowledge === 'available'
    }
    return sourcePolicy.web === 'available'
  })
  const taskIds = sourceTasks.map((task) => task.task_id)
  const tasks = [
    ...sourceTasks,
    {
      task_id: 's',
      ordinal: sourceTasks.length,
      title: 'Memo synthetisieren',
      tool_kind: 'synthesis' as const,
      objective: 'Belegtes Memo mit Handlungsempfehlungen',
      queries: [],
      gap_ids: [],
      depends_on: taskIds,
      budget: {},
      params: {},
      expected_output: 'Memo',
      status: 'pending',
      child_run_id: null,
      result_summary: '',
    },
  ].map((task, ordinal) => ({ ...task, ordinal }))
  const planParts = [
    ...(sourcePolicy.knowledge === 'available' ? ['Bestand auswerten'] : []),
    ...(sourcePolicy.web === 'available'
      ? ['aktuelle Lage extern verifizieren', 'Gegenposition prüfen']
      : []),
    'ein belegtes Memo erstellen',
  ]
  return {
    plan_id: `plan-${runId}`,
    run_id: runId,
    version: 1,
    status: 'proposed',
    created_by: 'agent',
    reason: '',
    created_at: Date.now() / 1000,
    summary_markdown: `${planParts.join(', ')}.`,
    assumptions: ['Fokus auf Anbieter mit Hochrisiko-Anwendungen.'],
    success_criteria: [
      'Jede Kernaussage mit Quelle belegt.',
      ...(sourcePolicy.web === 'available'
        && sourcePolicy.knowledge === 'available'
        ? ['Interne und externe Sicht abgeglichen.']
        : []),
      'Konkrete Handlungsempfehlungen enthalten.',
    ],
    tasks,
    versions: [
      {
        plan_id: `plan-${runId}`,
        version: 1,
        status: 'proposed',
        created_by: 'agent',
        reason: '',
        created_at: Date.now() / 1000,
      },
    ],
  }
}

const MEMO_SECTIONS = [
  '# Markteinschätzung: EU AI Act und der KI-Mittelstand\n\n## Kernaussagen\n\nDie Anforderungen des EU AI Act treffen mittelständische Anbieter von Hochrisiko-Systemen ab August 2026 mit voller Wirkung [K1]. Die interne Analyse zeigt: Konformitätsbewertung und technische Dokumentation binden im Schnitt 15-20% der Entwicklungskapazität [K2].',
  '\n\n## Aktuelle Entwicklung\n\nDie externe Quellenlage bestätigt eine Konsolidierungswelle: kleinere Anbieter suchen Partnerschaften mit etablierten Compliance-Plattformen [W1]. Gleichzeitig entstehen neue Beratungsangebote, die die Erstzertifizierung deutlich beschleunigen [W2].',
  '\n\n## Gegenposition\n\nKritische Stimmen halten die Compliance-Last für überzeichnet: harmonisierte Normen und Sandboxes senken die realen Kosten schneller als erwartet [W3]. Diese Einschätzung widerspricht teilweise der internen Projektion [K2] — beide Szenarien sind im Risikoboard zu führen.\n\n## Offene Punkte\n\nDie Kostenwirkung der harmonisierten Normen ist noch nicht belastbar quantifiziert.',
]

function memoMeta(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
): AgentArtifactMetaWire {
  return {
    artifact_id: `memo-${runId}`,
    run_id: runId,
    session_id: null,
    kind: 'memo',
    title: 'Markteinschätzung EU AI Act',
    status,
    revision,
    updated_by: 'agent',
    refs_count: 6,
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  }
}

/** Demo K-citation passages. They are VERBATIM lines of the demo
 * knowledge documents (`knowledge/demo.ts`) so the evidence reader
 * highlights a real match instead of opening on nothing — the demo
 * has to exercise the same path the live desk does. A parity test
 * pins both the document ids and these excerpts. */
const DEMO_K1_PASSAGE = 'Ein KI-System gilt als Hochrisiko-KI-System, wenn es als Sicherheitsbauteil eines unter die Harmonisierungsrechtsvorschriften fallenden Produkts verwendet wird oder selbst ein solches Produkt ist.'
const DEMO_K2_PASSAGE = 'Für Hochrisiko-Anwendungen empfiehlt das BSI eine dokumentierte Risikoanalyse je Lebenszyklusphase sowie kontinuierliches Monitoring im Betrieb.'

/** The demo's knowledge citations, in ONE place: three artifacts cited
 * the same passages as separate literals, so a retarget had to be made
 * three times (and was missed once). The parity test asserts these
 * against the knowledge demo corpus. */
export const agentDemoKnowledgeRefs = [
  {
    chunkIndex: 0,
    documentId: 'kdoc-ai-act-volltext',
    label: 'K1',
    sourceText: DEMO_K1_PASSAGE,
    title: 'EU-AI-Act-Volltext.pdf',
  },
  {
    chunkIndex: 0,
    documentId: 'kdoc-bsi-kriterien',
    label: 'K2',
    sourceText: DEMO_K2_PASSAGE,
    title: 'BSI-Kriterienkatalog-KI.pdf',
  },
] as const

const demoKnowledgeRefWire = (label: 'K1' | 'K2') => {
  const reference = agentDemoKnowledgeRefs.find(
    (candidate) => candidate.label === label,
  )
  if (!reference) throw new Error(`unknown demo reference ${label}`)
  return {
    chunk_index: reference.chunkIndex,
    document_id: reference.documentId,
    label: reference.label,
    source_text: reference.sourceText,
    title: reference.title,
  }
}

const ANSWER_MARKDOWN = `Kurzantwort: Der EU AI Act trifft den KI-Mittelstand ab August 2026 mit voller Wirkung [K1]. Die wichtigsten Punkte im Vergleich:

| Aspekt | Einschätzung | Beleg |
| --- | --- | --- |
| Pflichten | Konformitätsbewertung + technische Dokumentation | [K1] |
| Aufwand | 15–20 % der Entwicklungskapazität | [K2] |
| Markt | Konsolidierung, Compliance-Partnerschaften | [W1] |

Der Ablauf bis zur Konformität:

\`\`\`mermaid
flowchart LR
  A[Risikoklassifizierung] --> B[Gap-Analyse]
  B --> C[Technische Doku]
  C --> D[Konformitätsbewertung]
  D --> E[CE-Kennzeichnung]
\`\`\`

Offen bleibt die endgültige Auslegung der Normenreihe — hier lohnt ein Blick auf die laufende Harmonisierung [W2].`

/** Open points of a record that names none, per kind. */
const DEMO_NOTHING_OPEN = {
  information: 0, text_defect: 0, revised: 0, finding: 0, marked: 0, not_checked: 0,
}

/** The summaries of a review that left nothing open, in the server's
 * words: its statements by state. The answer cites four references in
 * five statements, the memo five in six; each rests on stored source text
 * (the knowledge passages, which state none of them word for word) or on
 * the search provider's context. */
const DEMO_CLEAN_REVIEW_SUMMARY = {
  4: 'Geprüft · 5 Aussagen: 3 mit Quelltext, 2 vom Anbieter zugeschrieben',
  5: 'Geprüft · 6 Aussagen: 3 mit Quelltext, 3 vom Anbieter zugeschrieben',
} as const

/** What that review did besides, in the record's details. */
const DEMO_CLEAN_REVIEW_DETAILS = {
  4: 'ohne Befund · 4 von 4 zitierten Belegen nachgelesen',
  5: 'ohne Befund · 5 von 5 zitierten Belegen nachgelesen',
} as const

/** Those statements counted by state, as the record carries them. */
const DEMO_CLEAN_REVIEW_CLAIMS = {
  4: { total: 5, verified: 0, source_text: 3, attributed: 2, insufficient: 0 },
  5: { total: 6, verified: 0, source_text: 3, attributed: 3, insufficient: 0 },
} as const

/** The outcome record of a demo run whose review ran and left nothing
 * open, every cited reference re-read — the shape the server sends. */
function demoCleanReviewOutcome(
  runId: string,
  cited: keyof typeof DEMO_CLEAN_REVIEW_SUMMARY,
): Record<string, unknown> {
  return {
    outcome_id: `${runId}:outcome`,
    final: 'completed',
    review: 'completed',
    round: 1,
    revision: 'none',
    accounting: 'complete',
    applied: 0,
    reach: { reread: cited, cited },
    codes: [],
    limits: [],
    open: { by_code: {}, by_kind: DEMO_NOTHING_OPEN },
    lines: [],
    claims: { [cited === 4 ? 'chat' : `memo-${runId}`]: DEMO_CLEAN_REVIEW_CLAIMS[cited] },
    summary: DEMO_CLEAN_REVIEW_SUMMARY[cited],
    details: DEMO_CLEAN_REVIEW_DETAILS[cited],
  }
}

/** The summaries of a run that completed without a review, in the
 * server's words: one that ended before a review, and one whose Stufe
 * skips it. */
const DEMO_UNREVIEWED_SUMMARY = {
  not_run: 'Ohne Prüfung beendet',
  waived: 'Ohne Prüfung (Stufe)',
} as const

/** The outcome record of a demo run that completed without a review: the
 * record exists and every surface stays quiet about it. */
function demoUnreviewedOutcome(
  runId: string,
  review: keyof typeof DEMO_UNREVIEWED_SUMMARY,
): Record<string, unknown> {
  return {
    outcome_id: `${runId}:outcome`,
    final: 'completed',
    review,
    round: 0,
    revision: 'none',
    accounting: 'incomplete',
    applied: 0,
    codes: [],
    limits: [],
    open: { by_code: {}, by_kind: DEMO_NOTHING_OPEN },
    lines: [],
    summary: DEMO_UNREVIEWED_SUMMARY[review],
  }
}

/** Whether a Stufe skips the review — the demo's mirror of the server's
 * `review_required`: a Stufe whose `verify` level is `labels` owes none.
 * A run without a Stufe keeps the review. */
function demoTierWaivesReview(tier: string): boolean {
  return DEMO_AGENT_TIERS.find((entry) => entry.id === tier)?.verify === 'labels'
}

/** The kernel's review in the delegation lane (Stufe tief): one finding
 * applied, one passage marked as insufficiently supported, one point the
 * review could not check. */
/** The answer the Tief run shows while its review runs: the reviewed
 * answer qualifies the date the draft states bare, which the reviewed
 * answer marks. */
const DEMO_DEEP_REVIEW_DRAFT = ANSWER_MARKDOWN.replace(
  'ab August 2026 mit voller Wirkung [K1]',
  'ab August 2026 [K1]',
)

const DEMO_DEEP_REVIEW_SUMMARY =
  'Geprüft · 5 Aussagen: 3 mit Quelltext, 1 vom Anbieter zugeschrieben, 1 nicht ausreichend belegt'

const DEMO_DEEP_REVIEW_DETAILS =
  '1 Befund überarbeitet, nicht nachgeprüft · 1 Punkt nicht geprüft · 5 von 5 zitierten Belegen nachgelesen'

/** The passage of the answer the review could measure only against the
 * search provider's summary: it stays as written, marked. */
const DEMO_DEEP_REVIEW_MARKED = '| Markt | Konsolidierung, Compliance-Partnerschaften | [W1] |'
const DEMO_DEEP_REVIEW_MARKED_FINDING = 'W1 berichtet die Konsolidierung, nennt aber keine Compliance-Partnerschaften.'

function answerMeta(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
): AgentArtifactMetaWire {
  return {
    artifact_id: `answer-${runId}`,
    run_id: runId,
    session_id: null,
    kind: 'answer',
    title: 'Antwort',
    status,
    revision,
    updated_by: 'agent',
    refs_count: 4,
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  }
}

function answerDetailAction(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
): ResearchDeskAction {
  return {
    artifact: {
      ...answerMeta(runId, revision, status),
      content_markdown: ANSWER_MARKDOWN,
      refs: [
        demoKnowledgeRefWire('K1'),
        demoKnowledgeRefWire('K2'),
        { label: 'W1', url: 'https://example.com/markt-konsolidierung', title: 'Marktbericht Konsolidierung' },
        { label: 'W2', url: 'https://example.com/zertifizierung', title: 'Zertifizierungs-Angebote 2026' },
      ],
      revisions: Array.from({ length: revision }, (_, index) => ({
        revision: index + 1,
        created_by: 'agent',
        created_at: Date.now() / 1000,
        // The demo memo streams like the real one: only the last flush
        // of a pass is a version the reader can pick.
        status: index + 1 < revision ? 'writing' : 'ready',
      })),
    },
    runId,
    type: 'setAgentRunArtifactDetail',
  }
}

/**
 * The kernel's DELEGATION lane (Stufe Tief): one `run_deep_mission` tool
 * row, then the projected progress of the mission child — task titles,
 * a search, written sections, one failed task, the summary — and the
 * mission memo as the deliverable. Pure: returns the steps, so the
 * parity test can inspect every child payload without timers.
 */
export function demoDelegationLane(
  runId: string,
  next: () => number,
  question: string,
  initialSummary: ResearchRunSummary,
): DemoStep[] {
  const childRunId = `${runId}-mission`
  const child = (data: Record<string, unknown>, runStatus = 'running') =>
    makeEvent(runId, next(), 'inqtrix.agent.child.progress', {
      task_id: 'demo-deleg-1',
      child_run_id: childRunId,
      run_status: runStatus,
      ...data,
    })
  const reviewRole = (data: Record<string, unknown>, runStatus = 'running') =>
    makeEvent(runId, next(), 'inqtrix.agent.child.progress', {
      task_id: 'role-review',
      child_run_id: `${runId}-review`,
      run_status: runStatus,
      ...data,
    })
  const todos = (statuses: [string, string, string]) =>
    makeEvent(runId, next(), 'inqtrix.agent.todo.updated', {
      todos: [
        { content: 'Auftrag und Zeitraum festlegen', status: statuses[0] },
        { content: 'Tiefen-Recherche als Unterauftrag durchführen', status: statuses[1] },
        { content: 'Kurzfazit mit Dokumentverweis formulieren', status: statuses[2] },
      ],
    })
  const waiting = { ...initialSummary, status: 'waiting_for_children' as const }
  const titles = [
    'Modellankündigungen der Woche',
    'Regulatorische KI-Entwicklungen EU',
    'Unternehmens- und Marktbewegungen',
    'Strittige Wochenmeldungen prüfen',
  ]
  return [
    {
      delayMs: 300,
      actions: [
        makeEvent(runId, next(), 'inqtrix.run.started', {}),
        makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution' }),
        todos(['completed', 'in_progress', 'pending']),
        makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
          tool: 'run_deep_mission',
          tool_call_id: 'demo-deleg-1',
          args_preview: `Erstelle ein belegtes Memo: ${question}`,
          todo_index: 1,
        }),
        { select: true, summary: waiting, type: 'upsertAgentRunSummary' },
        child({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'planning' } }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-plan', kind: 'plan', text: 'Mein Plan (4 Aufgaben): Modellankündigungen, Regulierung, Marktbewegungen und strittige Meldungen getrennt belegen.' }),
      ],
    },
    {
      delayMs: 1100,
      actions: [
        child({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'execution' } }),
        ...titles.map((title, ordinal) =>
          child({ event_type: 'inqtrix.agent.task.started', ordinal, tool_kind: 'web_research', title, child_task_id: `t${ordinal + 1}` })),
        child({
          event_type: 'inqtrix.agent.activity',
          operation: 'web.search.instant',
          status: 'started',
          query: 'Welche Modellankündigungen gab es diese Woche?',
          current: 1,
          total: 2,
          child_task_id: 't1',
        }),
      ],
    },
    {
      delayMs: 1400,
      actions: [
        child({
          event_type: 'inqtrix.agent.activity',
          operation: 'web.search.instant',
          status: 'completed',
          query: 'Welche Modellankündigungen gab es diese Woche?',
          current: 1,
          total: 2,
          child_task_id: 't1',
        }),
        child({ event_type: 'inqtrix.agent.task.finished', ordinal: 0, tool_kind: 'web_research', status: 'completed', child_task_id: 't1', metrics: { reference_count: 6 } }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-task-t1', kind: 'task', text: 'Modellankündigungen der Woche: Drei Modellstarts sind datiert belegt, ein vierter nur über eine Sekundärquelle.' }),
        child({ event_type: 'inqtrix.agent.task.finished', ordinal: 1, tool_kind: 'web_research', status: 'completed', child_task_id: 't2', metrics: { reference_count: 4 } }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-task-t2', kind: 'task', text: 'Regulatorische KI-Entwicklungen EU: Die Leitlinien zur Kennzeichnungspflicht sind seit dieser Woche veröffentlicht.' }),
        child({ event_type: 'inqtrix.agent.task.finished', ordinal: 2, tool_kind: 'web_research', status: 'completed', child_task_id: 't3', metrics: { reference_count: 5 } }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-task-t3', kind: 'task', text: 'Unternehmens- und Marktbewegungen: Zwei Finanzierungsrunden und eine Übernahme sind mit Primärquellen belegt.' }),
        child({ event_type: 'inqtrix.agent.task.failed', ordinal: 3, tool_kind: 'web_research', status: 'failed', child_task_id: 't4', error: 'capability_unavailable' }),
      ],
    },
    {
      delayMs: 1200,
      actions: [
        child({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'synthesis' } }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-synthesis', kind: 'synthesis', text: "Ich schreibe jetzt das Memo 'Markteinschätzung EU AI Act' mit 3 Abschnitten." }),
        { artifacts: [memoMeta(runId, 1, 'writing')], runId, type: 'setAgentRunArtifacts' },
        memoDetailAction(runId, 1, 'writing', 1),
        makeEvent(runId, next(), 'inqtrix.agent.artifact.created', {
          artifact_id: `memo-${runId}`, kind: 'memo', revision: 1, updated_by: 'agent',
        }),
      ],
    },
    {
      delayMs: 1300,
      actions: [
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-section-0', kind: 'synthesis', text: "Abschnitt 'Ausgangslage' geschrieben." }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-section-1', kind: 'synthesis', text: "Abschnitt 'Unternehmens- und Marktbewegungen' geschrieben." }),
        child({ event_type: 'inqtrix.agent.narration', narration_id: 'n-section-2', kind: 'synthesis', text: "Abschnitt 'Offene Punkte' geschrieben." }),
        memoDetailAction(runId, 3, 'ready', 3),
        makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', {
          artifact_id: `memo-${runId}`, kind: 'memo', revision: 3, updated_by: 'agent',
        }),
        child({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'critic' } }),
      ],
    },
    {
      delayMs: 1000,
      actions: [
        // How the mission's review ended crosses as its projected record:
        // states, counts and the bounded summary, never its lines.
        child({
          event_type: 'inqtrix.agent.outcome',
          final: 'completed',
          review: 'completed',
          round: 1,
          revision: 'none',
          accounting: 'complete',
          applied: 0,
          open: { by_code: {}, by_kind: DEMO_NOTHING_OPEN },
          summary: DEMO_CLEAN_REVIEW_SUMMARY[5],
        }),
        child({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'done' } }, 'completed'),
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'run_deep_mission', tool_call_id: 'demo-deleg-1', status: 'success',
        }),
        { select: true, summary: { ...initialSummary, status: 'running' as const }, type: 'upsertAgentRunSummary' },
        todos(['completed', 'completed', 'in_progress']),
      ],
    },
    // Stufe Tief ends with the review ROLE: a bounded child run that re-reads
    // the evidence and hands back a typed verdict. Its delegation row rides
    // the tool-row protocol, joined to the child by the role's origin key.
    {
      delayMs: 900,
      actions: [
        // The answer is read while the review runs (`inqtrix.answer.draft`).
        makeEvent(runId, next(), 'inqtrix.answer.draft', {
          artifact_id: `answer-${runId}`,
          draft_id: `drf-${runId}`,
          markdown: DEMO_DEEP_REVIEW_DRAFT,
          reference_labels: ['K1', 'K2', 'W1', 'W2'],
        }),
        makeEvent(runId, next(), 'inqtrix.agent.narration', {
          narration_id: 'kernel_deep_review', kind: 'synthesis', phase: 'execution',
          text: 'Verifikations-Durchlauf: Prüfung läuft.', final: false,
        }),
        makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
          tool: 'review_role', tool_call_id: 'role-review', invocation_id: 'role-review', args_preview: '',
        }),
        { select: true, summary: waiting, type: 'upsertAgentRunSummary' },
        reviewRole({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'execution' } }),
        // The role's settled steps cross like the server's projection:
        // the tool's name and the call count, first call and every fifth.
        reviewRole({ event_type: 'inqtrix.agent.tool.finished', tool: 'read_evidence', tool_calls: 1 }),
      ],
    },
    {
      delayMs: 700,
      actions: [
        reviewRole({ event_type: 'inqtrix.agent.tool.finished', tool: 'read_project_document', tool_calls: 5 }),
      ],
    },
    {
      delayMs: 1300,
      actions: [
        reviewRole({ event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'done' } }, 'completed'),
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'review_role', tool_call_id: 'role-review', invocation_id: 'role-review',
          status: 'success', result_preview: '2 Befunde gemeldet',
        }),
        { select: true, summary: { ...initialSummary, status: 'running' as const }, type: 'upsertAgentRunSummary' },
        makeEvent(runId, next(), 'inqtrix.agent.activity', {
          kind: 'deep_review_findings', scope: 'run', phase: 'execution',
          label: 'Verifikation: 2 Befunde',
          detail: `Die Frist der Kennzeichnungspflicht steht ohne ihre Bedingung.; ${DEMO_DEEP_REVIEW_MARKED_FINDING}`,
          findings: [
            { quote: 'ab August 2026 [K1]', finding: 'Die Frist der Kennzeichnungspflicht steht ohne ihre Bedingung.', target: 'chat' },
            { quote: DEMO_DEEP_REVIEW_MARKED, finding: DEMO_DEEP_REVIEW_MARKED_FINDING, target: 'chat', marked: true },
          ],
        }),
        // The final narration after a verdict is the record's summary.
        makeEvent(runId, next(), 'inqtrix.agent.narration', {
          narration_id: 'kernel_deep_review', kind: 'synthesis', phase: 'execution',
          text: `Verifikations-Durchlauf: ${DEMO_DEEP_REVIEW_SUMMARY}`, final: true,
        }),
      ],
    },
    {
      delayMs: 900,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'done', previous_phase: 'execution' }),
        { artifacts: [memoMeta(runId, 3, 'ready'), answerMeta(runId, 1, 'ready')], runId, type: 'setAgentRunArtifacts' },
        answerDetailAction(runId, 1, 'ready'),
        makeEvent(runId, next(), 'inqtrix.agent.artifact.created', {
          artifact_id: `answer-${runId}`, kind: 'answer', revision: 1, updated_by: 'agent',
        }),
        todos(['completed', 'completed', 'completed']),
        makeEvent(runId, next(), 'inqtrix.agent.outcome', {
          outcome_id: `${runId}:outcome`,
          final: 'completed',
          review: 'completed',
          round: 1,
          revision: 'unverified',
          accounting: 'complete',
          applied: 1,
          reach: { reread: 5, cited: 5 },
          codes: [],
          limits: [],
          open: { by_code: {}, by_kind: { ...DEMO_NOTHING_OPEN, marked: 1, not_checked: 1 } },
          lines: [{
            kind: 'not_checked',
            text: 'Der vierte Modellstart ist nur über eine Sekundärquelle belegt; die Primärquelle war nicht lesbar.',
            reasons: [],
          }, {
            kind: 'marked',
            text: `Nicht ausreichend belegt, nur am Text des Suchanbieters messbar: ${DEMO_DEEP_REVIEW_MARKED_FINDING}`,
            reasons: ['provider_context_only'],
          }],
          claims: { chat: { total: 5, verified: 0, source_text: 3, attributed: 1, insufficient: 1 } },
          marks: [{
            output_id: 'chat',
            quote: DEMO_DEEP_REVIEW_MARKED,
            state: 'insufficient',
            reason: 'finding',
            note: `Nicht ausreichend belegt: ${DEMO_DEEP_REVIEW_MARKED_FINDING}`,
          }],
          summary: DEMO_DEEP_REVIEW_SUMMARY,
          details: DEMO_DEEP_REVIEW_DETAILS,
        }),
        makeEvent(runId, next(), 'inqtrix.run.completed', { status: 'completed' }),
        { select: true, summary: { ...initialSummary, status: 'completed' as const }, type: 'upsertAgentRunSummary' },
      ],
    },
  ]
}

function memoDetailAction(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
  sections: number,
): ResearchDeskAction {
  return {
    artifact: {
      ...memoMeta(runId, revision, status),
      content_markdown: MEMO_SECTIONS.slice(0, sections).join(''),
      refs: [
        demoKnowledgeRefWire('K1'),
        demoKnowledgeRefWire('K2'),
        { label: 'W1', url: 'https://example.com/markt-konsolidierung', title: 'Marktbericht Konsolidierung' },
        { label: 'W2', url: 'https://example.com/zertifizierung', title: 'Zertifizierungs-Angebote 2026' },
        { label: 'W3', url: 'https://example.com/kritik', title: 'Kommentar Compliance-Kosten' },
      ],
      revisions: Array.from({ length: revision }, (_, index) => ({
        revision: index + 1,
        created_by: 'agent',
        created_at: Date.now() / 1000,
        // The demo memo streams like the real one: only the last flush
        // of a pass is a version the reader can pick.
        status: index + 1 < revision ? 'writing' : 'ready',
      })),
    },
    runId,
    type: 'setAgentRunArtifactDetail',
  }
}

export type AgentDemoActions = {
  applyPatch: (
    runId: string,
    patchId: string,
    expectedRevision: number,
  ) => Promise<
    | { kind: 'applied'; revision: number; appliedEditIds: string[] }
    | { kind: 'conflict'; currentRevision: number | null }
  >
  rejectPatch: (
    runId: string,
    patchId: string,
    note: string,
  ) => Promise<unknown>
  answerClarification: (
    runId: string,
    clarificationId: string,
    answer: AgentClarificationAnswerRequest,
  ) => Promise<unknown>
  decideApproval: (
    runId: string,
    approvalId: string,
    decision: {
      decision: string
      actions?: { tool: string; args: Record<string, unknown> }[]
      report_guidance?: string
      report_rule_ids?: string[]
    },
  ) => Promise<unknown>
  submit: (input: {
    autonomy: string
    question: string
    sessionId: string
    /** @-selected collection scope; flows into the demo plan's rag task
     * so the per-task source line shows the real selection. */
    collectionIds?: string[]
    documentId?: string
    /** Output form: 'chat' plays the inline-answer variant. */
    responseForm?: string
    /** Selected engine: 'agent_kernel' plays the conversational
     * inline-answer variant — the demo's honest approximation of the
     * kernel's direct-answer behavior. */
    engineMode?: string
    /** Attached skill labels: shown as an intake narration
     * line so an attached demo skill is visibly acknowledged. */
    skillLabels?: string[]
    /** Attached rule ids, resolved like the server resolves them (from
     * the project's own rules) and recorded with the same event. */
    ruleIds?: string[]
    sourcePolicy?: AgentSourcePolicy
    /** Thoroughness: 'deep' plays the verification narration
     * and stamps the Deep badge onto the run summary. */
    depth?: string
    /** Selected Stufe: echoed as agent_overrides.agent_tier so the
     * tier-aware gate UI (Suchtiefe ceiling, add-task defaults) is
     * demo-visible. */
    agentTier?: string
    /** Model-picker selection: feeds the fabricated model_resolution
     * event so the answer chip is demo-visible. */
    modelSelection?: {
      model: string | null
      effort: string | null
      tier: string | null
    }
  }) => void
  cancel: (runId: string) => void
  dispose: () => void
}

/** One library rule as the demo resolves it: label, revision and title. */
export type DemoRuleResolver = (
  ruleId: string,
) => { label: string; revision: number; title?: string } | null

/** One stored rule part, shaped like the server's: the title on one line
 * and only when the rule has one — a part never invents a title. */
function demoRulePart(
  ruleId: string,
  rule: { label: string; revision: number; title?: string },
): DemoRequirementParts['rules'][number] {
  const title = (rule.title ?? '').split(/\s+/).filter(Boolean).join(' ')
  return { label: rule.label, revision: rule.revision, template_id: ruleId, ...(title ? { title } : {}) }
}

/** The wire shape of a requirement's parts (gate payload, decision). */
type DemoRequirementParts = {
  free_text: string
  rules: { label: string; revision: number; template_id: string; title?: string }[]
}

/** Build the interactive demo runtime bound to the reducer dispatch.

 * `getDocumentMarkdown` reads the CURRENT local editor document so the
 * demo patch proposes real, deterministic edits against it and the
 * local apply lands as a visible tracked change. `resolveRule` stands
 * where the server reads its rule catalog: it resolves an attached rule
 * id to its label and revision, or `null` for one it does not know.
 */
export function createAgentDemo(
  dispatch: Dispatch<ResearchDeskAction>,
  getDocumentMarkdown: (documentId: string) => string | null = () => null,
  resolveRule: DemoRuleResolver = () => null,
): AgentDemoActions {
  const runtime: DemoRuntime = { dispatch, timeouts: new Set() }
  const patchTargets = new Map<string, string>()
  const responseForms = new Map<string, string>()
  const sourcePolicies = new Map<string, AgentSourcePolicy>()
  const acceptedSummaries = new Map<string, ResearchRunSummary>()
  // Thoroughness per run: read by the post-clarification
  // sequence, which runs outside the submit closure.
  const depths = new Map<string, string>()
  // Runs whose Stufe skips the review — same closure rule as `depths`.
  const waivedReviews = new Set<string>()
  // Model-picker selection per run — same closure rule as `depths`.
  const modelSelections = new Map<
    string,
    { model: string | null; effort: string | null; tier: string | null }
  >()
  // The requirement in force per run — what its plan gate opens with,
  // replaced by every decision that carries one (like the server).
  const requirementsInForce = new Map<string, DemoRequirementParts>()
  const lastDemoPatchWires = new Map<string, AgentPatchWire>()
  const lastDemoPatchEdits = new Map<string, AgentPatchWire['edits']>()
  // Strict quick-web tool gate: the search execution is HELD until
  // the tool approval is decided; the resume thunk (built in submit, capturing
  // the run's step sequence) is played by decideApproval's apr-tool branch.
  const heldQuickWeb = new Map<
    string,
    (decision: {
      decision: string
      actions?: { tool: string; args: Record<string, unknown> }[]
    }) => void
  >()

  const submit: AgentDemoActions['submit'] = ({
    autonomy,
    question,
    sessionId,
    collectionIds = [],
    documentId,
    responseForm,
    engineMode,
    skillLabels = [],
    ruleIds = [],
    sourcePolicy = { web: 'available', knowledge: 'available' },
    depth = 'normal',
    agentTier = '',
    modelSelection,
  }) => {
    demoRunCounter += 1
    const runId = `${DEMO_AGENT_RUN_PREFIX}${demoRunCounter}`
    const effectiveSourcePolicy = { ...sourcePolicy }
    sourcePolicies.set(runId, effectiveSourcePolicy)
    if (documentId) patchTargets.set(runId, documentId)
    if (depth === 'deep') depths.set(runId, 'deep')
    if (demoTierWaivesReview(agentTier)) waivedReviews.add(runId)
    if (modelSelection) modelSelections.set(runId, modelSelection)
    if ((responseForm === 'chat' || engineMode === 'agent_kernel') && !documentId) {
      responseForms.set(runId, 'chat')
    }
    const initialSummary = demoRunSummary(
      runId,
      sessionId,
      question,
      autonomy,
      'running',
      depth,
      engineMode ?? 'agent_kernel',
      responseForm ?? 'auto',
      sourcePolicy,
      modelSelection,
      agentTier,
    )
    acceptedSummaries.set(runId, initialSummary)
    dispatch({
      select: true,
      summary: initialSummary,
      type: 'upsertAgentRunSummary',
    })
    let seq = 0
    const next = () => (seq += 1)
    const rules = ruleIds.flatMap((ruleId) => {
      const rule = resolveRule(ruleId)
      if (!rule) {
        // The server refuses the whole run for an unknown rule; the demo
        // has no refusal path at submit, so it says so and goes on.
        console.warn(`Demo: unknown rule ${ruleId} is not attached.`)
        return []
      }
      return [demoRulePart(ruleId, rule)]
    })
    if (rules.length > 0) {
      requirementsInForce.set(runId, { free_text: '', rules })
      // Recorded once, when the run starts — the server's own event.
      dispatch(makeEvent(runId, next(), 'inqtrix.agent.requirement.attached', {
        free_text_chars: 0,
        // Like the server: the kernel's review judges each attached rule,
        // a mission checks the assignment as a whole.
        review: demoTierWaivesReview(agentTier)
          ? 'none'
          : (engineMode ?? 'agent_kernel') === 'agent_kernel' ? 'per_rule' : 'assignment',
        rules,
      }))
    }
    if ((engineMode ?? 'agent_kernel') === 'agent_kernel' && (agentTier === 'tief' || depth === 'deep')) {
      // Stufe Tief: the kernel delegates a mission — the demo mirrors the
      // real wire shape (tool row + projected child progress) so the
      // child line, the summary line and the folded todo list are all
      // visible in the shop window.
      playSteps(runtime, demoDelegationLane(runId, next, question, initialSummary))
      return
    }
    const gate = autonomy !== 'autonomous'
    const plan = demoPlan(runId, collectionIds, effectiveSourcePolicy)
    const taskCount = plan.tasks.length
    const steps: DemoStep[] = [
      {
        delayMs: 400,
        actions: [
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'intake' }),
          ...(skillLabels.length
            ? [makeEvent(runId, next(), 'inqtrix.agent.narration', {
              narration_id: 'n-skills',
              kind: 'discovery',
              phase: 'intake',
              text: `Angehängte Skills: ${skillLabels.map((label) => `/${label}`).join(', ')}.`,
              final: true,
            })]
            : []),
        ],
      },
      { delayMs: 1400, actions: [makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'discovery', previous_phase: 'intake' })] },
      ...(effectiveSourcePolicy.web === 'available'
        ? [{ delayMs: 900, actions: [makeEvent(runId, next(), 'inqtrix.agent.activity', { kind: 'searching', probe: 'EU AI Act Anforderungen' })] }]
        : []),
      ...(effectiveSourcePolicy.knowledge === 'available'
        ? [{ delayMs: 1500, actions: [makeEvent(runId, next(), 'inqtrix.agent.activity', { kind: 'searching', probe: 'Bestand: Compliance-Analysen' })] }]
        : []),
      {
        delayMs: 1300,
        actions: [
          makeEvent(runId, next(), 'inqtrix.agent.narration', {
            narration_id: 'n-discovery',
            kind: 'discovery',
            phase: 'discovery',
            text: 'Ich habe 2 belegte Fakten und 2 offene Lücken identifiziert. Wichtigste Lücke: Die aktuelle Marktlage seit Q1 2026 fehlt im Bestand.',
            final: true,
          }),
        ],
      },
      {
        delayMs: 1400,
        actions: [
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'planning', previous_phase: 'discovery' }),
          { plan, runId, type: 'setAgentRunPlan' },
          makeEvent(runId, next(), 'inqtrix.agent.plan.proposed', { plan_id: `plan-${runId}`, version: 1, task_count: taskCount }),
          makeEvent(runId, next(), 'inqtrix.agent.narration', {
            narration_id: 'n-plan-1',
            kind: 'plan',
            phase: 'planning',
            text: `${countLabel(taskCount, 'Mein Plan (1 Aufgabe):', 'Mein Plan ({count} Aufgaben):')} ${plan.summary_markdown}`,
            final: true,
          }),
        ],
      },
    ]
    if (gate) {
      steps.push({
        delayMs: 700,
        actions: [
          {
            approvals: [demoApproval(runId, requirementsInForce.get(runId))],
            runId,
            type: 'setAgentRunApprovals',
          },
          makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_approval' }),
        ],
      })
    }
    playSteps(runtime, steps)
    if (!gate) {
      // Autonomous: no gates at all — chain both execution parts after
      // the planning steps (drop the clarification-gate tail of part 1
      // and part 2's synthetic resume events).
      const part1 = executionUntilClarification(runId, () => next()).slice(0, -1)
      const part2 = executionAfterClarification(runId, () => next()).map(
        (step, index) =>
          index === 0
            ? {
              ...step,
              actions: step.actions.slice(2),
            }
            : step,
      )
      playSteps(
        runtime,
        [...part1, ...part2].map((step, index) => ({
          ...step,
          delayMs: step.delayMs + (index === 0 ? 6600 : 0),
        })),
      )
    }
  }

  const decideApproval: AgentDemoActions['decideApproval'] = (
    runId,
    approvalId,
    decision,
  ) => {
    const approved = decision.decision !== 'reject'
    if (approvalId.startsWith('apr-tool-')) {
      // The held strict quick-web search resumes (approve/edit) or ends with
      // a reject receipt — all choreography lives in the submit-closure thunk.
      const resume = heldQuickWeb.get(runId)
      heldQuickWeb.delete(runId)
      resume?.(decision)
      return Promise.resolve()
    }
    if (approvalId.startsWith('apr-patch-')) {
      // The patch gate: either way the run finishes NORMALLY (the memo
      // stays the deliverable; the patch decision is its own record).
      dispatch({
        approvals: [
          {
            ...demoApproval(runId),
            approval_id: approvalId,
            kind: 'patch',
            subject_type: 'editor_patch',
            subject_id: `pch-${runId}`,
            status: approved ? 'approved' : 'rejected',
            decision: decision.decision,
            decided_at: Date.now() / 1000,
          },
        ],
        runId,
        type: 'setAgentRunApprovals',
      })
      playSteps(runtime, [
        {
          delayMs: 300,
          actions: [
            makeEvent(runId, 300, 'inqtrix.agent.approval.decided', {
              approval_id: approvalId,
              status: approved ? 'approved' : 'rejected',
            }),
            makeEvent(runId, 301, 'inqtrix.agent.phase.changed', { phase: 'done', previous_phase: 'patch' }),
            makeEvent(runId, 302, 'inqtrix.agent.outcome', endOutcome(runId)),
            makeEvent(runId, 303, 'inqtrix.run.completed', {}),
          ],
        },
      ])
      return Promise.resolve()
    }
    // The decided requirement rides the payload in the shape the
    // approvals API returns it: the parts, never a composed text. A rule
    // in force keeps its frozen revision; an added one is resolved, and
    // an unknown one refuses the decision, exactly like the server.
    const touched = approved
      && ('report_guidance' in decision || 'report_rule_ids' in decision)
    const openedWith = requirementsInForce.get(runId)
    let decidedRequirement: DemoRequirementParts | null = null
    if (touched) {
      const inForce = openedWith?.rules ?? []
      const rules: DemoRequirementParts['rules'] = []
      for (const ruleId of [...new Set(decision.report_rule_ids ?? [])]) {
        const kept = inForce.find((rule) => rule.template_id === ruleId)
        const resolved = kept ? null : resolveRule(ruleId)
        if (!kept && !resolved) {
          return Promise.reject(new Error(`Unknown or non-visible rule: ${ruleId}`))
        }
        rules.push(kept ?? demoRulePart(ruleId, resolved!))
      }
      decidedRequirement = { free_text: (decision.report_guidance ?? '').trim(), rules }
      requirementsInForce.set(runId, decidedRequirement)
    }
    dispatch({
      approvals: [
        {
          ...demoApproval(runId, openedWith),
          approval_id: approvalId,
          status: approved ? 'approved' : 'rejected',
          decision: decision.decision,
          decision_payload: decidedRequirement
            ? { report_requirement: decidedRequirement }
            : {},
          decided_at: Date.now() / 1000,
        },
      ],
      runId,
      type: 'setAgentRunApprovals',
    })
    let seq = 100
    // The decided EVENT puts the decision into the transcript (the rows
    // above carry the content the stream joins with).
    const decided: DemoStep = {
      delayMs: 100,
      actions: [
        makeEvent(runId, (seq += 1), 'inqtrix.agent.approval.decided', {
          approval_id: approvalId,
          status: approved ? 'approved' : 'rejected',
        }),
      ],
    }
    if (!approved) {
      // Mirrors the backend contract: a rejected gate COMPLETES the run
      // with a deterministic receipt as its chat answer — no failure.
      const receipt =
        'Plan abgelehnt. Es wurde keine Recherche ausgeführt.'
        + '\n\nPasse den Auftrag an und sende ihn erneut, um einen neuen'
        + ' Plan zu erhalten.'
      playSteps(runtime, [
        decided,
        {
          delayMs: 300,
          actions: [
            makeEvent(runId, (seq += 1), 'inqtrix.agent.narration', {
              narration_id: 'n-plan-rejected-0',
              kind: 'plan',
              text: 'Plan abgelehnt; der Lauf endet ohne Recherche.',
              phase: 'planning',
              final: true,
            }),
            {
              artifacts: [
                {
                  ...answerMeta(runId, 1, 'ready'),
                  title: 'Plan abgelehnt',
                  refs_count: 0,
                },
              ],
              runId,
              type: 'setAgentRunArtifacts',
            },
            {
              artifact: {
                ...answerMeta(runId, 1, 'ready'),
                title: 'Plan abgelehnt',
                refs_count: 0,
                content_markdown: receipt,
                refs: [],
                revisions: [
                  {
                    revision: 1,
                    created_by: 'agent',
                    created_at: Date.now() / 1000,
                    status: 'ready',
                  },
                ],
              },
              runId,
              type: 'setAgentRunArtifactDetail',
            },
            makeEvent(runId, (seq += 1), 'inqtrix.agent.artifact.created', {
              artifact_id: `answer-${runId}`,
              kind: 'answer',
              revision: 1,
              updated_by: 'agent',
            }),
            makeEvent(runId, (seq += 1), 'inqtrix.agent.outcome', demoUnreviewedOutcome(runId, 'not_run')),
            makeEvent(runId, (seq += 1), 'inqtrix.run.completed', {}),
          ],
        },
      ])
      return Promise.resolve()
    }
    playSteps(runtime, [
      decided,
      ...executionUntilClarification(runId, () => (seq += 1)),
    ])
    return Promise.resolve()
  }

  const answerClarification: AgentDemoActions['answerClarification'] = (
    runId,
    clarificationId,
    answer,
  ) => {
    dispatch({
      clarifications: [
        {
          ...demoClarification(runId),
          clarification_id: clarificationId,
          status: 'answered',
          answer: answer.answer ?? '',
          option_id: answer.option_id ?? '',
          answers: answer.answers ?? {},
          answered_at: Date.now() / 1000,
        },
      ],
      runId,
      type: 'setAgentRunClarifications',
    })
    let seq = 200
    playSteps(runtime, [
      {
        delayMs: 100,
        actions: [
          makeEvent(runId, (seq += 1), 'inqtrix.agent.clarification.answered', {
            clarification_id: clarificationId,
          }),
        ],
      },
      ...executionAfterClarification(runId, () => (seq += 1)),
    ])
    return Promise.resolve()
  }

  const cancel: AgentDemoActions['cancel'] = (runId) => {
    playSteps(runtime, [
      {
        delayMs: 200,
        actions: [makeEvent(runId, 900, 'inqtrix.run.cancelled', {})],
      },
    ])
  }

  const applyPatch: AgentDemoActions['applyPatch'] = (runId, patchId) => {
    const documentId = patchTargets.get(runId)
    const markdown = documentId ? getDocumentMarkdown(documentId) : null
    if (!documentId || markdown === null) {
      return Promise.resolve({ kind: 'conflict', currentRevision: null })
    }
    // Local anchor application, mirroring the server semantics: verbatim
    // replace, append at the end; unresolvable anchors are skipped.
    let content = markdown
    const appliedEditIds: string[] = []
    for (const edit of lastDemoPatchEdits.get(patchId) ?? []) {
      if (edit.position === 'append') {
        content = `${content.trimEnd()}\n\n${edit.text}`
        appliedEditIds.push(edit.id)
      } else if (edit.find && content.includes(edit.find)) {
        content = content.replace(edit.find, edit.text)
        appliedEditIds.push(edit.id)
      }
    }
    dispatch({
      contentMarkdown: content,
      documentId,
      type: 'updateEditorDocumentMarkdown',
    })
    dispatch({
      patch: {
        ...(lastDemoPatchWires.get(patchId) as AgentPatchWire),
        status: 'accepted',
        applied_revision: 2,
        applied_edit_ids: appliedEditIds,
        decided_at: Date.now() / 1000,
        document_revision: 2,
      },
      runId,
      type: 'setAgentRunPatch',
    })
    return Promise.resolve({
      kind: 'applied',
      revision: 2,
      appliedEditIds,
    })
  }

  const rejectPatch: AgentDemoActions['rejectPatch'] = (runId, patchId, note) => {
    dispatch({
      patch: {
        ...(lastDemoPatchWires.get(patchId) as AgentPatchWire),
        status: 'rejected',
        note,
        decided_at: Date.now() / 1000,
      },
      runId,
      type: 'setAgentRunPatch',
    })
    return Promise.resolve()
  }

  return {
    answerClarification,
    applyPatch,
    cancel,
    decideApproval,
    dispose: () => {
      for (const id of runtime.timeouts) window.clearTimeout(id)
      runtime.timeouts.clear()
    },
    rejectPatch,
    submit,
  }

  /** References the run's output cites: a chat answer four, the memo five. */
  function citedByOutput(runId: string): keyof typeof DEMO_CLEAN_REVIEW_SUMMARY {
    return responseForms.get(runId) === 'chat' ? 4 : 5
  }

  /** The record a run sends at its end: a run whose Stufe skips the
   * review records it as waived, every other run its clean review. */
  function endOutcome(runId: string): Record<string, unknown> {
    return waivedReviews.has(runId)
      ? demoUnreviewedOutcome(runId, 'waived')
      : demoCleanReviewOutcome(runId, citedByOutput(runId))
  }

  function demoUsedSummaryAction(
    runId: string,
    policy: AgentSourcePolicy,
  ): ResearchDeskAction {
    const base = acceptedSummaries.get(runId)
    if (!base) {
      throw new Error(`Missing accepted demo summary for ${runId}`)
    }
    const snapshot = base.snapshot ?? {}
    const execution = snapshot.execution && typeof snapshot.execution === 'object'
      ? snapshot.execution as Record<string, unknown>
      : {}
    const updated: ResearchRunSummary = {
      ...base,
      snapshot: {
        ...snapshot,
        execution: {
          ...execution,
          tool_use_counts: {
            web: policy.web === 'available' ? 2 : 0,
            knowledge: policy.knowledge === 'available' ? 1 : 0,
          },
          // The quick web task ran one search; the research task is a
          // delegation whose child reports its own.
          web_searches: policy.web === 'available' ? 1 : 0,
        },
      },
    }
    acceptedSummaries.set(runId, updated)
    return { select: true, summary: updated, type: 'upsertAgentRunSummary' }
  }

  function executionUntilClarification(
    runId: string,
    next: () => number,
  ): DemoStep[] {
    const policy = sourcePolicies.get(runId)
      ?? { web: 'available', knowledge: 'available' }
    return [
      {
        delayMs: 400,
        actions: [
          makeEvent(runId, next(), 'inqtrix.run.started', {}),
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution', previous_phase: 'planning' }),
          ...(policy.knowledge === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.task.started', { task_id: 't1', ordinal: 0, tool_kind: 'rag_query', attempt: 1 })]
            : []),
          ...(policy.web === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.task.started', { task_id: 't2', ordinal: 1, tool_kind: 'web_research', attempt: 1 })]
            : []),
        ],
      },
      {
        delayMs: 700,
        actions: [
          // Per-query protocol rows (Verlauf = Portal): each query of
          // the rag task is its own step, exactly like the backend
          // emits per invocation.
          ...(policy.knowledge === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.activity', {
              kind: 'searching',
              operation: 'knowledge.search',
              task_id: 't1',
              status: 'started',
              query: 'Welche Anforderungen stellt der EU AI Act an Hochrisiko-Systeme?',
              current: 1,
              total: 2,
            })]
            : []),
        ],
      },
      {
        delayMs: 900,
        actions: [
          ...(policy.knowledge === 'available'
            ? [
              makeEvent(runId, next(), 'inqtrix.agent.activity', {
                kind: 'searching',
                operation: 'knowledge.search',
                task_id: 't1',
                status: 'completed',
                query: 'Welche Anforderungen stellt der EU AI Act an Hochrisiko-Systeme?',
                current: 1,
                total: 2,
                metrics: { result_count: 5 },
              }),
              makeEvent(runId, next(), 'inqtrix.agent.activity', {
                kind: 'searching',
                operation: 'knowledge.search',
                task_id: 't1',
                status: 'started',
                query: 'Welche internen Analysen bewerten die Compliance-Last für Anbieter?',
                current: 2,
                total: 2,
              }),
            ]
            : []),
          ...(policy.web === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.child.progress', {
              task_id: 't2',
              child_run_id: `${runId}-child`,
              snapshot: { current_node: 'search', total_sources: 4, total_queries: 6, executed_queries: 6, last_message: 'Durchsucht aktuelle Quellen …' },
            })]
            : []),
        ],
      },
      {
        delayMs: 1800,
        actions: [
          ...(policy.knowledge === 'available'
            ? [
              makeEvent(runId, next(), 'inqtrix.agent.activity', {
                kind: 'searching',
                operation: 'knowledge.search',
                task_id: 't1',
                status: 'completed',
                query: 'Welche internen Analysen bewerten die Compliance-Last für Anbieter?',
                current: 2,
                total: 2,
                metrics: { result_count: 4 },
              }),
              makeEvent(runId, next(), 'inqtrix.agent.task.finished', { task_id: 't1', ordinal: 0, tool_kind: 'rag_query', status: 'completed', result_summary: DEMO_RESULT_T1 }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-task-t1',
                kind: 'task',
                phase: 'execution',
                text: `Interne Berichte zu KI-Regulierung auswerten: ${DEMO_RESULT_T1}`,
                final: true,
              }),
            ]
            : []),
        ],
      },
      // Mid-execution clarification gate (bundled at the wave end, in the
      // clarification phase): the run PARKS until the user answers.
      {
        delayMs: 900,
        actions: [
          {
            clarifications: [demoClarification(runId)],
            runId,
            type: 'setAgentRunClarifications',
          },
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'clarification', previous_phase: 'execution' }),
          makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_input' }),
        ],
      },
    ]
  }

  function executionAfterClarification(
    runId: string,
    next: () => number,
  ): DemoStep[] {
    const policy = sourcePolicies.get(runId)
      ?? { web: 'available', knowledge: 'available' }
    // A Stufe that skips the review goes from the text straight to its
    // end, as the server does: no review phase, a waived record.
    const reviewed = !waivedReviews.has(runId)
    const beforeEnd = reviewed ? 'critic' : 'synthesis'
    return [
      {
        delayMs: 400,
        actions: [
          makeEvent(runId, next(), 'inqtrix.run.started', {}),
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution', previous_phase: 'clarification' }),
          ...(policy.web === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.task.started', { task_id: 't3', ordinal: 2, tool_kind: 'web_instant', attempt: 1 })]
            : []),
          ...(policy.knowledge === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.task.started', { task_id: 't4', ordinal: 3, tool_kind: 'file_analysis', attempt: 1 })]
            : []),
        ],
      },
      {
        delayMs: 1500,
        actions: [
          ...(policy.web === 'available'
            ? [
              makeEvent(runId, next(), 'inqtrix.agent.child.progress', {
                task_id: 't2',
                child_run_id: `${runId}-child`,
                snapshot: { current_node: 'evaluate', total_sources: 9, total_queries: 10, executed_queries: 9, consolidated_claim_count: 12, last_message: 'Bewertet Belege …' },
              }),
              makeEvent(runId, next(), 'inqtrix.agent.task.finished', { task_id: 't3', ordinal: 2, tool_kind: 'web_instant', status: 'completed' }),
            ]
            : []),
          // A whole-document reading: the search selects the document,
          // then one live row per document counts its windows.
          ...(policy.knowledge === 'available'
            ? [makeEvent(runId, next(), 'inqtrix.agent.activity', {
              kind: 'reading',
              operation: 'document_analysis',
              task_id: 't4',
              status: 'started',
              detail: 'KI-Verordnung (EU) 2024/1689',
              current: 12,
              total: 32,
            })]
            : []),
        ],
      },
      {
        delayMs: 1900,
        actions: [
          ...(policy.web === 'available'
            ? [
              makeEvent(runId, next(), 'inqtrix.agent.task.finished', { task_id: 't2', ordinal: 1, tool_kind: 'web_research', status: 'completed', child_run_id: `${runId}-child`, result_summary: DEMO_RESULT_T2 }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-task-t2',
                kind: 'task',
                phase: 'execution',
                text: `Aktuelle Marktlage recherchieren: ${DEMO_RESULT_T2}`,
                final: true,
              }),
            ]
            : []),
          ...(policy.knowledge === 'available'
            ? [
              makeEvent(runId, next(), 'inqtrix.agent.activity', {
                kind: 'reading',
                operation: 'document_analysis',
                task_id: 't4',
                status: 'completed',
                detail: 'KI-Verordnung (EU) 2024/1689',
                current: 32,
                total: 32,
              }),
              makeEvent(runId, next(), 'inqtrix.agent.task.finished', { task_id: 't4', ordinal: 3, tool_kind: 'file_analysis', status: 'completed', result_summary: DEMO_RESULT_T4 }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-task-t4',
                kind: 'task',
                phase: 'execution',
                text: `Verordnungstext vollständig prüfen: ${DEMO_RESULT_T4}`,
                final: true,
              }),
            ]
            : []),
          demoUsedSummaryAction(runId, policy),
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'evidence', previous_phase: 'execution' }),
        ],
      },
      ...(responseForms.get(runId) === 'chat'
        ? [
          {
            delayMs: 1400,
            actions: [
              makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'synthesis', previous_phase: 'evidence' }),
              makeEvent(runId, next(), 'inqtrix.node.model_resolution', demoModelResolution('agent_answer', modelSelections.get(runId))),
              { artifacts: [answerMeta(runId, 1, 'writing')], runId, type: 'setAgentRunArtifacts' as const },
              answerDetailAction(runId, 1, 'writing'),
              makeEvent(runId, next(), 'inqtrix.agent.artifact.created', { artifact_id: `answer-${runId}`, kind: 'answer', revision: 1, updated_by: 'agent' }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-answer',
                kind: 'synthesis',
                phase: 'synthesis',
                text: 'Antwort verfasst.',
                final: true,
              }),
            ],
          },
          {
            delayMs: 1800,
            actions: [
              { artifacts: [answerMeta(runId, 2, 'ready')], runId, type: 'setAgentRunArtifacts' as const },
              answerDetailAction(runId, 2, 'ready'),
              makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', { artifact_id: `answer-${runId}`, kind: 'answer', revision: 2, updated_by: 'agent' }),
            ],
          },
        ]
        : [
          {
            delayMs: 1400,
            actions: [
              makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'synthesis', previous_phase: 'evidence' }),
              makeEvent(runId, next(), 'inqtrix.node.model_resolution', demoModelResolution('agent_synthesis', modelSelections.get(runId))),
              { artifacts: [memoMeta(runId, 1, 'writing')], runId, type: 'setAgentRunArtifacts' as const },
              memoDetailAction(runId, 1, 'writing', 1),
              makeEvent(runId, next(), 'inqtrix.agent.artifact.created', { artifact_id: `memo-${runId}`, kind: 'memo', revision: 1, updated_by: 'agent' }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-synthesis',
                kind: 'synthesis',
                phase: 'synthesis',
                text: "Ich schreibe jetzt das Memo 'Markteinschätzung: EU AI Act und der KI-Mittelstand' mit 3 Abschnitten.",
                final: true,
              }),
              // The largest section is too big for one call: a section role
              // reads its evidence and writes it, on the review's row protocol.
              makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
                tool: 'section_role', tool_call_id: 'section:1:3', invocation_id: 'section:1:3',
                args_preview: 'Gegenposition',
              }),
            ],
          },
          {
            delayMs: 2600,
            actions: [
              memoDetailAction(runId, 2, 'writing', 2),
              makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', { artifact_id: `memo-${runId}`, kind: 'memo', revision: 2, updated_by: 'agent' }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-section-1',
                kind: 'synthesis',
                phase: 'synthesis',
                text: "Abschnitt 'Aktuelle Entwicklung' geschrieben.",
                final: true,
              }),
            ],
          },
          {
            delayMs: 2600,
            actions: [
              memoDetailAction(runId, 3, 'writing', 3),
              makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
                tool: 'section_role', tool_call_id: 'section:1:3', invocation_id: 'section:1:3',
                status: 'success', result_preview: '2 Belege nachgelesen',
              }),
              makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', { artifact_id: `memo-${runId}`, kind: 'memo', revision: 3, updated_by: 'agent' }),
              makeEvent(runId, next(), 'inqtrix.agent.narration', {
                narration_id: 'n-section-2',
                kind: 'synthesis',
                phase: 'synthesis',
                text: "Abschnitt 'Gegenposition' geschrieben.",
                final: true,
              }),
            ],
          },
        ]),
      ...(reviewed
        ? [{
          delayMs: 1500,
          actions: [
            makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'critic', previous_phase: 'synthesis' }),
            // Deep: the verification pass is demo-visible as its final
            // narration, which is the outcome record's summary.
            ...(depths.get(runId) === 'deep'
              ? [
                makeEvent(runId, next(), 'inqtrix.agent.narration', {
                  narration_id: 'kernel_deep_review',
                  kind: 'synthesis',
                  phase: 'critic',
                  text: `Verifikations-Durchlauf: ${DEMO_CLEAN_REVIEW_SUMMARY[citedByOutput(runId)]}`,
                  final: true,
                }),
              ]
              : []),
          ],
        }]
        : []),
      ...(patchTargets.has(runId)
        ? [
          {
            delayMs: 1600,
            actions: [
              memoDetailAction(runId, 3, 'ready', 3),
              makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'patch', previous_phase: beforeEnd }),
              ...demoPatchActions(runId, patchTargets.get(runId) as string, next),
              makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_approval' }),
            ],
          },
        ]
        : [
          {
            delayMs: 1600,
            actions: [
              // Chat runs deliver the inline answer — a final memo write
              // here would wrongly re-attach the canvas deliverable.
              ...(responseForms.get(runId) === 'chat'
                ? []
                : [memoDetailAction(runId, 3, 'ready', 3)]),
              makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'done', previous_phase: beforeEnd }),
              makeEvent(runId, next(), 'inqtrix.agent.outcome', endOutcome(runId)),
              makeEvent(runId, next(), 'inqtrix.run.completed', {}),
            ],
          },
        ]),
    ]
  }

  /** Patch proposal built from the REAL document content (deterministic). */
  function demoPatchActions(
    runId: string,
    documentId: string,
    next: () => number,
  ): ResearchDeskAction[] {
    const markdown = getDocumentMarkdown(documentId) ?? ''
    const paragraph = markdown
      .split('\n\n')
      .map((block) => block.trim())
      .find((block) => block && !block.startsWith('#') && block.length > 60)
    const edits = [
      ...(paragraph
        ? [{
          id: 'ed_1',
          find: paragraph,
          quote_before: '',
          quote_after: '',
          position: 'replace' as const,
          text: `${paragraph}\n\nDie aktuelle Recherche bestätigt diese Einschätzung und ergänzt sie um die Konsolidierungswelle bei Compliance-Plattformen [W1].`,
          note: 'Absatz um aktuelle Recherche-Erkenntnis ergänzt.',
        }]
        : []),
      {
        id: paragraph ? 'ed_2' : 'ed_1',
        find: '',
        quote_before: '',
        quote_after: '',
        position: 'append' as const,
        text: '## Empfehlung\n\nDie Compliance-Roadmap sollte um ein Szenario für beschleunigte Erstzertifizierung ergänzt werden [W2].',
        note: 'Neuer Empfehlungsabschnitt aus dem Memo.',
      },
    ]
    const wire: AgentPatchWire = {
      patch_id: `pch-${runId}`,
      document_id: documentId,
      run_id: runId,
      source: 'agent',
      status: 'pending',
      edit_count: edits.length,
      summary:
        'Zwei präzise Änderungen: Kernabsatz aktualisiert, Empfehlungsabschnitt ergänzt.',
      revision_before: 1,
      applied_revision: null,
      created_at: Date.now() / 1000,
      decided_at: null,
      edits,
      warnings: [],
      applied_edit_ids: null,
      note: '',
      document_revision: 1,
    }
    lastDemoPatchWires.set(wire.patch_id, wire)
    lastDemoPatchEdits.set(wire.patch_id, edits)
    return [
      {
        patch: {
          patch_id: `pch-${runId}`,
          document_id: documentId,
          run_id: runId,
          source: 'agent',
          status: 'pending',
          edit_count: edits.length,
          summary: 'Zwei präzise Änderungen: Kernabsatz aktualisiert, Empfehlungsabschnitt ergänzt.',
          revision_before: 1,
          applied_revision: null,
          created_at: Date.now() / 1000,
          decided_at: null,
          edits,
          warnings: [],
          applied_edit_ids: null,
          note: '',
          document_revision: 1,
        },
        runId,
        type: 'setAgentRunPatch',
      },
      makeEvent(runId, next(), 'inqtrix.agent.patch.proposed', {
        patch_id: `pch-${runId}`,
        document_id: documentId,
        edit_count: edits.length,
      }),
      {
        approvals: [
          {
            ...demoApproval(runId),
            approval_id: `apr-patch-${runId}`,
            kind: 'patch' as const,
            subject_type: 'editor_patch',
            subject_id: `pch-${runId}`,
          },
        ],
        runId,
        type: 'setAgentRunApprovals',
      },
    ]
  }
}

function demoApproval(
  runId: string,
  inForce?: DemoRequirementParts,
): AgentApprovalWire {
  return {
    approval_id: `apr-${runId}`,
    run_id: runId,
    kind: 'plan',
    status: 'pending',
    subject_type: 'plan',
    subject_id: `plan-${runId}`,
    // The gate opens with the requirement in force, as on the server;
    // the key is absent when nothing is.
    payload: inForce && (inForce.free_text || inForce.rules.length > 0)
      ? { report_requirement: inForce }
      : {},
    decision: '',
    note: '',
    decided_by_user_id: null,
    created_at: Date.now() / 1000,
    decided_at: null,
  }
}

function demoClarification(runId: string): AgentClarificationWire {
  return {
    clarification_id: `clr-${runId}`,
    run_id: runId,
    question:
      'Sollen auch Nicht-EU-Märkte betrachtet werden? '
      + 'Welche Aspekte sind am wichtigsten?',
    options: [],
    questions: [
      {
        id: 'q1',
        prompt: 'Sollen auch Nicht-EU-Märkte betrachtet werden?',
        options: [
          { id: 'q1_o1', label: 'Nur EU', description: '' },
          {
            id: 'q1_o2',
            label: 'EU + wichtigste Drittmärkte',
            description: 'USA, UK und Schweiz zusätzlich',
          },
        ],
        multi_select: false,
      },
      {
        id: 'q2',
        prompt: 'Welche Aspekte sind am wichtigsten?',
        options: [
          { id: 'q2_o1', label: 'Pflichten', description: '' },
          { id: 'q2_o2', label: 'Fristen', description: '' },
          { id: 'q2_o3', label: 'Sanktionen', description: '' },
        ],
        multi_select: true,
      },
    ],
    answers: {},
    default_assumption: 'Nur EU',
    status: 'pending',
    answer: '',
    option_id: '',
    answered_by_user_id: null,
    created_at: Date.now() / 1000,
    answered_at: null,
  }
}
