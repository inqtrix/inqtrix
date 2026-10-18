import type {
  KnowledgeDocumentText,
  KnowledgeProfileManifestEntry,
  KnowledgeSearchHit,
  ResearchRunEvent,
} from '@/features/researchRuns/types'
import type {
  KnowledgeAnswerRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import { searchTermsFromQuery } from './highlight'
import type { KnowledgeDataSource } from './types'

/**
 * Demo data for the knowledge workspace: a small German corpus tied to
 * the seeded vector indexes, a profile manifest shaped exactly like
 * `capabilities.knowledge.profiles`, a scripted ask (event sequence +
 * canned cited answer) and a local search — so the whole Ask/Find/Read
 * triad is demonstrable without a backend.
 */

type DemoDocument = {
  id: string
  /** Local vector-index id acting as the collection in demo mode. */
  collectionId: string
  title: string
  text: string
}

const DEMO_QUOTE_ARTICLE_6 = 'Ein KI-System gilt als Hochrisiko-KI-System, wenn es als Sicherheitsbauteil eines unter die Harmonisierungsrechtsvorschriften fallenden Produkts verwendet wird oder selbst ein solches Produkt ist.'
const DEMO_QUOTE_ANNEX_III = 'Anhang III nennt unter anderem KI-Systeme in den Bereichen Beschäftigung, Personalmanagement und Zugang zu wesentlichen privaten und öffentlichen Diensten.'

// The cited chunk each citation came from — the demo "Beleg" tab + hover preview
// render these with the quote (above) highlighted in context, so the signature
// citation experience is visible without a backend.
const DEMO_EXCERPT_ARTICLE_6 = `Artikel 6 — Einstufungsvorschriften für Hochrisiko-KI-Systeme. ${DEMO_QUOTE_ARTICLE_6} Die Einstufung löst die Konformitätsbewertung nach Artikel 43 aus.`
const DEMO_EXCERPT_ANNEX_III = `${DEMO_QUOTE_ANNEX_III} Damit fallen Recruiting- und Personalauswahlsysteme ausdrücklich in den Hochrisiko-Bereich.`
// A SECOND passage from the same document as K1 (the Volltext) so the demo shows
// citations grouped by document (one source, multiple cited passages).
const DEMO_QUOTE_ARTICLE_43 = 'Vor dem Inverkehrbringen eines Hochrisiko-KI-Systems ist eine Konformitätsbewertung nach Artikel 43 durchzuführen.'
const DEMO_EXCERPT_ARTICLE_43 = `Artikel 43 — Konformitätsbewertung. ${DEMO_QUOTE_ARTICLE_43} Sie umfasst das Qualitätsmanagement und die technische Dokumentation.`

export const DEMO_DOCUMENTS: DemoDocument[] = [
  {
    collectionId: 'vector-index-eu-recht',
    id: 'kdoc-ai-act-volltext',
    text: [
      'Artikel 6 — Einstufungsvorschriften für Hochrisiko-KI-Systeme.',
      '',
      DEMO_QUOTE_ARTICLE_6,
      'Zusätzlich gelten KI-Systeme als hochriskant, wenn sie in einem der in Anhang III aufgeführten Bereiche eingesetzt werden und ein erhebliches Risiko für Gesundheit, Sicherheit oder Grundrechte darstellen.',
      '',
      'Artikel 7 erlaubt der Kommission, die Liste in Anhang III durch delegierte Rechtsakte zu ändern, sofern neue Anwendungsfälle ein vergleichbares Risiko aufweisen.',
      'Anbieter können eine Ausnahme nach Artikel 6 Absatz 3 dokumentieren, wenn das System keine wesentliche Entscheidungsfindung beeinflusst.',
    ].join('\n'),
    title: 'EU-AI-Act-Volltext.pdf',
  },
  {
    collectionId: 'vector-index-eu-recht',
    id: 'kdoc-ai-act-annex',
    text: [
      'Anhang III — Hochrisiko-KI-Systeme gemäß Artikel 6 Absatz 2.',
      '',
      DEMO_QUOTE_ANNEX_III,
      'Dazu zählen biometrische Identifizierung, kritische Infrastruktur, allgemeine und berufliche Bildung sowie Strafverfolgung.',
      '',
      'Für jeden Bereich gilt: Die Einstufung hängt von der konkreten Zweckbestimmung des Systems ab, nicht von der eingesetzten Technologie.',
    ].join('\n'),
    title: 'AI-Act-Annex-III.pdf',
  },
  {
    collectionId: 'vector-index-eu-recht',
    id: 'kdoc-bsi-kriterien',
    text: [
      'BSI-Kriterienkatalog für KI-Systeme — Auszug.',
      '',
      'Der Katalog beschreibt Prüfkriterien für Robustheit, Transparenz und Datenqualität von KI-Systemen im behördlichen Einsatz.',
      'Für Hochrisiko-Anwendungen empfiehlt das BSI eine dokumentierte Risikoanalyse je Lebenszyklusphase sowie kontinuierliches Monitoring im Betrieb.',
    ].join('\n'),
    title: 'BSI-Kriterienkatalog-KI.pdf',
  },
  {
    collectionId: 'vector-index-anbieter',
    id: 'kdoc-perplexity-datenblatt',
    text: [
      'Perplexity Enterprise — Datenblatt (Auszug).',
      '',
      'Enterprise-Pläne bieten SSO, Audit-Logs und regionale Datenhaltung. Suchanfragen werden standardmäßig nicht für das Training verwendet.',
      'API-Zugriff erfolgt über tokenbasierte Authentifizierung mit Ratenlimits pro Workspace.',
    ].join('\n'),
    title: 'Perplexity-Enterprise-Datenblatt.pdf',
  },
  {
    collectionId: 'vector-index-anbieter',
    id: 'kdoc-azure-foundry',
    text: [
      'Azure AI Foundry — Web Search Grounding (Auszug).',
      '',
      'Grounding with Bing Search liefert zitierfähige Webtreffer in Agent-Workflows. Die Abrechnung erfolgt pro tausend Transaktionen.',
      'EU-Datenresidenz ist für ausgewählte Regionen verfügbar; Compliance-Nachweise liegen im Trust Center.',
    ].join('\n'),
    title: 'Azure-Foundry-WebSearch.pdf',
  },
]

/** Mirrors the backend manifest shape so the picker exercises the same
 * pure builder in demo mode (render-only-from-manifest stays honest). */
export const DEMO_KNOWLEDGE_PROFILE_MANIFEST: KnowledgeProfileManifestEntry[] = [
  {
    degraded: [],
    final_k_factor: 1,
    id: 'schnell',
    stages: { decompose: false, gate_rounds: 0, grounding: false, rerank: false, report: false, vocabulary_bridge: false },
  },
  {
    degraded: [],
    final_k_factor: 1,
    id: 'standard',
    stages: { decompose: false, gate_rounds: 0, grounding: true, rerank: true, report: false, vocabulary_bridge: false },
  },
  {
    degraded: ['rerank'],
    final_k_factor: 1,
    id: 'gruendlich',
    stages: { decompose: false, gate_rounds: 2, grounding: true, rerank: false, report: false, vocabulary_bridge: true },
  },
  {
    degraded: [],
    final_k_factor: 2,
    id: 'tief',
    stages: { decompose: true, gate_rounds: 2, grounding: true, rerank: true, report: true, vocabulary_bridge: true },
  },
  {
    delegates_to: ['schnell', 'standard', 'gruendlich'],
    id: 'auto',
  },
]

export const DEMO_KNOWLEDGE_DEFAULT_PROFILE = 'tief'
export const DEMO_KNOWLEDGE_DEFAULT_TOP_K = 8
export const DEMO_KNOWLEDGE_EVIDENCE_K_MAX = 40
export const DEMO_KNOWLEDGE_RERANKER_PROVIDER = 'cohere'

const DEMO_ANSWER_MARKDOWN = [
  'Ein KI-System gilt nach dem AI Act in zwei Konstellationen als Hochrisiko-System:',
  '',
  '1. **Produktsicherheits-Pfad:** Das System wird als Sicherheitsbauteil eines harmonisierten Produkts verwendet oder ist selbst ein solches Produkt [K1].',
  '2. **Anhang-III-Pfad:** Das System fällt in einen der in Anhang III gelisteten Bereiche — etwa Beschäftigung, Personalmanagement oder den Zugang zu wesentlichen Diensten — und birgt ein erhebliches Risiko für Gesundheit, Sicherheit oder Grundrechte [K1][K2].',
  '',
  'Entscheidend ist dabei die konkrete Zweckbestimmung des Systems, nicht die eingesetzte Technologie [K2]. Anbieter können eine dokumentierte Ausnahme geltend machen, wenn das System keine wesentliche Entscheidungsfindung beeinflusst [K1]. Vor dem Inverkehrbringen ist zudem eine Konformitätsbewertung nach Artikel 43 erforderlich [K3].',
].join('\n')

type DemoAnswerOptions = {
  candidateCount?: number
  degradedStages?: string[]
  gateMaxRounds?: number
  gateRoundsUsed?: number
  gateSufficient?: boolean
  profileId?: string
}

function demoAnswerRecord(options: DemoAnswerOptions = {}): KnowledgeAnswerRecord {
  const candidateCount = options.candidateCount ?? 24
  const degradedStages = options.degradedStages ?? ['rerank']
  const gateMaxRounds = options.gateMaxRounds ?? 2
  const gateRoundsUsed = options.gateRoundsUsed ?? 0
  const gateSufficient = options.gateSufficient ?? true
  const profileId = options.profileId ?? 'gruendlich'
  return {
    answerMarkdown: DEMO_ANSWER_MARKDOWN,
    autoSelected: true,
    candidateCount,
    degradedStages,
    evidenceUsed: 6,
    gate: { maxRounds: gateMaxRounds, roundsUsed: gateRoundsUsed, sufficient: gateSufficient },
    grounding: { degraded: false, total: 3, verified: 3 },
    profileId,
    quotes: [
      { label: 'K1', text: DEMO_QUOTE_ARTICLE_6, verified: true },
      { label: 'K2', text: DEMO_QUOTE_ANNEX_III, verified: true },
      { label: 'K3', text: DEMO_QUOTE_ARTICLE_43, verified: true },
    ],
    references: [
      {
        chunkIndex: 1,
        documentId: 'kdoc-ai-act-volltext',
        excerpt: DEMO_EXCERPT_ARTICLE_6,
        label: 'K1',
        sourceText: DEMO_EXCERPT_ARTICLE_6,
        tier: 'primary',
        title: 'EU-AI-Act-Volltext.pdf',
        url: 'inqtrix://documents/kdoc-ai-act-volltext#chunk-1',
      },
      {
        chunkIndex: 0,
        documentId: 'kdoc-ai-act-annex',
        excerpt: DEMO_EXCERPT_ANNEX_III,
        label: 'K2',
        sourceText: DEMO_EXCERPT_ANNEX_III,
        tier: 'primary',
        title: 'AI-Act-Annex-III.pdf',
        url: 'inqtrix://documents/kdoc-ai-act-annex#chunk-0',
      },
      {
        // Same document as K1 → the Belege panel groups both under one header.
        chunkIndex: 7,
        documentId: 'kdoc-ai-act-volltext',
        excerpt: DEMO_EXCERPT_ARTICLE_43,
        label: 'K3',
        sourceText: DEMO_EXCERPT_ARTICLE_43,
        tier: 'primary',
        title: 'EU-AI-Act-Volltext.pdf',
        url: 'inqtrix://documents/kdoc-ai-act-volltext#chunk-7',
      },
    ],
    refusal: false,
    retrievalDegradations: [],
  }
}

/** Completed example item shown when demo mode seeds the project. */
export function seedKnowledgeThreadItem(createdAt: string): KnowledgeThreadItemRecord {
  return {
    answer: demoAnswerRecord(),
    collectionTitles: ['EU-Recht'],
    createdAt,
    id: 'knowledge-item-seed',
    progress: {
      plan: {
        autoReason: 'question_complexity',
        autoSelected: true,
        decompose: false,
        degradedStages: ['rerank'],
        gateRounds: 2,
        grounding: true,
        profile: 'gruendlich',
        requestedProfile: null,
        vocabularyBridge: true,
      },
      steps: [
        { facts: { autoSelected: true, degradedStages: ['rerank'], profile: 'gruendlich' }, id: 'profile', kind: 'profile', status: 'done' },
        { facts: {}, id: 'vocabulary', kind: 'vocabulary', status: 'done' },
        { facts: { candidateCount: 24, collectionDocumentCount: 6, topK: 8, finalK: 8 }, id: 'retrieval', kind: 'retrieval', status: 'done' },
        { facts: { round: 1, roundsTotal: 3, sufficient: true }, id: 'gate-0', kind: 'gate', status: 'done' },
        { facts: {}, id: 'answer', kind: 'answer', status: 'done' },
        { facts: { quotesTotal: 2, quotesVerified: 2 }, id: 'grounding', kind: 'grounding', status: 'done' },
      ],
    },
    question: 'Wann gilt ein KI-System nach dem AI Act als Hochrisiko-System?',
    requestedProfile: null,
    runId: 'kn-demo-seed',
    sessionId: 'knowledge-session-demo',
    status: 'completed',
  }
}

export type DemoAskScriptStep = {
  delayMs: number
  event: ResearchRunEvent
}

export type DemoAskScript = {
  answer: KnowledgeAnswerRecord
  steps: DemoAskScriptStep[]
  /** Delay after the last event before the answer attaches. */
  completeAfterMs: number
}

/** Scripted event sequence for a demo ask — the same wire events the
 * backend emits, fed through the real `appendApiRunEvent` pipeline. */
export function buildDemoAskScript(runId: string): DemoAskScript {
  let sequence = 0
  const event = (type: string, data: Record<string, unknown>): ResearchRunEvent => ({
    created_at: Math.floor(Date.now() / 1000),
    data,
    run_id: runId,
    sequence: (sequence += 1),
    type,
  })

  return {
    answer: demoAnswerRecord({
      candidateCount: 8,
      degradedStages: ['rerank'],
      gateMaxRounds: 4,
      gateRoundsUsed: 1,
      gateSufficient: false,
      profileId: 'tief',
    }),
    completeAfterMs: 1200,
    steps: [
      {
        delayMs: 500,
        event: event('inqtrix.knowledge.profile.resolved', {
          auto_reason: null,
          auto_selected: false,
          decompose: true,
          degraded_stages: ['rerank'],
          gate_rounds: 3,
          grounding: true,
          profile: 'tief',
          report: true,
          requested_profile: 'tief',
          rerank: false,
          vocabulary_bridge: true,
        }),
      },
      {
        delayMs: 700,
        event: event('inqtrix.knowledge.decomposition.completed', {
          marker: '_knowledge_decomposition_parsed',
          sub_query_count: 0,
        }),
      },
      {
        delayMs: 900,
        event: event('inqtrix.knowledge.retrieval.completed', {
          candidate_count: 16,
          collection_document_count: 6,
          embedding_model: 'text-embedding-3-large',
          final_k: 16,
          final_k_overridden: false,
          top_k: 8,
        }),
      },
      {
        delayMs: 800,
        event: event('inqtrix.knowledge.gate.evaluated', {
          marker: '_knowledge_gate_parsed',
          rewritten: true,
          round: 0,
          sufficient: false,
        }),
      },
      {
        // The rewrite surfaces no new evidence → the gate stops early and
        // moves straight to the answer (demonstrates the gate-exhausted step).
        delayMs: 900,
        event: event('inqtrix.knowledge.gate.exhausted', {
          reason: 'no_new_evidence',
          round: 1,
        }),
      },
      {
        // One quote fails verbatim verification → the single visible
        // answer regeneration (demonstrates the answer-retry step).
        delayMs: 2400,
        event: event('inqtrix.knowledge.answer.retry', {
          attempt: 2,
          quotes_total: 3,
          quotes_unverified: 1,
        }),
      },
      {
        delayMs: 2400,
        event: event('inqtrix.knowledge.grounding.checked', {
          marker: '_knowledge_grounding_parsed',
          quotes_total: 3,
          quotes_verified: 3,
        }),
      },
    ],
  }
}

/** Local Finden search over the demo corpus: paragraph chunks scored
 * by term frequency, shaped exactly like the API search response. */
function searchDemoCorpus(query: string, collectionIds: string[]): KnowledgeSearchHit[] {
  const terms = searchTermsFromQuery(query).map((term) => term.toLowerCase())
  if (terms.length === 0) return []
  const scope = new Set(collectionIds)
  const hits: KnowledgeSearchHit[] = []

  for (const document of DEMO_DOCUMENTS) {
    if (scope.size > 0 && !scope.has(document.collectionId)) continue
    const chunks = document.text.split(/\n{2,}/)
    chunks.forEach((chunk, chunkIndex) => {
      const haystack = chunk.toLowerCase()
      let occurrences = 0
      for (const term of terms) {
        let from = 0
        for (;;) {
          const at = haystack.indexOf(term, from)
          if (at === -1) break
          occurrences += 1
          from = at + term.length
        }
      }
      if (occurrences === 0) return
      hits.push({
        chunk_id: `demo-chunk-${document.id}-${chunkIndex}`,
        chunk_index: chunkIndex,
        collection_id: document.collectionId,
        document_id: document.id,
        document_title: document.title,
        excerpt: chunk.trim(),
        generation_id: 'demo-generation',
        page_number: null,
        provenance_status: 'legacy_unspanned',
        rank: 0,
        reference_id: '',
        revision_id: 'demo-revision',
        score: Math.min(0.99, 0.35 + occurrences * 0.12),
        source_span: null,
      })
    })
  }

  return hits
    .sort((a, b) => b.score - a.score)
    .slice(0, 20)
    .map((hit, index) => ({
      ...hit,
      rank: index + 1,
      reference_id: `K${index + 1}`,
    }))
}

export function demoDocumentText(documentId: string): KnowledgeDocumentText {
  const document = DEMO_DOCUMENTS.find((entry) => entry.id === documentId)
  if (!document) {
    throw new Error('Dokument nicht gefunden.')
  }
  return {
    chunk_count: document.text.split(/\n{2,}/).length,
    collection_id: document.collectionId,
    created_at: 0,
    id: document.id,
    metadata: {},
    text: document.text,
    title: document.title,
  }
}

/** Demo implementation of the workspace data source (no network). */
export function createDemoKnowledgeDataSource(): KnowledgeDataSource {
  return {
    canLoadFileContent: null,
    loadDocumentText: (documentId) => Promise.resolve(demoDocumentText(documentId)),
    loadFileContent: null,
    search: (query, collectionIds) => Promise.resolve({
      data: searchDemoCorpus(query, collectionIds),
      warnings: [],
    }),
  }
}
