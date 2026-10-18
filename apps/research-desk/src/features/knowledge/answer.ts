import type { ResearchRunResult } from '@/features/researchRuns/types'
import type {
  KnowledgeAnswerRecord,
  KnowledgeReferenceRecord,
  KnowledgeRunProgressRecord,
} from '@/features/project/types'
import {
  citationLabelFromHref as citationLabelFromHrefGeneric,
  linkifyCitationLabels as linkifyCitationLabelsGeneric,
} from '@/components/markdown/citationLinks'
import {
  knowledgeRetrievalDegradations,
  knowledgeRetrievalWarnings,
  mergeKnowledgeRetrievalDegradations,
  mergeKnowledgeRetrievalWarnings,
} from './retrievalDegradation'

/**
 * Parse the document target out of a knowledge citation URL. The
 * algorithm emits either `{base}/v1/sources/{document_id}?chunk={n}`
 * (public base URL configured) or the `inqtrix://documents/{id}#chunk-{n}`
 * fallback. Unknown shapes yield null — the reference then renders
 * without a viewer link instead of opening the wrong document.
 */
export function parseKnowledgeReferenceUrl(
  url: string,
): { documentId: string; chunkIndex: number | null } | null {
  const http = url.match(/\/v1\/sources\/([^/?#]+)(?:\?chunk=(\d+))?/)
  if (http) {
    return { chunkIndex: http[2] ? Number(http[2]) : null, documentId: http[1] }
  }
  const internal = url.match(/^inqtrix:\/\/documents\/([^/#?]+)(?:#chunk-(\d+))?/)
  if (internal) {
    return { chunkIndex: internal[2] ? Number(internal[2]) : null, documentId: internal[1] }
  }
  return null
}

/** The honest no-evidence answer emitted by the knowledge algorithm. */
const REFUSAL_PATTERN = /keine relevanten\s+Inhalte/i

export function isKnowledgeRefusal(answer: string, referenceCount: number): boolean {
  return referenceCount === 0 && REFUSAL_PATTERN.test(answer)
}

/**
 * Project a completed native run result onto the knowledge answer
 * record. Reads the knowledge-specific result keys defensively: rich
 * deployments deliver `report_references` (with document titles and the
 * verified quotes each one supports), grounding counts and profile facts;
 * older payloads degrade to the plain `references` export without breaking
 * the card.
 */
export function knowledgeAnswerFromRunResult(result: ResearchRunResult): KnowledgeAnswerRecord {
  const rawReferences = result.report_references ?? result.references ?? []
  const references: KnowledgeReferenceRecord[] = rawReferences.map((reference) => {
    const parsed = parseKnowledgeReferenceUrl(reference.url)
    const explicit = reference as {
      title?: string
      document_id?: string | null
      chunk_index?: number | null
      excerpt?: string | null
      source_text?: string | null
      page_number?: number | null
    }
    return {
      // Prefer the explicit backend fields (reliable open + the exact passage);
      // fall back to the URL-parsed id for older payloads.
      chunkIndex: explicit.chunk_index ?? parsed?.chunkIndex ?? null,
      documentId: explicit.document_id ?? parsed?.documentId ?? null,
      excerpt: explicit.excerpt ?? null,
      sourceText: explicit.source_text ?? null,
      pageNumber: explicit.page_number ?? null,
      label: reference.label,
      tier: String(reference.tier ?? 'unknown'),
      title: explicit.title,
      url: reference.url,
    }
  })

  const gate = result.knowledge_gate
  const grounding = result.knowledge_grounding
  const profile = result.knowledge_profile

  return {
    answerMarkdown: result.answer,
    autoSelected: profile?.auto_selected === true,
    candidateCount: result.knowledge_candidates ?? null,
    degradedStages: profile?.degraded_stages ?? [],
    evidenceUsed: result.knowledge_evidence_used ?? null,
    gate: gate?.enabled && gate.sufficient !== undefined
      ? {
        maxRounds: gate.max_rounds ?? 0,
        roundsUsed: gate.rounds_used ?? 0,
        sufficient: gate.sufficient,
      }
      : null,
    grounding: grounding?.enabled && grounding.quotes_total !== undefined
      ? {
        degraded: grounding.marker?.includes('fallback') === true,
        total: grounding.quotes_total,
        verified: grounding.quotes_verified ?? 0,
      }
      : null,
    profileId: profile?.id ?? null,
    // Each published quote travels with the reference it supports; the run's
    // grounding summary carries counts only.
    quotes: rawReferences.flatMap((reference) =>
      (reference.quotes ?? []).map((quote) => ({
        label: reference.label,
        text: quote.text,
        verified: quote.verified,
      })),
    ),
    references,
    refusal: isKnowledgeRefusal(result.answer, references.length),
    retrievalDegradations: knowledgeRetrievalDegradations(
      result.knowledge_retrieval?.degradations,
    ),
    retrievalWarnings: knowledgeRetrievalWarnings(
      result.knowledge_retrieval?.warnings,
    ),
  }
}

/** Merge the live SSE ledger into the persisted result projection. Current
 * servers persist the same degradation in `knowledge_retrieval`, but retaining
 * the event copy makes reconnects and rolling upgrades fail visible rather
 * than letting the final answer erase an already observed limitation. */
export function knowledgeAnswerWithRunProgress(
  answer: KnowledgeAnswerRecord,
  progress: KnowledgeRunProgressRecord,
): KnowledgeAnswerRecord {
  const fromEvents = progress.steps.flatMap(
    (step) => step.facts.retrievalDegradations ?? [],
  )
  const retrievalDegradations = mergeKnowledgeRetrievalDegradations(
    answer.retrievalDegradations,
    fromEvents,
  )
  const warningEvents = progress.steps.flatMap(
    (step) => step.facts.retrievalWarnings ?? [],
  )
  const retrievalWarnings = mergeKnowledgeRetrievalWarnings(
    answer.retrievalWarnings ?? [],
    warningEvents,
  )
  const degradationsUnchanged = (
    retrievalDegradations.length === answer.retrievalDegradations.length
  )
  const warningsUnchanged = JSON.stringify(retrievalWarnings)
    === JSON.stringify(answer.retrievalWarnings ?? [])
  return degradationsUnchanged && warningsUnchanged
    ? answer
    : { ...answer, retrievalDegradations, retrievalWarnings }
}

const isKnowledgeCitationLabel = (label: string): boolean => /^K\d+$/.test(label)

/**
 * Make citation tokens clickable WITHOUT touching the off-limits Markdown
 * renderer: rewrite them into `[K1](#kref-K1)` links and let a capture-phase
 * click handler on the surrounding container intercept `#kref-*` anchors.
 * The shared citation grammar applies first: a grouped `[K1, K2]` reads as
 * `[K1] [K2]`, code is never rewritten, and a bracket no reader can parse
 * (`[K1–K3]`) stays as written.
 *
 * Two forms are handled:
 *  - Bracketed `[K1]` (not already a link) — always linkified.
 *  - BARE `K1` (the model sometimes drops the brackets) — linkified ONLY when
 *    the token matches a real reference label (`knownLabels`), so a stray
 *    `K2` in prose (e.g. potassium) never becomes a dead citation link.
 *
 * Bare tokens must be delimited (not part of a word and not inside an
 * already-rewritten link). Adjacent runs without a separator (`K3K2`) are
 * split only when every segment is a known label.
 */
export function linkifyCitationLabels(
  markdown: string,
  knownLabels?: ReadonlySet<string>,
): string {
  return linkifyCitationLabelsGeneric(
    markdown,
    isKnowledgeCitationLabel,
    knownLabels,
  )
}

/** Extract the citation label from an intercepted `#kref-*` href. */
export function citationLabelFromHref(href: string | null | undefined): string | null {
  return citationLabelFromHrefGeneric(href, isKnowledgeCitationLabel)
}
