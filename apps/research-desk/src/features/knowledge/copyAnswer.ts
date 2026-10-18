import type { KnowledgeAnswerRecord, KnowledgeReferenceRecord } from '@/features/project/types'
import { withAiDisclosure } from '@/lib/aiDisclosure'
import { collapseWhitespace } from './citations'

/**
 * What a knowledge answer copy includes:
 * - `'answer'`  — only the answer markdown (the historical copy behaviour).
 * - `'sources'` — answer + a compact source list keyed by the `[K#]` markers.
 * - `'evidence'`— answer + per source the full retrieved excerpt ("Beleg"),
 *   so the pasted content is self-contained rich material.
 */
export type AnswerCopyMode = 'answer' | 'sources' | 'evidence'

/**
 * Locale strings the formatter needs. Kept as plain args (no `t` coupling) so
 * the function stays pure and unit-testable.
 */
export type AnswerCopyLabels = {
  /** Heading above the source list in `'sources'` mode, e.g. "Quellen". */
  sourcesHeading: string
  /** Heading above the source+excerpt list in `'evidence'` mode, e.g. "Belege". */
  evidenceHeading: string
  /** Section label template carrying a `{n}` placeholder, e.g. "Abschnitt {n}". */
  sectionLabel: string
  /** Page label template carrying a `{n}` placeholder, e.g. "S. {n}". */
  pageLabel: string
  /** Marker appended when a citation's answer span was verbatim-verified, e.g. "belegt". */
  verifiedLabel: string
  /** AI-generation disclosure appended to the copied answer, e.g. "Dieser Text
   * wurde von einem KI-System erzeugt (Inqtrix)." */
  aiDisclosure: string
}

/** The citation URL only when it is a real web link; internal document refs
 * (kref://, inqtrix://) are noise in pasted output and are dropped. Used for both
 * the visible source name and the trailing metadata, so the rule lives once. */
function webUrl(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null
}

/** Trailing metadata (section · page · web URL) for one citation, omitting any
 * field that is absent. */
function referenceMeta(reference: KnowledgeReferenceRecord, labels: AnswerCopyLabels): string[] {
  const parts: string[] = []
  if (typeof reference.chunkIndex === 'number') {
    parts.push(labels.sectionLabel.replace('{n}', String(reference.chunkIndex + 1)))
  }
  if (typeof reference.pageNumber === 'number') {
    parts.push(labels.pageLabel.replace('{n}', String(reference.pageNumber)))
  }
  const web = webUrl(reference.url)
  if (web) parts.push(web)
  return parts
}

/** `[K1] Title · Abschnitt N · S. N · belegt` — the cross-reference line shared
 * by both source modes. The `· belegt` marker rides with the visible excerpt, so
 * callers only pass `verified` true in evidence mode. */
function referenceHeadline(
  reference: KnowledgeReferenceRecord,
  verified: boolean,
  labels: AnswerCopyLabels,
): string {
  const title = reference.title?.trim() || webUrl(reference.url) || reference.label
  const meta = referenceMeta(reference, labels)
  if (verified) meta.push(labels.verifiedLabel)
  const suffix = meta.length > 0 ? ` · ${meta.join(' · ')}` : ''
  return `[${reference.label}] ${title}${suffix}`
}

/**
 * Build the clipboard text for a completed knowledge answer. References are
 * listed flat in `[K#]` order (not grouped by document like the panel) so each
 * line maps 1:1 to an inline marker — the reader can paste the answer elsewhere
 * and still resolve every citation. A refusal / no-reference answer copies the
 * bare answer text in every mode (no empty source heading).
 *
 * Every mode ends with the AI-generation disclosure, including the refusal
 * path: the text leaves the app on the clipboard and carries no other context
 * once it is pasted.
 */
export function formatAnswerForCopy(
  answer: KnowledgeAnswerRecord,
  mode: AnswerCopyMode,
  labels: AnswerCopyLabels,
): string {
  const body = answer.answerMarkdown.trim()
  if (mode === 'answer' || answer.references.length === 0) {
    return withAiDisclosure(body, labels.aiDisclosure)
  }

  const verifiedByLabel = new Map(answer.quotes.map((quote) => [quote.label, quote.verified]))
  const heading = mode === 'evidence' ? labels.evidenceHeading : labels.sourcesHeading

  const lines = answer.references.map((reference) => {
    // The "belegt" marker only makes sense beside the visible excerpt, so it is
    // evidence-only; the source list (no excerpt) stays unmarked.
    const verified = mode === 'evidence' && (verifiedByLabel.get(reference.label) ?? false)
    const headline = referenceHeadline(reference, verified, labels)
    if (mode !== 'evidence') return `- ${headline}`
    const excerpt = reference.excerpt?.trim()
    return excerpt ? `**${headline}**\n> ${collapseWhitespace(excerpt)}` : `**${headline}**`
  })

  const separator = mode === 'evidence' ? '\n\n' : '\n'
  return withAiDisclosure(
    `${body}\n\n## ${heading}\n${lines.join(separator)}`,
    labels.aiDisclosure,
  )
}
