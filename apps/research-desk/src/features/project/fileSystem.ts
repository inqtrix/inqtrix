import {
  buildProjectFiles,
  parseChatRule,
  parseChatThread,
  parseEditorDocument,
  parseFileAsset,
  parseProjectManifest,
  parseResearchRun,
  type EditorDocumentImportIdentity,
  type ProjectFile,
} from './markdown'
import { createBootstrapKnowledgeSession } from './knowledgeSessionDefaults'
import { resolveExplorerSortState } from './explorerSort'
import type {
  EmbedModelId,
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
  PinnedExplorerState,
  ProjectState,
  ProjectWriteResult,
  VectorIndexMemberRecord,
  VectorIndexRecord,
  VectorIndexRunHistoryEntry,
  VectorIndexStatus,
} from './types'
import { DEFAULT_EMBED_MODEL_ID, EMBED_MODELS, PROJECT_SCHEMA_VERSION, VECTOR_INDEX_HISTORY_LIMIT } from './types'
import {
  createDefaultFileLibrarySections,
  temporaryFileSectionId,
} from '@/features/files/sections'
import {
  getOrCreateBrowserWorkspaceId,
  isWorkspaceId,
  rememberBrowserWorkspaceId,
} from './workspaceId'
import { EMPTY_CANVAS_STATE } from '@/features/canvas/types'
import { normalizePanelLayout } from './panelLayout'
import { buildZip } from './zip'
import { readerError } from '@/api/requestErrorText'

type DirectoryPickerMode = 'read' | 'readwrite'

type DirectoryPickerOptions = {
  id?: string
  mode?: DirectoryPickerMode
  startIn?: string
}

type DirectoryPickerWindow = Window & {
  showDirectoryPicker?: (options?: DirectoryPickerOptions) => Promise<FileSystemDirectoryHandle>
}

type PermissionCapableHandle = FileSystemDirectoryHandle & {
  queryPermission?: (descriptor?: { mode?: DirectoryPickerMode }) => Promise<PermissionState>
  requestPermission?: (descriptor?: { mode?: DirectoryPickerMode }) => Promise<PermissionState>
}

type IterableDirectoryHandle = FileSystemDirectoryHandle & {
  entries: () => AsyncIterable<[string, FileSystemHandle]>
}

type SelectedProjectFile = {
  contents: string
  fileName: string
  path: string
}

type ProjectActionCallbacks = {
  onWorkStart?: () => void
}

export function canPickDirectories() {
  return typeof (window as DirectoryPickerWindow).showDirectoryPicker === 'function'
}

export async function exportProject(
  state: ProjectState,
  callbacks: ProjectActionCallbacks = {},
): Promise<ProjectWriteResult> {
  const savedAt = new Date().toISOString()
  if (!canPickDirectories()) {
    downloadProjectZip(state)
    return {
      connection: {
        kind: 'download',
        writable: false,
      },
      savedAt,
    }
  }

  const parentHandle = await pickDirectory('readwrite')
  const directoryName = uniqueProjectDirectoryName(state.project.name)
  const projectHandle = await parentHandle.getDirectoryHandle(directoryName, { create: true })
  callbacks.onWorkStart?.()
  await writeProjectFiles(projectHandle, state)

  return {
    connection: {
      directoryHandle: projectHandle,
      directoryName,
      kind: 'directory',
      writable: true,
    },
    savedAt,
  }
}

export async function loadProject(callbacks: ProjectActionCallbacks = {}): Promise<ProjectState> {
  if (!canPickDirectories()) {
    const files = await pickProjectFilesWithInput()
    callbacks.onWorkStart?.()
    const projectState = await readProjectFromFiles(files)
    return {
      ...projectState,
      connection: {
        directoryName: projectState.connection.directoryName,
        kind: 'download',
        writable: false,
      },
      dirty: false,
    }
  }

  const directoryHandle = await pickDirectory('read')
  callbacks.onWorkStart?.()
  const projectState = await readProject(directoryHandle)
  const writable = await hasWritePermission(directoryHandle)

  return {
    ...projectState,
    connection: {
      directoryHandle,
      directoryName: directoryHandle.name,
      kind: 'directory',
      writable,
    },
    dirty: false,
  }
}

export async function saveProject(
  state: ProjectState,
  callbacks: ProjectActionCallbacks = {},
): Promise<ProjectWriteResult> {
  const savedAt = new Date().toISOString()
  const handle = state.connection.directoryHandle

  if (handle && await requestWritePermission(handle)) {
    callbacks.onWorkStart?.()
    await writeProjectFiles(handle, state)
    return {
      connection: {
        ...state.connection,
        kind: 'directory',
        writable: true,
      },
      savedAt,
    }
  }

  downloadProjectZip(state)
  return {
    connection: {
      ...state.connection,
      kind: 'download',
      writable: false,
    },
    savedAt,
  }
}

export function downloadProjectZip(state: ProjectState) {
  const rootName = safeName(state.project.name)
  const files = buildProjectFiles(state).map((file) => ({
    ...file,
    path: `${rootName}/${file.path}`,
  }))
  const blob = buildZip(files)
  downloadBlob(blob, `${safeName(state.project.name)}.zip`)
}

async function pickDirectory(mode: DirectoryPickerMode) {
  const pickerHost = window as DirectoryPickerWindow
  if (!pickerHost.showDirectoryPicker) {
    throw readerError('directoryPickerUnsupported')
  }
  try {
    return await pickerHost.showDirectoryPicker({
      id: 'inqtrix-project',
      mode,
    })
  } catch (error) {
    if (error instanceof TypeError) {
      return pickerHost.showDirectoryPicker({ mode })
    }
    throw error
  }
}

async function readProject(directoryHandle: FileSystemDirectoryHandle): Promise<ProjectState> {
  const manifestFile = await directoryHandle.getFileHandle('project.md')
  const manifestMarkdown = await (await manifestFile.getFile()).text()
  const manifest = parseProjectManifest(manifestMarkdown)
  const researchRuns = await readResearchRuns(directoryHandle)
  const {
    editorComments,
    editorDocuments,
    editorImportIdentities,
  } = await readEditorDocuments(directoryHandle)
  const chatThreads = await readChatThreads(directoryHandle)
  const chatRules = await readChatRules(directoryHandle)
  const fileAssets = await readFileAssets(directoryHandle)
  return buildProjectStateFromFiles({
    chatRules,
    chatThreads,
    directoryName: directoryHandle.name,
    editorComments,
    editorDocuments,
    editorImportIdentities,
    fileAssets,
    manifest,
    researchRuns,
  })
}

async function pickProjectFilesWithInput(): Promise<File[]> {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input')
    let settled = false

    input.type = 'file'
    input.multiple = true
    input.accept = '.md,text/markdown,text/plain'
    input.style.position = 'fixed'
    input.style.left = '-10000px'
    input.style.opacity = '0'
    input.setAttribute('directory', '')
    input.setAttribute('webkitdirectory', '')

    const settle = (callback: () => void) => {
      if (settled) return
      settled = true
      input.remove()
      callback()
    }
    const cancel = () => {
      settle(() => reject(new DOMException('Project folder selection canceled.', 'AbortError')))
    }

    input.addEventListener('change', () => {
      const files = Array.from(input.files ?? [])
      if (files.length > 0) {
        settle(() => resolve(files))
      } else {
        cancel()
      }
    })
    input.addEventListener('cancel', cancel)
    document.body.appendChild(input)
    input.click()
  })
}

async function readProjectFromFiles(files: File[]): Promise<ProjectState> {
  const markdownFiles = await Promise.all(
    files
      .filter((file) => file.name.endsWith('.md'))
      .map(async (file) => ({
        contents: await file.text(),
        fileName: file.name,
        path: inputFilePath(file),
      })),
  )
  const manifestFile = findManifestFile(markdownFiles)
  if (!manifestFile) {
    throw readerError('projectFileMissing')
  }

  const manifest = parseProjectManifest(manifestFile.contents)
  const rootPrefix = projectRootPrefix(manifestFile.path)
  const researchRuns: ProjectState['researchRuns'] = {}
  const chatThreads: ProjectState['chatThreads'] = {}
  const chatRules: ProjectState['chatRules'] = {}
  const editorDocuments: ProjectState['editorDocuments'] = {}
  const editorComments: ProjectState['editorComments'] = {}
  const editorImportIdentities: EditorDocumentImportIdentity[] = []
  const fileAssets: Record<string, FileAssetRecord> = {}

  for (const file of markdownFiles) {
    const relativePath = relativeProjectPath(file.path, rootPrefix)
    if (relativePath === 'project.md') continue
    if (relativePath.startsWith('search-history/') && relativePath.endsWith('.md')) {
      const run = parseResearchRun(file.contents)
      researchRuns[run.runId] = { ...run, source: 'imported' }
    }
    if (relativePath.startsWith('chat-history/') && relativePath.endsWith('.md')) {
      const thread = parseChatThread(file.contents)
      chatThreads[thread.id] = thread
    }
    if (relativePath.startsWith('rules/') && relativePath.endsWith('.md')) {
      const rule = parseChatRule(file.contents)
      chatRules[rule.id] = rule
    }
    if (relativePath.startsWith('files/') && relativePath.endsWith('.md')) {
      const asset = parseFileAsset(file.contents)
      fileAssets[asset.id] = asset
    }
    if (relativePath.startsWith('documents/') && relativePath.endsWith('.md')) {
      const parsed = parseEditorDocument(file.contents)
      editorDocuments[parsed.document.id] = parsed.document
      if (parsed.importIdentity) editorImportIdentities.push(parsed.importIdentity)
      for (const comment of parsed.comments) {
        editorComments[comment.id] = comment
      }
    }
  }

  if (
    Object.keys(researchRuns).length === 0
    && Object.keys(chatThreads).length === 0
    && Object.keys(chatRules).length === 0
    && Object.keys(editorDocuments).length === 0
    && Object.keys(fileAssets).length === 0
  ) {
    parseUnscopedProjectFiles(
      markdownFiles,
      manifestFile,
      researchRuns,
      chatThreads,
      chatRules,
      editorDocuments,
      editorComments,
      editorImportIdentities,
      fileAssets,
    )
  }

  return buildProjectStateFromFiles({
    chatRules,
    chatThreads,
    directoryName: projectDirectoryName(rootPrefix, manifest),
    editorComments,
    editorDocuments,
    editorImportIdentities,
    fileAssets,
    manifest,
    researchRuns,
  })
}

function remapEditorManifestReferences(
  manifest: Record<string, unknown>,
  identities: EditorDocumentImportIdentity[],
): Record<string, unknown> {
  if (identities.length === 0) return manifest

  const documentIds = uniqueImportIdentityMap(
    identities.map((identity) => identity.documentId),
  )
  const commentIds = uniqueImportIdentityMap(
    identities.flatMap((identity) => identity.commentIds),
  )
  const editorUi = recordValue(manifest.editor_ui)
  const ui = recordValue(manifest.ui)
  const pinnedExplorer = recordValue(ui?.pinnedExplorer)

  return {
    ...manifest,
    editor_document_order: remapImportReference(
      manifest.editor_document_order,
      documentIds,
    ),
    ...(editorUi
      ? {
          editor_ui: remapRecordReferences(editorUi, {
            activeDocumentId: documentIds,
            active_document_id: documentIds,
            openDocumentIds: documentIds,
            open_document_ids: documentIds,
            selectedCommentId: commentIds,
            selected_comment_id: commentIds,
          }),
        }
      : {}),
    ...(ui
      ? {
          ui: {
            ...ui,
            ...(pinnedExplorer
              ? {
                  pinnedExplorer: remapRecordReferences(pinnedExplorer, {
                    editorDocumentIds: documentIds,
                  }),
                }
              : {}),
          },
        }
      : {}),
  }
}

function uniqueImportIdentityMap(
  identities: Array<{ sourceId: string; targetId: string }>,
): Map<string, string | null> {
  const result = new Map<string, string | null>()
  for (const { sourceId, targetId } of identities) {
    if (!result.has(sourceId)) {
      result.set(sourceId, targetId)
      continue
    }
    if (result.get(sourceId) !== targetId) result.set(sourceId, null)
  }
  return result
}

function remapRecordReferences(
  record: Record<string, unknown>,
  references: Record<string, ReadonlyMap<string, string | null>>,
) {
  const remapped = { ...record }
  for (const [key, identities] of Object.entries(references)) {
    if (key in record) remapped[key] = remapImportReference(record[key], identities)
  }
  return remapped
}

function remapImportReference(
  value: unknown,
  identities: ReadonlyMap<string, string | null>,
): unknown {
  if (Array.isArray(value)) {
    return value
      .map((item) => remapImportReference(item, identities))
      .filter((item) => item !== null)
  }
  if (typeof value !== 'string' || !identities.has(value)) return value
  return identities.get(value) ?? null
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function buildProjectStateFromFiles({
  chatRules,
  chatThreads,
  directoryName,
  editorComments,
  editorDocuments,
  editorImportIdentities,
  fileAssets,
  manifest: rawManifest,
  researchRuns,
}: {
  chatRules: ProjectState['chatRules']
  chatThreads: ProjectState['chatThreads']
  directoryName: string
  editorComments: ProjectState['editorComments']
  editorDocuments: ProjectState['editorDocuments']
  editorImportIdentities: EditorDocumentImportIdentity[]
  fileAssets: Record<string, FileAssetRecord>
  manifest: Record<string, unknown>
  researchRuns: ProjectState['researchRuns']
}): ProjectState {
  const manifest = remapEditorManifestReferences(rawManifest, editorImportIdentities)
  const project = manifest.project
  const ui = manifest.ui
  const editorUi = manifest.editor_ui
  const researchRunOrder = Object.keys(researchRuns).sort((a, b) => {
    return researchRuns[b].submittedAt.localeCompare(researchRuns[a].submittedAt)
  })
  const manifestEditorDocumentOrder = editorDocumentOrderFromManifest(
    manifest.editor_document_order,
    editorDocuments,
  )
  const editorDocumentOrder = manifestEditorDocumentOrder.length > 0
    ? manifestEditorDocumentOrder
    : Object.keys(editorDocuments).sort((a, b) => {
      const byDate = editorDocuments[b].updatedAt.localeCompare(editorDocuments[a].updatedAt)
      return byDate || editorDocuments[a].title.localeCompare(editorDocuments[b].title)
    })
  // Restore the persisted thread order (manual mode's frozen sequence);
  // threads the manifest does not cover append by updatedAt desc, and a
  // manifest without the key (older exports) falls back entirely.
  const persistedThreadOrder = Array.isArray(manifest.chat_thread_order)
    ? (manifest.chat_thread_order as unknown[]).filter(
      (id): id is string => typeof id === 'string' && Boolean(chatThreads[id]),
    )
    : []
  const coveredThreadIds = new Set(persistedThreadOrder)
  const chatThreadOrder = [
    ...persistedThreadOrder,
    ...Object.keys(chatThreads)
      .filter((id) => !coveredThreadIds.has(id))
      .sort((a, b) => chatThreads[b].updatedAt.localeCompare(chatThreads[a].updatedAt)),
  ]
  const manifestRuleOrder = ruleOrderFromManifest(manifest.rule_order, chatRules)
  const chatRuleOrder = manifestRuleOrder.length > 0
    ? manifestRuleOrder
    : Object.keys(chatRules).sort((a, b) => {
      const byDate = chatRules[b].updatedAt.localeCompare(chatRules[a].updatedAt)
      return byDate || chatRules[a].title.localeCompare(chatRules[b].title)
    })
  const chatThreadGroups = chatThreadGroupsFromManifest(manifest.chat_groups)
  const chatThreadGroupOrder = chatThreadGroupOrderFromManifest(
    manifest.chat_group_order,
    chatThreadGroups,
  )
  const chatThreadGroupMemberships = chatThreadGroupMembershipsFromManifest(
    manifest.chat_thread_group_memberships,
    chatThreads,
    chatThreadGroups,
  )
  const editorFolders = editorFoldersFromManifest(manifest.editor_folders)
  const editorFolderOrder = editorFolderOrderFromManifest(
    manifest.editor_folder_order,
    editorFolders,
  )
  const filteredEditorComments = filterEditorComments(editorComments, editorDocuments)
  const workspaceId = workspaceIdOrDefault(manifest.workspace_id)
  rememberBrowserWorkspaceId(workspaceId)
  const fileLibrary = resolveFileLibraryFromManifest(manifest, fileAssets)
  const { vectorIndexOrder, vectorIndexes } = resolveVectorIndexesFromManifest(manifest, fileLibrary.fileAssets)
  const knowledgeSessionCreatedAt = stringOrNow((project as Record<string, unknown>).updatedAt)
  const defaultKnowledgeSession = createBootstrapKnowledgeSession(
    knowledgeSessionCreatedAt,
  )

  return {
    chatRuleOrder,
    chatRules,
    chatThreadGroupMemberships,
    chatThreadGroupOrder,
    chatThreadGroups,
    chatThreadOrder,
    chatThreads,
    connection: {
      kind: 'demo',
      writable: false,
    },
    fileAssetOrder: fileLibrary.fileAssetOrder,
    fileAssets: fileLibrary.fileAssets,
    fileGroupOrder: fileLibrary.fileGroupOrder,
    fileGroups: fileLibrary.fileGroups,
    fileLibrarySectionOrder: fileLibrary.sectionOrder,
    fileLibrarySections: fileLibrary.sections,
    // Live reindex progress is ephemeral — a freshly loaded project has
    // no in-flight jobs (a running server job is re-attached by the hook).
    indexingJobs: {},
    // The knowledge Q&A thread is session-scoped (it references
    // short-lived server runs) and is not part of project files.
    knowledgeItemOrder: [],
    knowledgeItems: {},
    knowledgeSessionGroupMemberships: {},
    knowledgeSessionGroupOrder: [],
    knowledgeSessionGroups: {},
    knowledgeSessionOrder: [defaultKnowledgeSession.id],
    knowledgeSessions: { [defaultKnowledgeSession.id]: defaultKnowledgeSession },
    selectedKnowledgeSessionId: defaultKnowledgeSession.id,
    // Agent Desk state is session-scoped (references short-lived server
    // runs) and not part of project files — server hydration rebuilds it.
    agentRuns: {},
    agentSessionArtifacts: {},
    agentSessionGroupOrder: [],
    agentSessionGroups: {},
    agentSessionOrder: [],
    agentSessions: {},
    selectedAgentSessionId: null,
    agentCanvas: EMPTY_CANVAS_STATE,
    agentPlanDrafts: {},
    dirty: false,
    editorComments: filteredEditorComments,
    editorDocumentOrder,
    editorDocuments,
    editorFolderOrder,
    editorFolders,
    editorSuggestionGroups: {},
    editorSuggestions: {},
    editorUi: editorUiFromManifest(editorUi, editorDocumentOrder, filteredEditorComments, editorDocuments),
    localRunCounter: nextLocalRunCounter(researchRunOrder),
    preferences: preferencesOrDefault(manifest.preferences),
    project: {
      createdAt: stringOrNow((project as Record<string, unknown>).createdAt),
      name: stringOrDefault((project as Record<string, unknown>).name, directoryName),
      schemaVersion: PROJECT_SCHEMA_VERSION,
      updatedAt: stringOrNow((project as Record<string, unknown>).updatedAt),
    },
    researchRunOrder,
    researchRuns,
    serverSyncEnabled: booleanOrDefault(manifest.server_sync_enabled, false),
    // Ephemeral; the reducer's hydrateProject bumps it on dispatch. The loaded
    // literal just needs a seed (never read from the manifest).
    projectEpoch: 0,
    vectorIndexOrder,
    vectorIndexes,
    workspaceId,
    ui: {
      activeFilter: filterOrDefault((ui as Record<string, unknown>).activeFilter),
      activeView: viewOrDefault((ui as Record<string, unknown>).activeView),
      chatChainingEnabled: booleanOrDefault((ui as Record<string, unknown>).chatChainingEnabled, false),
      expandedJobId: researchRunOrder.includes((ui as Record<string, unknown>).expandedJobId as string)
        ? (ui as Record<string, unknown>).expandedJobId as string
        : researchRunOrder[0] ?? null,
      isAgentSessionsVisible: booleanOrDefault((ui as Record<string, unknown>).isAgentSessionsVisible, true),
      isChatHistoryVisible: booleanOrDefault((ui as Record<string, unknown>).isChatHistoryVisible, true),
      isKnowledgeHistoryVisible: booleanOrDefault((ui as Record<string, unknown>).isKnowledgeHistoryVisible, true),
      isComposerVisible: booleanOrDefault((ui as Record<string, unknown>).isComposerVisible, true),
      isReportExpanded: booleanOrDefault((ui as Record<string, unknown>).isReportExpanded, false),
      isReportVisible: booleanOrDefault((ui as Record<string, unknown>).isReportVisible, true),
      panelLayout: normalizePanelLayout((ui as Record<string, unknown>).panelLayout),
      pendingChatAttachmentRefs: pendingAttachmentRefsOrDefault(
        (ui as Record<string, unknown>).pendingChatAttachmentRefs,
        (ui as Record<string, unknown>).pendingChatReportRunId,
        researchRuns,
        chatRules,
      ),
      pendingChatReportRunId: pendingReportRunIdOrDefault(
        (ui as Record<string, unknown>).pendingChatReportRunId,
        researchRuns,
      ),
      explorerSort: resolveExplorerSortState(
        (ui as Record<string, unknown>).explorerSort,
      ),
      pinnedExplorer: resolvePinnedExplorerFromManifest(
        (ui as Record<string, unknown>).pinnedExplorer,
        {
          chatThreadIds: chatThreadOrder,
          editorDocumentIds: editorDocumentOrder,
        },
      ),
      // Never restored from the file: the account preferences own the model
      // selection now. A manifest written by an older build still carries
      // these keys; ignoring them is what keeps the account row the single
      // source, and the cast above tolerates the surplus frontmatter without
      // a schema bump.
      selectedAgentEffort: null,
      selectedAgentModel: null,
      selectedAgentModelTier: null,
      selectedChatEffort: null,
      selectedChatModel: null,
      selectedChatModelTier: null,
      selectedChatThreadId: chatThreadOrder.includes((ui as Record<string, unknown>).selectedChatThreadId as string)
        ? (ui as Record<string, unknown>).selectedChatThreadId as string
        : chatThreadOrder[0] ?? null,
      // Intent only — agent sessions hydrate from the server later, so
      // membership cannot be validated here (unlike chat threads above).
      selectedAgentSessionId:
        stringOrDefault((ui as Record<string, unknown>).selectedAgentSessionId, '') || null,
      selectedJobId: researchRunOrder.includes((ui as Record<string, unknown>).selectedJobId as string)
        ? (ui as Record<string, unknown>).selectedJobId as string
        : researchRunOrder[0] ?? null,
      selectedStack: stringOrDefault((ui as Record<string, unknown>).selectedStack, 'anthropic_perplexity'),
    },
  }
}

function workspaceIdOrDefault(value: unknown) {
  return isWorkspaceId(value) ? value : getOrCreateBrowserWorkspaceId()
}

export function resolvePinnedExplorerFromManifest(
  value: unknown,
  validIds: {
    chatThreadIds?: readonly string[]
    editorDocumentIds?: readonly string[]
    knowledgeSessionIds?: readonly string[]
    agentSessionIds?: readonly string[]
  } = {},
): PinnedExplorerState {
  const record = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    chatThreadIds: pinnedIdsFromManifest(record.chatThreadIds, validIds.chatThreadIds),
    editorDocumentIds: pinnedIdsFromManifest(record.editorDocumentIds, validIds.editorDocumentIds),
    knowledgeSessionIds: pinnedIdsFromManifest(record.knowledgeSessionIds, validIds.knowledgeSessionIds),
    agentSessionIds: pinnedIdsFromManifest(record.agentSessionIds, validIds.agentSessionIds),
  }
}

function pinnedIdsFromManifest(value: unknown, validIds?: readonly string[]) {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const valid = validIds ? new Set(validIds) : null
  return value.filter((id): id is string => {
    if (typeof id !== 'string' || !id.trim()) return false
    if (seen.has(id)) return false
    if (valid && !valid.has(id)) return false
    seen.add(id)
    return true
  })
}

function chatThreadGroupsFromManifest(value: unknown): ProjectState['chatThreadGroups'] {
  if (!Array.isArray(value)) return {}
  const groups: ProjectState['chatThreadGroups'] = {}

  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' && record.id.trim()
      ? record.id
      : typeof record.group_id === 'string' && record.group_id.trim()
        ? record.group_id
        : ''
    const title = typeof record.title === 'string' && record.title.trim()
      ? record.title
      : ''
    if (!id || !title) continue
    groups[id] = {
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      id,
      title,
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    }
  }

  return groups
}

function chatThreadGroupOrderFromManifest(
  value: unknown,
  chatThreadGroups: ProjectState['chatThreadGroups'],
) {
  const orderedGroupIds = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(chatThreadGroups[item]))
    : []
  const missingGroupIds = Object.keys(chatThreadGroups)
    .filter((groupId) => !orderedGroupIds.includes(groupId))
    .sort((a, b) => {
      const byDate = chatThreadGroups[b].updatedAt.localeCompare(chatThreadGroups[a].updatedAt)
      return byDate || chatThreadGroups[a].title.localeCompare(chatThreadGroups[b].title)
    })
  return [...orderedGroupIds, ...missingGroupIds]
}

function chatThreadGroupMembershipsFromManifest(
  value: unknown,
  chatThreads: ProjectState['chatThreads'],
  chatThreadGroups: ProjectState['chatThreadGroups'],
) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const memberships: ProjectState['chatThreadGroupMemberships'] = {}
  for (const [threadId, groupId] of Object.entries(value as Record<string, unknown>)) {
    if (
      chatThreads[threadId]
      && typeof groupId === 'string'
      && Boolean(chatThreadGroups[groupId])
    ) {
      memberships[threadId] = groupId
    }
  }
  return memberships
}

function editorFoldersFromManifest(value: unknown): ProjectState['editorFolders'] {
  if (!Array.isArray(value)) return {}
  const folders: ProjectState['editorFolders'] = {}

  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' && record.id.trim() ? record.id : ''
    const title = typeof record.title === 'string' && record.title.trim() ? record.title : ''
    if (!id || !title) continue
    folders[id] = {
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      id,
      title,
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    }
  }

  return folders
}

function editorFolderOrderFromManifest(
  value: unknown,
  editorFolders: ProjectState['editorFolders'],
) {
  const orderedFolderIds = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(editorFolders[item]))
    : []
  const missingFolderIds = Object.keys(editorFolders)
    .filter((folderId) => !orderedFolderIds.includes(folderId))
    .sort((a, b) => {
      const byDate = editorFolders[b].updatedAt.localeCompare(editorFolders[a].updatedAt)
      return byDate || editorFolders[a].title.localeCompare(editorFolders[b].title)
    })
  return [...orderedFolderIds, ...missingFolderIds]
}

function editorDocumentOrderFromManifest(
  value: unknown,
  editorDocuments: ProjectState['editorDocuments'],
) {
  const orderedDocumentIds = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(editorDocuments[item]))
    : []
  const missingDocumentIds = Object.keys(editorDocuments).filter((documentId) => {
    return !orderedDocumentIds.includes(documentId)
  })
  return [...orderedDocumentIds, ...missingDocumentIds]
}

function editorUiFromManifest(
  value: unknown,
  editorDocumentOrder: string[],
  editorComments: ProjectState['editorComments'],
  editorDocuments: ProjectState['editorDocuments'],
): ProjectState['editorUi'] {
  const record = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  const openDocumentIds = Array.isArray(record.openDocumentIds)
    ? record.openDocumentIds.filter((item): item is string => typeof item === 'string' && Boolean(editorDocuments[item]))
    : Array.isArray(record.open_document_ids)
      ? record.open_document_ids.filter((item): item is string => typeof item === 'string' && Boolean(editorDocuments[item]))
      : []
  const activeDocumentId = stringFromCandidates(
    record.activeDocumentId,
    record.active_document_id,
  )
  const safeActiveDocumentId = activeDocumentId && editorDocuments[activeDocumentId]
    ? activeDocumentId
    : openDocumentIds[0] ?? editorDocumentOrder[0] ?? null
  const selectedCommentId = stringFromCandidates(
    record.selectedCommentId,
    record.selected_comment_id,
  )

  return {
    activeDocumentId: safeActiveDocumentId,
    assistantDraft: stringOrDefault(record.assistantDraft ?? record.assistant_draft, ''),
    isAssistantVisible: booleanOrDefault(record.isAssistantVisible ?? record.is_assistant_visible, true),
    isCommentPanelVisible: booleanOrDefault(record.isCommentPanelVisible ?? record.is_comment_panel_visible, true),
    isDiffVisible: booleanOrDefault(record.isDiffVisible ?? record.is_diff_visible, false),
    isTreeVisible: booleanOrDefault(record.isTreeVisible ?? record.is_tree_visible, true),
    openDocumentIds: safeActiveDocumentId
      ? addUniqueStrings(openDocumentIds, safeActiveDocumentId)
      : openDocumentIds,
    panelTab: record.panelTab === 'assistant' || record.panel_tab === 'assistant'
      ? 'assistant'
      : 'comments',
    selectedCommentId: selectedCommentId && editorComments[selectedCommentId]
      ? selectedCommentId
      : null,
    viewMode: record.viewMode === 'source' || record.view_mode === 'source'
      ? 'source'
      : 'live',
  }
}

function filterEditorComments(
  editorComments: ProjectState['editorComments'],
  editorDocuments: ProjectState['editorDocuments'],
) {
  return Object.fromEntries(
    Object.entries(editorComments).filter(([, comment]) => Boolean(editorDocuments[comment.documentId])),
  )
}

function stringFromCandidates(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return null
}

function addUniqueStrings(items: readonly string[], value: string) {
  return items.includes(value) ? [...items] : [...items, value]
}

function inputFilePath(file: File) {
  const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath
  return normalizeFilePath(relativePath || file.name)
}

function normalizeFilePath(path: string) {
  return path.replace(/\\/g, '/').replace(/^\/+/, '')
}

function findManifestFile(files: SelectedProjectFile[]) {
  return files.find((file) => {
    return file.path === 'project.md'
      || file.path.endsWith('/project.md')
      || file.fileName === 'project.md'
  })
}

function projectRootPrefix(manifestPath: string) {
  if (manifestPath === 'project.md') return ''
  return manifestPath.replace(/project\.md$/, '')
}

function relativeProjectPath(path: string, rootPrefix: string) {
  if (!rootPrefix) return path
  return path.startsWith(rootPrefix) ? path.slice(rootPrefix.length) : path
}

function projectDirectoryName(rootPrefix: string, manifest: Record<string, unknown>) {
  const project = manifest.project as Record<string, unknown> | undefined
  const fromRoot = rootPrefix.replace(/\/+$/, '').split('/').filter(Boolean).pop()
  return stringOrDefault(fromRoot, stringOrDefault(project?.name, 'Inqtrix Project'))
}

function parseUnscopedProjectFiles(
  files: SelectedProjectFile[],
  manifestFile: SelectedProjectFile,
  researchRuns: ProjectState['researchRuns'],
  chatThreads: ProjectState['chatThreads'],
  chatRules: ProjectState['chatRules'],
  editorDocuments: ProjectState['editorDocuments'],
  editorComments: ProjectState['editorComments'],
  editorImportIdentities: EditorDocumentImportIdentity[],
  fileAssets: Record<string, FileAssetRecord>,
) {
  for (const file of files) {
    if (file === manifestFile) continue
    try {
      const parsed = parseEditorDocument(file.contents)
      editorDocuments[parsed.document.id] = parsed.document
      if (parsed.importIdentity) editorImportIdentities.push(parsed.importIdentity)
      for (const comment of parsed.comments) {
        editorComments[comment.id] = comment
      }
      continue
    } catch {
      // Flat multi-file fallback cannot rely on folder paths, so kind detection is deliberate.
    }
    try {
      const asset = parseFileAsset(file.contents)
      fileAssets[asset.id] = asset
      continue
    } catch {
      // Flat multi-file fallback cannot rely on folder paths, so kind detection is deliberate.
    }
    try {
      const run = parseResearchRun(file.contents)
      researchRuns[run.runId] = { ...run, source: 'imported' }
      continue
    } catch {
      // Flat multi-file fallback cannot rely on folder paths, so kind detection is deliberate.
    }
    try {
      const thread = parseChatThread(file.contents)
      chatThreads[thread.id] = thread
      continue
    } catch {
      // Ignore unrelated Markdown files when the browser only provides a flat file selection.
    }
    try {
      const rule = parseChatRule(file.contents)
      chatRules[rule.id] = rule
    } catch {
      // Ignore unrelated Markdown files when the browser only provides a flat file selection.
    }
  }
}

async function readResearchRuns(directoryHandle: FileSystemDirectoryHandle) {
  const directory = await getOptionalDirectory(directoryHandle, 'search-history')
  const runs: ProjectState['researchRuns'] = {}
  if (!directory) return runs

  for await (const [, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind !== 'file' || !handle.name.endsWith('.md')) continue
    const file = await (handle as FileSystemFileHandle).getFile()
    const run = parseResearchRun(await file.text())
    runs[run.runId] = { ...run, source: 'imported' }
  }
  return runs
}

async function readEditorDocuments(directoryHandle: FileSystemDirectoryHandle) {
  const directory = await getOptionalDirectory(directoryHandle, 'documents')
  const editorDocuments: ProjectState['editorDocuments'] = {}
  const editorComments: ProjectState['editorComments'] = {}
  const editorImportIdentities: EditorDocumentImportIdentity[] = []
  if (!directory) return { editorComments, editorDocuments, editorImportIdentities }

  for await (const [, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind !== 'file' || !handle.name.endsWith('.md')) continue
    const file = await (handle as FileSystemFileHandle).getFile()
    const parsed = parseEditorDocument(await file.text())
    editorDocuments[parsed.document.id] = parsed.document
    if (parsed.importIdentity) editorImportIdentities.push(parsed.importIdentity)
    for (const comment of parsed.comments) {
      editorComments[comment.id] = comment
    }
  }

  return { editorComments, editorDocuments, editorImportIdentities }
}

async function readChatThreads(directoryHandle: FileSystemDirectoryHandle) {
  const directory = await getOptionalDirectory(directoryHandle, 'chat-history')
  const threads: ProjectState['chatThreads'] = {}
  if (!directory) return threads

  for await (const [, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind !== 'file' || !handle.name.endsWith('.md')) continue
    const file = await (handle as FileSystemFileHandle).getFile()
    const thread = parseChatThread(await file.text())
    threads[thread.id] = thread
  }
  return threads
}

async function readChatRules(directoryHandle: FileSystemDirectoryHandle) {
  const directory = await getOptionalDirectory(directoryHandle, 'rules')
  const rules: ProjectState['chatRules'] = {}
  if (!directory) return rules

  for await (const [, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind !== 'file' || !handle.name.endsWith('.md')) continue
    const file = await (handle as FileSystemFileHandle).getFile()
    const rule = parseChatRule(await file.text())
    rules[rule.id] = rule
  }
  return rules
}

async function readFileAssets(directoryHandle: FileSystemDirectoryHandle) {
  const directory = await getOptionalDirectory(directoryHandle, 'files')
  const fileAssets: Record<string, FileAssetRecord> = {}
  if (!directory) return fileAssets

  for await (const [, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind !== 'file' || !handle.name.endsWith('.md')) continue
    const file = await (handle as FileSystemFileHandle).getFile()
    const asset = parseFileAsset(await file.text())
    fileAssets[asset.id] = asset
  }
  return fileAssets
}

/**
 * Reconstruct the file library (sections, groups, assets and their orders) from
 * the manifest metadata plus the per-asset markdown files. Falls back to the
 * default three sections for projects saved before the library existed, always
 * keeps a temporary section, and re-homes any asset whose section/group no
 * longer exists so nothing silently disappears from the database view.
 */
function resolveFileLibraryFromManifest(
  manifest: Record<string, unknown>,
  loadedAssets: Record<string, FileAssetRecord>,
): {
  fileAssetOrder: string[]
  fileAssets: Record<string, FileAssetRecord>
  fileGroupOrder: string[]
  fileGroups: Record<string, FileGroupRecord>
  sectionOrder: string[]
  sections: Record<string, FileLibrarySectionRecord>
} {
  const now = new Date().toISOString()
  const parsedSections = fileLibrarySectionsFromManifest(manifest.file_sections)
  const sectionList = parsedSections.length > 0 ? parsedSections : createDefaultFileLibrarySections(now)
  const sections: Record<string, FileLibrarySectionRecord> = {}
  for (const section of sectionList) sections[section.id] = section
  if (!Object.values(sections).some((section) => section.kind === 'temporary')) {
    const fallbackTemp = createDefaultFileLibrarySections(now).find((section) => section.kind === 'temporary')
    if (fallbackTemp) sections[fallbackTemp.id] = fallbackTemp
  }
  const tempSectionId = temporaryFileSectionId(Object.values(sections))
  const sectionOrder = orderFromManifest(manifest.file_section_order, sections)

  const fileGroups: Record<string, FileGroupRecord> = {}
  for (const group of fileGroupsFromManifest(manifest.file_groups)) {
    if (sections[group.sectionId]) fileGroups[group.id] = group
  }
  const fileGroupOrder = orderFromManifest(manifest.file_group_order, fileGroups)

  const fileAssets: Record<string, FileAssetRecord> = {}
  for (const [id, asset] of Object.entries(loadedAssets)) {
    const sectionId = sections[asset.sectionId] ? asset.sectionId : tempSectionId
    const group = asset.groupId ? fileGroups[asset.groupId] : undefined
    const groupId = group && group.sectionId === sectionId ? asset.groupId : null
    fileAssets[id] = sectionId === asset.sectionId && groupId === asset.groupId
      ? asset
      : { ...asset, groupId, sectionId }
  }
  const fileAssetOrder = orderFromManifest(
    manifest.file_asset_order,
    fileAssets,
    (a, b) => fileAssets[a].createdAt.localeCompare(fileAssets[b].createdAt),
  )

  return { fileAssetOrder, fileAssets, fileGroupOrder, fileGroups, sectionOrder, sections }
}

function orderFromManifest<T extends { updatedAt: string }>(
  value: unknown,
  records: Record<string, T>,
  sortMissing?: (a: string, b: string) => number,
): string[] {
  const ordered = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(records[item]))
    : []
  const fallbackSort = sortMissing
    ?? ((a: string, b: string) => records[b].updatedAt.localeCompare(records[a].updatedAt))
  const missing = Object.keys(records).filter((id) => !ordered.includes(id)).sort(fallbackSort)
  return [...ordered, ...missing]
}

function fileLibrarySectionsFromManifest(value: unknown): FileLibrarySectionRecord[] {
  if (!Array.isArray(value)) return []
  const sections: FileLibrarySectionRecord[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' && record.id.trim() ? record.id : ''
    const title = typeof record.title === 'string' && record.title.trim() ? record.title : ''
    if (!id || !title) continue
    const lifecycleStatus = record.lifecycleStatus ?? record.lifecycle_status
    const semanticRole = record.semanticRole ?? record.semantic_role
    sections.push({
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      ...(record.deletionError !== undefined || record.deletion_error !== undefined
        ? {
            deletionError:
              typeof (record.deletionError ?? record.deletion_error) === 'string'
                ? String(record.deletionError ?? record.deletion_error)
                : null,
          }
        : {}),
      ...(record.deletionOperationId !== undefined
        || record.deletion_operation_id !== undefined
        ? {
            deletionOperationId:
              typeof (record.deletionOperationId ?? record.deletion_operation_id) === 'string'
                ? String(record.deletionOperationId ?? record.deletion_operation_id)
                : null,
          }
        : {}),
      ...(record.deletionStage !== undefined || record.deletion_stage !== undefined
        ? {
            deletionStage:
              typeof (record.deletionStage ?? record.deletion_stage) === 'string'
                ? String(record.deletionStage ?? record.deletion_stage)
                : null,
          }
        : {}),
      id,
      isBootstrapPlaceholder:
        record.isBootstrapPlaceholder === true
        || record.is_bootstrap_placeholder === true,
      kind: record.kind === 'temporary' ? 'temporary' : 'custom',
      ...(semanticRole === 'temporary'
      || semanticRole === 'library'
      || semanticRole === 'project_sources'
      || semanticRole === 'custom'
        ? { semanticRole }
        : {}),
      ...(lifecycleStatus === 'active'
        || lifecycleStatus === 'deleting'
        || lifecycleStatus === 'delete_failed'
        ? { lifecycleStatus }
        : {}),
      ...(typeof record.serverSynced === 'boolean'
        ? { serverSynced: record.serverSynced }
        : typeof record.server_synced === 'boolean'
          ? { serverSynced: record.server_synced }
          : {}),
      title,
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    })
  }
  return sections
}

function fileGroupsFromManifest(value: unknown): FileGroupRecord[] {
  if (!Array.isArray(value)) return []
  const groups: FileGroupRecord[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' && record.id.trim() ? record.id : ''
    const sectionId = typeof record.sectionId === 'string' && record.sectionId.trim()
      ? record.sectionId
      : typeof record.section_id === 'string' && record.section_id.trim()
        ? record.section_id
        : ''
    const title = typeof record.title === 'string' && record.title.trim() ? record.title : ''
    if (!id || !sectionId || !title) continue
    groups.push({
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      id,
      sectionId,
      title,
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    })
  }
  return groups
}

function embedModelIdOrDefault(value: unknown): EmbedModelId {
  return EMBED_MODELS.some((model) => model.id === value) ? (value as EmbedModelId) : DEFAULT_EMBED_MODEL_ID
}

function dimsForEmbedModelId(model: EmbedModelId): number {
  return EMBED_MODELS.find((entry) => entry.id === model)?.dims ?? 3072
}

function vectorIndexStatusOrDefault(
  value: unknown,
  members: readonly VectorIndexMemberRecord[],
): VectorIndexStatus {
  // A persisted 'indexing' is ALWAYS stale on load: no reindex run survives a
  // reload (the live `indexingJobs` map is never serialized), so an index
  // restored at 'indexing' has no job to finish it — the UI would show a
  // frozen spinner with Reindex disabled and Cancel a no-op, and its
  // server autosave would be deferred forever (vectorIndexChanged skips an
  // indexing record), silently stranding later membership/title edits.
  // Reconcile to the pre-run status (stale if any member still needs
  // embedding, else ready), exactly like markVectorIndexCancelled. A durable
  // server job, if one is still running, is re-attached by the indexing-job
  // resume sweep, which sets 'indexing' again.
  if (value === 'indexing') {
    return members.some((member) => member.state === 'pending') ? 'stale' : 'ready'
  }
  return value === 'stale'
    || value === 'error'
    || value === 'deleting'
    || value === 'delete_failed'
    ? value
    : 'ready'
}

function vectorIndexHistoryFromManifest(
  value: unknown,
): VectorIndexRunHistoryEntry[] {
  if (!Array.isArray(value)) return []
  const entries: VectorIndexRunHistoryEntry[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const result =
      record.result === 'error' || record.result === 'cancelled'
        ? record.result
        : 'ok'
    entries.push({
      documents: typeof record.documents === 'number' ? record.documents : 0,
      durationMs: typeof record.durationMs === 'number' ? record.durationMs : 0,
      error: typeof record.error === 'string' ? record.error : undefined,
      finishedAt: stringOrNow(record.finishedAt ?? record.finished_at),
      result,
      startedAt: stringOrNow(record.startedAt ?? record.started_at),
    })
  }
  return entries.slice(0, VECTOR_INDEX_HISTORY_LIMIT)
}

function vectorIndexMembersFromManifest(
  value: unknown,
  fileAssets: Record<string, FileAssetRecord>,
): VectorIndexMemberRecord[] {
  if (!Array.isArray(value)) return []
  const members: VectorIndexMemberRecord[] = []
  const seen = new Set<string>()
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const fileId = typeof record.fileId === 'string'
      ? record.fileId
      : typeof record.file_id === 'string'
        ? record.file_id
        : ''
    if (!fileId || seen.has(fileId) || !fileAssets[fileId]) continue
    seen.add(fileId)
    const state: VectorIndexMemberRecord['state'] =
      record.state === 'embedded' || record.state === 'skipped' ? record.state : 'pending'
    const serverDocumentId =
      typeof record.serverDocumentId === 'string' && record.serverDocumentId
        ? record.serverDocumentId
        : typeof record.server_document_id === 'string' && record.server_document_id
          ? record.server_document_id
          : undefined
    members.push({ fileId, state, ...(serverDocumentId ? { serverDocumentId } : {}) })
  }
  return members
}

function vectorIndexesFromManifest(
  value: unknown,
  fileAssets: Record<string, FileAssetRecord>,
): VectorIndexRecord[] {
  if (!Array.isArray(value)) return []
  const indexes: VectorIndexRecord[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' && record.id.trim() ? record.id : ''
    const title = typeof record.title === 'string' && record.title.trim() ? record.title : ''
    if (!id || !title) continue
    const model = embedModelIdOrDefault(record.model)
    const dims = typeof record.dims === 'number' ? record.dims : dimsForEmbedModelId(model)
    const history = vectorIndexHistoryFromManifest(record.history)
    const serverCollectionId =
      typeof record.serverCollectionId === 'string'
        ? record.serverCollectionId
        : typeof record.server_collection_id === 'string'
          ? record.server_collection_id
          : null
    const serverCollectionModel =
      typeof record.serverCollectionModel === 'string'
        ? record.serverCollectionModel
        : typeof record.server_collection_model === 'string'
          ? record.server_collection_model
          : null
    const members = vectorIndexMembersFromManifest(record.members, fileAssets)
    indexes.push({
      createdAt: stringOrNow(record.createdAt ?? record.created_at),
      dims,
      handle: typeof record.handle === 'string' && record.handle.trim() ? record.handle : id,
      ...(history.length > 0 ? { history } : {}),
      id,
      ...(typeof record.lastError === 'string' ? { lastError: record.lastError } : {}),
      members,
      model,
      ...(serverCollectionId ? { serverCollectionId } : {}),
      ...(serverCollectionModel ? { serverCollectionModel } : {}),
      status: vectorIndexStatusOrDefault(record.status, members),
      title,
      updatedAt: stringOrNow(record.updatedAt ?? record.updated_at),
    })
  }
  return indexes
}

/** Rebuilds the vector-index map + order from manifest frontmatter. Members
 * whose source asset is missing are dropped; an old project without the
 * `vector_indexes` key yields an empty map (no error). Exported for tests. */
export function resolveVectorIndexesFromManifest(
  manifest: Record<string, unknown>,
  fileAssets: Record<string, FileAssetRecord>,
): { vectorIndexOrder: string[]; vectorIndexes: Record<string, VectorIndexRecord> } {
  const vectorIndexes: Record<string, VectorIndexRecord> = {}
  for (const index of vectorIndexesFromManifest(manifest.vector_indexes, fileAssets)) {
    vectorIndexes[index.id] = index
  }
  const vectorIndexOrder = orderFromManifest(manifest.vector_index_order, vectorIndexes)
  return { vectorIndexOrder, vectorIndexes }
}

async function writeProjectFiles(
  directoryHandle: FileSystemDirectoryHandle,
  state: ProjectState,
) {
  const files = buildProjectFiles(state)
  await clearMarkdownDirectory(directoryHandle, 'search-history')
  await clearMarkdownDirectory(directoryHandle, 'documents')
  await clearMarkdownDirectory(directoryHandle, 'chat-history')
  await clearMarkdownDirectory(directoryHandle, 'rules')
  await clearMarkdownDirectory(directoryHandle, 'files')

  for (const file of files) {
    await writeProjectFile(directoryHandle, file)
  }
}

async function writeProjectFile(
  directoryHandle: FileSystemDirectoryHandle,
  file: ProjectFile,
) {
  const pathParts = file.path.split('/')
  const fileName = pathParts.pop()
  if (!fileName) throw readerError('projectFileNameMissing')
  let currentDirectory = directoryHandle

  for (const part of pathParts) {
    currentDirectory = await currentDirectory.getDirectoryHandle(part, { create: true })
  }

  const fileHandle = await currentDirectory.getFileHandle(fileName, { create: true })
  const writable = await fileHandle.createWritable()
  await writable.write(file.contents)
  await writable.close()
}

async function clearMarkdownDirectory(
  directoryHandle: FileSystemDirectoryHandle,
  name: string,
) {
  const directory = await directoryHandle.getDirectoryHandle(name, { create: true })
  for await (const [entryName, handle] of (directory as IterableDirectoryHandle).entries()) {
    if (handle.kind === 'file' && entryName.endsWith('.md')) {
      await directory.removeEntry(entryName)
    }
  }
}

async function getOptionalDirectory(
  directoryHandle: FileSystemDirectoryHandle,
  name: string,
) {
  try {
    return await directoryHandle.getDirectoryHandle(name)
  } catch {
    return null
  }
}

async function hasWritePermission(handle: FileSystemDirectoryHandle) {
  const permissionHandle = handle as PermissionCapableHandle
  if (!permissionHandle.queryPermission) return false
  return (await permissionHandle.queryPermission({ mode: 'readwrite' })) === 'granted'
}

async function requestWritePermission(handle: FileSystemDirectoryHandle) {
  const permissionHandle = handle as PermissionCapableHandle
  if (!permissionHandle.requestPermission) return false
  return (await permissionHandle.requestPermission({ mode: 'readwrite' })) === 'granted'
}

function downloadBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function uniqueProjectDirectoryName(projectName: string) {
  const timestamp = new Date()
    .toISOString()
    .replace(/[-:]/g, '')
    .replace(/\.\d{3}Z$/, 'Z')
  return `${safeName(projectName)}-${timestamp}`
}

function safeName(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 72) || 'inqtrix-project'
}

function nextLocalRunCounter(runIds: string[]) {
  const maxRunNumber = runIds.reduce((max, runId) => {
    const match = runId.match(/^RO-(\d+)$/)
    return match ? Math.max(max, Number(match[1])) : max
  }, 0)
  return maxRunNumber + 1
}

function stringOrDefault(value: unknown, fallback: string) {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function stringOrNow(value: unknown) {
  return typeof value === 'string' ? value : new Date().toISOString()
}

function booleanOrDefault(value: unknown, fallback: boolean) {
  return typeof value === 'boolean' ? value : fallback
}

function filterOrDefault(value: unknown) {
  if (
    value === 'cancelled'
    || value === 'completed'
    || value === 'queued'
    || value === 'running'
  ) return value
  return 'all'
}

function viewOrDefault(value: unknown) {
  if (
    value === 'chat'
    || value === 'database'
    || value === 'editor'
    || value === 'knowledge'
    || value === 'prompt-library'
    || value === 'settings'
  ) return value
  return 'research'
}

function preferencesOrDefault(value: unknown): ProjectState['preferences'] {
  const preferences = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    // Privacy default OFF; only an explicit true opts in (the server row is
    // authoritative and wins on login regardless of this device cache).
    agentMemoryEnabled: preferences.agentMemoryEnabled === true,
    agentModelTier: modelTierPreferenceOrDefault(preferences.agentModelTier),
    chatModelTier: modelTierPreferenceOrDefault(preferences.chatModelTier),
    contrastMode: contrastModeOrDefault(preferences.contrastMode),
    locale: localeOrDefault(preferences.locale),
    theme: themeOrDefault(preferences.theme),
    themePreset: themePresetOrDefault(preferences.themePreset),
    userBubbleTone: userBubbleToneOrDefault(preferences.userBubbleTone),
  }
}

/** An unknown tier resolves to `''` (no preference) rather than an invented
 * one, so a file written by a future version cannot silently pin a tier this
 * build does not know. */
function modelTierPreferenceOrDefault(
  value: unknown,
): ProjectState['preferences']['chatModelTier'] {
  return value === 'high' || value === 'mid' || value === 'fast' ? value : ''
}

function localeOrDefault(value: unknown): ProjectState['preferences']['locale'] {
  return value === 'en' || value === 'de' ? value : 'de'
}

function contrastModeOrDefault(value: unknown): ProjectState['preferences']['contrastMode'] {
  return value === 'high' ? value : 'standard'
}

function themeOrDefault(value: unknown): ProjectState['preferences']['theme'] {
  return value === 'light' || value === 'dark' || value === 'system'
    ? value
    : 'system'
}

function themePresetOrDefault(value: unknown): ProjectState['preferences']['themePreset'] {
  return value === 'petrol'
    || value === 'standard'
    || value === 'slate'
    || value === 'graphite'
    || value === 'sage'
    ? value
    : 'petrol'
}

function userBubbleToneOrDefault(value: unknown): ProjectState['preferences']['userBubbleTone'] {
  return value === 'gray'
    || value === 'mint'
    || value === 'orange'
    || value === 'sky'
    || value === 'violet'
    || value === 'ink'
    ? value
    : 'gray'
}

function pendingReportRunIdOrDefault(
  value: unknown,
  researchRuns: ProjectState['researchRuns'],
) {
  if (typeof value !== 'string') return null
  const run = researchRuns[value]
  return run?.status === 'completed' && Boolean(run.result?.markdown) ? value : null
}

function ruleOrderFromManifest(
  value: unknown,
  chatRules: ProjectState['chatRules'],
) {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => (
    typeof item === 'string' && Boolean(chatRules[item])
  ))
}

function pendingAttachmentRefsOrDefault(
  value: unknown,
  legacyReportRunId: unknown,
  researchRuns: ProjectState['researchRuns'],
  chatRules: ProjectState['chatRules'],
): ProjectState['ui']['pendingChatAttachmentRefs'] {
  const refs: ProjectState['ui']['pendingChatAttachmentRefs'] = Array.isArray(value)
    ? value.flatMap<ProjectState['ui']['pendingChatAttachmentRefs'][number]>((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return []
      const record = item as Record<string, unknown>
      if (
        record.kind === 'research-report'
        && typeof record.runId === 'string'
        && researchRuns[record.runId]?.status === 'completed'
        && researchRuns[record.runId]?.result?.markdown
      ) {
        return [{ kind: 'research-report' as const, runId: record.runId }]
      }
      if (
        record.kind === 'chat-rule'
        && typeof record.ruleId === 'string'
        && chatRules[record.ruleId]
      ) {
        return [{ kind: 'chat-rule' as const, ruleId: record.ruleId }]
      }
      return []
    })
    : []

  if (refs.length > 0) return dedupePendingAttachmentRefs(refs)
  const legacyRunId = pendingReportRunIdOrDefault(legacyReportRunId, researchRuns)
  return legacyRunId ? [{ kind: 'research-report', runId: legacyRunId }] : []
}

function dedupePendingAttachmentRefs(
  refs: ProjectState['ui']['pendingChatAttachmentRefs'],
) {
  const seen = new Set<string>()
  return refs.filter((ref) => {
    const key = ref.kind === 'research-report'
      ? `research:${ref.runId}`
      : ref.kind === 'chat-rule'
        ? `rule:${ref.ruleId}`
        : ref.kind === 'file-asset'
          ? `file:${ref.fileId}`
          : `filegroup:${ref.groupId}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
