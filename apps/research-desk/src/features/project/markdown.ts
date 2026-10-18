import { normalizeAgentModelSelection } from '@/features/agent/executionPolicy'
import type {
  ResearchSource,
  ResearchRunAccess,
} from '@/features/researchRuns/types'
import type {
  ChatContextReferenceRecord,
  ChatMessageAttachmentRecord,
  ChatMessageModelResolutionRecord,
  ChatMessageRequestContextRecord,
  ChatMessageRecord,
  ChatRuleRecord,
  ChatThreadRecord,
  EditorCommentAnchorRecord,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  FileAssetOrigin,
  FileAssetRecord,
  FileParseStatus,
  ProjectState,
  ProjectUiState,
  ResearchRunEventKind,
  ResearchRunEventRecord,
  ResearchRunEventSeverity,
  ResearchRunRecord,
} from './types'
import type { JobStatus } from '@/features/researchDesk/types'
import { PROJECT_SCHEMA_VERSION } from './types'
import { normalizeReportReferences } from './reportReferences'
import { AI_CONTENT_DISCLOSURE, aiDisclosureFrontmatter } from '@/lib/aiDisclosure'
import { asNonEmptyString, asString } from '@/lib/coerce'
import {
  chatRuleAutocompleteOrDefault,
  chatRuleCategoryOrDefault,
  chatRuleVisibilityOrDefault,
  normalizeChatRule,
  normalizeLinkedContextRefs,
} from './chatRules'

const MESSAGE_START = /^<!-- inqtrix:message (.+) -->$/
const MESSAGE_END = '<!-- /inqtrix:message -->'
const ATTACHMENT_START = /^<!-- inqtrix:attachment (.+) -->$/
const ATTACHMENT_END = '<!-- /inqtrix:attachment -->'

export type ProjectFile = {
  contents: string
  path: string
}

type ProjectFileDescriptor<T> = {
  id: string
  item: T
  path: string
  timestamp: string
  title: string
}

type ProjectExportPlan = {
  chatRules: ProjectFileDescriptor<ChatRuleRecord>[]
  chatThreads: ProjectFileDescriptor<ChatThreadRecord>[]
  editorDocuments: ProjectFileDescriptor<EditorDocumentRecord>[]
  fileAssets: ProjectFileDescriptor<FileAssetRecord>[]
  researchRuns: ProjectFileDescriptor<ResearchRunRecord>[]
}

export function buildProjectFiles(state: ProjectState): ProjectFile[] {
  const exportPlan = buildProjectExportPlan(state)
  const files: ProjectFile[] = [serializeProjectManifest(state, exportPlan)]

  for (const descriptor of exportPlan.chatRules) {
    files.push(serializeChatRule(descriptor.item, descriptor.path))
  }

  for (const descriptor of exportPlan.researchRuns) {
    files.push(serializeResearchRun(descriptor.item, descriptor.path))
  }

  for (const descriptor of exportPlan.editorDocuments) {
    files.push(serializeEditorDocument(descriptor.item, state, descriptor.path))
  }

  for (const descriptor of exportPlan.chatThreads) {
    files.push(serializeChatThread(descriptor.item, descriptor.path))
  }

  for (const descriptor of exportPlan.fileAssets) {
    files.push(serializeFileAsset(descriptor.item, descriptor.path))
  }

  return files
}

export function serializeProjectManifest(
  state: ProjectState,
  exportPlan = buildProjectExportPlan(state),
): ProjectFile {
  const savedUi: ProjectUiState = {
    ...state.ui,
    activeView: state.ui.activeView,
    // The model selection is NOT project data. It lives in the account
    // preferences, which carry a load-time precedence rule (a loaded file
    // must not bleed into the live account row). These six fields have no
    // such rule, so writing them here would create a second, competing store
    // that silently wins whenever a project is opened.
    selectedAgentEffort: null,
    selectedAgentModel: null,
    selectedAgentModelTier: null,
    selectedChatEffort: null,
    selectedChatModel: null,
    selectedChatModelTier: null,
  }
  const frontmatter = {
    chat_group_order: state.chatThreadGroupOrder,
    // Manual sort mode froze an explicit thread order — persist it, or a
    // reload would show 'Manuell' with an order the user never arranged.
    chat_thread_order: state.chatThreadOrder,
    chat_groups: state.chatThreadGroupOrder.flatMap((groupId) => {
      const group = state.chatThreadGroups[groupId]
      return group ? [group] : []
    }),
    chat_thread_group_memberships: state.chatThreadGroupMemberships,
    exported_at: new Date().toISOString(),
    kind: 'inqtrix.project',
    preferences: state.preferences,
    project: state.project,
    editor_folder_order: state.editorFolderOrder,
    editor_folders: state.editorFolderOrder.flatMap((folderId) => {
      const folder = state.editorFolders[folderId]
      return folder ? [folder] : []
    }),
    // Die Ordnung beschreibt DIESEN Export, nicht den Zustand: der Exportplan
    // ueberspringt Fremdfreigaben und nicht exportfaehige Eintraege, also
    // darf das Manifest sie auch nicht auffuehren. Sonst verweist es auf
    // Dateien, die im Archiv fehlen.
    editor_document_order: exportPlan.editorDocuments.map(
      (descriptor) => descriptor.id,
    ),
    editor_ui: state.editorUi,
    file_asset_order: state.fileAssetOrder,
    file_group_order: state.fileGroupOrder,
    file_groups: state.fileGroupOrder.flatMap((groupId) => {
      const group = state.fileGroups[groupId]
      return group ? [group] : []
    }),
    file_section_order: state.fileLibrarySectionOrder,
    file_sections: state.fileLibrarySectionOrder.flatMap((sectionId) => {
      const section = state.fileLibrarySections[sectionId]
      return section ? [section] : []
    }),
    vector_index_order: state.vectorIndexOrder,
    vector_indexes: state.vectorIndexOrder.flatMap((indexId) => {
      const index = state.vectorIndexes[indexId]
      return index ? [index] : []
    }),
    rule_order: exportPlan.chatRules.map((descriptor) => descriptor.id),
    schema_version: PROJECT_SCHEMA_VERSION,
    // The server-persistence opt-in: survives a reload so a re-opened
    // server project re-hydrates instead of reverting to local-first.
    server_sync_enabled: state.serverSyncEnabled,
    ui: savedUi,
    workspace_id: state.workspaceId,
  }

  return {
    contents: withFrontmatter(frontmatter, renderProjectManifestBody(state, exportPlan)),
    path: 'project.md',
  }
}

export function serializeResearchRun(
  run: ResearchRunRecord,
  path = defaultResearchRunPath(run),
): ProjectFile {
  const frontmatter = {
    // AI-generation markers ride in the front matter only. The body is
    // assigned straight back to `result.markdown` on import, so a body
    // marker would be absorbed into the answer and duplicated on every
    // export/import round trip.
    ...aiDisclosureFrontmatter(AI_CONTENT_DISCLOSURE),
    agent_overrides: run.agentOverrides,
    created_at: run.createdAt,
    duration_seconds: run.durationSeconds ?? null,
    events: run.events,
    file_id: run.runId,
    finished_at: run.finishedAt ?? null,
    kind: 'inqtrix.research',
    metrics: run.metrics,
    phase_state: run.phaseState,
    result_metrics: run.result?.metrics ?? null,
    references: run.result?.references ?? [],
    run_id: run.runId,
    schema_version: PROJECT_SCHEMA_VERSION,
    snapshot: run.snapshot ?? null,
    source: run.source,
    stack: run.stack,
    started_at: run.startedAt ?? null,
    status: run.status,
    submitted_at: run.submittedAt,
    summary: run.summary,
    // Additive shared-in marker — dropping it on round-trip would
    // silently relabel shared-in runs as owned after a reload.
    access: run.access ?? null,
    // Default-on mention availability; only an explicit false hides the report
    // from the @-autocomplete, so it must survive the round-trip.
    include_in_autocomplete: run.includeInAutocomplete ?? true,
    top_claims: run.result?.topClaims ?? [],
    top_sources: run.result?.topSources ?? [],
    usage: run.result?.usage ?? null,
  }

  return {
    contents: withFrontmatter(frontmatter, run.result?.markdown ?? ''),
    path,
  }
}

export function serializeChatThread(
  thread: ChatThreadRecord,
  path = defaultChatThreadPath(thread),
): ProjectFile {
  const frontmatter = {
    // See serializeResearchRun: markers stay out of the body so the chat
    // round-trip keeps every message byte-exact.
    ...aiDisclosureFrontmatter(AI_CONTENT_DISCLOSURE),
    created_at: thread.createdAt,
    kind: 'inqtrix.chat',
    message_order: thread.messages.map((message) => message.id),
    // The thread's own property (not a second global store): in file-backed
    // projects this IS the durable home of the pick.
    model_selection: thread.modelSelection ?? null,
    preview: thread.preview,
    schema_version: PROJECT_SCHEMA_VERSION,
    source: thread.source,
    thread_id: thread.id,
    title: thread.title,
    updated_at: thread.updatedAt,
  }

  return {
    contents: withFrontmatter(frontmatter, renderChatBody(thread.messages)),
    path,
  }
}

export function serializeChatRule(
  rule: ChatRuleRecord,
  path = `rules/${sanitizeFileSegment(rule.label)}.md`,
): ProjectFile {
  const normalized = normalizeChatRule(rule)
  const frontmatter = {
    category: normalized.category,
    created_at: rule.createdAt,
    include_in_autocomplete: normalized.includeInAutocomplete,
    kind: 'inqtrix.chat_rule',
    label: rule.label,
    linked_context_refs: linkedContextRefsToFrontmatter(normalized.linkedContextRefs ?? []),
    rule_id: rule.id,
    schema_version: PROJECT_SCHEMA_VERSION,
    // The sync link, mandatory revision anchor, and access metadata
    // must survive the round-trip — losing serverTemplateId re-uploads
    // the rule as a brand-new server template on its next save, and
    // losing serverRevision would prevent safe server updates
    // until the next hydrate.
    server_template_id: rule.serverTemplateId ?? null,
    server_revision: rule.serverRevision ?? null,
    // The composed size the server measured for that revision; dropped
    // on save, every load would hydrate it back as a change.
    server_composed_chars: rule.serverComposedChars ?? null,
    access: rule.access ?? null,
    title: rule.title,
    updated_at: rule.updatedAt,
    visibility: normalized.visibility,
  }

  return {
    contents: withFrontmatter(frontmatter, normalized.contentMarkdown),
    path,
  }
}

export function serializeFileAsset(
  asset: FileAssetRecord,
  path = `files/${sanitizeFileSegment(asset.label)}.md`,
): ProjectFile {
  const frontmatter = {
    created_at: asset.createdAt,
    ...(asset.deletionError !== undefined
      ? { deletion_error: asset.deletionError }
      : {}),
    ...(asset.deletionOperationId !== undefined
      ? { deletion_operation_id: asset.deletionOperationId }
      : {}),
    ...(asset.deletionStage !== undefined
      ? { deletion_stage: asset.deletionStage }
      : {}),
    file_id: asset.id,
    file_name: asset.fileName,
    group_id: asset.groupId,
    kind: 'inqtrix.file_asset',
    label: asset.label,
    ...(asset.lifecycleStatus !== undefined
      ? { lifecycle_status: asset.lifecycleStatus }
      : {}),
    mime_type: asset.mimeType,
    origin: asset.origin,
    page_count: asset.pageCount,
    parse_status: asset.parseStatus,
    parse_warning: asset.parseWarning,
    schema_version: PROJECT_SCHEMA_VERSION,
    section_id: asset.sectionId,
    ...(asset.serverSynced !== undefined
      ? { server_synced: asset.serverSynced }
      : {}),
    size_bytes: asset.sizeBytes,
    text_truncated: asset.textTruncated,
    title: asset.title,
    updated_at: asset.updatedAt,
  }

  return {
    contents: withFrontmatter(frontmatter, asset.extractedText),
    path,
  }
}

export function serializeEditorDocument(
  document: EditorDocumentRecord,
  state: Pick<ProjectState, 'editorComments'>,
  path = defaultEditorDocumentPath(document),
): ProjectFile {
  const detachedCollaboration = document.contentMode === 'collaboration'
  const comments = Object.values(state.editorComments)
    .filter((comment) => comment.documentId === document.id)
    .sort((a, b) => a.anchor.from - b.anchor.from || a.createdAt.localeCompare(b.createdAt))
    .map((comment) => {
      const { suggestionDraft, ...exportableComment } = comment
      void suggestionDraft
      return exportableComment
    })
  const frontmatter = {
    comments,
    created_at: document.createdAt,
    ...(detachedCollaboration ? { detached_from_collaboration: true } : {}),
    // A diff anchor is a self-contained markdown snapshot. It remains valid in
    // a detached export and does not reconnect the imported document to Yjs.
    diff_anchor_markdown: document.diffAnchorMarkdown ?? null,
    diff_anchor_updated_at: document.diffAnchorUpdatedAt ?? null,
    document_id: document.id,
    folder_id: detachedCollaboration ? null : document.folderId,
    kind: 'inqtrix.editor_document',
    ...(!detachedCollaboration && document.recovery
      ? {
          recovery: {
            captured_at: document.recovery.capturedAt,
            original_document_id: document.recovery.originalDocumentId,
            reason: document.recovery.reason,
          },
        }
      : {}),
    revision: document.revision,
    schema_version: PROJECT_SCHEMA_VERSION,
    ...(!detachedCollaboration && document.serverSynced !== undefined
      ? { server_synced: document.serverSynced }
      : {}),
    source: document.source,
    source_run_id: document.sourceRunId ?? null,
    title: document.title,
    updated_at: document.updatedAt,
  }

  return {
    contents: withFrontmatter(frontmatter, document.contentMarkdown),
    path,
  }
}

function buildProjectExportPlan(state: ProjectState): ProjectExportPlan {
  const usedPaths = new Set<string>(['project.md'])
  const chatRules: ProjectExportPlan['chatRules'] = []
  const researchRuns: ProjectExportPlan['researchRuns'] = []
  const chatThreads: ProjectExportPlan['chatThreads'] = []
  const editorDocuments: ProjectExportPlan['editorDocuments'] = []
  const fileAssets: ProjectExportPlan['fileAssets'] = []

  for (const ruleId of state.chatRuleOrder) {
    const rule = state.chatRules[ruleId]
    if (!rule || !isProjectAutosaveEligible(rule)) continue
    chatRules.push({
      id: rule.id,
      item: rule,
      path: uniqueProjectPath(`rules/${sanitizeFileSegment(rule.label)}.md`, usedPaths),
      timestamp: rule.updatedAt,
      title: rule.title,
    })
  }

  for (const runId of state.researchRunOrder) {
    const run = state.researchRuns[runId]
    if (
      !run
      || !isProjectAutosaveEligible(run)
      || run.mode === 'knowledge'
      || run.status !== 'completed'
      || !run.result?.markdown
    ) continue
    researchRuns.push({
      id: run.runId,
      item: run,
      path: uniqueProjectPath(defaultResearchRunPath(run), usedPaths),
      timestamp: run.submittedAt,
      title: run.summary.title,
    })
  }

  for (const documentId of state.editorDocumentOrder) {
    const document = state.editorDocuments[documentId]
    if (!document || document.access?.mode === 'shared') continue
    editorDocuments.push({
      id: document.id,
      item: document,
      path: uniqueProjectPath(defaultEditorDocumentPath(document), usedPaths),
      timestamp: document.updatedAt,
      title: document.title,
    })
  }

  for (const threadId of state.chatThreadOrder) {
    const thread = state.chatThreads[threadId]
    if (!thread) continue
    chatThreads.push({
      id: thread.id,
      item: thread,
      path: uniqueProjectPath(defaultChatThreadPath(thread), usedPaths),
      timestamp: thread.updatedAt,
      title: thread.title,
    })
  }

  for (const fileId of state.fileAssetOrder) {
    const asset = state.fileAssets[fileId]
    if (!asset) continue
    fileAssets.push({
      id: asset.id,
      item: asset,
      path: uniqueProjectPath(`files/${sanitizeFileSegment(asset.label)}.md`, usedPaths),
      timestamp: asset.updatedAt,
      title: asset.title,
    })
  }

  return {
    chatRules,
    chatThreads,
    editorDocuments,
    fileAssets,
    researchRuns,
  }
}

/** Automatic project persistence must not turn live shared access into an
 * owned copy. Explicit duplicate/export commands are separate user actions
 * and do not use this autosave policy. */
function isProjectAutosaveEligible(
  resource: { access?: ResearchRunAccess },
): boolean {
  return resource.access?.mode !== 'shared'
}

function renderProjectManifestBody(state: ProjectState, exportPlan: ProjectExportPlan) {
  return [
    `# ${state.project.name}`,
    '',
    'Inqtrix project manifest.',
    '',
    '## Export Index',
    '',
    ...renderManifestSection('Research runs', exportPlan.researchRuns),
    ...renderManifestSection('Documents', exportPlan.editorDocuments),
    ...renderChatGroupSection(state),
    ...renderManifestSection('Chat threads', exportPlan.chatThreads),
    ...renderManifestSection('Rules', exportPlan.chatRules),
    ...renderManifestSection('Files', exportPlan.fileAssets),
  ].join('\n')
}

function renderChatGroupSection(state: ProjectState) {
  const lines = ['### Chat groups', '']
  const groups = state.chatThreadGroupOrder
    .map((groupId) => state.chatThreadGroups[groupId])
    .filter((group): group is NonNullable<typeof group> => Boolean(group))
  if (groups.length === 0) {
    lines.push('_No groups exported._', '')
    return lines
  }

  for (const group of groups) {
    const threadCount = Object.values(state.chatThreadGroupMemberships).filter((groupId) => groupId === group.id).length
    lines.push(`- ${group.updatedAt} - \`${group.id}\` - ${singleLine(group.title)} - ${threadCount} chats`)
  }
  lines.push('')
  return lines
}

function renderManifestSection<T>(
  title: string,
  entries: ProjectFileDescriptor<T>[],
) {
  const lines = [`### ${title}`, '']
  if (entries.length === 0) {
    lines.push('_No entries exported._', '')
    return lines
  }

  for (const entry of entries) {
    lines.push(
      `- ${entry.timestamp} - \`${entry.id}\` - ${singleLine(entry.title)} - \`${entry.path}\``,
    )
  }
  lines.push('')
  return lines
}

function defaultResearchRunPath(run: ResearchRunRecord) {
  return [
    'search-history/',
    compactIsoStamp(run.submittedAt),
    '_',
    compactEntityId(run.runId, 'run'),
    '.md',
  ].join('')
}

function defaultChatThreadPath(thread: ChatThreadRecord) {
  return [
    'chat-history/',
    compactIsoStamp(thread.updatedAt),
    '_',
    compactEntityId(thread.id, 'chat'),
    '.md',
  ].join('')
}

function defaultEditorDocumentPath(document: EditorDocumentRecord) {
  return [
    'documents/',
    compactIsoStamp(document.updatedAt),
    '_',
    sanitizeFileSegment(document.title.replace(/\.md$/i, '')),
    '.md',
  ].join('')
}

export function parseProjectManifest(markdown: string) {
  const parsed = parseFrontmatter(markdown)
  requireKind(parsed.data, 'inqtrix.project')
  return parsed.data
}

export function parseResearchRun(markdown: string): ResearchRunRecord {
  const parsed = parseFrontmatter(markdown)
  const data = parsed.data
  requireKind(data, 'inqtrix.research')
  const submittedAt = stringValue(data.submitted_at)
  const topSources = arrayValue<ResearchSource>(data.top_sources)

  return {
    agentOverrides: objectValue(data.agent_overrides),
    createdAt: stringValue(data.created_at),
    durationSeconds: optionalNumber(data.duration_seconds),
    events: normalizeRunEvents(data.events, submittedAt),
    finishedAt: asString(data.finished_at),
    metrics: objectValue(data.metrics),
    phaseState: objectValue(data.phase_state),
    result: {
      markdown: parsed.body.trimStart(),
      metrics: nullableObject(data.result_metrics),
      references: normalizeReportReferences(data.references, parsed.body, topSources),
      topClaims: arrayValue(data.top_claims),
      topSources,
      usage: nullableObject(data.usage),
    },
    runId: stringValue(data.run_id),
    snapshot: nullableObject(data.snapshot),
    source: data.source === 'api' || data.source === 'mock' ? data.source : 'imported',
    stack: stringValue(data.stack),
    startedAt: asString(data.started_at),
    status: researchRunStatusOrDefault(data.status),
    submittedAt,
    summary: objectValue(data.summary),
    ...(isRunAccess(data.access) ? { access: data.access } : {}),
    // Default-on: absent or true means available, so only an explicit false is
    // carried onto the record (matching the optional-field shape of `access`).
    ...(data.include_in_autocomplete === false ? { includeInAutocomplete: false } : {}),
  }
}

/** Frontmatter guard for canonical resource authorization metadata. */
function isRunAccess(value: unknown): value is ResearchRunAccess {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as { mode?: unknown; permission?: unknown }
  if (candidate.mode === 'shared') {
    return candidate.permission === 'view' || candidate.permission === 'edit'
  }
  return (
    (candidate.mode === 'owner' || candidate.mode === 'unscoped')
    && candidate.permission === undefined
  )
}

export function parseChatThread(markdown: string): ChatThreadRecord {
  const parsed = parseFrontmatter(markdown)
  const data = parsed.data
  requireKind(data, 'inqtrix.chat')
  const messages = parseChatBody(parsed.body)

  const modelSelection = normalizeAgentModelSelection(data.model_selection)
  return {
    createdAt: stringValue(data.created_at),
    id: stringValue(data.thread_id),
    messages,
    ...(modelSelection ? { modelSelection } : {}),
    preview: stringValue(data.preview),
    source: 'imported',
    title: stringValue(data.title),
    updatedAt: stringValue(data.updated_at),
  }
}

export function parseChatRule(markdown: string): ChatRuleRecord {
  const parsed = parseFrontmatter(markdown)
  const data = parsed.data
  requireKind(data, 'inqtrix.chat_rule')

  return {
    category: chatRuleCategoryOrDefault(data.category),
    contentMarkdown: parsed.body.trimStart(),
    createdAt: stringValue(data.created_at),
    id: stringValue(data.rule_id),
    includeInAutocomplete: chatRuleAutocompleteOrDefault(data.include_in_autocomplete),
    label: stringValue(data.label),
    linkedContextRefs: chatRuleCategoryOrDefault(data.category) === 'context'
      ? linkedContextRefsFromUnknown(data.linked_context_refs)
      : [],
    title: stringValue(data.title),
    updatedAt: stringValue(data.updated_at),
    visibility: chatRuleVisibilityOrDefault(data.visibility),
    ...(typeof data.server_template_id === 'string' && data.server_template_id
      ? { serverTemplateId: data.server_template_id }
      : {}),
    ...(Number.isInteger(data.server_revision) && Number(data.server_revision) > 0
      ? { serverRevision: Number(data.server_revision) }
      : {}),
    ...(Number.isInteger(data.server_composed_chars) && Number(data.server_composed_chars) >= 0
      ? { serverComposedChars: Number(data.server_composed_chars) }
      : {}),
    ...(isRunAccess(data.access) ? { access: data.access } : {}),
  }
}

function linkedContextRefsToFrontmatter(refs: readonly ChatContextReferenceRecord[]) {
  return normalizeLinkedContextRefs(refs).map((ref) => {
    if (ref.kind === 'file-asset') {
      return {
        file_id: ref.fileId,
        kind: 'file-asset',
      }
    }
    return {
      group_id: ref.groupId,
      kind: 'file-group',
    }
  })
}

function linkedContextRefsFromUnknown(value: unknown): ChatContextReferenceRecord[] {
  if (!Array.isArray(value)) return []
  const refs = value.flatMap<ChatContextReferenceRecord>((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const record = item as Record<string, unknown>
    if (record.kind === 'file-asset') {
      const fileId = asString(record.fileId) ?? asString(record.file_id)
      return fileId ? [{ fileId, kind: 'file-asset' }] : []
    }
    if (record.kind === 'file-group') {
      const groupId = asString(record.groupId) ?? asString(record.group_id)
      return groupId ? [{ groupId, kind: 'file-group' }] : []
    }
    return []
  })
  return normalizeLinkedContextRefs(refs)
}

export function parseFileAsset(markdown: string): FileAssetRecord {
  const parsed = parseFrontmatter(markdown)
  const data = parsed.data
  requireKind(data, 'inqtrix.file_asset')
  const lifecycleStatus = fileAssetLifecycle(data.lifecycle_status)

  return {
    createdAt: stringValue(data.created_at),
    ...(data.deletion_error !== undefined
      ? { deletionError: asString(data.deletion_error) ?? null }
      : {}),
    ...(data.deletion_operation_id !== undefined
      ? { deletionOperationId: asString(data.deletion_operation_id) ?? null }
      : {}),
    ...(data.deletion_stage !== undefined
      ? { deletionStage: asString(data.deletion_stage) ?? null }
      : {}),
    extractedText: parsed.body.trimStart(),
    fileName: stringValue(data.file_name),
    groupId: asString(data.group_id) ?? null,
    id: stringValue(data.file_id),
    label: stringValue(data.label),
    ...(lifecycleStatus ? { lifecycleStatus } : {}),
    mimeType: stringValue(data.mime_type),
    origin: fileAssetOriginOrDefault(data.origin),
    pageCount: optionalNumber(data.page_count) ?? null,
    parseStatus: fileParseStatusOrDefault(data.parse_status),
    parseWarning: asString(data.parse_warning) ?? null,
    sectionId: stringValue(data.section_id),
    ...(typeof data.server_synced === 'boolean'
      ? { serverSynced: data.server_synced }
      : {}),
    sizeBytes: optionalNumber(data.size_bytes) ?? 0,
    textTruncated: data.text_truncated === true,
    title: stringValue(data.title),
    updatedAt: stringValue(data.updated_at),
  }
}

function fileAssetOriginOrDefault(value: unknown): FileAssetOrigin {
  return value === 'chat' || value === 'editor' || value === 'library' ? value : 'library'
}

function fileAssetLifecycle(value: unknown): FileAssetRecord['lifecycleStatus'] | undefined {
  return value === 'active' || value === 'deleting' || value === 'delete_failed'
    ? value
    : undefined
}

function fileParseStatusOrDefault(value: unknown): FileParseStatus {
  return value === 'parsed' || value === 'partial' || value === 'unsupported' || value === 'error'
    ? value
    : 'parsed'
}

export type EditorDocumentImportIdentity = {
  commentIds: Array<{ sourceId: string; targetId: string }>
  documentId: { sourceId: string; targetId: string }
}

export function parseEditorDocument(markdown: string): {
  comments: EditorCommentThreadRecord[]
  document: EditorDocumentRecord
  importIdentity?: EditorDocumentImportIdentity
} {
  const parsed = parseFrontmatter(markdown)
  const data = parsed.data
  requireKind(data, 'inqtrix.editor_document')
  const detachedCollaboration = data.detached_from_collaboration === true
  const sourceDocumentId = stringValue(data.document_id)
  const documentId = detachedCollaboration
    ? createDetachedEntityId('editor-doc')
    : sourceDocumentId
  const recovery = detachedCollaboration
    ? undefined
    : editorDocumentRecoveryOrUndefined(data.recovery)
  const normalizedComments = normalizeEditorComments(data.comments, documentId, {
    remapIdentity: detachedCollaboration,
  })

  return {
    comments: normalizedComments.comments,
    document: {
      contentMarkdown: parsed.body.trimStart(),
      createdAt: stringValue(data.created_at),
      diffAnchorMarkdown: asString(data.diff_anchor_markdown),
      diffAnchorUpdatedAt: asString(data.diff_anchor_updated_at),
      folderId: asString(data.folder_id) ?? null,
      id: documentId,
      revision: detachedCollaboration
        ? 0
        : typeof data.revision === 'number' ? data.revision : 1,
      ...(recovery ? { recovery } : {}),
      ...(!detachedCollaboration && typeof data.server_synced === 'boolean'
        ? { serverSynced: data.server_synced }
        : {}),
      source: editorDocumentSourceOrDefault(data.source),
      sourceRunId: asString(data.source_run_id),
      title: normalizeEditorTitle(stringValue(data.title)),
      updatedAt: stringValue(data.updated_at),
    },
    ...(detachedCollaboration
      ? {
          importIdentity: {
            commentIds: normalizedComments.importIdentities,
            documentId: { sourceId: sourceDocumentId, targetId: documentId },
          },
        }
      : {}),
  }
}

function editorDocumentRecoveryOrUndefined(
  value: unknown,
): EditorDocumentRecord['recovery'] | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const candidate = value as Record<string, unknown>
  if (
    (candidate.reason !== 'remote_deleted' && candidate.reason !== 'generation_replaced')
    || typeof candidate.captured_at !== 'string'
    || typeof candidate.original_document_id !== 'string'
  ) return undefined
  return {
    capturedAt: candidate.captured_at,
    originalDocumentId: candidate.original_document_id,
    reason: candidate.reason,
  }
}

function createDetachedEntityId(prefix: 'editor-comment' | 'editor-doc'): string {
  const randomId = globalThis.crypto?.randomUUID?.()
  if (randomId) return `${prefix}-${randomId}`
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function withFrontmatter(data: Record<string, unknown>, body: string) {
  return `---\n${serializeYamlHeader(data)}---\n${body.trimStart()}`
}

function serializeYamlHeader(data: Record<string, unknown>) {
  return Object.entries(data)
    .map(([key, value]) => `${key}: ${serializeYamlValue(value)}`)
    .join('\n')
    .concat('\n')
}

function serializeYamlValue(value: unknown) {
  if (value === undefined) return 'null'
  if (typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) {
    return JSON.stringify(value)
  }
  return JSON.stringify(value)
}

function parseFrontmatter(markdown: string) {
  // Normalize CRLF and lone-CR to LF before any structural parsing. The delimiter
  // guard and the offset math below are LF-only; a Windows checkout (core.autocrlf)
  // or a user-imported Windows project would otherwise throw here. Normalizing first
  // keeps every offset correct and yields an LF body (all body consumers already
  // tolerate either line ending).
  const normalized = markdown.replace(/\r\n?/g, '\n')
  if (!normalized.startsWith('---\n')) {
    throw new Error('Markdown file is missing YAML frontmatter.')
  }
  const endIndex = normalized.indexOf('\n---', 4)
  if (endIndex < 0) {
    throw new Error('Markdown file has an unterminated YAML frontmatter block.')
  }

  const header = normalized.slice(4, endIndex)
  const body = normalized.slice(endIndex + 5)
  const data: Record<string, unknown> = {}

  for (const line of header.split(/\r?\n/)) {
    if (!line.trim()) continue
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/)
    if (!match) {
      throw new Error(`Unsupported YAML frontmatter line: ${line}`)
    }
    data[match[1]] = parseYamlValue(match[2])
  }

  return { body, data }
}

function parseYamlValue(value: string): unknown {
  const trimmed = value.trim()
  if (trimmed === '') return ''
  if (trimmed === 'null') return null
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed)
  if (
    trimmed.startsWith('"')
    || trimmed.startsWith('{')
    || trimmed.startsWith('[')
  ) {
    return JSON.parse(trimmed)
  }
  return trimmed
}

function researchRunStatusOrDefault(value: unknown): JobStatus {
  if (
    value === 'cancelled'
    || value === 'completed'
    || value === 'expired'
    || value === 'failed'
    || value === 'queued'
    || value === 'running'
  ) {
    return value
  }
  return 'completed'
}

function renderChatBody(messages: ChatMessageRecord[]) {
  return messages.map((message) => {
    const attrs = [
      `id=${JSON.stringify(message.id)}`,
      `role=${JSON.stringify(message.role)}`,
      `created_at=${JSON.stringify(message.createdAt)}`,
      ...renderMessageModelResolutionAttrs(message.modelResolution),
      ...renderMessageRequestContextAttrs(message.requestContext),
    ].join(' ')
    const attachments = renderMessageAttachments(message.attachments ?? [])
    const body = [
      message.contentMarkdown.trim(),
      attachments,
    ].filter(Boolean).join('\n\n')

    return `<!-- inqtrix:message ${attrs} -->\n${body}\n${MESSAGE_END}`
  }).join('\n\n')
}

function renderMessageAttachments(attachments: ChatMessageAttachmentRecord[]) {
  return attachments.map((attachment) => [
    `<!-- inqtrix:attachment ${messageAttachmentAttrs(attachment)} -->`,
    attachment.contentMarkdown.trim(),
    ATTACHMENT_END,
  ].join('\n')).join('\n\n')
}

function messageAttachmentAttrs(attachment: ChatMessageAttachmentRecord): string {
  const common = [
    `label=${JSON.stringify(attachment.label ?? '')}`,
    `title=${JSON.stringify(attachment.title)}`,
    `attached_at=${JSON.stringify(attachment.attachedAt)}`,
  ]
  switch (attachment.kind) {
    case 'research-report':
      return [`kind=${JSON.stringify(attachment.kind)}`, `run_id=${JSON.stringify(attachment.runId)}`, ...common].join(' ')
    case 'chat-rule':
      return [`kind=${JSON.stringify(attachment.kind)}`, `rule_id=${JSON.stringify(attachment.ruleId)}`, ...common].join(' ')
    case 'file-asset':
      return [
        `kind=${JSON.stringify(attachment.kind)}`,
        `file_id=${JSON.stringify(attachment.fileId)}`,
        `page_count=${JSON.stringify(attachment.pageCount)}`,
        `size_bytes=${JSON.stringify(attachment.sizeBytes)}`,
        ...common,
      ].join(' ')
    case 'file-group':
      return [
        `kind=${JSON.stringify(attachment.kind)}`,
        `file_id=${JSON.stringify(attachment.fileId)}`,
        `group_id=${JSON.stringify(attachment.groupId)}`,
        `group_label=${JSON.stringify(attachment.groupLabel)}`,
        `page_count=${JSON.stringify(attachment.pageCount)}`,
        `size_bytes=${JSON.stringify(attachment.sizeBytes)}`,
        ...common,
      ].join(' ')
  }
}

function parseChatBody(body: string): ChatMessageRecord[] {
  const lines = body.split(/\r?\n/)
  const messages: ChatMessageRecord[] = []
  let current: {
    attrs: Record<string, string>
    content: string[]
  } | null = null

  for (const line of lines) {
    const startMatch = line.match(MESSAGE_START)
    if (startMatch) {
      if (current) throw new Error('Nested chat message block found.')
      current = { attrs: parseAttributes(startMatch[1]), content: [] }
      continue
    }
    if (line.trim() === MESSAGE_END) {
      if (!current) throw new Error('Closing chat message marker without opening marker.')
      const parsedMessageBody = parseMessageBody(current.content)
      messages.push({
        attachments: parsedMessageBody.attachments,
        contentMarkdown: parsedMessageBody.contentMarkdown,
        createdAt: stringValue(current.attrs.created_at),
        id: stringValue(current.attrs.id),
        modelResolution: parseMessageModelResolutionAttrs(current.attrs),
        requestContext: parseMessageRequestContextAttrs(current.attrs),
        role: current.attrs.role === 'user' ? 'user' : 'assistant',
      })
      current = null
      continue
    }
    if (current) {
      current.content.push(line)
    }
  }

  if (current) throw new Error('Unclosed chat message block.')
  return messages
}

function renderMessageModelResolutionAttrs(
  resolution: ChatMessageModelResolutionRecord | undefined,
) {
  if (!resolution?.model) return []
  return [
    `model=${JSON.stringify(resolution.model)}`,
    `model_tier=${JSON.stringify(resolution.tier)}`,
    `model_effort=${JSON.stringify(resolution.effort)}`,
    `model_requested_tier=${JSON.stringify(resolution.requestedTier)}`,
    `model_source=${JSON.stringify(resolution.modelSource)}`,
    `model_effort_source=${JSON.stringify(resolution.effortSource)}`,
  ]
}

function parseMessageModelResolutionAttrs(
  attrs: Record<string, string>,
): ChatMessageModelResolutionRecord | undefined {
  const model = asNonEmptyString(attrs.model)
  if (!model) return undefined
  return {
    effort: attrs.model_effort ?? '',
    effortSource: attrs.model_effort_source ?? '',
    model,
    modelSource: attrs.model_source ?? '',
    requestedTier: attrs.model_requested_tier ?? '',
    tier: attrs.model_tier ?? '',
  }
}

function renderMessageRequestContextAttrs(
  context: ChatMessageRequestContextRecord | undefined,
) {
  if (!context?.knowledgeCollectionIds || context.knowledgeCollectionIds.length === 0) return []
  return [
    `request_context=${JSON.stringify(JSON.stringify({
      knowledgeCollectionIds: context.knowledgeCollectionIds,
    }))}`,
  ]
}

function parseMessageRequestContextAttrs(
  attrs: Record<string, string>,
): ChatMessageRequestContextRecord | undefined {
  if (!attrs.request_context) return undefined
  try {
    const value = JSON.parse(attrs.request_context) as unknown
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
    const record = value as { knowledgeCollectionIds?: unknown }
    if (!Array.isArray(record.knowledgeCollectionIds)) return undefined
    const knowledgeCollectionIds = record.knowledgeCollectionIds.filter((id): id is string => (
      typeof id === 'string' && id.trim().length > 0
    ))
    return knowledgeCollectionIds.length > 0 ? { knowledgeCollectionIds } : undefined
  } catch {
    return undefined
  }
}

function parseMessageBody(lines: string[]): {
  attachments?: ChatMessageAttachmentRecord[]
  contentMarkdown: string
} {
  const contentLines: string[] = []
  const attachments: ChatMessageAttachmentRecord[] = []
  let currentAttachment: {
    attrs: Record<string, string>
    content: string[]
  } | null = null

  for (const line of lines) {
    const startMatch = line.match(ATTACHMENT_START)
    if (startMatch) {
      if (currentAttachment) throw new Error('Nested chat attachment block found.')
      currentAttachment = { attrs: parseAttributes(startMatch[1]), content: [] }
      continue
    }

    if (line.trim() === ATTACHMENT_END) {
      if (!currentAttachment) {
        throw new Error('Closing chat attachment marker without opening marker.')
      }
      attachments.push(parseAttachmentBlock(currentAttachment.attrs, currentAttachment.content))
      currentAttachment = null
      continue
    }

    if (currentAttachment) {
      currentAttachment.content.push(line)
    } else {
      contentLines.push(line)
    }
  }

  if (currentAttachment) throw new Error('Unclosed chat attachment block.')

  return {
    attachments: attachments.length > 0 ? attachments : undefined,
    contentMarkdown: contentLines.join('\n').trim(),
  }
}

function parseAttachmentBlock(
  attrs: Record<string, string>,
  content: string[],
): ChatMessageAttachmentRecord {
  if (attrs.kind === 'chat-rule') {
    return {
      attachedAt: stringValue(attrs.attached_at),
      contentMarkdown: content.join('\n').trim(),
      kind: 'chat-rule',
      label: stringValue(attrs.label),
      ruleId: stringValue(attrs.rule_id),
      title: stringValue(attrs.title),
    }
  }

  if (attrs.kind === 'research-report') {
    return {
      attachedAt: stringValue(attrs.attached_at),
      contentMarkdown: content.join('\n').trim(),
      kind: 'research-report',
      label: asNonEmptyString(attrs.label),
      runId: stringValue(attrs.run_id),
      title: stringValue(attrs.title),
    }
  }

  throw new Error(`Unsupported chat attachment kind: ${String(attrs.kind)}.`)
}

function parseAttributes(value: string) {
  const attrs: Record<string, string> = {}
  const regex = /([a-z_]+)=("(?:[^"\\]|\\.)*")/g
  let match = regex.exec(value)
  while (match) {
    attrs[match[1]] = JSON.parse(match[2]) as string
    match = regex.exec(value)
  }
  return attrs
}

function requireKind(data: Record<string, unknown>, kind: string) {
  if (data.kind !== kind) {
    throw new Error(`Expected ${kind} markdown, received ${String(data.kind)}.`)
  }
  if (data.schema_version !== PROJECT_SCHEMA_VERSION) {
    throw new Error(`Unsupported project schema version: ${String(data.schema_version)}.`)
  }
}

function sanitizeFileSegment(value: string) {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return normalized.slice(0, 72) || 'untitled'
}

function compactIsoStamp(iso: string) {
  const parsed = new Date(iso)
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')
  }

  const normalized = iso
    .trim()
    .replace(/[-:]/g, '')
    .replace(/\.\d{3}Z$/, 'Z')
    .replace(/[^0-9TZ]/g, '')
    .slice(0, 16)
  return normalized || 'undated'
}

function compactEntityId(id: string, fallbackPrefix: string) {
  const normalized = sanitizeFileSegment(id)
  if (normalized.length <= 24) return normalized
  return `${fallbackPrefix}-${stableFileHash(id)}`
}

function stableFileHash(value: string) {
  let hash = 0x811c9dc5
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(16).padStart(8, '0').slice(0, 7)
}

function uniqueProjectPath(path: string, usedPaths: Set<string>) {
  const dotIndex = path.lastIndexOf('.')
  const stem = dotIndex >= 0 ? path.slice(0, dotIndex) : path
  const extension = dotIndex >= 0 ? path.slice(dotIndex) : ''
  let candidate = path
  let suffix = 2

  while (usedPaths.has(candidate)) {
    candidate = `${stem}-${suffix}${extension}`
    suffix += 1
  }

  usedPaths.add(candidate)
  return candidate
}

function singleLine(value: string) {
  return value.replace(/\s+/g, ' ').trim() || 'Untitled'
}

function stringValue(value: unknown) {
  if (typeof value !== 'string') throw new Error('Expected string frontmatter value.')
  return value
}

function stringOrNow(value: unknown) {
  return typeof value === 'string' && value.trim()
    ? value
    : new Date().toISOString()
}

function optionalNumber(value: unknown) {
  return typeof value === 'number' ? value : undefined
}

function objectValue<T extends object>(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Expected object frontmatter value.')
  }
  return value as T
}

function nullableObject<T extends object>(value: unknown) {
  if (value === null || value === undefined) return undefined
  return objectValue<T>(value)
}

function editorDocumentSourceOrDefault(value: unknown): EditorDocumentRecord['source'] {
  if (
    value === 'blank'
    || value === 'imported-research-report'
    || value === 'pasted'
    || value === 'agent-artifact'
  ) {
    return value
  }
  return 'blank'
}

function normalizeEditorTitle(value: string) {
  const trimmed = value.replace(/\s+/g, ' ').trim() || 'Untitled'
  return trimmed.endsWith('.md') ? trimmed : `${trimmed}.md`
}

function normalizeEditorComments(
  value: unknown,
  fallbackDocumentId: string,
  { remapIdentity = false }: { remapIdentity?: boolean } = {},
): {
  comments: EditorCommentThreadRecord[]
  importIdentities: Array<{ sourceId: string; targetId: string }>
} {
  const comments: EditorCommentThreadRecord[] = []
  const importIdentities: Array<{ sourceId: string; targetId: string }> = []
  if (!Array.isArray(value)) return { comments, importIdentities }

  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const anchor = normalizeEditorCommentAnchor(record.anchor)
    const serializedId = asString(record.id)
    const commentMarkdown = asString(record.commentMarkdown ?? record.comment_markdown)
    if ((!serializedId && !remapIdentity) || !anchor || !commentMarkdown) continue
    const id = remapIdentity ? createDetachedEntityId('editor-comment') : serializedId!
    comments.push({
      anchor,
      commentMarkdown,
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      documentId: remapIdentity
        ? fallbackDocumentId
        : asString(record.documentId ?? record.document_id) ?? fallbackDocumentId,
      id,
      kind: editorCommentKindOrDefault(record.kind),
      status: editorCommentStatusOrDefault(record.status),
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    })
    if (remapIdentity && serializedId) {
      importIdentities.push({ sourceId: serializedId, targetId: id })
    }
  }

  return { comments, importIdentities }
}

function normalizeEditorCommentAnchor(value: unknown): EditorCommentAnchorRecord | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  const from = typeof record.from === 'number' ? record.from : null
  const to = typeof record.to === 'number' ? record.to : null
  if (from === null || to === null || from >= to) return null
  const blockId = asString(record.blockId ?? record.block_id)
  const selectedMarkdown = asString(record.selectedMarkdown ?? record.selected_markdown)
  return {
    ...(blockId ? { blockId } : {}),
    from,
    quoteAfter: asString(record.quoteAfter ?? record.quote_after) ?? '',
    quoteBefore: asString(record.quoteBefore ?? record.quote_before) ?? '',
    ...(selectedMarkdown ? { selectedMarkdown } : {}),
    selectedText: asString(record.selectedText ?? record.selected_text) ?? '',
    to,
  }
}

function editorCommentStatusOrDefault(value: unknown): EditorCommentThreadRecord['status'] {
  if (value === 'resolved' || value === 'stale') return value
  return 'open'
}

function editorCommentKindOrDefault(value: unknown): EditorCommentThreadRecord['kind'] {
  if (value === 'inline_edit' || value === 'evidence_review') return value
  return 'collect'
}


function normalizeRunEvents(
  value: unknown,
  fallbackCreatedAt: string,
): ResearchRunEventRecord[] {
  if (!Array.isArray(value)) return []

  return value.flatMap((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const record = item as Record<string, unknown>
    const title = asString(record.title)?.trim()
    if (!title) return []

    const createdAt = asString(record.createdAt)
      ?? asString(record.created_at)
      ?? fallbackCreatedAt
    const id = asString(record.id)
      ?? `imported-event-${index}-${createdAt}`
    const active = typeof record.active === 'boolean' ? record.active : undefined
    const phase = normalizeEventPhase(record.phase)

    return [{
      active,
      createdAt,
      id,
      kind: normalizeEventKind(record.kind),
      ...(phase ? { phase } : {}),
      severity: normalizeEventSeverity(record.severity),
      title,
    }]
  })
}

function normalizeEventKind(value: unknown): ResearchRunEventKind {
  return value === 'system' || value === 'progress' ? value : 'progress'
}

function normalizeEventSeverity(value: unknown): ResearchRunEventSeverity {
  if (
    value === 'error'
    || value === 'info'
    || value === 'success'
    || value === 'warning'
  ) {
    return value
  }
  return 'info'
}

function normalizeEventPhase(value: unknown): ResearchRunEventRecord['phase'] {
  if (
    value === 'analysis'
    || value === 'planning'
    || value === 'search'
    || value === 'evaluation'
    || value === 'answer'
  ) {
    return value
  }
  return undefined
}

function arrayValue<T>(value: unknown) {
  if (!Array.isArray(value)) throw new Error('Expected array frontmatter value.')
  return value as T[]
}
