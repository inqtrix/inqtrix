/**
 * Pure conversion + push helpers between the local ProjectState file-library
 * records and the server wire shape (project-persistence tier).
 *
 * The file-library counterpart of features/editor/editorSync.ts: the API
 * client speaks the verbatim server shape (snake_case, unix-seconds floats);
 * this module maps it to/from the ISO-timestamped FileLibrarySectionRecord /
 * FileGroupRecord / FileAssetRecord the reducer uses. An asset's heavy
 * extractedText round-trips as ``extracted_text`` — empty on list rows,
 * present on a single-asset GET (the editor's content_markdown pattern).
 *
 * Pure (no React) so the conversion is unit-testable; the entity fingerprints
 * for the autosave diff are plain ``updatedAt`` strings (every mutation that
 * needs syncing bumps the entity's ``updatedAt``).
 */

import type { ClientOptions } from '@/api/inqtrixClient'
import {
  saveAsset,
  saveAssetGroup,
  saveAssetSection,
  type ServerAsset,
  type ServerAssetGroup,
  type ServerAssetSection,
  type ServerFileUploadBinding,
} from '@/api/inqtrixClient'
import type { UploadBinding } from '@/features/files/ingest'
import type {
  FileAssetOrigin,
  FileAssetRecord,
  FileAssetUploadStatus,
  FileGroupRecord,
  FileLibrarySectionRecord,
  FileParseStatus,
  FileSectionKind,
  FileSectionSemanticRole,
} from '@/features/project/types'
import { isPristineDefaultFileSection } from '@/features/files/sections'
import { isoFromUnixSeconds, unixSecondsFromIso } from '@/lib/time'

const VALID_KINDS: ReadonlySet<string> = new Set(['temporary', 'custom'])
const VALID_SECTION_ROLES: ReadonlySet<string> = new Set([
  'temporary',
  'library',
  'project_sources',
  'custom',
])
const VALID_ORIGINS: ReadonlySet<string> = new Set(['chat', 'editor', 'library'])
const VALID_PARSE: ReadonlySet<string> = new Set([
  'parsed',
  'partial',
  'unsupported',
  'error',
])

function normalizeKind(kind: string): FileSectionKind {
  return VALID_KINDS.has(kind) ? (kind as FileSectionKind) : 'custom'
}

function normalizeSectionRole(
  role: ServerAssetSection['semantic_role'],
): FileSectionSemanticRole | null {
  return role && VALID_SECTION_ROLES.has(role)
    ? role as FileSectionSemanticRole
    : null
}

function normalizeOrigin(origin: string): FileAssetOrigin {
  return VALID_ORIGINS.has(origin) ? (origin as FileAssetOrigin) : 'library'
}

function normalizeParse(status: string): FileParseStatus {
  return VALID_PARSE.has(status) ? (status as FileParseStatus) : 'parsed'
}

const VALID_UPLOAD: ReadonlySet<string> = new Set([
  'awaiting_upload',
  'uploading',
  'retrying',
  'parsing',
  'finalizing',
  'ready',
  'failed',
  'cancelled',
])

export function isUploadPending(status: FileAssetUploadStatus | undefined): boolean {
  return status === 'awaiting_upload'
    || status === 'uploading'
    || status === 'retrying'
    || status === 'parsing'
    || status === 'finalizing'
}

function normalizeUploadStatus(status: string | undefined): FileAssetUploadStatus {
  return status && VALID_UPLOAD.has(status)
    ? status as FileAssetUploadStatus
    : 'ready'
}

// -- server -> record ------------------------------------------------------ #

export function sectionRecordFromServer(
  section: ServerAssetSection,
): FileLibrarySectionRecord {
  return {
    createdAt: isoFromUnixSeconds(section.created_at),
    id: section.id,
    kind: normalizeKind(section.kind),
    semanticRole: normalizeSectionRole(section.semantic_role),
    serverSynced: true,
    title: section.title,
    updatedAt: isoFromUnixSeconds(section.updated_at),
  }
}

export function groupRecordFromServer(group: ServerAssetGroup): FileGroupRecord {
  return {
    createdAt: isoFromUnixSeconds(group.created_at),
    id: group.id,
    sectionId: group.section_id,
    serverSynced: true,
    title: group.title,
    updatedAt: isoFromUnixSeconds(group.updated_at),
  }
}

/** One server asset -> its local record. ``extracted_text`` is empty on a
 * list (metadata) row and the real body on a single-asset GET. */
export function assetRecordFromServer(asset: ServerAsset): FileAssetRecord {
  const uploadStatus = normalizeUploadStatus(asset.upload_status)
  return {
    createdAt: isoFromUnixSeconds(asset.created_at),
    extractedText: asset.extracted_text ?? '',
    fileName: asset.file_name,
    groupId: asset.group_id,
    id: asset.id,
    label: asset.label,
    mimeType: asset.mime_type,
    origin: normalizeOrigin(asset.origin),
    pageCount: asset.page_count,
    parseStatus: normalizeParse(asset.parse_status),
    parseWarning: asset.parse_warning,
    sectionId: asset.section_id,
    sizeBytes: asset.size_bytes,
    textTruncated: asset.text_truncated,
    title: asset.title,
    updatedAt: isoFromUnixSeconds(asset.updated_at),
    serverFileId: asset.server_file_id,
    serverSynced: true,
    parserId: asset.parser_id ?? null,
    preparedParserId: asset.prepared_parser_id ?? null,
    preparedContentHash: asset.prepared_content_hash ?? null,
    preparedAt: asset.prepared_at == null
      ? null
      : isoFromUnixSeconds(asset.prepared_at),
    preparedText: asset.prepared_text ?? '',
    lifecycleStatus: asset.lifecycle_status ?? 'active',
    deletionOperationId: asset.deletion_operation_id ?? null,
    deletionStage: asset.deletion_stage ?? null,
    deletionError: asset.deletion_error ?? null,
    uploadOperationId: asset.upload_operation_id ?? null,
    uploadPending: isUploadPending(uploadStatus),
    uploadStatus,
    uploadError: uploadStatus === 'failed'
      || uploadStatus === 'cancelled'
      || uploadStatus === 'retrying'
      ? asset.upload_error
        ?? (uploadStatus === 'cancelled'
          ? 'Upload abgebrochen.'
          : uploadStatus === 'failed'
            ? 'Server-Upload fehlgeschlagen.'
            : null)
      : null,
  }
}

/**
 * Keep the legacy rail usable without deleting or relabelling server data.
 *
 * A NULL semantic role means only "created before the identity contract".
 * Such a row cannot be proven to be either a bootstrap duplicate or an
 * intentionally same-titled custom section. For backwards-compatible
 * usability, exact untouched/unreferenced legacy default signatures remain a
 * non-destructive projection: one is shown when no canonical role exists and
 * none when a canonical role exists. This may hide an unreferenced historical
 * custom row with the same title, but never deletes it. Every newly-created
 * custom row is explicit (`semanticRole=custom`) and is always shown.
 */
export function visibleServerAssetSections(
  sections: readonly FileLibrarySectionRecord[],
  groups: readonly FileGroupRecord[],
  assets: readonly FileAssetRecord[],
): FileLibrarySectionRecord[] {
  const referencedIds = new Set([
    ...groups.map((group) => group.sectionId),
    ...assets.map((asset) => asset.sectionId),
  ])
  const referencedSignatures = new Set(
    sections
      .filter((section) =>
        referencedIds.has(section.id) && isPristineDefaultFileSection(section),
      )
      .map((section) => `${section.kind}:${section.title}`),
  )
  const keptUnreferencedSignatures = new Set<string>()
  const semanticSignatures = new Set(
    sections.flatMap((section) => {
      if (section.semanticRole === 'temporary') return ['temporary:Temporäre Dateien']
      if (section.semanticRole === 'library') return ['custom:Bibliothek']
      if (section.semanticRole === 'project_sources') return ['custom:Projekt-Quellen']
      return []
    }),
  )

  return sections.filter((section) => {
    // Every row written under the new contract is explicit. In particular,
    // `custom` sections remain visible even when their title exactly matches
    // a prepared label. Only unclassified legacy rows enter the compatibility
    // projection below.
    if (section.semanticRole !== null && section.semanticRole !== undefined) {
      return true
    }
    if (!isPristineDefaultFileSection(section)) return true
    if (referencedIds.has(section.id)) return true

    const signature = `${section.kind}:${section.title}`
    if (semanticSignatures.has(signature)) return false
    if (referencedSignatures.has(signature)) return false
    if (keptUnreferencedSignatures.has(signature)) return false
    keptUnreferencedSignatures.add(signature)
    return true
  })
}

/** Whether an asset may be autosaved right now. Placeholder rows mid-upload/
 * mid-parse are excluded from the sync diff: the bound upload (or the settle
 * actions) owns their server state, and a half-built PUT would race it with
 * an empty body. Every settle path clears both flags, so the push is only
 * deferred, never lost. */
export function isAssetSettledForSync(asset: FileAssetRecord): boolean {
  return (
    (asset.lifecycleStatus ?? 'active') === 'active'
    && !asset.uploadPending
    && !asset.parsePending
  )
}

export function assetAutosaveFingerprint(
  asset: FileAssetRecord,
  serverFingerprint: string | undefined,
): string {
  // A tracked row stays present in the sync collection while its upload or
  // parse lifecycle owns the server record. Reuse the last confirmed server
  // fingerprint during that interval: presence prevents a false delete, but
  // the incomplete local snapshot cannot authorize a full-record PUT.
  if (!isAssetSettledForSync(asset) && serverFingerprint !== undefined) {
    return serverFingerprint
  }
  return asset.updatedAt
}

// -- record -> server payload ---------------------------------------------- #

/** The wire form fields of a bound upload (`POST /v1/files`): the placement
 * the server persists together with the file bytes. Parse fields stay
 * client-side — the placeholder's neutral values match the server defaults,
 * and the real parse result follows via the regular asset autosave. */
export function serverUploadBinding(binding: UploadBinding): ServerFileUploadBinding {
  return {
    asset_id: binding.assetId,
    created_at: unixSecondsFromIso(binding.createdAt),
    group_id: binding.groupId,
    label: binding.label,
    origin: binding.origin,
    section_id: binding.sectionId,
    title: binding.title,
    updated_at: unixSecondsFromIso(binding.updatedAt),
  }
}

export function serverSectionPayload(record: FileLibrarySectionRecord): {
  created_at: number
  kind: string
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    kind: record.kind,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

export function serverGroupPayload(record: FileGroupRecord): {
  created_at: number
  section_id: string
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    section_id: record.sectionId,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

export function serverAssetPayload(record: FileAssetRecord): {
  created_at: number
  extracted_text: string
  file_name: string
  group_id: string | null
  label: string
  mime_type: string
  origin: string
  page_count: number | null
  parse_status: string
  parse_warning: string | null
  section_id: string
  server_file_id: string | null
  parser_id: string | null
  size_bytes: number
  text_truncated: boolean
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    extracted_text: record.extractedText,
    file_name: record.fileName,
    group_id: record.groupId,
    label: record.label,
    mime_type: record.mimeType,
    origin: record.origin,
    page_count: record.pageCount,
    parse_status: record.parseStatus,
    parse_warning: record.parseWarning,
    section_id: record.sectionId,
    server_file_id: record.serverFileId ?? null,
    parser_id: record.parserId ?? null,
    size_bytes: record.sizeBytes,
    text_truncated: record.textTruncated,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

// -- whole-project push (the explicit import) ------------------------------ #

/** Push ALL of a local project's file-library entities to the server (the
 * one-time import). Sections first, then groups (they reference a section),
 * then assets (with bodies — a local project has them loaded). Idempotent
 * server upserts make a re-run safe. */
export async function pushAllAssetEntities(
  args: {
    sections: Record<string, FileLibrarySectionRecord>
    groups: Record<string, FileGroupRecord>
    assets: Record<string, FileAssetRecord>
  },
  options: ClientOptions,
): Promise<void> {
  for (const section of Object.values(args.sections)) {
    await saveAssetSection(section.id, serverSectionPayload(section), options)
  }
  for (const group of Object.values(args.groups)) {
    await saveAssetGroup(group.id, serverGroupPayload(group), options)
  }
  for (const asset of Object.values(args.assets)) {
    await saveAsset(asset.id, serverAssetPayload(asset), options)
  }
}
