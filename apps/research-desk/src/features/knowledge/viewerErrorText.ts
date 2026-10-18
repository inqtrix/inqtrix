import { hasHttpStatus } from '@/api/inqtrixClient'
import type { TranslationDictionary } from '@/i18n/translations'

/** The reader's text when a cited document cannot be loaded. A document that
 * is gone has left the collection, and the reader learns that, not only that
 * something was not found. */
export function viewerErrorText(error: unknown, t: TranslationDictionary): string {
  if (hasHttpStatus(error, 404)) return t.knowledge.viewerDocumentGone
  return error instanceof Error ? error.message : t.knowledge.viewerError
}
