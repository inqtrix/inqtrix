import type { KnowledgeRunStepRecord } from '@/features/project/types'

export type KnowledgeStepListVariant = 'default' | 'live'

export const KNOWLEDGE_STEP_FOLLOW_SAFE_BOTTOM_PX = 28
export const KNOWLEDGE_STEP_SMART_FADE_MIN_STEPS = 7

export type KnowledgeStepViewportState = {
  activeStepId: string | null
  followStepId: string | null
  overflowing: boolean
  managedScroll: boolean
  smartFade: boolean
}

export function knowledgeStepViewportState({
  failed = false,
  steps,
  variant,
}: {
  failed?: boolean
  steps: readonly KnowledgeRunStepRecord[]
  variant: KnowledgeStepListVariant
}): KnowledgeStepViewportState {
  const smartFade = variant === 'live'
  const activeStepId = failed ? null : steps.find((step) => step.status === 'running')?.id ?? null
  const followStepId = failed ? null : activeStepId ?? steps.at(-1)?.id ?? null

  return {
    activeStepId,
    followStepId,
    managedScroll: smartFade,
    overflowing: smartFade && steps.length >= KNOWLEDGE_STEP_SMART_FADE_MIN_STEPS,
    smartFade,
  }
}

export type KnowledgeStepGlyphState = 'complete' | 'review-complete' | 'running'

export function knowledgeStepGlyphState({
  failed = false,
  status,
  variant,
}: {
  failed?: boolean
  status: KnowledgeRunStepRecord['status']
  variant: KnowledgeStepListVariant
}): KnowledgeStepGlyphState {
  if (!failed && status === 'running') return 'running'
  return variant === 'live' ? 'complete' : 'review-complete'
}

export function knowledgeStepFollowOffset({
  followBottom,
  maxOffset,
  safeBottom = KNOWLEDGE_STEP_FOLLOW_SAFE_BOTTOM_PX,
  viewportHeight,
}: {
  followBottom: number
  maxOffset: number
  safeBottom?: number
  viewportHeight: number
}): number {
  return Math.min(maxOffset, Math.max(0, followBottom - viewportHeight + safeBottom))
}
