import type {
  KnowledgeRunStepRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import type { TranslationDictionary } from '@/i18n/translations'
import { knowledgeStepLine, profileDisplayName } from './stepLines'

export const KNOWLEDGE_RUN_FACT_PLACEHOLDER = '—'

type KnowledgeStrings = TranslationDictionary['knowledge']

export type KnowledgeRunFactId = 'collections' | 'hits' | 'profile' | 'round'

export type KnowledgeRunFact = {
  id: KnowledgeRunFactId
  label: string
  pending: boolean
  value: string
}

export type KnowledgeRunHeaderStatus = {
  title: string
  value: string
}

export function knowledgeRunHeaderStatus({
  collectionCount,
  fallback,
  step,
  t,
}: {
  collectionCount: number
  fallback: string
  step: KnowledgeRunStepRecord | null
  t: KnowledgeStrings
}): KnowledgeRunHeaderStatus {
  if (!step) {
    return { title: fallback, value: fallback }
  }

  const line = knowledgeStepLine(step, { collectionCount, t })
  return {
    title: line.secondary ? `${line.primary} · ${line.secondary}` : line.primary,
    value: line.primary,
  }
}

export function knowledgeRunFacts({
  collectionCount,
  item,
  t,
}: {
  collectionCount: number
  item: KnowledgeThreadItemRecord
  t: KnowledgeStrings
}): KnowledgeRunFact[] {
  const steps = item.progress.steps
  const profileId = item.progress.plan?.profile ?? item.requestedProfile ?? ''
  const retrieval = lastStepOfKind(steps, 'retrieval')
  const gate = lastStepOfKind(steps, 'gate')
  const hits = retrieval?.facts.candidateCount
  const round = gate?.facts.round
  const roundsTotal = gate?.facts.roundsTotal

  return [
    {
      id: 'profile',
      label: t.runMetricProfile,
      pending: !profileId,
      value: profileId ? profileDisplayName(profileId, t) : KNOWLEDGE_RUN_FACT_PLACEHOLDER,
    },
    {
      id: 'collections',
      label: t.runMetricCollections,
      pending: false,
      value: String(collectionCount),
    },
    {
      id: 'hits',
      label: t.runMetricHits,
      pending: hits === undefined,
      value: hits === undefined ? KNOWLEDGE_RUN_FACT_PLACEHOLDER : String(hits),
    },
    {
      id: 'round',
      label: t.runMetricRound,
      pending: round === undefined || roundsTotal === undefined,
      value: round === undefined || roundsTotal === undefined
        ? KNOWLEDGE_RUN_FACT_PLACEHOLDER
        : `${round}/${roundsTotal}`,
    },
  ]
}

function lastStepOfKind(
  steps: readonly KnowledgeRunStepRecord[],
  kind: KnowledgeRunStepRecord['kind'],
) {
  return [...steps].reverse().find((step) => step.kind === kind)
}
