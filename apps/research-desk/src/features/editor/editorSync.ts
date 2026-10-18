/**
 * Pure conversion + push helpers between the local ProjectState editor
 * records and the server wire shape (project-persistence tier).
 *
 * The editor counterpart of ``features/chat/chatHistorySync.ts``: the API
 * client speaks the verbatim server shape (snake_case, unix-seconds floats);
 * this module maps it to/from the ISO-timestamped EditorDocumentRecord /
 * EditorFolderRecord / EditorCommentThreadRecord the reducer uses. Folder
 * membership is the ``folderId`` ON the document record (unlike chat, where
 * membership is a separate map). The document body round-trips as
 * ``content_markdown`` — empty on list rows, present on a single-document GET.
 *
 * Pure (no React) so the conversion is unit-testable; the entity
 * fingerprints for the autosave diff are plain ``updatedAt`` strings (every
 * editor mutation that needs syncing bumps the entity's ``updatedAt``).
 */

import type { ClientOptions } from '@/api/inqtrixClient'
import {
  saveEditorComments,
  saveEditorDocument,
  saveEditorFolder,
  type EditorPrivateSuggestionDraftWire,
  type ServerEditorComment,
  type ServerEditorDocument,
  type ServerEditorFolder,
} from '@/api/inqtrixClient'
import type {
  EditorCommentAnchorRecord,
  EditorCommentKind,
  EditorCommentStatus,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorDocumentSource,
  EditorFolderRecord,
  EditorPrivateSuggestionDraftRecord,
} from '@/features/project/types'
import { isoFromUnixSeconds, unixSecondsFromIso } from '@/lib/time'

const VALID_SOURCES: ReadonlySet<string> = new Set([
  'blank',
  'imported-research-report',
  'pasted',
  'agent-artifact',
])

function normalizeSource(source: string): EditorDocumentSource {
  return VALID_SOURCES.has(source)
    ? (source as EditorDocumentSource)
    : 'blank'
}

// -- server -> record ------------------------------------------------------ #

/** One server document -> its local record. ``content_markdown`` is empty
 * on a list (metadata) row and the real body on a single-document GET. */
export function documentRecordFromServer(
  document: ServerEditorDocument,
): EditorDocumentRecord {
  return {
    access: document.access ?? { mode: 'owner', permission: 'edit' },
    contentMarkdown: document.content_markdown ?? '',
    contentMode: document.content_mode ?? 'markdown',
    createdAt: isoFromUnixSeconds(document.created_at),
    folderId: document.folder_id,
    id: document.id,
    metadataRevision: document.metadata_revision ?? 1,
    revision: document.revision,
    serverSynced: true,
    source: normalizeSource(document.source),
    title: document.title,
    updatedAt: isoFromUnixSeconds(document.updated_at),
    ...(document.collaboration
      ? {
          collaboration: {
            commentRevision: document.collaboration.comment_revision ?? 0,
            generation: document.collaboration.generation,
            persistedSequence: document.collaboration.persisted_sequence,
            projectionSequence: document.collaboration.projection_sequence,
            schemaVersion: document.collaboration.schema_version,
            ...(document.collaboration.projection_updated_at != null
              ? {
                  projectionUpdatedAt: isoFromUnixSeconds(
                    document.collaboration.projection_updated_at,
                  ),
                }
              : {}),
          },
        }
      : {}),
    ...(document.source_run_id ? { sourceRunId: document.source_run_id } : {}),
    ...(document.diff_anchor_markdown
      ? { diffAnchorMarkdown: document.diff_anchor_markdown }
      : {}),
    ...(document.diff_anchor_updated_at != null
      ? { diffAnchorUpdatedAt: isoFromUnixSeconds(document.diff_anchor_updated_at) }
      : {}),
  }
}

/** Local-only documents predate the content mode field and remain markdown. */
export function isCollaborationDocument(record: EditorDocumentRecord): boolean {
  return record.contentMode === 'collaboration'
}

export function folderRecordFromServer(folder: ServerEditorFolder): EditorFolderRecord {
  return {
    createdAt: isoFromUnixSeconds(folder.created_at),
    id: folder.id,
    title: folder.title,
    updatedAt: isoFromUnixSeconds(folder.updated_at),
  }
}

export function commentRecordFromServer(
  comment: ServerEditorComment,
): EditorCommentThreadRecord {
  return {
    anchor: comment.anchor as unknown as EditorCommentAnchorRecord,
    commentMarkdown: comment.comment_markdown,
    createdAt: isoFromUnixSeconds(comment.created_at),
    documentId: comment.document_id,
    id: comment.id,
    kind: comment.kind as EditorCommentKind,
    status: comment.status as EditorCommentStatus,
    updatedAt: isoFromUnixSeconds(comment.updated_at),
    ...(comment.suggestion_draft
      ? {
          suggestionDraft: privateSuggestionDraftRecordFromServer(
            comment.suggestion_draft,
          ),
        }
      : {}),
  }
}

export function privateSuggestionDraftRecordFromServer(
  draft: EditorPrivateSuggestionDraftWire,
): EditorPrivateSuggestionDraftRecord {
  return {
    ...(draft.anchor_text ? { anchorText: draft.anchor_text } : {}),
    anchorVersion: draft.anchor_version,
    changeSummary: [...draft.change_summary],
    createdAt: isoFromUnixSeconds(draft.created_at),
    // Bestandsentwuerfe kennen das Feld nicht; es fehlt dort legitim und
    // bedeutet weiterhin "Ersetzung", wie beim Kommentarweg schon immer.
    ...(draft.edit_position ? { editPosition: draft.edit_position } : {}),
    groupId: draft.group_id,
    patchId: draft.patch_id,
    proposedText: draft.proposed_text,
    publicationCommandId: draft.publication_command_id,
    revision: draft.revision,
    revisionHistory: draft.revision_history.map((revision) => ({
      changeSummary: [...revision.change_summary],
      createdAt: isoFromUnixSeconds(revision.created_at),
      ...(revision.instruction ? { instruction: revision.instruction } : {}),
      proposedText: revision.proposed_text,
      source: revision.source,
      warnings: [...revision.warnings],
    })),
    suggestionId: draft.suggestion_id,
    updatedAt: isoFromUnixSeconds(draft.updated_at),
    warnings: [...draft.warnings],
    ...(draft.evidence
      ? {
          evidence: {
            mode: draft.evidence.mode,
            sources: draft.evidence.sources.map((source) => ({ ...source })),
          },
        }
      : {}),
  }
}

// -- record -> server payload ---------------------------------------------- #

export function serverDocumentPayload(record: EditorDocumentRecord): {
  content_markdown: string
  created_at: number
  diff_anchor_markdown: string | null
  diff_anchor_updated_at: number | null
  folder_id: string | null
  revision: number
  source: string
  source_run_id: string | null
  title: string
  updated_at: number
} {
  return {
    content_markdown: record.contentMarkdown,
    created_at: unixSecondsFromIso(record.createdAt),
    diff_anchor_markdown: record.diffAnchorMarkdown ?? null,
    diff_anchor_updated_at: record.diffAnchorUpdatedAt
      ? unixSecondsFromIso(record.diffAnchorUpdatedAt)
      : null,
    folder_id: record.folderId,
    // `record.revision` is the last-synced SERVER revision (the base this
    // edit is built on); the save creates base+1. The store CAS accepts it
    // only when the stored revision is still that base, so a stale writer
    // (base behind the server) gets a 409 to rebase -- the same
    // read-current/write-current+1 contract the agent patch path uses.
    revision: record.revision + 1,
    source: record.source,
    source_run_id: record.sourceRunId ?? null,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

export function serverFolderPayload(record: EditorFolderRecord): {
  created_at: number
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

export function serverCommentPayload(record: EditorCommentThreadRecord): {
  anchor: Record<string, unknown>
  comment_markdown: string
  created_at: number
  evidence_preset: string | null
  id: string
  kind: string
  status: string
  updated_at: number
} {
  return {
    anchor: record.anchor as unknown as Record<string, unknown>,
    comment_markdown: record.commentMarkdown,
    created_at: unixSecondsFromIso(record.createdAt),
    // Evidence presets never reach a model, so the client writes none;
    // the column stays for stored rows.
    evidence_preset: null,
    id: record.id,
    kind: record.kind,
    status: record.status,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

// -- entity fingerprints (autosave diff) ----------------------------------- #

/** The editor entities all sync on a plain ``updatedAt`` change (every
 * mutation that matters bumps it). String fingerprint + string-equality. */
export function fingerprintByUpdatedAt(record: { updatedAt: string }): string {
  return record.updatedAt
}

export function stringChanged(previous: string | undefined, current: string): boolean {
  return previous !== current
}

export function editorEntitiesForServerImport(args: {
  comments: Record<string, EditorCommentThreadRecord>
  documents: Record<string, EditorDocumentRecord>
}): {
  comments: Record<string, EditorCommentThreadRecord>
  documents: Record<string, EditorDocumentRecord>
} {
  const documents = Object.fromEntries(
    Object.entries(args.documents).filter(([, document]) => (
      document.access?.mode !== 'shared'
      && document.recovery === undefined
    )),
  )
  return {
    comments: Object.fromEntries(
      Object.entries(args.comments).filter(([, comment]) => (
        documents[comment.documentId] !== undefined
      )),
    ),
    documents,
  }
}

// -- whole-project push (the explicit import) ------------------------------ #

/** Push ALL of a local project's editor entities to the server (the
 * one-time import). Folders first (documents reference them), then
 * documents (with bodies — a local project has them loaded), then comments
 * batched per document. Idempotent server upserts make a re-run safe. */
export async function pushAllEditorEntities(
  args: {
    documents: Record<string, EditorDocumentRecord>
    folders: Record<string, EditorFolderRecord>
    comments: Record<string, EditorCommentThreadRecord>
  },
  options: ClientOptions,
): Promise<void> {
  const importable = editorEntitiesForServerImport(args)
  for (const folder of Object.values(args.folders)) {
    await saveEditorFolder(folder.id, serverFolderPayload(folder), options)
  }
  for (const document of Object.values(importable.documents)) {
    await saveEditorDocument(
      document.id,
      serverDocumentPayload(document),
      options,
    )
  }
  const byDocument = new Map<string, EditorCommentThreadRecord[]>()
  for (const comment of Object.values(importable.comments)) {
    const bucket = byDocument.get(comment.documentId) ?? []
    bucket.push(comment)
    byDocument.set(comment.documentId, bucket)
  }
  for (const [documentId, comments] of byDocument) {
    await saveEditorComments(
      documentId,
      comments.map(serverCommentPayload),
      options,
    )
  }
}
