import { ingestFiles } from '@/features/files/ingest'
import type { FileParser } from '@/features/files/parsing'
import { createFileSectionId } from '@/features/files/sections'
import type { FileAssetRecord, ProjectState } from '@/features/project/types'

/**
 * Ingest chat attachments for an incognito session: client-parse only, never
 * uploaded.
 *
 * Mirrors a normal chat attach with an ephemeral local section id
 * but deliberately omits the `serverUpload` callback, so {@link ingestFiles}
 * leaves `serverFileId === null` and the original bytes never leave the device.
 * The returned records carry the in-browser `extractedText`, which is all the
 * chat send path needs — the LLM still receives the attachment content. The
 * caller holds these records in incognito-local state (not the synced
 * `state.fileAssets`), so they are never persisted and vanish on exit.
 */
export async function ingestIncognitoFiles(
  files: readonly File[],
  existingLabels: readonly string[] = [],
  parser?: FileParser,
): Promise<FileAssetRecord[]> {
  return ingestFiles(
    files,
    { kind: 'chat', sectionId: createFileSectionId() },
    parser,
    existingLabels,
  )
}

/**
 * A read-only project view whose `fileAssets` also include the incognito-local
 * assets, used only to RESOLVE incognito attachment refs in the chat path
 * (chips, token budget, the outgoing message).
 *
 * Incognito assets are kept out of `state.fileAssets` on purpose so they never
 * sync to the server and never appear in the file library or `@files:` mention
 * list. The attachment resolvers (`chatAttachmentsFromRefs`,
 * `chatAttachmentChipsFromRefs`, `assetIdsFromChatRefs`) read
 * `state.fileAssets[id]`, so a transient merge is the single, minimal point that
 * lets those resolvers see the local asset without leaking it into the synced
 * store. Only `fileAssets` is overridden — incognito attachments are individual
 * file-assets, never file groups.
 */
export function chatStateForIncognito(
  state: ProjectState,
  incognitoAssets: Record<string, FileAssetRecord>,
): ProjectState {
  return { ...state, fileAssets: { ...state.fileAssets, ...incognitoAssets } }
}
