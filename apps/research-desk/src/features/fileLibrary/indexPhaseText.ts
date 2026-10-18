import type { TranslationDictionary } from '@/i18n/translations'

/** The phase of an index job in the words of its document row, for the
 * banner of a paused index: one vocabulary for the server's phase names
 * (`queued`, `starting`, `contextualization`, `embedding`, `embedding_wait`,
 * `validation`, `publication`). A phase without words (`delta`, or a name
 * the server adds later) shows none rather than its raw name. */
export function indexPhaseText(
  phase: string | undefined,
  currentBatch: number,
  totalBatches: number,
  t: TranslationDictionary,
): string | null {
  const texts = t.vectorIndex
  const batchesKnown = currentBatch > 0 && totalBatches > 0
  switch (phase) {
    case 'queued':
      return texts.memberQueued
    case 'starting':
      return texts.memberPreparing
    case 'contextualization':
      return batchesKnown
        ? texts.memberContextBatchTooltip(currentBatch, totalBatches)
        : texts.memberContext
    case 'embedding':
      return batchesKnown
        ? texts.memberEmbeddingBatchTooltip(currentBatch, totalBatches)
        : texts.memberEmbedding
    case 'embedding_wait':
      return texts.memberEmbeddingWait
    case 'validation':
      return texts.memberValidating
    case 'publication':
      return texts.memberPublishing
    default:
      return null
  }
}
