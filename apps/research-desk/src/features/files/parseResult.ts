/** Visible warning when content is capped — retained for the backend's
 * last-resort clamp; ingest itself does not truncate. */
export const truncationWarning = 'Textinhalt gekürzt (Dokument zu groß für das Modell-Kontextfenster).'

/** Visible warning when a supported file yields no extractable text. */
export const emptyTextWarning = 'Kein Text extrahierbar (evtl. gescanntes Dokument ohne Textebene).'

/** Keep the full extracted text at ingest — no silent per-document truncation.
 * The composer token meter shows what fits against the
 * selected model's context window, and the backend re-clamps visibly as the
 * last resort. The return shape is preserved for callers; ``textTruncated`` is
 * always ``false`` now. */
export function clampText(text: string): { extractedText: string; textTruncated: boolean } {
  return { extractedText: text, textTruncated: false }
}
