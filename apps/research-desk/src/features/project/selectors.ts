import { countLabel } from '@/i18n/plural'
import type { Locale } from '@/i18n/translations'
import type {
  ChatContextReferenceRecord,
  ChatMessageAttachmentRecord,
  ChatRuleCategory,
  ChatRuleRecord,
  ChatRuleVisibility,
  ChatThreadGroupRecord,
  ChatThreadRecord,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorFolderRecord,
  FileAssetBodyLoadState,
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  ProjectState,
  ResearchRunRecord,
  VectorIndexMemberRecord,
  VectorIndexRecord,
} from './types'
import { type JobPhase, type LocalizedText, type ResearchJob } from '@/features/researchDesk/types'
import type { ReferenceDoc } from '@/features/files/referenceBlocks'
import { sortExplorerFolders, sortExplorerItems } from './explorerSort'
import {
  normalizeChatRule,
  normalizeLinkedContextRefs,
} from './chatRules'
import {
  renderChatRuleAttachmentContent,
} from './chatRuleRendering'

export type CompletedReportOption = {
  label: string
  markdown: string
  runId: string
  title: string
}

export type ChatRuleOption = {
  category: ChatRuleCategory
  /** Composed size the server measured (see `ChatRuleRecord`). */
  composedChars?: number
  includeInAutocomplete: boolean
  label: string
  linkedContextRefs: ChatContextReferenceRecord[]
  markdown: string
  /** The LOCAL id the chat keys its attachments by. A rule pushed to the
   * server keeps it until the next hydrate, so it is not a wire id. */
  ruleId: string
  /** The id the server resolves (absent = browser-local rule). */
  serverTemplateId?: string
  serverRevision?: number
  title: string
  visibility: ChatRuleVisibility
}

export type ChatRuleSurface = 'agent' | 'chat' | 'editor'

export type ChatHistorySection =
  | {
    group: ChatThreadGroupRecord
    groupId: string
    kind: 'group'
    threads: ChatThreadRecord[]
  }
  | {
    groupId: null
    kind: 'ungrouped'
    threads: ChatThreadRecord[]
  }

export type KnowledgeSessionHistorySection =
  | {
    group: KnowledgeSessionGroupRecord
    groupId: string
    kind: 'group'
    sessions: KnowledgeSessionRecord[]
  }
  | {
    groupId: null
    kind: 'ungrouped'
    sessions: KnowledgeSessionRecord[]
  }

/**
 * Whether a run belongs to the research-desk surface — its job cards, the
 * editor "import report" picker, and the chat `@research` mentions. That is
 * every mode EXCEPT the "Wissen" thread: knowledge-mode runs ride the same
 * run pipeline but belong to the knowledge workspace and must not appear as
 * importable reports. A missing `mode` is a legacy run and counts as research
 * (so older reports stay visible). This is the SINGLE definition shared by
 * `projectResearchJobs` and `completedReportOptions`, so the desk list and the
 * editor/chat lists can never drift apart again.
 */
export function isResearchDeskRun(run: ResearchRunRecord): boolean {
  if ((run.mode ?? 'research') === 'knowledge') return false
  // Workspace-agent runs (and their child runs) live on the Agent Desk;
  // showing them here would duplicate every agent assignment as a
  // research card.
  if (run.mode === 'workspace_agent') return false
  return (run.kind ?? 'standard') === 'standard'
}

export function projectResearchJobs(state: ProjectState): ResearchJob[] {
  return state.researchRunOrder
    .map((runId) => state.researchRuns[runId])
    .filter((run): run is ResearchRunRecord => Boolean(run))
    .filter(isResearchDeskRun)
    .map(researchRunToJob)
}

export function projectKnowledgeItems(state: ProjectState) {
  const selectedSessionId = state.selectedKnowledgeSessionId
  return projectAllKnowledgeItems(state)
    .filter((item) => selectedSessionId === null || item.sessionId === selectedSessionId)
}

export function projectAllKnowledgeItems(state: ProjectState) {
  return state.knowledgeItemOrder
    .map((itemId) => state.knowledgeItems[itemId])
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
}

export function projectKnowledgeSessions(state: ProjectState): KnowledgeSessionRecord[] {
  return state.knowledgeSessionOrder
    .map((sessionId) => state.knowledgeSessions[sessionId])
    .filter((session): session is KnowledgeSessionRecord => Boolean(session))
}

export function projectKnowledgeSessionSections(state: ProjectState): KnowledgeSessionHistorySection[] {
  const groupOrder = state.knowledgeSessionGroupOrder ?? []
  const groups = state.knowledgeSessionGroups ?? {}
  const memberships = state.knowledgeSessionGroupMemberships ?? {}
  const validGroupIds = new Set(groupOrder.filter((groupId) => Boolean(groups[groupId])))
  const groupedSessions = new Map<string, KnowledgeSessionRecord[]>()
  const ungroupedSessions: KnowledgeSessionRecord[] = []

  for (const groupId of validGroupIds) {
    groupedSessions.set(groupId, [])
  }

  for (const session of projectKnowledgeSessions(state)) {
    const groupId = memberships[session.id]
    if (groupId && validGroupIds.has(groupId)) {
      groupedSessions.get(groupId)?.push(session)
    } else {
      ungroupedSessions.push(session)
    }
  }

  // Sorting: automatic modes order folder sections alphabetically
  // and the rows inside every section by activity/name; manual keeps
  // the explicit arrays (drag placement) untouched.
  const mode = state.ui.explorerSort.knowledge
  const groupSections = groupOrder.flatMap((groupId) => {
    const group = groups[groupId]
    if (!group) return []
    return [{
      group,
      groupId,
      kind: 'group' as const,
      sessions: sortExplorerItems(
        groupedSessions.get(groupId) ?? [],
        mode,
        (session) => session.updatedAt,
        (session) => session.title,
      ),
    }]
  })
  const sections: KnowledgeSessionHistorySection[] = sortExplorerFolders(
    groupSections,
    mode,
    (section) => section.group.title,
  )

  if (ungroupedSessions.length > 0 || sections.length === 0 || groupOrder.length > 0) {
    sections.push({
      groupId: null,
      kind: 'ungrouped',
      sessions: sortExplorerItems(
        ungroupedSessions,
        mode,
        (session) => session.updatedAt,
        (session) => session.title,
      ),
    })
  }

  return sections
}

export function selectedResearchRun(state: ProjectState) {
  const selectedJobId = state.ui.selectedJobId
  return selectedJobId ? state.researchRuns[selectedJobId] ?? null : null
}

/** SSOT for a chat row's activity time: the last message's createdAt,
 * falling back to updatedAt — the sidebar sorts AND labels with this. */
export function chatThreadActivityTimeIso(thread: ChatThreadRecord): string {
  return thread.messages[thread.messages.length - 1]?.createdAt ?? thread.updatedAt
}

export function projectChatThreads(state: ProjectState): ChatThreadRecord[] {
  return state.chatThreadOrder
    .map((threadId) => state.chatThreads[threadId])
    .filter((thread): thread is ChatThreadRecord => Boolean(thread))
}

export function projectChatHistorySections(state: ProjectState): ChatHistorySection[] {
  const validGroupIds = new Set(state.chatThreadGroupOrder.filter((groupId) => Boolean(state.chatThreadGroups[groupId])))
  const groupedThreads = new Map<string, ChatThreadRecord[]>()
  const ungroupedThreads: ChatThreadRecord[] = []

  for (const groupId of validGroupIds) {
    groupedThreads.set(groupId, [])
  }

  for (const thread of projectChatThreads(state)) {
    const groupId = state.chatThreadGroupMemberships[thread.id]
    if (groupId && validGroupIds.has(groupId)) {
      groupedThreads.get(groupId)?.push(thread)
    } else {
      ungroupedThreads.push(thread)
    }
  }

  // Sorting: same three-mode vocabulary as every desk rail.
  // Activity time is thread.updatedAt (bumped by every exchange), so
  // `recent` finally re-bubbles active conversations — the underlying
  // chatThreadOrder stays the untouched created_at pagination prefix.
  const mode = state.ui.explorerSort.chat
  // Activity time == the row's visible age label (last message, with
  // updatedAt only as fallback) so 'recent' never reorders without a
  // visible cause (renames/message edits bump updatedAt, not activity).
  const groupSections = state.chatThreadGroupOrder.flatMap((groupId) => {
    const group = state.chatThreadGroups[groupId]
    if (!group) return []
    return [{
      group,
      groupId,
      kind: 'group' as const,
      threads: sortExplorerItems(
        groupedThreads.get(groupId) ?? [],
        mode,
        chatThreadActivityTimeIso,
        (thread) => thread.title,
      ),
    }]
  })
  const sections: ChatHistorySection[] = sortExplorerFolders(
    groupSections,
    mode,
    (section) => section.group.title,
  )

  if (ungroupedThreads.length > 0 || sections.length === 0 || state.chatThreadGroupOrder.length > 0) {
    sections.push({
      groupId: null,
      kind: 'ungrouped',
      threads: sortExplorerItems(
        ungroupedThreads,
        mode,
        chatThreadActivityTimeIso,
        (thread) => thread.title,
      ),
    })
  }

  return sections
}

export function projectChatRules(state: ProjectState): ChatRuleRecord[] {
  return state.chatRuleOrder
    .map((ruleId) => state.chatRules[ruleId])
    .filter((rule): rule is ChatRuleRecord => Boolean(rule))
    .map(normalizeChatRule)
}

export function selectedChatThread(state: ProjectState) {
  const selectedThreadId = state.ui.selectedChatThreadId
  return selectedThreadId ? state.chatThreads[selectedThreadId] ?? null : null
}

export function projectEditorFolders(state: ProjectState): EditorFolderRecord[] {
  return state.editorFolderOrder
    .map((folderId) => state.editorFolders[folderId])
    .filter((folder): folder is EditorFolderRecord => Boolean(folder))
}

export function projectEditorDocuments(state: ProjectState): EditorDocumentRecord[] {
  return state.editorDocumentOrder
    .map((documentId) => state.editorDocuments[documentId])
    .filter((document): document is EditorDocumentRecord => Boolean(document))
}

/** Recovery copies remain visible in the editor tree but are not server
 * resources and therefore cannot be selected as workspace-agent targets. */
export function projectAgentTargetEditorDocuments(state: ProjectState): EditorDocumentRecord[] {
  return projectEditorDocuments(state).filter((document) => document.recovery === undefined)
}

export function openEditorDocuments(state: ProjectState): EditorDocumentRecord[] {
  return state.editorUi.openDocumentIds
    .map((documentId) => state.editorDocuments[documentId])
    .filter((document): document is EditorDocumentRecord => Boolean(document))
}

export function selectedEditorDocument(state: ProjectState) {
  const selectedDocumentId = state.editorUi.activeDocumentId
  return selectedDocumentId ? state.editorDocuments[selectedDocumentId] ?? null : null
}

export function editorCommentsForDocument(
  state: ProjectState,
  documentId: string | null,
): EditorCommentThreadRecord[] {
  if (!documentId) return []
  return Object.values(state.editorComments)
    .filter((comment) => comment.documentId === documentId)
    .sort((a, b) => {
      const byStatus = commentStatusRank(a.status) - commentStatusRank(b.status)
      return byStatus || a.anchor.from - b.anchor.from || b.updatedAt.localeCompare(a.updatedAt)
    })
}

export function completedReportOptions(state: ProjectState): CompletedReportOption[] {
  const reports = state.researchRunOrder
    .map((runId) => state.researchRuns[runId])
    .filter((run): run is ResearchRunRecord & { result: { markdown: string } } => {
      return run?.status === 'completed'
        && Boolean(run.result?.markdown)
        && isResearchDeskRun(run)
    })
    .map((run) => ({
      label: slugLabel(run.summary.title, run.runId, 'report'),
      markdown: run.result.markdown,
      runId: run.runId,
      title: run.summary.title,
    }))
  return withUniqueLabels(reports)
}

/**
 * The subset of {@link completedReportOptions} the user has left available for
 * the `@research` mention autocomplete. Gate ONLY the mention source with this;
 * `completedReportOptions` still resolves already-attached report chips, so
 * hiding a report from new mentions never strips it from a chat that already
 * references it.
 */
export function mentionableReportOptions(state: ProjectState): CompletedReportOption[] {
  return completedReportOptions(state).filter(
    (option) => state.researchRuns[option.runId]?.includeInAutocomplete !== false,
  )
}

function commentStatusRank(status: EditorCommentThreadRecord['status']) {
  if (status === 'open') return 0
  if (status === 'stale') return 1
  return 2
}

export function chatRuleOptions(state: ProjectState, surface?: ChatRuleSurface): ChatRuleOption[] {
  return chatRuleOptionsFromRules(projectChatRules(state), surface)
}

export function chatRuleOptionsFromRules(
  rules: readonly ChatRuleRecord[],
  surface?: ChatRuleSurface,
): ChatRuleOption[] {
  return rules
    .map(normalizeChatRule)
    .filter((rule) => (
      surface
        ? rule.includeInAutocomplete !== false && rule.visibility?.[surface] !== false
        : true
    ))
    .map((rule) => ({
      category: rule.category ?? 'instruction',
      composedChars: rule.serverComposedChars,
      includeInAutocomplete: rule.includeInAutocomplete ?? true,
      label: rule.label,
      linkedContextRefs: rule.linkedContextRefs ?? [],
      markdown: rule.contentMarkdown,
      ruleId: rule.id,
      serverRevision: rule.serverRevision,
      serverTemplateId: rule.serverTemplateId,
      title: rule.title,
      visibility: rule.visibility
        ?? { agent: false, chat: true, editor: true },
    }))
}

export type FileMentionOption = {
  fileId: string
  label: string
  pageCount: number | null
  sizeBytes: number
  title: string
}

export type FileGroupMentionOption = {
  fileCount: number
  groupId: string
  label: string
  title: string
}

export function projectFileLibrarySections(state: ProjectState): FileLibrarySectionRecord[] {
  return state.fileLibrarySectionOrder
    .map((sectionId) => state.fileLibrarySections[sectionId])
    .filter((section): section is FileLibrarySectionRecord => Boolean(section))
}

export function projectFileAssets(state: ProjectState): FileAssetRecord[] {
  return state.fileAssetOrder
    .map((fileId) => state.fileAssets[fileId])
    .filter((asset): asset is FileAssetRecord => Boolean(asset))
}

export function projectFileGroups(state: ProjectState): FileGroupRecord[] {
  return state.fileGroupOrder
    .map((groupId) => state.fileGroups[groupId])
    .filter((group): group is FileGroupRecord => Boolean(group))
}

export function fileGroupsForSection(state: ProjectState, sectionId: string): FileGroupRecord[] {
  return projectFileGroups(state).filter((group) => group.sectionId === sectionId)
}

export function fileAssetsForSection(state: ProjectState, sectionId: string): FileAssetRecord[] {
  return projectFileAssets(state).filter((asset) => asset.sectionId === sectionId)
}

export function fileAssetsForGroup(state: ProjectState, groupId: string): FileAssetRecord[] {
  return projectFileAssets(state).filter((asset) => asset.groupId === groupId)
}

/** The asset ids referenced by chat-context refs, including those reached
 * indirectly through a context chat-rule's linked refs (a file-group expands
 * to its member assets). Used to prefetch/await asset bodies before any
 * of them is read synchronously into an attachment — at chat send, an editor
 * AI run, or a context-rule render. Must stay exhaustive: any ref shape that
 * resolves to an asset body downstream has to be covered here, or that body
 * can be sent empty on a fresh (server-hydrated, body-less) device. */
export function assetIdsFromChatRefs(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
): string[] {
  const ids = new Set<string>()
  const addContextRef = (ref: ChatContextReferenceRecord) => {
    if (ref.kind === 'file-asset') ids.add(ref.fileId)
    else if (ref.kind === 'file-group') {
      for (const asset of fileAssetsForGroup(state, ref.groupId)) ids.add(asset.id)
    }
  }
  for (const ref of refs) {
    if (ref.kind === 'file-asset' || ref.kind === 'file-group') {
      addContextRef(ref)
    } else if (ref.kind === 'chat-rule') {
      const rule = state.chatRules[ref.ruleId]
      if (!rule) continue
      const normalized = normalizeChatRule(rule)
      if (normalized.category !== 'context') continue
      for (const linked of normalizeLinkedContextRefs(normalized.linkedContextRefs ?? [])) {
        addContextRef(linked)
      }
    }
  }
  return [...ids]
}

export type AttachmentContextReadinessStatus = 'failed' | 'pending' | 'ready'

export type AttachmentContextReadinessReason =
  | 'content_empty'
  | 'group_empty'
  | 'missing'
  | 'parse_failed'
  | 'server_preparation_missing'
  | 'source_deleting'
  | 'upload_failed'
  | 'upload_not_bound'
  | 'upload_pending'

export type AttachmentContextReadiness = {
  /** Optional provider/server detail. It is safe to display, but never used as
   * the sole explanation because it may be absent after a reload. */
  error: string | null
  /** Assets whose durable upload can be explicitly retried. */
  retryAssetIds: string[]
  reason: AttachmentContextReadinessReason | null
  status: AttachmentContextReadinessStatus
}

type AttachmentContextReadinessOptions = {
  /** Incognito is the only supported local-file exception. Normal Chat and
   * Editor calls deliberately leave this false, so a client parse cannot
   * silently stand in for a missing server source. */
  allowLocalFiles?: boolean
  /** Fresh load-on-use bodies. In connected mode these are canonical
   * server-prepared bodies; only explicit incognito uses local extraction. */
  assetBodyOverride?: ReadonlyMap<string, string>
  /** Load-on-use state for metadata-only server assets. Without an observed
   * successful load, an empty local body is pending rather than falsely ready. */
  bodyLoadStates?: Readonly<Record<string, FileAssetBodyLoadState>>
  /** The metadata phase permits a ready server asset whose body is lazy.
   * Immediately before a model call, requireContent must be true. */
  requireContent?: boolean
}

const UPLOAD_TERMINAL_FAILURES = new Set(['cancelled', 'failed'])
const UPLOAD_PENDING_STATES = new Set([
  'awaiting_upload',
  'uploading',
  'retrying',
  'parsing',
  'finalizing',
])

function failedAttachmentReadiness(
  reason: AttachmentContextReadinessReason,
  error: string | null = null,
  retryAssetIds: string[] = [],
): AttachmentContextReadiness {
  return { error, reason, retryAssetIds, status: 'failed' }
}

function assetAttachmentReadiness(
  asset: FileAssetRecord,
  options: AttachmentContextReadinessOptions,
): AttachmentContextReadiness {
  if ((asset.lifecycleStatus ?? 'active') !== 'active') {
    return failedAttachmentReadiness(
      'source_deleting',
      asset.deletionError ?? null,
    )
  }
  if (UPLOAD_TERMINAL_FAILURES.has(asset.uploadStatus ?? '')) {
    return failedAttachmentReadiness(
      'upload_failed',
      asset.uploadError ?? null,
      [asset.id],
    )
  }
  if (asset.uploadPending || UPLOAD_PENDING_STATES.has(asset.uploadStatus ?? '')) {
    return {
      error: asset.uploadError ?? null,
      reason: 'upload_pending',
      retryAssetIds: [],
      status: 'pending',
    }
  }

  if (!options.allowLocalFiles) {
    if (asset.uploadStatus !== 'ready' || !asset.serverFileId) {
      return failedAttachmentReadiness(
        'upload_not_bound',
        asset.uploadError ?? null,
        asset.uploadStatus === 'failed' || asset.uploadStatus === 'cancelled'
          ? [asset.id]
          : [],
      )
    }
    if (
      !asset.preparedParserId
      || !asset.preparedContentHash
      || !asset.preparedAt
    ) {
      return failedAttachmentReadiness(
        'server_preparation_missing',
        'Die serverseitig vorbereitete Dokumentquelle ist nicht verfügbar.',
      )
    }
  }

  if (asset.parsePending) {
    return {
      error: null,
      reason: 'upload_pending',
      retryAssetIds: [],
      status: 'pending',
    }
  }
  if (asset.parseStatus === 'error' || asset.parseStatus === 'unsupported') {
    return failedAttachmentReadiness('parse_failed', asset.parseWarning)
  }
  if (
    !options.allowLocalFiles
    && !options.requireContent
    && !asset.preparedText?.trim()
  ) {
    const bodyState = options.bodyLoadStates?.[asset.id]
    if (bodyState?.status === 'failed') {
      return failedAttachmentReadiness('content_empty', bodyState.error)
    }
    if (bodyState?.status !== 'ready') {
      return {
        error: null,
        reason: 'upload_pending',
        retryAssetIds: [],
        status: 'pending',
      }
    }
  }
  if (options.requireContent) {
    const body = options.assetBodyOverride?.get(asset.id)
      ?? (options.allowLocalFiles ? asset.extractedText : asset.preparedText ?? '')
    if (!body.trim()) return failedAttachmentReadiness('content_empty')
  }
  return { error: null, reason: null, retryAssetIds: [], status: 'ready' }
}

function mergeAttachmentReadiness(
  results: readonly AttachmentContextReadiness[],
): AttachmentContextReadiness {
  const failed = results.find((result) => result.status === 'failed')
  if (failed) {
    return {
      ...failed,
      retryAssetIds: [...new Set(results.flatMap((result) => result.retryAssetIds))],
    }
  }
  const pending = results.find((result) => result.status === 'pending')
  if (pending) return pending
  return { error: null, reason: null, retryAssetIds: [], status: 'ready' }
}

/**
 * Single attachment admission contract for Chat, Editor and their shared
 * chips. It is intentionally evaluated twice: metadata readiness controls the
 * UI, then content readiness runs after load-on-use and immediately before a
 * model request. A group is atomic — one pending/failed child blocks the whole
 * group instead of silently sending the remaining subset.
 */
export function attachmentContextReadiness(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
  options: AttachmentContextReadinessOptions = {},
): AttachmentContextReadiness {
  const results: AttachmentContextReadiness[] = []
  const visit = (ref: ChatContextReferenceRecord) => {
    if (ref.kind === 'file-asset') {
      const asset = state.fileAssets[ref.fileId]
      results.push(
        asset
          ? assetAttachmentReadiness(asset, options)
          : failedAttachmentReadiness('missing'),
      )
      return
    }
    if (ref.kind === 'file-group') {
      const group = state.fileGroups[ref.groupId]
      if (!group) {
        results.push(failedAttachmentReadiness('missing'))
        return
      }
      if ((group.lifecycleStatus ?? 'active') !== 'active') {
        results.push(
          failedAttachmentReadiness(
            'source_deleting',
            group.deletionError ?? null,
          ),
        )
        return
      }
      const assets = fileAssetsForGroup(state, ref.groupId)
      if (assets.length === 0) {
        results.push(failedAttachmentReadiness('group_empty'))
        return
      }
      results.push(...assets.map((asset) => assetAttachmentReadiness(asset, options)))
      return
    }
    if (ref.kind === 'research-report') {
      if (!state.researchRuns[ref.runId]) results.push(failedAttachmentReadiness('missing'))
      return
    }
    const rule = state.chatRules[ref.ruleId]
    if (!rule) {
      results.push(failedAttachmentReadiness('missing'))
      return
    }
    const normalized = normalizeChatRule(rule)
    if (normalized.category !== 'context') return
    for (const linked of normalizeLinkedContextRefs(normalized.linkedContextRefs ?? [])) {
      visit(linked)
    }
  }

  for (const ref of refs) visit(ref)
  return mergeAttachmentReadiness(results)
}

export function projectVectorIndexes(state: ProjectState): VectorIndexRecord[] {
  return state.vectorIndexOrder
    .map((indexId) => state.vectorIndexes[indexId])
    .filter((index): index is VectorIndexRecord => Boolean(index))
}

export function vectorIndexById(state: ProjectState, indexId: string): VectorIndexRecord | null {
  return state.vectorIndexes[indexId] ?? null
}

export type VectorIndexMemberResolved = {
  asset: FileAssetRecord
  member: VectorIndexMemberRecord
}

export function vectorIndexMembersResolved(state: ProjectState, indexId: string): VectorIndexMemberResolved[] {
  const index = state.vectorIndexes[indexId]
  if (!index) return []
  return index.members.flatMap((member) => {
    const asset = state.fileAssets[member.fileId]
    return asset ? [{ asset, member }] : []
  })
}

/** Durable reference count for the file-library "used in" column: vector-index
 * memberships plus chat threads that attach the file directly (`file-asset`)
 * or via a group it belongs to (`file-group`). Each thread counts once. Keys
 * on ids, never labels (labels are deduped by withUniqueLabels). */
export function fileAssetReferenceCount(state: ProjectState, fileId: string): number {
  const asset = state.fileAssets[fileId]
  if (!asset) return 0
  const indexCount = Object.values(state.vectorIndexes).filter((index) =>
    index.members.some((member) => member.fileId === fileId),
  ).length
  const groupId = asset.groupId
  let threadCount = 0
  for (const thread of Object.values(state.chatThreads)) {
    const referenced = thread.messages.some((message) =>
      (message.attachments ?? []).some((attachment) => {
        if (attachment.kind === 'file-asset') return attachment.fileId === fileId
        if (attachment.kind === 'file-group') return groupId != null && attachment.groupId === groupId
        return false
      }),
    )
    if (referenced) threadCount += 1
  }
  return indexCount + threadCount
}

/** One-pass variant of {@link fileAssetReferenceCount} for whole-list
 * rendering (the library renders a count per row; the per-id scan would be
 * O(rows x (indexes + threads x messages))). Returns the same number per
 * existing asset id; ids referenced by stale members/attachments may appear
 * as extra keys and are simply not looked up. */
export function fileAssetReferenceCounts(state: ProjectState): Map<string, number> {
  const counts = new Map<string, number>()
  const bump = (fileId: string) => counts.set(fileId, (counts.get(fileId) ?? 0) + 1)
  // Each vector index contributes once per distinct member file.
  for (const index of Object.values(state.vectorIndexes)) {
    const seen = new Set<string>()
    for (const member of index.members) {
      if (seen.has(member.fileId)) continue
      seen.add(member.fileId)
      bump(member.fileId)
    }
  }
  const groupMembers = new Map<string, string[]>()
  for (const asset of Object.values(state.fileAssets)) {
    if (asset.groupId == null) continue
    const list = groupMembers.get(asset.groupId)
    if (list) list.push(asset.id)
    else groupMembers.set(asset.groupId, [asset.id])
  }
  // Each thread counts once per asset, whether attached directly, via its
  // group, or both.
  for (const thread of Object.values(state.chatThreads)) {
    const referenced = new Set<string>()
    for (const message of thread.messages) {
      for (const attachment of message.attachments ?? []) {
        if (attachment.kind === 'file-asset') referenced.add(attachment.fileId)
        else if (attachment.kind === 'file-group') {
          for (const id of groupMembers.get(attachment.groupId) ?? []) referenced.add(id)
        }
      }
    }
    for (const id of referenced) bump(id)
  }
  return counts
}

export function projectStorageTotalBytes(state: ProjectState): number {
  return projectFileAssets(state).reduce((total, asset) => total + asset.sizeBytes, 0)
}

export function fileMentionOptions(state: ProjectState): FileMentionOption[] {
  return withUniqueLabels(projectFileAssets(state).map((asset) => ({
    fileId: asset.id,
    label: asset.label,
    pageCount: asset.pageCount,
    sizeBytes: asset.sizeBytes,
    title: asset.title,
  })))
}

export function fileGroupMentionOptions(state: ProjectState): FileGroupMentionOption[] {
  return withUniqueLabels(projectFileGroups(state).map((group) => ({
    fileCount: fileAssetsForGroup(state, group.id).length,
    groupId: group.id,
    label: slugLabel(group.title, group.id, 'group'),
    title: group.title,
  })))
}

export function pendingChatAttachments(state: ProjectState): ChatMessageAttachmentRecord[] {
  return chatAttachmentsFromRefs(state, state.ui.pendingChatAttachmentRefs)
}

export function pendingChatReportAttachment(state: ProjectState) {
  return pendingChatAttachments(state).find((attachment) => attachment.kind === 'research-report') ?? null
}

export function chatAttachmentsFromRefs(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
  /** Freshly fetched asset bodies (id -> extractedText) that override the
   * state copy. Used at chat send when the bodies were just loaded on
   * demand: the dispatched bodies have not yet reached this state snapshot, so
   * the awaited fetch results are passed in directly to avoid empty
   * attachments. Absent in the common case (bodies already in state). */
  assetBodyOverride?: ReadonlyMap<string, string>,
): ChatMessageAttachmentRecord[] {
  const attachedAt = new Date().toISOString()
  const reports = completedReportOptions(state)
  const rules = projectChatRules(state)
  const seen = new Set<string>()
  const bodyOf = (asset: { id: string; extractedText: string }): string =>
    assetBodyOverride?.get(asset.id) ?? asset.extractedText

  return refs.flatMap<ChatMessageAttachmentRecord>((ref) => {
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

    const key = chatContextRefKey(ref)
    if (seen.has(key)) return []
    seen.add(key)

    if (ref.kind === 'research-report') {
      const report = reports.find((option) => option.runId === ref.runId)
      if (!report) return []
      return [{
        attachedAt,
        contentMarkdown: report.markdown,
        kind: 'research-report' as const,
        label: report.label,
        runId: report.runId,
        title: report.title,
      }]
    }

    if (ref.kind === 'file-asset') {
      const asset = state.fileAssets[ref.fileId]
      if (!asset) return []
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
    }

    const rule = rules.find((item) => item.id === ref.ruleId)
    if (!rule) return []
    return [{
      attachedAt,
      contentMarkdown: renderChatRuleAttachmentContent(state, rule, attachedAt, assetBodyOverride),
      kind: 'chat-rule' as const,
      label: rule.label,
      ruleId: rule.id,
      title: rule.title,
    }]
  })
}

export function chatContextRefKey(ref: ChatContextReferenceRecord) {
  switch (ref.kind) {
    case 'research-report':
      return `research-report:${ref.runId}`
    case 'file-asset':
      return `file-asset:${ref.fileId}`
    case 'file-group':
      return `file-group:${ref.groupId}`
    case 'chat-rule':
      return `chat-rule:${ref.ruleId}`
  }
}

export function attachmentToRef(attachment: ChatMessageAttachmentRecord): ChatContextReferenceRecord {
  switch (attachment.kind) {
    case 'research-report':
      return { kind: 'research-report', runId: attachment.runId }
    case 'chat-rule':
      return { kind: 'chat-rule', ruleId: attachment.ruleId }
    case 'file-asset':
      return { fileId: attachment.fileId, kind: 'file-asset' }
    case 'file-group':
      return { groupId: attachment.groupId, kind: 'file-group' }
  }
}

/**
 * Drop duplicate context references, keeping the first occurrence of each. Used
 * wherever two ref sources are merged (composer pills plus rule/drop refs) so a
 * file referenced both inline and via the attach button appears only once.
 */
export function dedupeChatContextRefs(
  refs: readonly ChatContextReferenceRecord[],
): ChatContextReferenceRecord[] {
  const seen = new Set<string>()
  return refs.filter((ref) => {
    const key = chatContextRefKey(ref)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/**
 * Resolve attachment references to backend `ReferenceDoc` DTOs: file, file-group
 * and research-report sources (chat rules reach the model via the rule snippet,
 * not as a reference doc, so they are excluded). A file group expands to one
 * document per member; a research report contributes its markdown. Used by the
 * editor to send attached documents to `/v1/editor/*`.
 */
export function referenceDocsFromRefs(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
  /** Freshly fetched canonical prepared bodies, or explicit incognito-local
   * bodies, loaded before an editor AI run and overriding stale state. */
  assetBodyOverride?: ReadonlyMap<string, string>,
): ReferenceDoc[] {
  return chatAttachmentsFromRefs(state, refs, assetBodyOverride)
    .filter((
      attachment,
    ): attachment is Extract<ChatMessageAttachmentRecord, { kind: 'file-asset' | 'file-group' | 'research-report' }> =>
      attachment.kind !== 'chat-rule')
    .map((attachment) => ({
      content: attachment.contentMarkdown,
      label: attachment.label ?? attachment.title,
      pageCount: 'pageCount' in attachment ? attachment.pageCount : null,
      sizeBytes: 'sizeBytes' in attachment ? attachment.sizeBytes : undefined,
    }))
}

export type ChatAttachmentChipModel = {
  error: string | null
  fileCount: number | null
  kind: ChatContextReferenceRecord['kind']
  label: string
  readiness: AttachmentContextReadinessStatus
  readinessReason: AttachmentContextReadinessReason | null
  ref: ChatContextReferenceRecord
  retryAssetIds: string[]
  title: string
}

/**
 * Build one chip descriptor per draft reference. Unlike `chatAttachmentsFromRefs`
 * (which expands a group into one attachment per member for sending), this keeps
 * a file group as a single chip and is index-aligned with the ref array so chip
 * reordering maps straight onto `pendingChatAttachmentRefs`.
 */
export function chatAttachmentChipsFromRefs(
  state: ProjectState,
  refs: readonly ChatContextReferenceRecord[],
  options: Pick<
    AttachmentContextReadinessOptions,
    'allowLocalFiles' | 'bodyLoadStates'
  > = {},
): ChatAttachmentChipModel[] {
  const reports = completedReportOptions(state)
  const rules = projectChatRules(state)
  const seen = new Set<string>()

  return refs.flatMap<ChatAttachmentChipModel>((ref) => {
    const key = chatContextRefKey(ref)
    if (seen.has(key)) return []
    seen.add(key)

    switch (ref.kind) {
      case 'research-report': {
        const report = reports.find((option) => option.runId === ref.runId)
        if (!report) return []
        const readiness = attachmentContextReadiness(state, [ref], options)
        return [{ ...readiness, fileCount: null, kind: ref.kind, label: `@research:${report.label}`, readiness: readiness.status, readinessReason: readiness.reason, ref, title: report.title }]
      }
      case 'chat-rule': {
        const rule = rules.find((item) => item.id === ref.ruleId)
        if (!rule) return []
        const readiness = attachmentContextReadiness(state, [ref], options)
        return [{ ...readiness, fileCount: null, kind: ref.kind, label: `@rules:${rule.label}`, readiness: readiness.status, readinessReason: readiness.reason, ref, title: rule.title }]
      }
      case 'file-asset': {
        const asset = state.fileAssets[ref.fileId]
        if (!asset) return []
        const readiness = attachmentContextReadiness(state, [ref], options)
        return [{ ...readiness, fileCount: null, kind: ref.kind, label: `@files:${asset.label}`, readiness: readiness.status, readinessReason: readiness.reason, ref, title: asset.title }]
      }
      case 'file-group': {
        const group = state.fileGroups[ref.groupId]
        if (!group) return []
        const readiness = attachmentContextReadiness(state, [ref], options)
        return [{
          ...readiness,
          fileCount: fileAssetsForGroup(state, ref.groupId).length,
          kind: ref.kind,
          label: `@filegroups:${slugLabel(group.title, group.id, 'group')}`,
          readiness: readiness.status,
          readinessReason: readiness.reason,
          ref,
          title: group.title,
        }]
      }
    }
  })
}

/**
 * Collapse resolved message attachments back into one chip per logical unit.
 * Group members (stored as N `file-group` records) fold into a single chip with
 * the member count.
 */
export function chatAttachmentChipsFromAttachments(
  attachments: readonly ChatMessageAttachmentRecord[],
): ChatAttachmentChipModel[] {
  const groupCounts = new Map<string, number>()
  for (const attachment of attachments) {
    if (attachment.kind === 'file-group') {
      groupCounts.set(attachment.groupId, (groupCounts.get(attachment.groupId) ?? 0) + 1)
    }
  }
  const seen = new Set<string>()

  return attachments.flatMap<ChatAttachmentChipModel>((attachment) => {
    const ref = attachmentToRef(attachment)
    const key = chatContextRefKey(ref)
    if (seen.has(key)) return []
    seen.add(key)

    switch (attachment.kind) {
      case 'research-report':
        return [{ error: null, fileCount: null, kind: attachment.kind, label: `@research:${attachment.label ?? attachment.title}`, readiness: 'ready', readinessReason: null, ref, retryAssetIds: [], title: attachment.title }]
      case 'chat-rule':
        return [{ error: null, fileCount: null, kind: attachment.kind, label: `@rules:${attachment.label}`, readiness: 'ready', readinessReason: null, ref, retryAssetIds: [], title: attachment.title }]
      case 'file-asset':
        return [{ error: null, fileCount: null, kind: attachment.kind, label: `@files:${attachment.label}`, readiness: 'ready', readinessReason: null, ref, retryAssetIds: [], title: attachment.title }]
      case 'file-group':
        return [{ error: null, fileCount: groupCounts.get(attachment.groupId) ?? 1, kind: attachment.kind, label: `@filegroups:${attachment.groupLabel}`, readiness: 'ready', readinessReason: null, ref, retryAssetIds: [], title: attachment.groupLabel }]
    }
  })
}

export function researchRunToJob(run: ResearchRunRecord): ResearchJob {
  return {
    access: run.access,
    activePhase: run.phaseState.activePhase,
    cancelRequested: run.cancelRequested === true
      || run.events.some((event) => event.title === 'Cancellation requested'),
    confidence: run.snapshot?.confidence ? `${run.snapshot.confidence} / 10` : undefined,
    completedPhases: run.phaseState.completedPhases,
    duration: run.durationSeconds === undefined ? undefined : formatDuration(run.durationSeconds),
    error: run.error,
    events: run.events
      .filter(isDisplayableResearchEvent)
      .map((event) => ({
        active: event.active,
        arrivedLive: event.arrivedLive,
        // Carried through, not re-derived: the record's id is already the
        // stable `<runId>-<sequence>` identity the live rows key on.
        id: event.id,
        kind: event.kind ?? 'progress',
        phase: event.phase,
        severity: event.severity ?? 'info',
        time: formatTime(event.createdAt),
        title: text(event.title),
      })),
    id: run.runId,
    metrics: run.metrics,
    phaseVisitCounts: phaseVisitCountsFromEvents(run),
    queueNote: run.summary.queueNote ? text(run.summary.queueNote) : undefined,
    score: run.summary.score,
    startedAt: run.startedAt ? formatTime(run.startedAt) : undefined,
    startedAtIso: run.startedAt,
    status: run.status,
    unavailable: run.unavailable === true ? true : undefined,
    submittedAt: formatTime(run.submittedAt),
    title: text(run.summary.title),
  }
}

export function displayDateTime(iso: string, locale: Locale) {
  return new Intl.DateTimeFormat(locale === 'de' ? 'de-DE' : 'en-US', {
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    month: '2-digit',
  }).format(new Date(iso))
}

export function displayRelativeDate(iso: string, locale: Locale) {
  const now = new Date()
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return ''

  const diffMs = now.getTime() - value.getTime()
  const oneDay = 24 * 60 * 60 * 1000

  if (isSameCalendarDay(value, now)) {
    if (diffMs >= 0 && diffMs < 60 * 1000) return locale === 'de' ? 'Gerade eben' : 'Just now'
    return displayTime(value, locale)
  }

  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (isSameCalendarDay(value, yesterday)) {
    const label = locale === 'de' ? 'Gestern' : 'Yesterday'
    return `${label}, ${displayTime(value, locale)}`
  }

  if (diffMs >= 0 && diffMs < oneDay * 2) return locale === 'de' ? 'Gestern' : 'Yesterday'
  return displayDateTime(iso, locale)
}

export function displayRelativeAge(iso: string, locale: Locale, now = new Date()) {
  const value = new Date(iso)
  if (Number.isNaN(value.getTime()) || Number.isNaN(now.getTime())) return ''

  const diffMs = Math.max(0, now.getTime() - value.getTime())
  const minuteMs = 60 * 1000
  const hourMs = 60 * minuteMs
  const dayMs = 24 * hourMs
  const weekMs = 7 * dayMs

  if (diffMs < minuteMs) return locale === 'de' ? 'Gerade eben' : 'Just now'
  if (diffMs < hourMs) return `${Math.floor(diffMs / minuteMs)} ${locale === 'de' ? 'Min.' : 'min'}`
  if (diffMs < dayMs) return `${Math.floor(diffMs / hourMs)} ${locale === 'de' ? 'Std.' : 'h'}`
  if (diffMs < weekMs) {
    const days = Math.floor(diffMs / dayMs)
    return locale === 'de'
      ? countLabel(days, '1 Tag', '{count} Tage')
      : `${days} d`
  }
  return `${Math.floor(diffMs / weekMs)} ${locale === 'de' ? 'W' : 'w'}`
}

function displayTime(value: Date, locale: Locale) {
  return new Intl.DateTimeFormat(locale === 'de' ? 'de-DE' : 'en-US', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(value)
}

function isSameCalendarDay(left: Date, right: Date) {
  return (
    left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
  )
}

function text(value: string): LocalizedText {
  return { de: value, en: value }
}

function withUniqueLabels<T extends { label: string }>(items: readonly T[]): T[] {
  const counts = new Map<string, number>()
  return items.map((item) => {
    const seenCount = counts.get(item.label) ?? 0
    counts.set(item.label, seenCount + 1)
    return seenCount === 0 ? item : { ...item, label: `${item.label}-${seenCount + 1}` }
  })
}

function slugLabel(value: string, fallbackId: string, fallbackWord = 'item') {
  const normalized = value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  if (normalized) return normalized
  return fallbackId.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 48) || fallbackWord
}

function isDisplayableResearchEvent(event: ResearchRunRecord['events'][number]) {
  const title = event.title.trim()
  return !(
    /^run snapshot$/i.test(title)
    || /^queued$/i.test(title)
    || /^run started$/i.test(title)
    || /^started\s+\w+/i.test(title)
    || /^finished\s+\w+/i.test(title)
  )
}

function phaseVisitCountsFromEvents(run: ResearchRunRecord): Record<JobPhase, number> {
  const counts = emptyPhaseVisitCounts()
  let previousPhase: JobPhase | null = null

  for (const event of run.events.filter(isDisplayableResearchEvent)) {
    const phase = event.phase ?? phaseFromEventTitle(event.title)
    if (!phase || phase === previousPhase) continue

    counts[phase] += 1
    previousPhase = phase
  }

  if (run.status === 'running' && counts[run.phaseState.activePhase] === 0) {
    counts[run.phaseState.activePhase] = 1
  }

  return counts
}

function emptyPhaseVisitCounts(): Record<JobPhase, number> {
  return {
    analysis: 0,
    answer: 0,
    evaluation: 0,
    planning: 0,
    search: 0,
  }
}

function phaseFromEventTitle(title: string): JobPhase | undefined {
  const normalized = title.trim()
  if (!normalized) return undefined

  if (/\b(analysiere|analyzing|analyseziele|analysis goals|analysis targets|websuche erforderlich|web search required|detected analysis)\b/i.test(normalized)) {
    return 'analysis'
  }
  if (/\b(plane suchanfragen|planning search queries|neue suchanfragen|suchanfragen generiert|generated \d+ new search queries|generated .* search queries)\b/i.test(normalized)) {
    return 'planning'
  }
  if (/\b(durchsuche|searching \d+|suchantworten verarbeitet|processed \d+ search responses|referenzen gesammelt|references|evidence units|evidence records|evidence-records|evidenz|claims-lage|claim status|report evidence|source mix|quellenmix|related questions|verwandte fragen|semantically grouping)\b/i.test(normalized)) {
    return 'search'
  }
  if (/\b(formuliere|formulating answer|writing report|preparing final report|schreibe bericht|run completed|bericht wird|answer)\b/i.test(normalized)) {
    return 'answer'
  }
  if (/\b(bewerte|evaluating|confidence|vertrauen|weitere recherche|more research|research completed|recherche abgeschlossen|contradiction|contradictions|widerspruch|moegliche erklaerungen|mögliche erklärungen|quality)\b/i.test(normalized)) {
    return 'evaluation'
  }

  return undefined
}

function formatTime(iso: string) {
  return new Intl.DateTimeFormat('de-DE', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}

function formatDuration(seconds: number) {
  const wholeSeconds = Math.max(0, Math.round(seconds))
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainingSeconds = wholeSeconds % 60

  return [hours, minutes, remainingSeconds]
    .map((part) => part.toString().padStart(2, '0'))
    .join(':')
}
