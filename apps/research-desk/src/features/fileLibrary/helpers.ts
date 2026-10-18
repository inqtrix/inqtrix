import { FileText, Paperclip, Table2, type LucideIcon } from '@/components/icons'
import type { Locale } from '@/i18n/translations'
import type { FileAssetRecord, IndexingJobLive, VectorIndexMemberState, VectorIndexRecord } from '@/features/project/types'
import type { VectorIndexMemberResolved } from '@/features/project/selectors'

/** Whether *fileId* is part of the actively running job's working set — only
 * then does its row read "läuft". A still-QUEUED job (``queuePosition`` set) is
 * not processing anything yet, so no row pulses while the header shows "In
 * Warteschlange"; a file outside ``runningFileIds`` keeps its persisted state. */
export function isMemberInRun(
  job: IndexingJobLive | null | undefined,
  fileId: string,
): boolean {
  if (!job || job.queuePosition != null) return false
  return job.runningFileIds.includes(fileId)
}

/** The status a vector-index member's cell should display, given its persisted
 * `state`, whether it is part of the CURRENT run's working set (`inRun`), and
 * its server-confirmed live outcome this run (`liveProgress`).
 *
 * Only a file actually in the run reads "running" — a file outside the run
 * keeps its persisted state, so indexing one new document never makes the
 * already-embedded rows read "läuft" (the prior bug, where this was gated on
 * the whole index's `indexing` status). A confirmed outcome (embedded/skipped)
 * always wins as it lands. */
export function memberCellState(
  state: VectorIndexMemberState,
  inRun: boolean,
  liveProgress?: 'embedded' | 'skipped',
): 'embedded' | 'skipped' | 'running' | 'pending' {
  return liveProgress ?? (inRun ? 'running' : state)
}

/** Human-readable byte size. German locale uses a decimal comma to match the
 * rest of the UI (e.g. "2,4 MB"). */
export function formatBytes(bytes: number, locale: Locale = 'en'): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const text = value.toFixed(unitIndex === 0 || value >= 10 ? 0 : 1)
  return `${locale === 'de' ? text.replace('.', ',') : text} ${units[unitIndex]}`
}

/** Compact, locale-aware "added at" stamp (date + time of day) for the file
 * list's "Hinzugefügt" column, e.g. "06. Juni, 14:32" (de, 24h) /
 * "Jun 06, 02:32 PM" (en, 12h). Returns an em dash for unparseable input so a
 * malformed timestamp never renders as "Invalid Date". */
export function formatAddedAt(iso: string, locale: Locale = 'en'): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(locale, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

/** Full "added at" stamp (weekday + year + time) for the column's hover
 * tooltip, where the compact form omits the year and weekday. */
export function formatAddedAtFull(iso: string, locale: Locale = 'en'): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(locale, {
    weekday: 'short',
    year: 'numeric',
    month: 'long',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Lowercase file extension derived from the file name (there is no `ext`
 * field on FileAssetRecord). */
export function fileExt(asset: Pick<FileAssetRecord, 'fileName'>): string {
  const match = /\.([a-z0-9]+)$/i.exec(asset.fileName)
  return match ? match[1].toLowerCase() : ''
}

export type FileTypeMeta = {
  Icon: LucideIcon
  label: string
  paged: boolean
}

const TYPE_BY_EXT: Record<string, FileTypeMeta> = {
  pdf: { Icon: FileText, label: 'PDF', paged: true },
  doc: { Icon: FileText, label: 'DOC', paged: true },
  docx: { Icon: FileText, label: 'DOCX', paged: true },
  rtf: { Icon: FileText, label: 'RTF', paged: true },
  xls: { Icon: Table2, label: 'XLS', paged: false },
  xlsx: { Icon: Table2, label: 'XLSX', paged: false },
  csv: { Icon: Table2, label: 'CSV', paged: false },
  txt: { Icon: FileText, label: 'TXT', paged: false },
  md: { Icon: FileText, label: 'MD', paged: false },
}

/** Type tile/badge metadata. Type is conveyed via a neutral tile + label, never
 * via color — color stays reserved for parse status. */
export function typeMeta(asset: Pick<FileAssetRecord, 'fileName'>): FileTypeMeta {
  const ext = fileExt(asset)
  return TYPE_BY_EXT[ext] ?? { Icon: Paperclip, label: ext.toUpperCase() || 'DATEI', paged: false }
}

/** Whether an index member can NEVER embed as it stands: no extracted text,
 * no server original to re-parse from, AND no upload/parse still in flight
 * that could deliver either. A fresh placeholder (upload or parse pending)
 * is "queued", not "no text" — the two read very differently in the UI. */
export function memberHasNoEmbeddableText(
  asset: Pick<FileAssetRecord, 'extractedText' | 'parsePending' | 'serverFileId' | 'uploadPending'>,
): boolean {
  return (
    asset.extractedText.trim().length === 0
    && !asset.serverFileId
    && !asset.parsePending
    && !asset.uploadPending
  )
}

/** The visual-order id range between the selection anchor and the shift-click
 * target, inclusive, band-boundary-agnostic (both endpoints live in the same
 * flattened render order). No anchor or an unknown target degrades to just
 * the target — a shift-click is then an ordinary toggle. */
export function rangeBetween(
  orderedIds: readonly string[],
  anchorId: string | null,
  targetId: string,
): string[] {
  const to = orderedIds.indexOf(targetId)
  if (to < 0) return []
  const from = anchorId ? orderedIds.indexOf(anchorId) : -1
  if (from < 0) return [targetId]
  const [start, end] = from < to ? [from, to] : [to, from]
  return orderedIds.slice(start, end + 1)
}

export type IngestBadgeState = 'uploading' | 'upload-error' | 'parsing' | null

/** Which transient ingest badge a row shows, in precedence order: the upload
 * in flight, then a failed upload (retryable), then the parse in flight.
 * `null` = settled — the row falls back to the provenance badge. Pure so the
 * precedence is unit-testable like {@link memberCellState}. */
export function ingestBadgeState(
  asset: Pick<FileAssetRecord, 'parsePending' | 'uploadError' | 'uploadPending'>,
): IngestBadgeState {
  if (asset.uploadPending) return 'uploading'
  if (asset.uploadError != null) return 'upload-error'
  if (asset.parsePending) return 'parsing'
  return null
}

export type FileStatusKind = 'ok' | 'truncated' | 'failed'

/** Maps the parser's `parseStatus`/`textTruncated` onto the single colored
 * status signal shown on files. `parsed` is neutral (no marker). */
export function fileStatus(asset: FileAssetRecord): FileStatusKind {
  if (asset.parseStatus === 'unsupported' || asset.parseStatus === 'error') return 'failed'
  if (asset.parseStatus === 'partial') return asset.textTruncated ? 'truncated' : 'failed'
  return 'ok'
}

/** Estimated embedding-chunk count for a document. Page-based when available
 * (≈4.3 chunks/page), else size-based. This is a client-side estimate shown
 * until a real embedding backend reports actual counts. */
export function chunkEstimate(asset: FileAssetRecord): number {
  return asset.pageCount != null
    ? Math.max(40, Math.round(asset.pageCount * 4.3))
    : Math.max(24, Math.round(asset.sizeBytes / 2600))
}

/** Display-only slug for a group's `@filegroups:` handle. ASCII-only (umlauts
 * collapse to hyphens); the chat/editor resolver owns the authoritative label. */
export function groupSlug(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'group'
}

/** The knowledge-collection ids that merely BACK a local vector index. Such a
 * collection is the index's server-side storage, not a thing of its own, so the
 * rail must never list it a second time under the server collections. */
export function indexBackedCollectionIds(
  indexes: readonly Pick<VectorIndexRecord, 'serverCollectionId'>[],
): Set<string> {
  const ids = new Set<string>()
  for (const index of indexes) {
    if (index.serverCollectionId) ids.add(index.serverCollectionId)
  }
  return ids
}

/** Server collections the rail shows in their own right: everything that is
 * not the storage of a local vector index (shared-with-me collections and
 * collections created outside an index). */
export function railVisibleServerCollections<T extends { id: string }>(
  collections: readonly T[],
  backedIds: ReadonlySet<string>,
): T[] {
  return collections.filter((collection) => !backedIds.has(collection.id))
}

/** Total vectors an index serves = sum of chunk estimates over its embedded
 * members (pending members are not yet embedded). */
export function indexVectorCount(members: VectorIndexMemberResolved[]): number {
  return members.reduce(
    (total, { asset, member }) => (member.state === 'embedded' ? total + chunkEstimate(asset) : total),
    0,
  )
}
