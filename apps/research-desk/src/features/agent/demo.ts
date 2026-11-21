/**
 * Agent Desk demo simulator (the `buildDemoAskScript` pattern): plays the
 * kernel's arcs through the REAL reducer pipeline — synthetic wire events
 * via `appendApiRunEvent` for live state plus control-row actions for
 * approvals, clarifications and artifacts (rows are the truth).
 * Interactive: a run that asks at its gates parks at its question and at
 * the approval of its web search or its batch; the user's answer plays on.
 */

import type { Dispatch } from 'react'

import type { ResearchDeskAction } from '@/features/researchDesk/state'
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
} from './types'
import type { AgentOverviewSource } from './agentStatusOverview'
import type { AttachedReportsCapability } from './contextCategories'
import type { ReportRequirementCapability } from './reportRequirement'
import type {
  AgentSourcePolicy,
} from './executionPolicy'

export const DEMO_AGENT_RUN_PREFIX = 'RA-demo-'

/** Demo mirror of `capabilities.agent.tiers`, so the Stufen ladder shows
 * in the demo. */
export const DEMO_AGENT_TIERS: AgentTierCapability[] = [
  {
    id: 'schnell',
    clarification_rounds: 0,
    verify: 'labels',
  },
  {
    id: 'gruendlich',
    clarification_rounds: 1,
    verify: 'standard',
  },
  {
    id: 'tief',
    clarification_rounds: 2,
    verify: 'escalating',
  },
]

/** Demo mirror of `capabilities.agent.report_requirement`: without it the
 * demo has no rule cap and no composed ceiling, so the composer said the
 * server checks on sending while the twin checks nothing, and no cap state
 * ever showed. Kept literal on purpose, like the
 * tiers. Demo rules carry no composed size, so the size check stays
 * unchecked there — and the rule menu says so. */
export const DEMO_AGENT_REPORT_REQUIREMENT: ReportRequirementCapability = {
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

/**
 * The demo's mirror of `capabilities.agent` for the composer's run
 * overview — same values a CURRENT server publishes. Kept literal on
 * purpose: the demo simulates a server, it never imports server code.
 */
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
      schnell: { tool_calls: 30, tool_calls_ceiling: 30, steps: 96, steps_ceiling: 96 },
      normal: { tool_calls: 30, tool_calls_ceiling: 60, steps: 120, steps_ceiling: 240 },
      deep: { tool_calls: 60, tool_calls_ceiling: 120, steps: 120, steps_ceiling: 240 },
    },
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
      kernel_gated_tools: [
        'delegate_batch',
        'load_skill',
        'run_web_research',
        'web_instant',
      ],
      kernel_conditional_tools: ['search_project_knowledge'],
      kernel_always_gated: ['propose_editor_patch'],
    },
    autonomous: {
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
  responseForm: string = 'auto',
  sourcePolicy: AgentSourcePolicy = {
    web: 'available',
    knowledge: 'available',
  },
  modelSelection?: DemoModelSelection,
  agentTier: string = '',
): ResearchRunSummary {
  const effectiveSourcePolicy = { ...sourcePolicy }
  const effectiveResponseForm = responseForm === 'chat' || responseForm === 'canvas'
    ? responseForm
    : 'chat'
  return {
    access: { mode: 'owner' },
    run_id: runId,
    status,
    queue_position: null,
    question,
    stack: 'demo',
    mode: 'agent_kernel',
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
        modelSelection,
        responseForm: effectiveResponseForm,
        sourcePolicy: effectiveSourcePolicy,
        toolUseCounts: { web: 0, knowledge: 0 },
      }),
    },
    error: null,
    events_url: `/v1/runs/${runId}/events`,
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
  modelSelection,
  responseForm,
  sourcePolicy,
  toolUseCounts,
  webSearches = 0,
}: DemoExecutionBlockInput): Record<string, unknown> {
  return {
    effective_mode: 'agent_kernel',
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
    // The simulator's knowledge uses are searches: it models no document read.
    knowledge_searches: toolUseCounts.knowledge,
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
const REPORT_SECTIONS = [
  '# Markteinschätzung: EU AI Act und der KI-Mittelstand\n\n## Kernaussagen\n\nDie Anforderungen des EU AI Act treffen mittelständische Anbieter von Hochrisiko-Systemen ab August 2026 mit voller Wirkung [K1]. Die interne Analyse zeigt: Konformitätsbewertung und technische Dokumentation binden im Schnitt 15-20% der Entwicklungskapazität [K2].',
  '\n\n## Aktuelle Entwicklung\n\nDie externe Quellenlage bestätigt eine Konsolidierungswelle: kleinere Anbieter suchen Partnerschaften mit etablierten Compliance-Plattformen [W1]. Gleichzeitig entstehen neue Beratungsangebote, die die Erstzertifizierung deutlich beschleunigen [W2].',
  '\n\n## Gegenposition\n\nKritische Stimmen halten die Compliance-Last für überzeichnet: harmonisierte Normen und Sandboxes senken die realen Kosten schneller als erwartet [W3]. Diese Einschätzung widerspricht teilweise der internen Projektion [K2] — beide Szenarien sind im Risikoboard zu führen.\n\n## Offene Punkte\n\nDie Kostenwirkung der harmonisierten Normen ist noch nicht belastbar quantifiziert.',
]

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

/** The web sources of the demo. */
const DEMO_WEB_SOURCES = {
  W1: { title: 'Marktbericht Konsolidierung', url: 'https://example.com/markt-konsolidierung' },
  W2: { title: 'Zertifizierungs-Angebote 2026', url: 'https://example.com/zertifizierung' },
  W3: { title: 'Kommentar Compliance-Kosten', url: 'https://example.com/kritik' },
} as const
type DemoWebLabel = keyof typeof DEMO_WEB_SOURCES

/** What the demo search says and which source it cites for it, in the
 * order of its answer. W1 is cited at three places, so its evidence view
 * lists them instead of showing the first alone. */
const DEMO_WEB_STATEMENTS: [DemoWebLabel, string][] = [
  ['W1', 'Der Markt für KI-Compliance konsolidiert sich: Kleinere Anbieter suchen Partnerschaften mit etablierten Compliance-Plattformen.'],
  ['W2', 'Zertifizierungsstellen bauen ihre Angebote für Hochrisiko-Systeme aus.'],
  ['W1', 'Seit Anfang 2026 haben drei Plattformen Übernahmen angekündigt.'],
  ['W3', 'Ein Verbandskommentar hält die Compliance-Kosten kleiner Anbieter für unterschätzt.'],
  ['W1', 'Für den Mittelstand entstehen daraus gebündelte Angebote aus Beratung und Software.'],
]
const DEMO_WEB_QUERY_ID = 'query-demo-marktlage'
const demoWebCitationId = (index: number) => `citation-demo-${index + 1}`
const demoWebSourceId = (label: DemoWebLabel) => `source-demo-${label.toLowerCase()}`

/** The search record behind the demo's web citations, in the shape the
 * server stores: the provider answer, and for every place that cites a
 * source the position of the link and the statement it stands behind. */
export function demoWebSearchLedger(runId: string): Record<string, unknown> {
  let answer = ''
  const citations = DEMO_WEB_STATEMENTS.map(([label, statement], index) => {
    const source = DEMO_WEB_SOURCES[label]
    const link = `([${source.title}](${source.url}))`
    const start = answer.length + statement.length + 1
    answer += `${statement} ${link}\n\n`
    return {
      annotation_end: start + link.length,
      annotation_start: start,
      citation_id: demoWebCitationId(index),
      grounded_support: statement,
      mapping_status: 'provider_citation_marker',
      origin: 'provider',
      rank: Object.keys(DEMO_WEB_SOURCES).indexOf(label) + 1,
      source_id: demoWebSourceId(label),
      title: source.title,
      url: source.url,
    }
  })
  return {
    kind: 'web_search_ledger',
    schema_version: 1,
    searches: {
      [DEMO_WEB_QUERY_ID]: {
        citations,
        duration_ms: 4200,
        invocation_id: DEMO_WEB_QUERY_ID,
        provider: 'Demo-Websuche',
        provider_answer: answer.trimEnd(),
        query: 'Wie entwickelt sich der Markt für KI-Compliance im Mittelstand seit Anfang 2026?',
        query_id: DEMO_WEB_QUERY_ID,
        source_run_id: runId,
        status: 'completed',
      },
    },
  }
}

/** A web reference of the demo with the pointers into its search record. */
export const demoWebRefWire = (label: DemoWebLabel, runId: string) => {
  const places = DEMO_WEB_STATEMENTS.flatMap(([cited, statement], index) => (
    cited === label ? [{ citationId: demoWebCitationId(index), statement }] : []
  ))
  return {
    ...DEMO_WEB_SOURCES[label],
    citation_id: places[0].citationId,
    citation_ids: places.map((place) => place.citationId),
    grounded_support: places[0].statement,
    label,
    query_id: DEMO_WEB_QUERY_ID,
    query_ids: [DEMO_WEB_QUERY_ID],
    source_id: demoWebSourceId(label),
    source_run_id: runId,
  }
}

/** The evidence bundle of a demo run: it carries the search record the
 * evidence view of a web citation reads. */
function demoEvidenceBundleAction(runId: string): ResearchDeskAction {
  return {
    artifact: {
      artifact_id: `evidence-${runId}`,
      content_markdown: '',
      created_at: Date.now() / 1000,
      kind: 'evidence_bundle',
      payload: { web_search_ledger: demoWebSearchLedger(runId) },
      refs: [],
      refs_count: 0,
      revision: 1,
      revisions: [{ revision: 1, created_by: 'agent', created_at: Date.now() / 1000, status: 'ready' }],
      run_id: runId,
      session_id: null,
      status: 'ready',
      title: 'Belege',
      updated_at: Date.now() / 1000,
      updated_by: 'agent',
    },
    runId,
    type: 'setAgentRunArtifactDetail',
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
        demoWebRefWire('W1', runId),
        demoWebRefWire('W2', runId),
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

/** The report the demo writes into the canvas: the output form of the
 * Stufe Tief, and of a run that asks for a canvas. */
const REPORT_TITLE = 'Markteinschätzung: EU AI Act und der KI-Mittelstand'

function reportMeta(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
): AgentArtifactMetaWire {
  return {
    artifact_id: `deliverable-${runId}`,
    run_id: runId,
    session_id: null,
    kind: 'deliverable',
    title: REPORT_TITLE,
    status,
    revision,
    updated_by: 'agent',
    refs_count: 5,
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  }
}

function reportDetailAction(
  runId: string,
  revision: number,
  status: 'writing' | 'ready',
  sections: number,
): ResearchDeskAction {
  return {
    artifact: {
      ...reportMeta(runId, revision, status),
      content_markdown: REPORT_SECTIONS.slice(0, sections).join(''),
      refs: [
        demoKnowledgeRefWire('K1'),
        demoKnowledgeRefWire('K2'),
        demoWebRefWire('W1', runId),
        demoWebRefWire('W2', runId),
        demoWebRefWire('W3', runId),
      ],
      revisions: Array.from({ length: revision }, (_, index) => ({
        revision: index + 1,
        created_by: 'agent',
        created_at: Date.now() / 1000,
        // The report streams like the real one: only the last flush of a
        // pass is a version the reader can pick.
        status: index + 1 < revision ? 'writing' : 'ready',
      })),
    },
    runId,
    type: 'setAgentRunArtifactDetail',
  }
}

/** The kernel writes its report into the canvas (`write_canvas`) in two
 * flushes, with the evidence bundle its citations open on. */
function demoReportSteps(runId: string, next: () => number): DemoStep[] {
  return [
    {
      delayMs: 1300,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
          tool: 'write_canvas', tool_call_id: `demo-canvas-${runId}`, args_preview: REPORT_TITLE,
        }),
        { artifacts: [reportMeta(runId, 1, 'writing')], runId, type: 'setAgentRunArtifacts' },
        reportDetailAction(runId, 1, 'writing', 1),
        demoEvidenceBundleAction(runId),
        makeEvent(runId, next(), 'inqtrix.agent.artifact.created', {
          artifact_id: `deliverable-${runId}`, kind: 'deliverable', revision: 1, updated_by: 'agent',
        }),
      ],
    },
    {
      delayMs: 1500,
      actions: [
        { artifacts: [reportMeta(runId, 2, 'ready')], runId, type: 'setAgentRunArtifacts' },
        reportDetailAction(runId, 2, 'ready', REPORT_SECTIONS.length),
        makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', {
          artifact_id: `deliverable-${runId}`, kind: 'deliverable', revision: 2, updated_by: 'agent',
        }),
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'write_canvas', tool_call_id: `demo-canvas-${runId}`, status: 'success',
        }),
      ],
    },
  ]
}

/**
 * The kernel's deep lane (Stufe Tief), after its batch: it writes its report
 * into the canvas (unless the run asked for the chat, which comes before the
 * Stufe's default), shows the answer while its review ROLE re-reads the
 * evidence, and ends with the reviewed answer and its outcome record. Its
 * first step takes the run up (run started, execution), so a lane that
 * follows the batch drops those two events. Pure: returns the steps, so the
 * parity test can inspect every payload without timers.
 */
export function demoDeepLane(
  runId: string,
  next: () => number,
  initialSummary: ResearchRunSummary,
  canvas = true,
): DemoStep[] {
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
        { content: canvas ? 'Bericht im Canvas schreiben' : 'Antwort im Chat schreiben', status: statuses[1] },
        { content: canvas ? 'Kurzfazit mit Dokumentverweis formulieren' : 'Belege der Antwort prüfen', status: statuses[2] },
      ],
    })
  const waiting = { ...initialSummary, status: 'waiting_for_children' as const }
  return [
    {
      delayMs: 300,
      actions: [
        makeEvent(runId, next(), 'inqtrix.run.started', {}),
        makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution' }),
        todos(['completed', 'in_progress', 'pending']),
      ],
    },
    ...(canvas ? demoReportSteps(runId, next) : []),
    { delayMs: 300, actions: [todos(['completed', 'completed', 'in_progress'])] },
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
        {
          artifacts: canvas
            ? [reportMeta(runId, 2, 'ready'), answerMeta(runId, 1, 'ready')]
            : [answerMeta(runId, 1, 'ready')],
          runId,
          type: 'setAgentRunArtifacts',
        },
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
            text: 'Die Kostenwirkung der harmonisierten Normen ist nur über eine Sekundärquelle belegt; die Primärquelle war nicht lesbar.',
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

export type AgentDemoActions = {
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
      approval_scope?: 'once' | 'run'
    },
  ) => Promise<unknown>
  submit: (input: {
    autonomy: string
    question: string
    sessionId: string
    /** Output form: 'chat' plays the inline-answer variant. */
    responseForm?: string
    /** Attached rule ids, resolved like the server resolves them (from
     * the project's own rules) and recorded with the same event. */
    ruleIds?: string[]
    sourcePolicy?: AgentSourcePolicy
    /** Thoroughness: 'deep' plays the verification narration
     * and stamps the Deep badge onto the run summary. */
    depth?: string
    /** Selected Stufe: echoed as agent_overrides.agent_tier and played as
     * that Stufe (its clarification rounds, its review, and the deep lane
     * for Tief). */
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
 * `resolveRule` stands where the server reads its rule catalog: it
 * resolves an attached rule id to its label and revision, or `null` for
 * one it does not know.
 */
export function createAgentDemo(
  dispatch: Dispatch<ResearchDeskAction>,
  resolveRule: DemoRuleResolver = () => null,
): AgentDemoActions {
  const runtime: DemoRuntime = { dispatch, timeouts: new Set() }
  const responseForms = new Map<string, string>()
  const sourcePolicies = new Map<string, AgentSourcePolicy>()
  const acceptedSummaries = new Map<string, ResearchRunSummary>()
  // Runs whose Stufe skips the review — same closure rule as `depths`.
  const waivedReviews = new Set<string>()
  // Model-picker selection per run — same closure rule as `depths`.
  const modelSelections = new Map<
    string,
    { model: string | null; effort: string | null; tier: string | null }
  >()
  // The tool gate of a run that asks at its gates (its web search, the
  // batch of subtasks of a deep run): the run HOLDS
  // the rest of its execution until the tool approval is decided; the
  // resume thunk is played by decideApproval's apr-tool branch.
  const heldToolGate = new Map<
    string,
    (decision: {
      decision: string
      actions?: { tool: string; args: Record<string, unknown> }[]
      approval_scope?: 'once' | 'run'
    }) => void
  >()
  // Runs whose quick web search was rejected at its gate: the search is
  // skipped, the run goes on without it.
  const refusedQuickWeb = new Set<string>()

  const submit: AgentDemoActions['submit'] = ({
    autonomy,
    question,
    sessionId,
    responseForm,
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
    if (demoTierWaivesReview(agentTier)) waivedReviews.add(runId)
    if (modelSelection) modelSelections.set(runId, modelSelection)
    // The kernel answers in the chat unless the run asks for a canvas.
    responseForms.set(runId, responseForm === 'canvas' ? 'canvas' : 'chat')
    const initialSummary = demoRunSummary(
      runId,
      sessionId,
      question,
      autonomy,
      'running',
      depth,
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
      // Recorded once, when the run starts — the server's own event.
      dispatch(makeEvent(runId, next(), 'inqtrix.agent.requirement.attached', {
        free_text_chars: 0,
        // Like the server: the kernel's review judges each attached rule.
        review: demoTierWaivesReview(agentTier) ? 'none' : 'per_rule',
        rules,
      }))
    }
    if (agentTier === 'tief' || depth === 'deep') {
      // Stufe Tief: the kernel delegates a batch of subtasks, analyses a
      // whole document with project knowledge, writes its report into the
      // canvas and lets its review role re-read the evidence. The batch
      // follows the source switches of the run; with both sources off
      // nothing is left to delegate.
      const batch = demoBatchAction(effectiveSourcePolicy)
      if (!batch) {
        playSteps(runtime, demoDeepLane(runId, next, initialSummary, responseForm !== 'chat'))
        return
      }
      // With project knowledge the run analyses a whole document after its
      // batch; only the strict mode would ask first, and it does not play
      // here. The lane's steps are built only when they play: their events
      // take their place after the batch's.
      const analysis = () => (effectiveSourcePolicy.knowledge === 'available' && autonomy !== 'strict'
        ? demoDocumentAnalysis(runId, next)
        : [])
      const lane = () => withoutResumeEvents(
        demoDeepLane(runId, next, initialSummary, responseForm !== 'chat'),
      )
      if (autonomy === 'autonomous') {
        playSteps(runtime, [
          ...demoBatchStart(runId, next),
          ...demoBatchSettled(runId, next, initialSummary, batch, true),
          ...analysis(),
          ...lane(),
        ])
        return
      }
      // A run that asks at its gates parks at the approval of the batch.
      playSteps(runtime, [...demoBatchStart(runId, next), demoBatchGate(runId, next, batch)])
      heldToolGate.set(runId, (decision) => {
        const rejected = decision.decision === 'reject'
        const status = rejected ? 'rejected' : decision.decision === 'edit' ? 'edited' : 'approved'
        dispatch({
          approvals: [{
            ...demoBatchApproval(runId, batch),
            status,
            decision: decision.decision,
            decision_payload: toolDecisionPayload(decision),
            decided_at: Date.now() / 1000,
          }],
          runId,
          type: 'setAgentRunApprovals',
        })
        playSteps(runtime, [
          {
            delayMs: 200,
            actions: [
              makeEvent(runId, next(), 'inqtrix.agent.approval.decided', { approval_id: `apr-tool-${runId}`, status }),
              makeEvent(runId, next(), 'inqtrix.run.started', {}),
            ],
          },
          ...demoBatchSettled(runId, next, initialSummary, batch, !rejected),
          ...analysis(),
          ...lane(),
        ])
      })
      return
    }
    // Schnell and Gründlich: the kernel answers in the chat (or into the
    // canvas, when the run asks for one). A run that asks at its gates puts
    // the question of its Stufe to the user first (`ask_user`; Schnell asks
    // none) and asks before its web search; a run that decides for itself
    // goes straight to its searches and its answer.
    const asks = autonomy !== 'autonomous'
    const tier = DEMO_AGENT_TIERS.find((entry) => entry.id === agentTier)
    const start: DemoStep = {
      delayMs: 400,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'intake' }),
        makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution', previous_phase: 'intake' }),
      ],
    }
    if (asks && (tier?.clarification_rounds ?? 1) > 0) {
      playSteps(runtime, [
        start,
        {
          delayMs: 900,
          actions: [
            { clarifications: [demoClarification(runId)], runId, type: 'setAgentRunClarifications' },
            makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_input' }),
          ],
        },
      ])
      return
    }
    if (asks && effectiveSourcePolicy.web === 'available') {
      playSteps(runtime, [start, webGate(runId, next)])
      holdWebGate(runId, next)
      return
    }
    playSteps(runtime, [start, ...kernelAnswerSteps(runId, next)])
  }

  const decideApproval: AgentDemoActions['decideApproval'] = (
    runId,
    approvalId,
    decision,
  ) => {
    // Every gate of a kernel run is a tool gate: the held call resumes
    // (approve/edit) or settles as refused — all choreography lives in the
    // thunk its run stored.
    if (approvalId.startsWith('apr-tool-')) {
      const resume = heldToolGate.get(runId)
      heldToolGate.delete(runId)
      resume?.(decision)
    }
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
    const next = () => (seq += 1)
    const answered: DemoStep = {
      delayMs: 100,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.clarification.answered', {
          clarification_id: clarificationId,
        }),
        // The run takes its execution up once, here, before its web gate.
        makeEvent(runId, next(), 'inqtrix.run.started', {}),
      ],
    }
    if ((sourcePolicies.get(runId)?.web ?? 'available') !== 'available') {
      playSteps(runtime, [answered, ...kernelAnswerSteps(runId, next)])
      return Promise.resolve()
    }
    playSteps(runtime, [answered, webGate(runId, next)])
    holdWebGate(runId, next)
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

  return {
    answerClarification,
    cancel,
    decideApproval,
    dispose: () => {
      for (const id of runtime.timeouts) window.clearTimeout(id)
      runtime.timeouts.clear()
    },
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
    used: { web: number; knowledge: number },
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
          tool_use_counts: { web: used.web, knowledge: used.knowledge },
          web_searches: used.web,
        },
      },
    }
    acceptedSummaries.set(runId, updated)
    return { select: true, summary: updated, type: 'upsertAgentRunSummary' }
  }

  /** The run parks at the approval of its web search, which shows the
   * question and the form the search is asked to answer in. */
  function webGate(runId: string, next: () => number): DemoStep {
    return {
      delayMs: 700,
      actions: [
        { approvals: [demoToolApproval(runId)], runId, type: 'setAgentRunApprovals' },
        makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_approval' }),
      ],
    }
  }

  /** The decision on the web gate: approved or edited, the search runs;
   * rejected, the run answers without it. */
  function holdWebGate(runId: string, next: () => number): void {
    heldToolGate.set(runId, (decision) => {
      const rejected = decision.decision === 'reject'
      const status = rejected
        ? 'rejected'
        : decision.decision === 'edit' ? 'edited' : 'approved'
      dispatch({
        approvals: [
          {
            ...demoToolApproval(runId),
            status,
            decision: decision.decision,
            decision_payload: toolDecisionPayload(decision),
            decided_at: Date.now() / 1000,
          },
        ],
        runId,
        type: 'setAgentRunApprovals',
      })
      if (rejected) refusedQuickWeb.add(runId)
      playSteps(runtime, [
        {
          delayMs: 200,
          actions: [
            makeEvent(runId, next(), 'inqtrix.agent.approval.decided', {
              approval_id: `apr-tool-${runId}`,
              status,
            }),
          ],
        },
        ...kernelAnswerSteps(runId, next),
      ])
    })
  }

  /**
   * The kernel's work after its gates: one search in the project knowledge
   * and one web search where the run has those sources (a web search
   * rejected at its gate is skipped), the report into the canvas when the
   * run asks for one, then the answer, the outcome record and the end.
   */
  function kernelAnswerSteps(runId: string, next: () => number): DemoStep[] {
    const policy = sourcePolicies.get(runId)
      ?? { web: 'available', knowledge: 'available' }
    const knowledge = policy.knowledge === 'available'
    const searched = policy.web === 'available' && !refusedQuickWeb.has(runId)
    const canvas = responseForms.get(runId) === 'canvas'
    const tool = (name: string, callId: string, preview: string): DemoStep[] => [
      {
        delayMs: 500,
        actions: [
          makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
            tool: name, tool_call_id: callId, args_preview: preview,
          }),
        ],
      },
      {
        delayMs: 1300,
        actions: [
          makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
            tool: name, tool_call_id: callId, status: 'success',
          }),
        ],
      },
    ]
    return [
      ...(knowledge
        ? tool('search_project_knowledge', `call-knowledge-${runId}`, 'Pflichten für Hochrisiko-Systeme im EU AI Act')
        : []),
      ...(searched
        ? tool('web_instant', `call-web-${runId}`, DEMO_QUICK_WEB_ACTION.args.query)
        : []),
      ...(canvas ? demoReportSteps(runId, next) : []),
      {
        delayMs: 900,
        actions: [
          demoUsedSummaryAction(runId, { web: searched ? 1 : 0, knowledge: knowledge ? 1 : 0 }),
          makeEvent(runId, next(), 'inqtrix.node.model_resolution', demoModelResolution('agent_kernel', modelSelections.get(runId))),
          { artifacts: [answerMeta(runId, 1, 'writing')], runId, type: 'setAgentRunArtifacts' as const },
          answerDetailAction(runId, 1, 'writing'),
          ...(canvas ? [] : [demoEvidenceBundleAction(runId)]),
          makeEvent(runId, next(), 'inqtrix.agent.artifact.created', { artifact_id: `answer-${runId}`, kind: 'answer', revision: 1, updated_by: 'agent' }),
        ],
      },
      {
        delayMs: 1600,
        actions: [
          {
            artifacts: [...(canvas ? [reportMeta(runId, 2, 'ready')] : []), answerMeta(runId, 2, 'ready')],
            runId,
            type: 'setAgentRunArtifacts' as const,
          },
          answerDetailAction(runId, 2, 'ready'),
          makeEvent(runId, next(), 'inqtrix.agent.artifact.updated', { artifact_id: `answer-${runId}`, kind: 'answer', revision: 2, updated_by: 'agent' }),
          makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'done', previous_phase: 'execution' }),
          makeEvent(runId, next(), 'inqtrix.agent.outcome', endOutcome(runId)),
          makeEvent(runId, next(), 'inqtrix.run.completed', {}),
        ],
      },
    ]
  }
}

/** What the server stores with a tool-gate decision: an edit its own
 * arguments, a run-wide approval its scope (`approval_scope: 'run'`). */
function toolDecisionPayload(decision: {
  decision: string
  actions?: { tool: string; args: Record<string, unknown> }[]
  approval_scope?: 'once' | 'run'
}): Record<string, unknown> {
  return {
    ...(decision.decision === 'edit' && decision.actions ? { actions: decision.actions } : {}),
    ...(decision.approval_scope === 'run' ? { approval_scope: 'run' } : {}),
  }
}

/** The document analysis a deep demo run with project knowledge runs after
 * its batch of subtasks, before it writes its report: the tool row with what the analysis is for, then one live row
 * per document that counts its windows. The server sends the first and every
 * fifth processed window and the settled row; the demo shows three of them. */
export const DEMO_ANALYSIS_CALL = 'demo-analysis-1'
export const DEMO_ANALYSIS_OBJECTIVE =
  'Pflichten für Betreiber von Hochrisiko-Systemen aus der KI-Verordnung herausziehen'
const DEMO_ANALYSIS_DOCUMENT = { id: 'kd-demo-ki-verordnung', title: 'KI-Verordnung (EU) 2024/1689', windows: 32 }

function demoAnalysisRow(runId: string, sequence: number, status: 'started' | 'completed', current: number): ResearchDeskAction {
  return makeEvent(runId, sequence, 'inqtrix.agent.activity', {
    scope: 'run',
    phase: 'execution',
    kind: 'reading',
    operation: 'document_analysis',
    activity_id: `${DEMO_ANALYSIS_CALL}:document-analysis:${DEMO_ANALYSIS_DOCUMENT.id}`,
    status,
    detail: DEMO_ANALYSIS_DOCUMENT.title,
    document_id: DEMO_ANALYSIS_DOCUMENT.id,
    current,
    total: DEMO_ANALYSIS_DOCUMENT.windows,
  })
}

export function demoDocumentAnalysis(runId: string, next: () => number): DemoStep[] {
  return [
    {
      delayMs: 400,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
          tool: 'analyze_document', tool_call_id: DEMO_ANALYSIS_CALL, invocation_id: DEMO_ANALYSIS_CALL,
          args_preview: DEMO_ANALYSIS_OBJECTIVE,
        }),
        demoAnalysisRow(runId, next(), 'started', 1),
      ],
    },
    { delayMs: 900, actions: [demoAnalysisRow(runId, next(), 'started', 15)] },
    { delayMs: 900, actions: [demoAnalysisRow(runId, next(), 'started', 30)] },
    {
      delayMs: 700,
      actions: [
        demoAnalysisRow(runId, next(), 'completed', DEMO_ANALYSIS_DOCUMENT.windows),
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'analyze_document', tool_call_id: DEMO_ANALYSIS_CALL, invocation_id: DEMO_ANALYSIS_CALL,
          status: 'success',
        }),
      ],
    },
  ]
}

/** Drop the two events that take a parked run up again (run started,
 * phase changed) from a step sequence that begins with them: for a run
 * that never parked, or that took its execution up before these steps. */
function withoutResumeEvents(steps: DemoStep[]): DemoStep[] {
  return steps.map((step, index) => (
    index === 0 ? { ...step, actions: step.actions.slice(2) } : step
  ))
}

/** The batch of subtasks a deep demo run delegates first, with
 * both sources switched on: one question to the project knowledge and one
 * web research with a time window, so its gate shows both lines a batch can
 * carry — the collections a knowledge assignment searches and the window a
 * research prefers. */
export const DEMO_BATCH_ACTION = {
  tool: 'delegate_batch',
  args: {
    assignments: [
      {
        objective: 'Welche Pflichten nennen unsere Unterlagen zum EU AI Act für Anbieter von Hochrisiko-Systemen?',
        mode: 'knowledge',
      },
      {
        objective: 'Welche Leitlinien zum EU AI Act hat die EU-Kommission zuletzt veröffentlicht?',
        mode: 'research',
        recency: 'month',
      },
    ],
  },
}

type DemoBatchAction = typeof DEMO_BATCH_ACTION

/** The collections the demo run may search, as its gate names them. */
export const DEMO_BATCH_KNOWLEDGE_SCOPE = ['EU-Recht']

const DEMO_BATCH_CALL = 'demo-batch-1'

/** What the child of an assignment is called and what it hands back. */
const DEMO_BATCH_CHILD: Record<string, { name: string; found: string }> = {
  knowledge: {
    name: 'wissen',
    found: 'Anbieter müssen ein Risikomanagement führen, die technische Dokumentation pflegen und Vorfälle melden. [K1]',
  },
  research: {
    name: 'recherche',
    found: 'Die Kommission hat im letzten Monat Leitlinien zur Kennzeichnungspflicht veröffentlicht. [W1]',
  },
}

/** The batch under the source switches of the run: an assignment whose
 * source is switched off is not delegated, as the server would refuse it.
 * `null` where nothing is left to delegate. */
export function demoBatchAction(policy: { web: string; knowledge: string }): DemoBatchAction | null {
  const assignments = DEMO_BATCH_ACTION.args.assignments.filter((assignment) => (
    (assignment.mode === 'knowledge' ? policy.knowledge : policy.web) === 'available'
  ))
  return assignments.length > 0 ? { tool: 'delegate_batch', args: { assignments } } : null
}

function demoBatchApproval(runId: string, action: DemoBatchAction): AgentApprovalWire {
  // The gate names the collections only where the batch asks them.
  const asksKnowledge = action.args.assignments.some((assignment) => assignment.mode === 'knowledge')
  return {
    ...demoApprovalBase(runId),
    subject_id: DEMO_BATCH_CALL,
    payload: {
      actions: [action],
      ...(asksKnowledge ? { knowledge_scope: DEMO_BATCH_KNOWLEDGE_SCOPE } : {}),
    },
  }
}

/** A deep kernel run takes up its work and calls the batch. */
function demoBatchStart(runId: string, next: () => number): DemoStep[] {
  return [{
    delayMs: 300,
    actions: [
      makeEvent(runId, next(), 'inqtrix.run.started', {}),
      makeEvent(runId, next(), 'inqtrix.agent.phase.changed', { phase: 'execution' }),
      makeEvent(runId, next(), 'inqtrix.agent.tool.started', {
        tool: 'delegate_batch', tool_call_id: DEMO_BATCH_CALL, args_preview: '',
      }),
    ],
  }]
}

/** The run parks at the approval of its batch. */
function demoBatchGate(runId: string, next: () => number, action: DemoBatchAction): DemoStep {
  return {
    delayMs: 600,
    actions: [
      { approvals: [demoBatchApproval(runId, action)], runId, type: 'setAgentRunApprovals' },
      makeEvent(runId, next(), 'inqtrix.agent.approval.requested', {
        approval_id: `apr-tool-${runId}`, kind: 'tool',
      }),
      makeEvent(runId, next(), 'inqtrix.run.waiting', { status: 'waiting_for_approval' }),
    ],
  }
}

/**
 * How the batch ends. Approved, each assignment runs as a child of the
 * call — joined to its row by the call id and its place — and hands back
 * one result line; refused, the call settles as a failed step and no child
 * starts. A knowledge child and a research child report no tasks: their
 * line says that they work, and their result says the rest.
 */
function demoBatchSettled(
  runId: string,
  next: () => number,
  initialSummary: ResearchRunSummary,
  action: DemoBatchAction,
  approved: boolean,
): DemoStep[] {
  if (!approved) {
    return [{
      delayMs: 300,
      actions: [
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'delegate_batch', tool_call_id: DEMO_BATCH_CALL, status: 'error',
        }),
      ],
    }]
  }
  const children = action.args.assignments.map((assignment, place) => ({
    assignment, place, ...DEMO_BATCH_CHILD[assignment.mode],
  }))
  const progress = (place: number, name: string, data: Record<string, unknown>, runStatus = 'running') =>
    makeEvent(runId, next(), 'inqtrix.agent.child.progress', {
      task_id: `${DEMO_BATCH_CALL}:${place}`,
      child_run_id: `${runId}-${name}`,
      run_status: runStatus,
      ...data,
    })
  return [
    {
      delayMs: 400,
      actions: [
        ...children.flatMap(({ place, name }) => [
          progress(place, name, { event_type: 'inqtrix.run.queued' }, 'queued'),
          progress(place, name, { event_type: 'inqtrix.run.started' }),
        ]),
        { select: true, summary: { ...initialSummary, status: 'waiting_for_children' as const }, type: 'upsertAgentRunSummary' },
      ],
    },
    ...children.map(({ assignment, place, name }) => (assignment.mode === 'knowledge'
      ? {
        delayMs: 1500,
        actions: [
          progress(place, name, {
            event_type: 'inqtrix.knowledge.grounding.checked', status: 'verified',
            metrics: { quotes_total: 4, quotes_verified: 4 },
          }),
          progress(place, name, {
            event_type: 'inqtrix.run.completed', current_node: 'knowledge', message: 'completed',
            snapshot: { current_node: 'knowledge', total_queries: 1, total_citations: 8 },
          }, 'completed'),
        ],
      }
      : {
        delayMs: 1300,
        actions: [
          progress(place, name, {
            event_type: 'inqtrix.run.completed', message: 'completed',
            snapshot: { total_queries: 4, executed_queries: 4, total_citations: 6 },
          }, 'completed'),
        ],
      })),
    {
      delayMs: 200,
      actions: [
        { select: true, summary: { ...initialSummary, status: 'running' as const }, type: 'upsertAgentRunSummary' },
        ...children.map(({ assignment, name, found }) => makeEvent(runId, next(), 'inqtrix.agent.narration', {
          narration_id: `n-child-${runId}-${name}`, kind: 'task', phase: 'execution',
          text: `${assignment.objective}: ${found}`, final: true,
        })),
        makeEvent(runId, next(), 'inqtrix.agent.tool.finished', {
          tool: 'delegate_batch', tool_call_id: DEMO_BATCH_CALL, status: 'success',
        }),
      ],
    },
  ]
}

/** The quick web search a demo run asks approval for. It names a form for
 * the answer, so the gate shows what the search is asked for and its edit
 * offers the forms as a choice. */
export const DEMO_QUICK_WEB_ACTION = {
  tool: 'web_instant',
  args: {
    query:
      'Welche Kritik an den Compliance-Kosten des EU AI Act für den Mittelstand '
      + 'wurde seit Anfang 2026 veröffentlicht?',
    answer_form: 'timeline',
  },
}

function demoToolApproval(runId: string): AgentApprovalWire {
  return {
    ...demoApprovalBase(runId),
    subject_id: `call-web-${runId}`,
    payload: { actions: [DEMO_QUICK_WEB_ACTION] },
  }
}

function demoApprovalBase(runId: string): AgentApprovalWire {
  return {
    approval_id: `apr-tool-${runId}`,
    run_id: runId,
    kind: 'tool',
    status: 'pending',
    subject_type: 'tool',
    subject_id: '',
    payload: {},
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
