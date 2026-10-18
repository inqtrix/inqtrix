import type { KnowledgeRunStepRecord } from '@/features/project/types'
import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import {
  knowledgeRetrievalDegradationText,
  knowledgeSearchWarningNotice,
} from './retrievalDegradation'

export type KnowledgeStepLine = {
  id: string
  information?: string
  status: 'running' | 'done'
  primary: string
  secondary?: string
  warning?: string
}

type KnowledgeStrings = TranslationDictionary['knowledge']

/** Render one captured step record as localized card lines. */
export function knowledgeStepLine(
  step: KnowledgeRunStepRecord,
  options: { collectionCount: number; t: KnowledgeStrings },
): KnowledgeStepLine {
  const { collectionCount, t } = options
  const facts = step.facts

  switch (step.kind) {
    case 'queued':
      // Deliberately position-less: the queue emits one frame per segment,
      // so a rendered position would go stale within seconds.
      return {
        id: step.id,
        primary: step.status === 'running' ? t.stepQueuedRunning : t.stepQueuedDone,
        status: step.status,
      }
    case 'context':
      return {
        id: step.id,
        primary: facts.rewritten ? t.stepContextRewritten : t.stepContextUnchanged,
        secondary: facts.contextMarker?.includes('fallback') ? t.stepContextFallback : undefined,
        status: step.status,
      }
    case 'profile': {
      const template = facts.autoSelected ? t.stepProfileAuto : t.stepProfile
      return {
        id: step.id,
        primary: template.replace('{profile}', profileDisplayName(facts.profile ?? '', t)),
        secondary: facts.degradedStages && facts.degradedStages.length > 0
          ? t.stepDegraded.replace('{stages}', facts.degradedStages.join(', '))
          : undefined,
        status: step.status,
      }
    }
    case 'decompose':
      return {
        id: step.id,
        primary: countLabel(facts.subQueryCount ?? 0, t.stepDecomposeOne, t.stepDecompose),
        status: step.status,
      }
    case 'vocabulary':
      return { id: step.id, primary: t.stepVocabulary, status: step.status }
    case 'retrieval': {
      // Done WITH a known document count → surface coverage ("N documents
      // searched"). Otherwise the collection-scoped line (running, or done when
      // the count couldn't be resolved).
      const scope = facts.candidateCount === undefined
        ? countLabel(collectionCount, t.stepRetrievalRunningOne, t.stepRetrievalRunning)
        : facts.collectionDocumentCount !== undefined
          ? countLabel(facts.collectionDocumentCount, t.stepRetrievalDoneDocsOne, t.stepRetrievalDoneDocs)
          : countLabel(collectionCount, t.stepRetrievalDoneOne, t.stepRetrievalDone)
      const searchWarningNotices = (facts.retrievalWarnings ?? []).map(
        (warning) => knowledgeSearchWarningNotice(warning, t),
      )
      const warningMessages = [
        ...(facts.retrievalDegradations ?? []).map(
          (degradation) => knowledgeRetrievalDegradationText(degradation, t),
        ),
        ...searchWarningNotices
          .filter((notice) => notice.tone === 'warning')
          .map((notice) => notice.message),
      ]
      const informationalMessages = searchWarningNotices
        .filter((notice) => notice.tone === 'informational')
        .map((notice) => notice.message)
      return {
        id: step.id,
        information: informationalMessages.length > 0
          ? [...new Set(informationalMessages)].join(' ')
          : undefined,
        primary: scope.replace(
          '{hits}',
          countLabel(facts.candidateCount ?? 0, t.hitCountOne, t.hitCount),
        ),
        // Make both retrieval widths visible — top_k (per-query) and final_k
        // (surfaced evidence) — flagging an explicit final_k override.
        secondary: facts.topK !== undefined && facts.finalK !== undefined
          ? (facts.finalKOverridden ? t.stepRetrievalWidthsOverridden : t.stepRetrievalWidths)
              .replace('{topK}', String(facts.topK))
              .replace('{finalK}', String(facts.finalK))
          : undefined,
        status: step.status,
        warning: warningMessages.length > 0
          ? [...new Set(warningMessages)].join(' ')
          : undefined,
      }
    }
    case 'evidence': {
      const kept = facts.kept ?? 0
      return {
        id: step.id,
        primary: countLabel(kept + (facts.dropped ?? 0), t.stepEvidenceOne, t.stepEvidence)
          .replace('{kept}', String(kept)),
        status: step.status,
      }
    }
    case 'gate': {
      const verdict = facts.sufficient ? t.stepGateSufficient : t.stepGateInsufficient
      const rewrite = facts.rewritten ? ` · ${t.stepGateRewritten}` : ''
      const degraded = facts.gateMarker?.includes('fallback')
        ? ` · ${t.stepGateFallback}`
        : ''
      return {
        id: step.id,
        primary: t.stepGate
          .replace('{round}', String(facts.round ?? 1))
          .replace('{total}', String(facts.roundsTotal ?? 1)),
        secondary: `${verdict}${rewrite}${degraded}`,
        status: step.status,
      }
    }
    case 'gate-exhausted':
      return {
        id: step.id,
        primary: t.stepGateExhausted,
        status: step.status,
      }
    case 'answer':
      return {
        id: step.id,
        primary: step.status === 'running' ? t.stepAnswerRunning : t.stepAnswerDone,
        status: step.status,
      }
    case 'answer-retry':
      return {
        id: step.id,
        primary: step.status === 'running'
          ? countLabel(facts.quotesUnverified ?? 0, t.stepAnswerRetryRunningOne, t.stepAnswerRetryRunning)
          : countLabel(facts.quotesUnverified ?? 0, t.stepAnswerRetryDoneOne, t.stepAnswerRetryDone),
        status: step.status,
      }
    case 'grounding':
      return {
        id: step.id,
        primary: facts.groundingMarker?.includes('fallback')
          ? t.stepGroundingUnavailable
          : countLabel(facts.quotesTotal ?? 0, t.stepGroundingOne, t.stepGrounding)
            .replace('{verified}', String(facts.quotesVerified ?? 0)),
        status: step.status,
      }
  }
}

/** German display name for a known profile id; raw id otherwise — the
 * picker and step lines must never invent profiles the manifest does
 * not carry, but unknown ids still render honestly. */
export function profileDisplayName(profileId: string, t: KnowledgeStrings): string {
  switch (profileId) {
    case 'schnell':
      return t.profileSchnell
    case 'standard':
      return t.profileStandard
    case 'gruendlich':
      return t.profileGruendlich
    case 'tief':
      return t.profileTief
    case 'auto':
      return t.profileAuto
    default:
      return profileId
  }
}

/** One-line picker description (scope + expected latency) per known
 * profile id; empty for unknown ids. */
export function profileDescription(profileId: string, t: KnowledgeStrings): string {
  switch (profileId) {
    case 'schnell':
      return t.profileSchnellDescription
    case 'standard':
      return t.profileStandardDescription
    case 'gruendlich':
      return t.profileGruendlichDescription
    case 'tief':
      return t.profileTiefDescription
    case 'auto':
      return t.profileAutoDescription
    default:
      return ''
  }
}
