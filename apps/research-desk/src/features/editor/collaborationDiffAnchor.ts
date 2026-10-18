import {
  patchEditorDocumentMetadata,
  type ClientOptions,
  type EditorCollaborationProjection,
  type ServerEditorDocument,
} from '@/api/inqtrixClient'
import type { EditorDocumentRecord } from '@/features/project/types'
import type { CollaborationAuthorityGuard } from './collaborationAuthority'
import {
  flushCollaborationProjectionBarrier,
  type CollaborationProjectionController,
} from './collaborationProjection'
import { documentRecordFromServer } from './editorSync'

type CollaborationDiffAnchorOptions = {
  authorityGuard: CollaborationAuthorityGuard
  clientOptions: ClientOptions
  controller: CollaborationProjectionController | null
  documentId: string
  expectedMetadataRevision: number
  flushProjection?: (
    documentId: string,
    options: ClientOptions,
  ) => Promise<EditorCollaborationProjection>
  locale: 'de' | 'en'
  onAdoptMetadataRevision: (metadataRevision: number) => void
  onDocumentSaved: (document: EditorDocumentRecord) => void
  onServerDocumentObserved?: (document: EditorDocumentRecord) => void
  patchMetadata?: typeof patchEditorDocumentMetadata
}

export async function persistCollaborationDiffAnchor({
  authorityGuard,
  clientOptions,
  controller,
  documentId,
  expectedMetadataRevision,
  flushProjection,
  locale,
  onAdoptMetadataRevision,
  onDocumentSaved,
  onServerDocumentObserved,
  patchMetadata = patchEditorDocumentMetadata,
}: CollaborationDiffAnchorOptions): Promise<EditorDocumentRecord> {
  authorityGuard.assertCurrent()
  const projection = await flushCollaborationProjectionBarrier({
    authorityGuard,
    clientOptions,
    controller,
    documentId,
    generation: authorityGuard.identity.generation,
    flushProjection,
  })
  authorityGuard.assertCurrent()
  const saved: ServerEditorDocument = await patchMetadata(
    documentId,
    {
      diff_anchor_markdown: projection.markdown,
      diff_anchor_updated_at: Date.parse(projection.confirmedAt) / 1_000,
      expected_metadata_revision: expectedMetadataRevision,
    },
    clientOptions,
  )
  authorityGuard.assertCurrent()
  if (
    saved.metadata_revision === undefined
    || !Number.isSafeInteger(saved.metadata_revision)
    || saved.metadata_revision < 1
  ) {
    throw new Error(locale === 'de'
      ? 'Der Server hat keine gültige Metadatenrevision bestätigt.'
      : 'The server did not confirm a valid metadata revision.')
  }

  const savedDocument = documentRecordFromServer(saved)
  authorityGuard.assertCurrent()
  onAdoptMetadataRevision(saved.metadata_revision)
  authorityGuard.assertCurrent()
  onDocumentSaved(savedDocument)
  authorityGuard.assertCurrent()
  onServerDocumentObserved?.(savedDocument)
  return savedDocument
}
