/**
 * Token-budget math for the composer context meter. A single place that turns
 * text into an estimated token count and assembles the per-category breakdown
 * + capacity model the meter renders.
 *
 * The live estimate is exactly that -- an estimate. Cross-provider exactness is
 * impossible client-side (tokenizers are model-specific), so this uses `tokenx`
 * (~96% of a real tokenizer in ~2kB). The authoritative number is the backend
 * `usage.prompt_tokens` surfaced after a send.
 */
import { approximateTokenSize } from 'tokenx'

import type { MentionTone } from '@/lib/tone'

/** Context categories shown in the breakdown. `conversation` is the chat
 * history in chat mode and the full editor document in editor mode. */
export type ContextCategoryKey =
  | 'documents'
  | 'reports'
  | 'composer'
  | 'conversation'
  | 'rules'

/** A category with its already-computed token estimate (kept pure/memoisable). */
export type ContextCategoryInput = {
  key: ContextCategoryKey
  tone: MentionTone
  tokens: number
}

export type ContextThreshold = 'ok' | 'warning' | 'critical' | 'unknown'

export type ContextTokenModel = {
  /** Non-empty categories only, in input order. */
  categories: ContextCategoryInput[]
  totalTokens: number
  /** Usable input budget = context window − reserved output − safety; null when
   * the model's context window is unknown (no model card). */
  capacityTokens: number | null
  /** Output headroom reserved from the window (shown as its own bar segment). */
  reservedOutputTokens: number
  /** total / capacity, or null when capacity is unknown. */
  usedFraction: number | null
  threshold: ContextThreshold
}

/** Default safety margin (tokens) kept free on top of the reserved output. */
const DEFAULT_SAFETY_TOKENS = 2000

/** Estimate tokens for a text via tokenx. Empty/blank text is zero. */
export function estimateTokensFromText(text: string): number {
  if (!text || !text.trim()) return 0
  return approximateTokenSize(text)
}

/**
 * Assemble the meter's view-model from per-category token counts and the
 * selected model's capacity.
 *
 * Capacity follows the input/output split (see backend Gotcha #38): the usable
 * input budget is the context window minus the model's reserved output budget
 * minus a small safety margin. An unknown context window yields a `null`
 * capacity and an `unknown` threshold so the UI shows "context window unknown"
 * rather than a fabricated percentage.
 */
export function buildContextTokenModel(
  categories: ContextCategoryInput[],
  options: {
    contextWindowTokens: number | null
    reservedOutputTokens: number
    safetyTokens?: number
  },
): ContextTokenModel {
  const totalTokens = categories.reduce((sum, category) => sum + category.tokens, 0)
  const safety = options.safetyTokens ?? DEFAULT_SAFETY_TOKENS
  const reservedOutputTokens = Math.max(0, options.reservedOutputTokens)
  const capacityTokens =
    options.contextWindowTokens == null
      ? null
      : Math.max(0, options.contextWindowTokens - reservedOutputTokens - safety)
  const usedFraction =
    capacityTokens && capacityTokens > 0 ? totalTokens / capacityTokens : null
  const threshold: ContextThreshold =
    usedFraction == null
      ? 'unknown'
      : usedFraction >= 0.9
        ? 'critical'
        : usedFraction >= 0.75
          ? 'warning'
          : 'ok'
  return {
    categories: categories.filter((category) => category.tokens > 0),
    totalTokens,
    capacityTokens,
    reservedOutputTokens,
    usedFraction,
    threshold,
  }
}
