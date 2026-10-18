import { renderAttachmentContextBlocks } from './attachmentContext'
import {
  normalizeChatRule,
  normalizeLinkedContextRefs,
} from './chatRules'
import type {
  ChatContextReferenceRecord,
  ChatMessageAttachmentRecord,
  ChatRuleRecord,
  FileAssetRecord,
  ProjectState,
} from './types'

export const contextPackPlaceholder = '{{context}}'

export function databaseContextAttachmentsFromRefs(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
  attachedAt: string,
  /** Freshly fetched asset bodies (id -> extractedText) that override the
   * state copy, used when bodies were just loaded on demand at send.
   * Absent in the common case (bodies already in state). */
  assetBodyOverride?: ReadonlyMap<string, string>,
): ChatMessageAttachmentRecord[] {
  const seen = new Set<string>()
  const bodyOf = (asset: { id: string; extractedText: string }): string =>
    assetBodyOverride?.get(asset.id) ?? asset.extractedText
  return normalizeLinkedContextRefs(refs).flatMap<ChatMessageAttachmentRecord>((ref) => {
    if (ref.kind === 'file-group') {
      const group = state.fileGroups[ref.groupId]
      if (!group) return []
      return fileAssetsForGroup(state, ref.groupId).flatMap<ChatMessageAttachmentRecord>((asset) => {
        const memberKey = `file-asset:${asset.id}`
        if (seen.has(memberKey)) return []
        seen.add(memberKey)
        return [{
          attachedAt,
          contentMarkdown: bodyOf(asset),
          fileId: asset.id,
          groupId: group.id,
          groupLabel: group.title,
          kind: 'file-group' as const,
          label: asset.label,
          pageCount: asset.pageCount,
          sizeBytes: asset.sizeBytes,
          title: asset.title,
        }]
      })
    }

    const asset = state.fileAssets[ref.fileId]
    if (!asset) return []
    const key = `file-asset:${asset.id}`
    if (seen.has(key)) return []
    seen.add(key)
    return [{
      attachedAt,
      contentMarkdown: bodyOf(asset),
      fileId: asset.id,
      kind: 'file-asset' as const,
      label: asset.label,
      pageCount: asset.pageCount,
      sizeBytes: asset.sizeBytes,
      title: asset.title,
    }]
  })
}

export function renderChatRuleAttachmentContent(
  state: ProjectState,
  rule: ChatRuleRecord,
  attachedAt: string,
  assetBodyOverride?: ReadonlyMap<string, string>,
): string {
  const normalized = normalizeChatRule(rule)
  if (normalized.category !== 'context') return normalized.contentMarkdown
  const contextAttachments = databaseContextAttachmentsFromRefs(
    state,
    normalized.linkedContextRefs ?? [],
    attachedAt,
    assetBodyOverride,
  )
  if (contextAttachments.length === 0) return normalized.contentMarkdown
  const contextBlocks = renderAttachmentContextBlocks(contextAttachments)
  if (normalized.contentMarkdown.includes(contextPackPlaceholder)) {
    return normalized.contentMarkdown.replaceAll(contextPackPlaceholder, contextBlocks)
  }
  return [
    normalized.contentMarkdown,
    '',
    contextBlocks,
  ].join('\n')
}

function fileAssetsForGroup(state: ProjectState, groupId: string): FileAssetRecord[] {
  return state.fileAssetOrder
    .map((fileId) => state.fileAssets[fileId])
    .filter((asset): asset is FileAssetRecord => Boolean(asset))
    .filter((asset) => asset.groupId === groupId)
}
