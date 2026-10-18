import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react'
import { createRunEventCoalescer } from '@/features/agent/deltaCoalescer'
import { useReducedMotion } from 'motion/react'
import {
  uploadServerFile,
  reserveServerFileUpload,
  createChatCompletion,
  fetchKnowledgeDocumentText,
  fetchServerFileContent,
  fetchServerFileInfo,
  hasHttpStatus,
  listResearchRuns,
  loginLocal,
  saveAssetGroup,
  saveAssetSection,
  searchKnowledge,
  setExpectedUserIdentity,
  streamChatCompletion,
  type AuthConfig,
  type ChatCompletionMessage,
  type ClientOptions,
} from '@/api/inqtrixClient'
import {
  type AuthMode,
  isCookieSessionMode,
  isKnownAuthMode,
} from '@/features/auth/authMode'
import { AuthLockScreen } from './components/AuthLockScreen'
import ChatWorkspace from '@/features/chat/ChatWorkspace'
import type { KnowledgeIndexOption } from '@/features/chat/ChatWorkspace'
import {
  buildChatRetryMessages,
  findAssistantRetryTarget,
  type ChatRetryMode,
  type ChatRetryOptions,
} from '@/features/chat/retry'
import { useChatHistoryApi } from '@/features/chat/useChatHistoryApi'
import { useSessionTitle } from '@/features/sessionTitle/useSessionTitle'
import { updateTitleDraftIdentity } from '@/features/sessionTitle/draftStorage'
import { clearScrollMemory } from '@/features/scroll/scrollMemory'
import { hydrateSharedEditorTarget } from '@/features/sharing/sharedEditorTarget'
import { useEditorHistoryApi } from '@/features/editor/useEditorHistoryApi'
import {
  confirmedProjectionFallback,
  flushCollaborationProjectionBarrier,
  type CollaborationProjectionController,
} from '@/features/editor/collaborationProjection'
import { useAssetHistoryApi } from '@/features/fileLibrary/useAssetHistoryApi'
import { useVectorIndexHistoryApi } from '@/features/fileLibrary/useVectorIndexHistoryApi'
import { useAccountPreferences } from '@/features/account/useAccountPreferences'
import { useProjectServerImport } from '@/features/project/useProjectServerImport'
import { prepareProjectFileImport } from '@/features/project/detachedImport'
import EditorWorkspace, {
  type EditorDocumentDetailsSummary,
  type EditorRecoveryCaptureProvider,
} from '@/features/editor/EditorWorkspace'
import { exportProject, loadProject, saveProject } from '@/features/project/fileSystem'
import {
  attachmentContextReadiness,
  assetIdsFromChatRefs,
  chatAttachmentsFromRefs,
  projectChatHistorySections,
  chatRuleOptionsFromRules,
  chatAttachmentChipsFromRefs,
  chatContextRefKey,
  mentionableReportOptions,
  dedupeChatContextRefs,
  fileGroupMentionOptions,
  fileMentionOptions,
  projectAgentTargetEditorDocuments,
  projectAllKnowledgeItems,
  projectChatThreads,
  projectChatRules,
  projectFileAssets,
  projectKnowledgeItems,
  projectKnowledgeSessionSections,
  projectKnowledgeSessions,
  projectResearchJobs,
  projectVectorIndexes,
  selectedResearchRun,
  type ChatAttachmentChipModel,
} from '@/features/project/selectors'
import { chatFunctionChainTemplatesFromRefs } from '@/features/project/chatRules'
import type {
  ChatChainStepRecord,
  ChatContextReferenceRecord,
  ChatMessageAttachmentRecord,
  ChatMessageModelResolutionRecord,
  ChatMessageRequestContextRecord,
  ChatMessageRecord,
  ChatThreadRecord,
  EmbedModelDescriptor,
  EditorDocumentRecord,
  KnowledgeAnswerRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
  ProjectPreferences,
  ProjectState,
} from '@/features/project/types'
import { EMBED_MODELS, type FileAssetRecord } from '@/features/project/types'
import { isPillKind } from '@/features/composer/mentionDoc'
import { RunStillCancellingError, useResearchRunApi } from '@/features/researchRuns/useResearchRunApi'
import { useResearchRunImport } from '@/features/researchRuns/useResearchRunImport'
import { useServerDiscovery } from '@/features/researchRuns/useServerDiscovery'
import { deriveChatStepTimeoutMs, isMissingServerTimeouts } from '@/features/researchRuns/clientTimeouts'
import type {
  ChatModelOption,
  ChatModelTier,
  CreateResearchRunRequest,
  InqtrixHealth,
  InqtrixStack,
  ModelCatalogEntry,
  NodeModelResolution,
  ResearchRunEvent,
  ResearchRunResult,
  ResearchRunSummary,
} from '@/features/researchRuns/types'
import SettingsWorkspace from '@/features/settings/SettingsWorkspace'
import { KnowledgeWorkspace, type KnowledgeAskOptions, type KnowledgeMode } from '@/features/knowledge/KnowledgeWorkspace'
import {
  knowledgeAnswerFromRunResult,
  knowledgeAnswerWithRunProgress,
} from '@/features/knowledge/answer'
import { buildKnowledgeAskMessages } from '@/features/knowledge/conversationContext'
import { knowledgeComposerContextForSession } from '@/features/knowledge/composerSessionContext'
import { AgentWorkspace } from '@/features/agent/AgentWorkspace'
import { emptyAgentComposerDraft } from '@/features/agent/composerDraft'
import { effectiveAgentDepth } from '@/features/agent/agentStatusOverview'
import { createAgentDemo } from '@/features/agent/demo'
import { agentRuleSources } from '@/features/agent/contextCategories'
import { applyKnowledgeRunEvent } from '@/features/knowledge/runSteps'
import { useKnowledgeSessionsApi } from '@/features/knowledge/useKnowledgeSessionsApi'
import {
  buildDemoAskScript,
  createDemoKnowledgeDataSource,
  DEMO_KNOWLEDGE_DEFAULT_PROFILE,
  DEMO_KNOWLEDGE_DEFAULT_TOP_K,
  DEMO_KNOWLEDGE_EVIDENCE_K_MAX,
  DEMO_KNOWLEDGE_PROFILE_MANIFEST,
  DEMO_KNOWLEDGE_RERANKER_PROVIDER,
} from '@/features/knowledge/demo'
import {
  knowledgeProfileOptionsFromManifest,
  resolveKnowledgeDefaultProfileId,
} from '@/features/knowledge/profileOptions'
import type { KnowledgeCollectionOption, KnowledgeDataSource } from '@/features/knowledge/types'
import { reloadApplication, useAuthSession } from '@/features/auth/useAuthSession'
import {
  clearAllPlanDrafts,
  identityChanged,
} from '@/features/agent/plan/planDraftStorage'
import { flushActiveCollaborationDocuments } from '@/features/editor/useCollaborationDocument'
import { useReplacedGenerationRecovery } from '@/features/editor/collaborationRecovery'
import {
  knowledgeCollectionOptions,
  useKnowledgeCollectionsApi,
} from '@/features/knowledge/useKnowledgeCollectionsApi'
import { QuotaMeterProvider } from '@/features/quota/QuotaMeterContext'
import {
  carryCollectionsToNewSession,
  collectionIdsForSession,
  withSessionCollectionIds,
} from '@/features/agent/sessionComposer'
import { ShareDialog } from '@/features/sharing/ShareDialog'
import { DEMO_OWNER } from '@/features/sharing/demoShares'
import {
  DEMO_RUNNING_MAX_ROUNDS,
  DEMO_RUNNING_RUN_ID,
} from '@/features/project/seedProject'
import { isSharingEnabled } from '@/features/sharing/gate'
import {
  outgoingShareCounts,
  sharedResourceDestination,
} from '@/features/sharing/shareModel'
import { useSharingInbox } from '@/features/sharing/useSharingInbox'
import { refreshTokenFor, useUserInvalidationEvents } from '@/features/sharing/useUserInvalidationEvents'
import type { InboxShare } from '@/features/sharing/types'
import { seedSystemHealth } from '@/features/admin/demo'
import { useTemplateSync } from '@/features/promptLibrary/useTemplateSync'
import { FileLibraryWorkspace } from '@/features/fileLibrary/FileLibraryWorkspace'
import {
  assetRecordFromServer,
  serverGroupPayload,
  serverSectionPayload,
  serverUploadBinding,
} from '@/features/fileLibrary/assetSync'
import { PromptLibraryWorkspace } from '@/features/promptLibrary/PromptLibraryWorkspace'
import { modelOverridesFromSelection } from '@/features/researchRuns/modelSelection'
import { useSkillsApi } from '@/features/skills/useSkillsApi'
import {
  createFileAssetPlaceholders,
  createFileUploadRegistry,
  runFileIngestPipeline,
  serverUploadFailureMessage,
  type ServerFileUpload,
  type FileUploadRegistry,
  type UploadBinding,
  uploadBindingForRecord,
} from '@/features/files/ingest'
import { createDefaultFileParser } from '@/features/files/parsing'
import { chatCompletionBase, INCOGNITO_THREAD_ID } from './chatRequest'
import { chatStateForIncognito, ingestIncognitoFiles } from './incognitoAttachments'
import { moveItem } from '@/features/composer/reorder'
import { temporaryFileSectionId } from '@/features/files/sections'
import { createProjectEntityId as createClientId } from '@/features/project/entityId'
import {
  evaluateBudget,
  shouldShowAttachmentBudgetNotice,
} from '@/features/files/budget'
import { estimateTokensFromText } from '@/features/files/contextTokens'
import { useLocale } from '@/i18n/LocaleProvider'
import { useTheme } from '@/theme/ThemeProvider'
import { contentWithAttachmentContext } from '@/features/project/attachmentContext'
import { AppRail } from './components/AppRail'
import { ProfileAvatar } from './components/ProfileAvatar'
import { ResearchWorkspace } from './components/ResearchWorkspace'
import { Topbar } from './components/Topbar'
import { useMediaQuery } from './hooks/useMediaQuery'
import type { ResearchSubmissionOutcome } from './researchSubmission'
import {
  initializeResearchDeskState,
  researchDeskReducer,
  stackOptions,
  visibleResearchJobs,
} from './state'
import { carriedErrorText, messageFromError, readerError, readerTexts } from '@/api/requestErrorText'

type ActiveChatRequest = {
  assistantMessageId: string
  phase: 'streaming' | 'submitted'
  threadId: string
}

type ScheduledChatContent = {
  assistantMessageId: string
  contentMarkdown: string
  threadId: string
}

type ChatSendOptions = {
  modelTier?: ChatModelTier
  model?: string | null
  effort?: string | null
  knowledgeCollectionIds?: string[]
}

type ChatModelOptionsState = {
  options: ChatModelOption[]
  status: 'available' | 'missing' | 'unresolved'
}

type SharedOpenTarget = Pick<InboxShare, 'resource_id' | 'resource_type'>

const MAX_PARALLEL_CHAT_REQUESTS = 3

function removeChatRequest(
  requests: Record<string, ActiveChatRequest>,
  threadId: string,
  assistantMessageId?: string,
) {
  const request = requests[threadId]
  if (!request) return requests
  if (assistantMessageId && request.assistantMessageId !== assistantMessageId) return requests

  const next = { ...requests }
  delete next[threadId]
  return next
}

export function ResearchDesk({
  authConfig = null,
}: { authConfig?: AuthConfig | null } = {}) {
  const { locale, setLocale, t } = useLocale()
  const {
    agentMemoryEnabled,
    agentModelTier,
    chatModelTier,
    contrastMode,
    preset: themePreset,
    setAgentMemoryEnabled,
    setAgentModelTier,
    setChatModelTier,
    setContrastMode,
    setPreset: setThemePreset,
    setTheme,
    setUserBubbleTone,
    theme,
    userBubbleTone,
  } = useTheme()
  const reduceMotion = useReducedMotion()
  const isDesktop = useMediaQuery('(min-width: 1024px)')
  const [state, dispatch] = useReducer(
    researchDeskReducer,
    undefined,
    initializeResearchDeskState,
  )
  const uploadRegistryRef = useRef<FileUploadRegistry | null>(null)
  if (uploadRegistryRef.current === null) {
    uploadRegistryRef.current = createFileUploadRegistry()
  }
  const uploadRegistry = uploadRegistryRef.current
  useEffect(() => {
    uploadRegistry.clear()
  }, [state.projectEpoch, uploadRegistry])
  useEffect(() => {
    for (const asset of Object.values(state.fileAssets)) {
      if (
        asset.uploadStatus === 'ready'
        || asset.uploadStatus === 'cancelled'
        || (asset.lifecycleStatus ?? 'active') !== 'active'
      ) {
        uploadRegistry.delete(asset.id)
      }
    }
  }, [state.fileAssets, uploadRegistry])
  const [projectAction, setProjectAction] = useState<'export' | 'load' | 'save' | null>(null)
  const [projectActionError, setProjectActionError] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [authLockError, setAuthLockError] = useState<string | null>(null)
  const [isAuthSubmitting, setIsAuthSubmitting] = useState(false)
  // Credential-mode (local) login fields; the password never persists
  // beyond a successful submit.
  const [authIdentifier, setAuthIdentifier] = useState('')
  const [authPassword, setAuthPassword] = useState('')
  const [localResourceRefreshRevision, setLocalResourceRefreshRevision] = useState(0)
  const [sharedOpenTarget, setSharedOpenTarget] = useState<SharedOpenTarget | null>(null)
  const [sharedResourceError, setSharedResourceError] = useState<string | null>(null)
  // Mirrors the cookie session's authenticated state for the run-list hook
  // (declared before it; the session itself resolves later from health).
  const [cookieAuthed, setCookieAuthed] = useState(false)
  const [cancelSubmittingRunIds, setCancelSubmittingRunIds] = useState<ReadonlySet<string>>(() => new Set())
  const [cancelErrorByRunId, setCancelErrorByRunId] = useState<Record<string, string>>({})
  const [activeChatRequestsByThreadId, setActiveChatRequestsByThreadId] = useState<Record<string, ActiveChatRequest>>({})
  const [chatErrorByThreadId, setChatErrorByThreadId] = useState<Record<string, string>>({})
  const [chatNoticeByThreadId, setChatNoticeByThreadId] = useState<Record<string, string>>({})
  const [isIncognitoChat, setIsIncognitoChat] = useState(false)
  const [incognitoThread, setIncognitoThread] = useState<ChatThreadRecord>(() => createIncognitoThread(
    t.chat.incognitoTitle,
    t.chat.incognitoPreview,
  ))
  // Incognito chat attachments are held here, NOT in the synced `state.fileAssets`,
  // so they never upload, never sync to the DB, and never surface in the library
  // or `@files:` mention list. Mirrors the local-only `incognitoThread`. The refs
  // are kept locally too (not in the reducer's `pendingChatAttachmentRefs`) because
  // the reducer's `contextRefExists` guard would reject a ref whose asset is not in
  // `state.fileAssets`. Both are discarded on every incognito-session reset.
  const [incognitoAssets, setIncognitoAssets] = useState<Record<string, FileAssetRecord>>({})
  const [incognitoAttachmentRefs, setIncognitoAttachmentRefs] = useState<ChatContextReferenceRecord[]>([])
  const [chatStreamingEnabled, setChatStreamingEnabled] = useState(true)
  const chatControllerByThreadIdRef = useRef<Map<string, AbortController>>(new Map())
  const chatSubmittingThreadIdsRef = useRef<Set<string>>(new Set())
  const chatFlushFrameByThreadIdRef = useRef<Map<string, number>>(new Map())
  const chatStreamContentByThreadIdRef = useRef<Map<string, string>>(new Map())
  const activeCollaborationControllerRef = useRef<{
    controller: CollaborationProjectionController
    documentId: string
  } | null>(null)
  const handleCollaborationControllerChange = useCallback((
    registration: {
      controller: CollaborationProjectionController
      documentId: string
    } | null,
  ) => {
    activeCollaborationControllerRef.current = registration
  }, [])
  const scheduledChatContentByThreadIdRef = useRef<Map<string, ScheduledChatContent>>(new Map())
  const allJobs = useMemo(
    () => projectResearchJobs(state),
    [state.researchRunOrder, state.researchRuns],
  )
  const visibleJobs = useMemo(
    () => visibleResearchJobs(allJobs, state.ui.activeFilter),
    [allJobs, state.ui.activeFilter],
  )
  useEffect(() => {
    if (sharedOpenTarget?.resource_type !== 'run') return
    if (allJobs.some((job) => job.id === sharedOpenTarget.resource_id)) {
      dispatch({ jobId: sharedOpenTarget.resource_id, type: 'selectJob' })
      setSharedOpenTarget(null)
      return
    }
    const agentRun = state.agentRuns[sharedOpenTarget.resource_id]
    if (!agentRun) return
    dispatch({
      sessionId: agentRun.sessionId ?? agentRun.runId,
      type: 'selectAgentSession',
    })
    dispatch({ type: 'setActiveView', view: 'agent' })
    setSharedOpenTarget(null)
  }, [allJobs, sharedOpenTarget, state.agentRuns])
  // Stable identity so the memoized ReportPanel isn't re-rendered on every
  // unrelated desk dispatch (dispatch is stable, so this never changes).
  const handleReportVisibleChange = useCallback(
    (isVisible: boolean) => dispatch({ isVisible, type: 'setReportVisible' }),
    [],
  )
  const handleSetReportAutocomplete = useCallback(
    (runId: string, includeInAutocomplete: boolean) =>
      dispatch({ includeInAutocomplete, runId, type: 'setResearchRunAutocomplete' }),
    [],
  )
  const handleUseReportInChat = useCallback(
    (runId: string) => dispatch({ runId, type: 'attachReportToNewChat' }),
    [],
  )
  const isDemoMode = state.connection.kind === 'demo'

  /** The view the WORKSPACE renders, one step behind the view the CHROME
   * renders.
   *
   * A desk switch tears the old workspace down and builds the new one up.
   * That build blocks the main thread for up to a few hundred milliseconds,
   * more on ordinary office hardware. In the same urgent update as
   * `activeView` itself, nothing could paint until it finished: the click
   * would look ignored, the old desk would stay frozen on screen, and only
   * then would the entry animation start. That stall — not the animation —
   * is what reads as "it jumps".
   *
   * Rail and topbar keep reading `state.ui.activeView`, so the button
   * highlights and the title change in the very next frame. Only the
   * workspace reads this deferred copy: React builds it in a non-urgent lane
   * and yields to the browser while it does, so the previous desk stays
   * painted and interactive until the new one is ready to appear whole. */
  const deferredActiveView = useDeferredValue(state.ui.activeView)

  // Demo-only "live" simulator: while the seed run is running in demo mode, feed
  // synthetic snapshot events through the real appendApiRunEvent pipeline so the
  // running card visibly progresses (phases advance, metrics count up and flash,
  // new live-status rows rise). No-op outside demo and for real API runs.
  const researchRunsRef = useRef(state.researchRuns)
  useEffect(() => {
    researchRunsRef.current = state.researchRuns
  }, [state.researchRuns])
  // The demo simulator reads the active view through a ref so it can skip its
  // dispatch while the user is on another view WITHOUT tearing the interval
  // down (a teardown would reset the local phase/round counters and jump the
  // card backwards on return). See the interval body below.
  const activeViewRef = useRef(state.ui.activeView)
  useEffect(() => {
    activeViewRef.current = state.ui.activeView
  }, [state.ui.activeView])
  useEffect(() => {
    if (!isDemoMode) return undefined
    // The seed run id is shared with seedProject so the two never drift.
    const seedId = DEMO_RUNNING_RUN_ID
    const seed = researchRunsRef.current[seedId]
    if (!seed || seed.status !== 'running') return undefined

    const maxRounds = DEMO_RUNNING_MAX_ROUNDS
    const nodeOrder = ['classify', 'plan', 'search', 'evaluate', 'answer'] as const
    // Resume from the seed's active phase (evaluation, round 1) so the streamed
    // counter continues the card's static metrics rather than contradicting them.
    let nodeIndex = nodeOrder.indexOf('evaluate')
    let round = 1
    let queries = seed.metrics.queries
    let sources = seed.metrics.sources
    let claims = seed.metrics.claims
    let sequence = 1000

    const intervalId = window.setInterval(() => {
      if (researchRunsRef.current[seedId]?.status !== 'running') return
      // Off the research view the running card is unmounted, so a dispatch here
      // would only force a wasted re-render of whatever heavy workspace is
      // mounted (the "light reload" felt on the Database view in demo mode).
      // Skip without advancing the counters: the card resumes where it paused.
      if (activeViewRef.current !== 'research') return
      nodeIndex += 1
      if (nodeIndex >= nodeOrder.length) {
        nodeIndex = 0
        round = round >= maxRounds ? 1 : round + 1
      }
      const node = nodeOrder[nodeIndex]
      queries += 3
      sources += 5
      claims += node === 'search' || node === 'evaluate' ? 4 : 1
      const message = node === 'plan'
        ? `Plane Suchanfragen (Runde ${round}/${maxRounds})...`
        : node === 'search'
          ? `Durchsuche ${4 + round} Suchanfragen (Runde ${round}/${maxRounds})...`
          : node === 'evaluate'
            ? `Bewerte Informationsqualitaet (nach Runde ${round}/${maxRounds})...`
            : node === 'answer'
              ? 'Formuliere Antwort aus verifizierter Evidenz...'
              : 'Analysiere Frage und extrahiere Pflichtaspekte...'

      sequence += 1
      dispatch({
        event: {
          created_at: Math.floor(Date.now() / 1000),
          data: {
            message,
            snapshot: {
              active_round: round,
              consolidated_claim_count: claims,
              current_node: node,
              max_rounds: maxRounds,
              total_queries: queries,
              total_sources: sources,
            },
          },
          run_id: seedId,
          sequence,
          type: 'inqtrix.progress.message',
        },
        type: 'appendApiRunEvent',
      })
    }, 3500)

    return () => window.clearInterval(intervalId)
  }, [isDemoMode, dispatch])

  const selectedRun = selectedResearchRun(state)
  const chatThreads = useMemo(
    () => projectChatThreads(state),
    [state.chatThreadOrder, state.chatThreads],
  )
  const chatHistorySections = useMemo(
    () => projectChatHistorySections(state),
    [
      state.chatThreadGroupMemberships,
      state.chatThreadGroupOrder,
      state.chatThreadGroups,
      state.chatThreadOrder,
      state.chatThreads,
      // The selector sorts by the desk's sort mode — omitting it made a
      // mode switch a silent no-op on the cached sections (review find).
      state.ui.explorerSort.chat,
    ],
  )
  const chatRules = useMemo(
    () => projectChatRules(state),
    [state.chatRuleOrder, state.chatRules],
  )
  const ruleOptions = useMemo(
    () => chatRuleOptionsFromRules(chatRules, 'chat'),
    [chatRules],
  )
  const reportOptions = useMemo(
    () => mentionableReportOptions(state),
    [state.researchRunOrder, state.researchRuns],
  )
  const [chatPillRefs, setChatPillRefs] = useState<ChatContextReferenceRecord[]>([])
  // Session-scoped composer drafts held in the shell (which never unmounts on a view
  // switch) so unsent text survives navigation. Intentionally NOT in the reducer:
  // these must not outlive a full reload (per the "page-switch only" decision).
  const [chatDraft, setChatDraft] = useState('')
  const [researchQuestion, setResearchQuestion] = useState('')
  // In incognito the pinned attachments live in local state (incognitoAttachmentRefs),
  // never in the reducer — so they stay out of the synced store. Inline @mention
  // pills (chatPillRefs) only ever reference existing library data, so they are
  // unchanged in both modes.
  const pendingChatRefs = isIncognitoChat ? incognitoAttachmentRefs : state.ui.pendingChatAttachmentRefs
  const combinedChatRefs = useMemo(
    () => dedupeChatContextRefs([...chatPillRefs, ...pendingChatRefs]),
    [chatPillRefs, pendingChatRefs],
  )
  // Resolver view for the chat path: in incognito it also sees the local-only
  // attachments, so chips/budget/the outgoing message can resolve their bodies
  // without those assets ever entering the synced store (see incognitoAttachments).
  const chatResolveState = useMemo(
    () => (isIncognitoChat ? chatStateForIncognito(state, incognitoAssets) : state),
    [
      incognitoAssets,
      isIncognitoChat,
      state.chatRuleOrder,
      state.chatRules,
      state.fileAssetOrder,
      state.fileAssets,
      state.fileGroupOrder,
      state.fileGroups,
      state.researchRunOrder,
      state.researchRuns,
    ],
  )
  const fileOptions = useMemo(
    () => fileMentionOptions(state),
    [state.fileAssetOrder, state.fileAssets],
  )
  const fileGroupOptions = useMemo(
    () => fileGroupMentionOptions(state),
    [state.fileAssetOrder, state.fileAssets, state.fileGroupOrder, state.fileGroups],
  )

  const handleAttachChatFiles = async (files: File[]) => {
    if (isIncognitoChat) {
      // Local-only: client-parse without upload, hold the records + refs in
      // incognito state (NOT the reducer, whose contextRefExists guard would
      // drop a ref whose asset is absent from state.fileAssets).
      const existingLabels = Object.values(incognitoAssets).map((asset) => asset.label)
      const assets = await ingestIncognitoFiles(files, existingLabels)
      if (assets.length === 0) return
      setIncognitoAssets((current) => {
        const next = { ...current }
        for (const asset of assets) next[asset.id] = asset
        return next
      })
      setIncognitoAttachmentRefs((current) => [
        ...current,
        ...assets.map((asset) => ({ fileId: asset.id, kind: 'file-asset' as const })),
      ])
      return
    }
    if (files.length === 0) return
    const existingLabels = projectFileAssets(state).map((asset) => asset.label)
    const sectionId = temporaryFileSectionId(Object.values(state.fileLibrarySections))
    const { queue, records } = createFileAssetPlaceholders(
      files,
      { kind: 'chat', sectionId },
      existingLabels,
      Boolean(serverFileUpload),
    )
    dispatch({ assets: records, type: 'ingestFileAssets' })
    for (const asset of records) {
      dispatch({ ref: { fileId: asset.id, kind: 'file-asset' }, type: 'attachChatContextToDraft' })
    }
    const bindings = new Map(records.map((record) => [record.id, uploadBindingForRecord(record)]))
    for (const item of queue) {
      const binding = bindings.get(item.assetId)
      if (binding) uploadRegistry.register(item.assetId, { binding, file: item.file })
    }
    const parser = createDefaultFileParser()
    void (async () => {
      const bindingReady = serverFileUpload
        ? await ensureUploadTarget(sectionId, null).catch(() => false)
        : false
      await runFileIngestPipeline(queue, {
        needsClientParse: () => true,
        onParsed: (assetId, parsed, clearParsePending) => dispatch({
          assetId,
          clearParsePending,
          extractedText: parsed.extractedText,
          pageCount: parsed.pageCount,
          parseStatus: parsed.parseStatus,
          parseWarning: parsed.parseWarning,
          textTruncated: parsed.textTruncated,
          type: 'applyFileAssetClientParse',
        }),
        onUploadAccepted: (assetId, result) => {
          dispatch({ assetId, ...result, type: 'adoptFileAssetUploadLifecycle' })
          if (result.status !== 'ready' || !result.serverFileId) return
          uploadRegistry.delete(assetId)
        },
        onUploadFailed: (assetId, message) => {
          dispatch({ assetId, message, type: 'failFileAssetUpload' })
        },
        parse: (file) => parser.parse(file),
        serverParseWillRun: (_assetId, uploaded) => uploaded && serverParserAvailable,
        upload: serverFileUpload
          ? (item) => {
              const binding = bindings.get(item.assetId)
              if (!binding) return Promise.reject(readerError('uploadBindingMissing'))
              if (!bindingReady) {
                return Promise.reject(
                  readerError('uploadTargetUnavailable'),
                )
              }
              return serverFileUpload(item.file, binding)
            }
          : undefined,
      })
    })()
  }

  const displayedChatThread = isIncognitoChat
    ? incognitoThread
    : state.ui.selectedChatThreadId
    ? state.chatThreads[state.ui.selectedChatThreadId]
    : chatThreads[0] ?? null
  const activeChatThreadId = isIncognitoChat ? INCOGNITO_THREAD_ID : displayedChatThread?.id ?? null
  const activeChatRequest = activeChatThreadId
    ? activeChatRequestsByThreadId[activeChatThreadId] ?? null
    : null
  const runningChatThreadIds = useMemo(
    () => new Set(Object.keys(activeChatRequestsByThreadId)),
    [activeChatRequestsByThreadId],
  )
  // Memoized so the reference is stable across unrelated re-renders (chat
  // streaming re-renders ~every frame): the account-preferences autosave
  // effect then re-arms only on a genuine preference change, matching the
  // stable-reference contract the sibling sync hooks rely on.
  const currentPreferences = useMemo<ProjectPreferences>(
    () => ({
      agentMemoryEnabled,
      agentModelTier,
      chatModelTier,
      contrastMode,
      locale,
      theme,
      themePreset,
      userBubbleTone,
    }),
    [
      agentMemoryEnabled,
      agentModelTier,
      chatModelTier,
      contrastMode,
      locale,
      theme,
      themePreset,
      userBubbleTone,
    ],
  )
  const handleApiSummary = useCallback((summary: ResearchRunSummary, options?: { select?: boolean }) => {
    dispatch({ select: options?.select, summary, type: 'upsertApiRunSummary' })
  }, [])
  const handleApiSummaryReplacement = useCallback((summaries: ResearchRunSummary[]) => {
    dispatch({ summaries, type: 'replaceApiRunSummaries' })
  }, [])
  const handleImportedRun = useCallback((sourceRunId: string, summary: ResearchRunSummary) => {
    dispatch({ sourceRunId, summary, type: 'adoptImportedApiRun' })
  }, [])
  // Live events go through ONE coalescer: contiguous answer deltas of a
  // publication (a live answer arrives token by token) become one dispatch
  // per animation frame; every other event flushes first so nothing
  // reorders.
  const liveEventCoalescer = useRef(
    createRunEventCoalescer((event) => dispatch({
      event,
      // Arrival time for the run clock's server offset (a frame of
      // coalescing is far below its one-second resolution).
      receivedAt: Date.now() / 1000,
      type: 'appendApiRunEvent',
    })),
  ).current
  const handleApiEvent = useCallback((event: ResearchRunEvent) => {
    liveEventCoalescer.push(event)
  }, [liveEventCoalescer])
  // Catch-up batches (SSE replay after a reload/reconnect, first polling
  // page): one dispatch, one commit, no arrivedLive — history renders in
  // place instead of replaying itself row by row. The batch continues
  // after the channel's replay cursor, which already counts any delta the
  // coalescer still holds, so that delta must land first.
  const handleApiHistory = useCallback((events: ResearchRunEvent[]) => {
    liveEventCoalescer.flush()
    dispatch({ events, type: 'appendApiRunEvents' })
  }, [liveEventCoalescer])
  const handleApiResult = useCallback((result: ResearchRunResult, finishedAt?: string) => {
    dispatch({ finishedAt, result, type: 'attachApiRunResult' })
  }, [])
  const handleApiRunError = useCallback((runId: string, message: string) => {
    dispatch({ message, runId, type: 'markApiRunError' })
  }, [])
  const handleApiResultError = useCallback((runId: string, message: string) => {
    dispatch({ message, runId, type: 'markApiRunResultError' })
  }, [])
  const handleApiRunUnavailable = useCallback((runId: string) => {
    dispatch({ runId, type: 'markApiRunUnavailable' })
  }, [])
  // Server discovery (health/capabilities/stacks) is workspace-INDEPENDENT and
  // resolved first, because `health` gates cookie mode and `capabilities` gates
  // the persistence tier -- both feeding the auth + namespace resolution below.
  const {
    capabilities,
    defaultStackName,
    health,
    lastError: discoveryError,
    ready: discoveryReady,
    stackDiscoveryStatus,
    stackNames: apiStackNames,
    stacks: apiStacks,
  } = useServerDiscovery({ enabled: !isDemoMode })
  const effectiveHealth = useMemo(
    () => (isDemoMode ? seedSystemHealth() : health),
    [health, isDemoMode],
  )
  // Client AI-request aborts are derived from the server's published HTTP waits
  // (the /v1/capabilities timeouts block) so a raised server-side timeout is not
  // silently capped by the browser. A ref keeps run-start handlers on the
  // current value without re-threading it through their dependency lists.
  const chatStepTimeoutMsRef = useRef(deriveChatStepTimeoutMs(capabilities))
  chatStepTimeoutMsRef.current = deriveChatStepTimeoutMs(capabilities)
  // No Silent Fallbacks: warn once when a real backend exposes no timeouts block
  // (older server) so the editor/chat fixed-fallback aborts are never silent.
  const clientTimeoutsWarnedRef = useRef(false)
  useEffect(() => {
    if (isMissingServerTimeouts(capabilities) && !clientTimeoutsWarnedRef.current) {
      clientTimeoutsWarnedRef.current = true
      console.warn(
        '/v1/capabilities exposed no timeouts block; editor and chat client '
        + 'aborts are using fixed fallbacks. Update the backend so client '
        + 'timeouts track the server waits.',
      )
    }
  }, [capabilities])
  // Auth + the per-user project namespace are resolved here, after discovery and
  // before the run-API hook, so the run hook and every project-data call scope to
  // the SAME namespace from one resolved session (no in-hook re-probe).
  const authMode: AuthMode = isDemoMode
    ? 'none'
    : health?.auth_mode
      ?? (health?.auth_required ? 'apikey' : 'none')
  const isCookieMode = isCookieSessionMode(authMode)
  // A server ahead of or behind this bundle can report a mode this build
  // does not render. It then falls through to the token form, which cannot
  // unlock it -- so say so instead of leaving the operator with a dead form.
  useEffect(() => {
    if (!isDemoMode && health?.auth_mode && !isKnownAuthMode(health.auth_mode)) {
      console.warn(
        `[inqtrix] the server reports auth_mode "${health.auth_mode}", which `
        + 'this build does not support. Sign-in is unavailable until the '
        + 'frontend and backend are on the same version.',
      )
    }
  }, [health?.auth_mode, isDemoMode])
  const {
    session: authSession,
    login: ssoLogin,
    logout: ssoLogout,
    logoutError,
    refresh: refreshAuthSession,
  } = useAuthSession(
    isCookieMode,
    state.workspaceId,
    flushActiveCollaborationDocuments,
  )
  const lastIdentityRef = useRef<string | null | undefined>(undefined)
  const lastTitleIdentityRef = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    const identity
      = isCookieMode && authSession.status === 'authenticated'
        ? authSession.user?.id ?? null
        : null
    setExpectedUserIdentity(identity)
    // Unsent plan drafts live in `localStorage`, which survives the hard
    // reload the auth layer performs on an identity transition. Without
    // this sweep one account's unsent rejection note could reappear in
    // the next account's gate on a shared profile.
    if (identityChanged(lastIdentityRef.current, identity)) {
      clearAllPlanDrafts()
    }
    lastIdentityRef.current = identity
    lastTitleIdentityRef.current = updateTitleDraftIdentity(lastTitleIdentityRef.current, identity,
      !isCookieMode || authSession.status !== 'unknown')
    return () => setExpectedUserIdentity(null)
  }, [authSession.status, authSession.user?.id, isCookieMode])
  const handleLogout = useCallback(async () => {
    const succeeded = await ssoLogout()
    if (succeeded) return
    setSettingsRequestedSection('security')
    dispatch({ type: 'setActiveView', view: 'settings' })
  }, [ssoLogout])
  // Project-persistence tier: durable server persistence is offered only
  // by a Postgres backend (not demo). For an authenticated cookie-session user
  // (local/oidc) it is AUTOMATIC -- serverSyncEnabled is derived from the
  // session below, so the project auto-hydrates on boot and auto-saves with no
  // opt-in. The apikey / local-first tiers keep the explicit import opt-in.
  const projectPersistenceAvailable =
    !isDemoMode && capabilities?.features.project_persistence === true
  // The single auth gate: admitted to list/run when anonymous (`none`), apikey
  // with a key, or a live cookie session. Drives the run list, the auth-lock
  // screen (below), and the namespace flip -- all from the one resolved session.
  const authUnlocked =
    authMode === 'none'
    || (authMode === 'apikey' && apiKey.trim() !== '')
    || (isCookieMode && authSession.status === 'authenticated')
  // The workspace namespace every project-data + run server call scopes to. For
  // an authenticated cookie-session user it is the per-user namespace resolved
  // from the session (adopted once on first boot, identical on every device, so
  // the project follows the user); otherwise (anonymous / apikey / local-first /
  // demo) it stays the browser-local id. Gated on the SAME signal as authUnlocked
  // (authSession.status), so the namespace is in hand the instant the desk is
  // usable: a run created the moment the composer unlocks already scopes to the
  // namespace (no browser-id window). serverSyncEnabled (which activates the
  // project-data sync hooks) lags one render behind via cookieAuthed, by which
  // point this is already the namespace -- so the sync lifecycle keys on the
  // per-user namespace from its first hydrate and never re-keys from the browser id.
  const effectiveWorkspaceId =
    projectPersistenceAvailable
    && authSession.status === 'authenticated'
    && authSession.projectNamespace
      ? authSession.projectNamespace
      : state.workspaceId
  const userInvalidation = useUserInvalidationEvents({
    enabled: !isDemoMode
      && isCookieMode
      && authSession.status === 'authenticated',
    userId: authSession.user?.id ?? null,
  })
  // Each list refreshes for the scopes that can change it: a run starting or
  // finishing touches the run list only. A local mutation, a reconnect, a
  // reset or an unknown scope refreshes every list.
  const scopedRefreshToken = (scopes: readonly string[]) =>
    refreshTokenFor(userInvalidation.refresh, scopes) + localResourceRefreshRevision
  const runsRefreshToken = scopedRefreshToken(['runs', 'sharing'])
  const skillsRefreshToken = scopedRefreshToken(['skills'])
  const knowledgeRefreshToken = scopedRefreshToken(['knowledge_collections', 'indexing', 'sharing'])
  const editorRefreshToken = scopedRefreshToken(['editor_documents', 'sharing', 'collaboration_guest_policy'])
  const sharingListRefreshToken = scopedRefreshToken(['sharing'])
  const templatesRefreshToken = scopedRefreshToken(['prompt_templates'])
  const deletionOperationsRefreshToken = scopedRefreshToken(['deletion_operations'])
  const shareDialogRefreshToken = scopedRefreshToken(['sharing', 'editor_documents', 'collaboration_guest_policy'])
  const requestResourceRefresh = useCallback(() => {
    setLocalResourceRefreshRevision((current) => current + 1)
  }, [])
  const contentClientOptions = useMemo<ClientOptions>(
    () => ({
      apiKey: apiKey.trim() || undefined,
      workspaceId: effectiveWorkspaceId,
    }),
    [apiKey, effectiveWorkspaceId],
  )
  // Skill library: server-first behind features.skills; the
  // demo serves its in-memory list so the tab stays demo-visible.
  const skillsEnabled = isDemoMode
    || (capabilities?.features.skills === true && authUnlocked)
  // Memoized: an inline literal would give useSkillsApi.refresh a new
  // identity per render, and its load effect would refetch /v1/skills
  // in a loop (every completed fetch renders the next one).
  const skillsClientOptions = useMemo(
    () =>
      isDemoMode
        ? null
        : authUnlocked
          ? contentClientOptions
          : null,
    [authUnlocked, contentClientOptions, isDemoMode],
  )
  const skillsApi = useSkillsApi({
    clientOptions: skillsClientOptions,
    demo: isDemoMode,
    enabled: skillsEnabled,
    refreshToken: skillsRefreshToken,
  })
  const {
    cancelRun,
    deleteRun,
    lastError: runError,
    pollingRunIds,
    replayRunHistory,
    requestRunResults,
    retryResult: retryRunResult,
    runsHydrated,
    submitRun,
  } = useResearchRunApi({
    apiKey: apiKey.trim() || undefined,
    // Gate on discoveryReady so listing waits for the real auth mode: until
    // health settles, authMode reads as `none` (authUnlocked true) and would
    // list prematurely under the browser id before the namespace resolves.
    canList: discoveryReady && authUnlocked,
    enabled: !isDemoMode,
    onEvent: handleApiEvent,
    onHistory: handleApiHistory,
    onResult: handleApiResult,
    onResultError: handleApiResultError,
    onRunError: handleApiRunError,
    onRunUnavailable: handleApiRunUnavailable,
    onReplace: handleApiSummaryReplacement,
    onSummary: handleApiSummary,
    refreshToken: runsRefreshToken,
    workspaceId: effectiveWorkspaceId,
  })
  // Finished agent runs replay their history only for the open session; the
  // other sessions replay when the user opens them.
  const selectedAgentRunIds = state.selectedAgentSessionId
    ? state.agentSessions[state.selectedAgentSessionId]?.runIds
    : undefined
  useEffect(() => {
    if (selectedAgentRunIds?.length) replayRunHistory(selectedAgentRunIds)
  }, [replayRunHistory, selectedAgentRunIds])
  // A finished knowledge ask's result loads once its question is in view
  // (its session opened or prefetched); questions never loaded cost nothing.
  const knowledgeItemRunIds = useMemo(
    () => Object.values(state.knowledgeItems).flatMap((item) => (item.runId ? [item.runId] : [])),
    [state.knowledgeItems],
  )
  useEffect(() => {
    if (knowledgeItemRunIds.length > 0) requestRunResults(knowledgeItemRunIds)
  }, [knowledgeItemRunIds, requestRunResults])
  // One badge for any API error: discovery probes or run operations.
  const apiError = logoutError ?? discoveryError ?? runError
  const singleStackLabel = useMemo(() => resolveSingleStackLabel(effectiveHealth), [effectiveHealth])
  const knowledgeAvailable = !isDemoMode && capabilities?.features.knowledge === true
  // The server's reason when this deployment cannot build indexes
  // (`operator_disabled`, `store_unreachable`): the database view turns
  // read-only instead of simulating. `null` in demo mode and whenever
  // knowledge works.
  const knowledgeUnavailableReason = !isDemoMode && capabilities && !knowledgeAvailable
    ? capabilities.feature_status?.knowledge?.reason_code ?? null
    : null
  const embedCatalog = useMemo<readonly EmbedModelDescriptor[]>(() => {
    const serverCatalog = capabilities?.knowledge?.embedding_catalog
    if (!knowledgeAvailable || !serverCatalog || serverCatalog.length === 0) {
      return EMBED_MODELS
    }
    return serverCatalog.map((entry) => ({
      dims: entry.card?.dims ?? 0,
      id: entry.model_id,
      label: entry.card?.display_name ?? entry.model_id,
      provider: entry.card?.vendor ?? '',
    }))
  }, [capabilities, knowledgeAvailable])
  const serverFilesAvailable = !isDemoMode && capabilities?.features.files === true
  const serverParserAvailable =
    serverFilesAvailable && capabilities?.features.document_parser === true
  const knowledgeSyncOptions = useMemo(
    () => (knowledgeAvailable
      ? {
          apiKey: apiKey.trim() || undefined,
          useFileIngestion: serverParserAvailable,
          workspaceId: effectiveWorkspaceId,
        }
      : null),
    [apiKey, knowledgeAvailable, serverParserAvailable, effectiveWorkspaceId],
  )
  const projectKnowledgeIndexes = useMemo(
    () => projectVectorIndexes(state),
    [state.vectorIndexOrder, state.vectorIndexes],
  )
  const knowledgeCollectionsApi = useKnowledgeCollectionsApi({
    clientOptions: knowledgeAvailable && authUnlocked
      ? contentClientOptions
      : null,
    enabled: discoveryReady && knowledgeAvailable && authUnlocked,
    refreshToken: knowledgeRefreshToken,
  })
  const liveKnowledgeCollectionOptions = useMemo(
    () => knowledgeCollectionOptions({
      localIndexes: projectKnowledgeIndexes,
      serverCollections: knowledgeCollectionsApi.collections,
      serverLoaded: knowledgeCollectionsApi.loaded,
    }),
    [
      knowledgeCollectionsApi.collections,
      knowledgeCollectionsApi.loaded,
      projectKnowledgeIndexes,
    ],
  )
  // Late-bound handle: the asset-history hook mounts further down, but the
  // upload closure above it must report bound uploads into its synced map.
  const noteServerAssetRecordRef = useRef<(assetId: string) => void>(() => undefined)
  const serverFileUpload = useMemo<ServerFileUpload | undefined>(() => {
    const syncActive = projectPersistenceAvailable && state.serverSyncEnabled
    if (!serverFilesAvailable || !syncActive) return undefined
    const options = { apiKey: apiKey.trim() || undefined, workspaceId: effectiveWorkspaceId }
    // Upload the ORIGINAL bytes — the file appears instantly as a pending
    // row; the MarkItDown upgrade runs in the background (see runServerParse)
    // and again at index time as a fallback. With an active project sync the
    // upload also carries its section binding, so the server persists the
    // collection placement atomically with the bytes (reload-safe).
    return async (file: File, binding: UploadBinding) => {
      const wireBinding = serverUploadBinding(binding)
      const reserved = await reserveServerFileUpload(file, wireBinding, options)
      dispatch({ assets: [assetRecordFromServer(reserved)], type: 'upsertServerAssetMetadata' })
      noteServerAssetRecordRef.current(reserved.id)
      const info = await uploadServerFile(file, options, wireBinding)
      dispatch({ assets: [assetRecordFromServer(info.asset)], type: 'upsertServerAssetMetadata' })
      noteServerAssetRecordRef.current(info.asset.id)
      return {
        error: carriedErrorText(info.upload_operation.error, readerTexts().fileLibrary.uploadFailedLabel),
        operationId: info.upload_operation.operation_id,
        serverFileId: info.asset.server_file_id,
        status: info.asset.upload_status ?? (
          info.upload_operation.status === 'ready' ? 'ready' : 'retrying'
        ),
      }
    }
  }, [apiKey, dispatch, serverFilesAvailable, effectiveWorkspaceId, projectPersistenceAvailable, state.serverSyncEnabled])
  // Pre-flight for bound uploads: the target section (+ group) must exist
  // server-side before a binding referencing it can be accepted. Idempotent
  // PUTs of the records the autosave would push anyway, just earlier.
  const ensureUploadTarget = useCallback(
    async (sectionId: string, groupId: string | null): Promise<boolean> => {
      if (!(projectPersistenceAvailable && state.serverSyncEnabled)) return false
      const section = state.fileLibrarySections[sectionId]
      if (!section) return false
      const options = { apiKey: apiKey.trim() || undefined, workspaceId: effectiveWorkspaceId }
      try {
        await saveAssetSection(section.id, serverSectionPayload(section), options)
        dispatch({ sectionId: section.id, type: 'markFileLibrarySectionServerSynced' })
        if (groupId) {
          const group = state.fileGroups[groupId]
          if (!group) return false
          await saveAssetGroup(group.id, serverGroupPayload(group), options)
        }
        return true
      } catch {
        return false
      }
    },
    [
      apiKey,
      dispatch,
      effectiveWorkspaceId,
      projectPersistenceAvailable,
      state.fileGroups,
      state.fileLibrarySections,
      state.serverSyncEnabled,
    ],
  )
  // Client options for the file-library preview (getAsset body + original file
  // download). Gated on the FILES/persistence tier — NOT on knowledge: an
  // original exists whenever it was uploaded to the files server, independent of
  // whether vector/knowledge is enabled. `serverFileId` is the per-asset gate.
  const fileApiOptions = useMemo<ClientOptions | null>(
    () => (!isDemoMode && (serverFilesAvailable || projectPersistenceAvailable)
      ? { apiKey: apiKey.trim() || undefined, workspaceId: effectiveWorkspaceId }
      : null),
    [apiKey, isDemoMode, serverFilesAvailable, projectPersistenceAvailable, effectiveWorkspaceId],
  )
  const serverFeatureLabels = useMemo<string[] | null>(() => {
    if (isDemoMode || !capabilities) return null
    const features = capabilities.features
    const labels: string[] = []
    if (features.knowledge) labels.push(t.vectorIndex.featureKnowledge)
    if (features.hybrid_retrieval) labels.push(t.vectorIndex.featureHybrid)
    if (features.reranker) labels.push(t.vectorIndex.featureReranker)
    if (features.contextual_retrieval) labels.push(t.vectorIndex.featureContextual)
    if (features.document_parser) labels.push(t.vectorIndex.featureParser)
    if (features.files) labels.push(t.vectorIndex.featureFiles)
    return labels
  }, [capabilities, isDemoMode, t])
  const projectSyncActive = projectPersistenceAvailable && state.serverSyncEnabled
  useSessionTitle({ state, dispatch, enabled: authUnlocked && capabilities?.session_titles?.enabled === true,
    demo: isDemoMode, chatPersisted: projectSyncActive, workspaceId: effectiveWorkspaceId,
    apiKey: apiKey.trim() || undefined, userId: authSession.user?.id, language: locale,
    stack: stackDiscoveryStatus === 'available' ? state.ui.selectedStack : undefined })
  const editorRecoveryCaptureProviderRef =
    useRef<EditorRecoveryCaptureProvider | null>(null)
  const consumeEditorRecoveryCapture = useCallback((documentId: string) => (
    editorRecoveryCaptureProviderRef.current?.(documentId) ?? null
  ), [])
  const setEditorRecoveryCaptureProvider = useCallback((
    provider: EditorRecoveryCaptureProvider | null,
  ) => {
    editorRecoveryCaptureProviderRef.current = provider
  }, [])
  // A collaboration document whose generation was replaced while it held
  // changes the service can no longer confirm keeps them as a recovery copy,
  // so it never holds up the sign-out.
  useReplacedGenerationRecovery({
    dispatch,
    documents: state.editorDocuments,
    workspaceId: effectiveWorkspaceId,
  })
  const {
    deleteThread: deleteChatThreadOnServer,
    error: chatSyncError,
    hasMoreThreads: chatHistoryHasMore,
    isLoadingMore: chatHistoryLoadingMore,
    isSelectedThreadHistoryPending: chatHistoryPending,
    isSelectedThreadMessagesLoading: chatMessagesLoading,
    loadMoreThreads: loadMoreChatHistory,
    prefetchThreadMessages,
    retrySelectedThreadMessages,
    selectedThreadMessagesError: chatMessagesLoadError,
  } = useChatHistoryApi({
    apiKey: apiKey.trim() || undefined,
    chatThreadGroupMemberships: state.chatThreadGroupMemberships,
    chatThreadGroups: state.chatThreadGroups,
    chatThreads: state.chatThreads,
    dispatch,
    projectEpoch: state.projectEpoch,
    selectedThreadId: state.ui.selectedChatThreadId,
    syncActive: projectSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  const {
    error: editorSyncError,
    flushDocumentForShare,
    prefetchDocumentBody,
    registerOpenedServerDocument,
    selectedDocumentBodyHydration,
  } = useEditorHistoryApi({
    apiKey: apiKey.trim() || undefined,
    consumeCollaborationRecovery: consumeEditorRecoveryCapture,
    dispatch,
    editorCommentOutbox: state.editorCommentOutbox,
    editorComments: state.editorComments,
    editorDocuments: state.editorDocuments,
    editorFolders: state.editorFolders,
    projectEpoch: state.projectEpoch,
    refreshToken: editorRefreshToken,
    selectedDocumentId: state.editorUi.activeDocumentId,
    syncActive: projectSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  useEffect(() => {
    if (sharedOpenTarget?.resource_type !== 'editor_document') return
    const target = sharedOpenTarget
    const consumeTarget = () => {
      setSharedOpenTarget((current) => (
        current?.resource_type === target.resource_type
          && current.resource_id === target.resource_id
          ? null
          : current
      ))
    }
    if (isDemoMode) {
      setSharedResourceError(locale === 'de'
        ? 'Das geteilte Editor-Dokument ist in dieser Demo nicht verfügbar.'
        : 'The shared editor document is not available in this demo.')
      consumeTarget()
      return
    }

    const controller = new AbortController()
    void hydrateSharedEditorTarget(
      target.resource_id,
      { ...contentClientOptions, signal: controller.signal },
      locale,
    ).then((document) => {
      if (controller.signal.aborted) return
      registerOpenedServerDocument(document, 'exact_detail')
      dispatch({ documents: [document], type: 'upsertServerEditorDocuments' })
      dispatch({ document, type: 'setServerEditorDocumentDetail' })
      dispatch({ documentId: document.id, type: 'openEditorDocument' })
      setSharedResourceError(null)
      consumeTarget()
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return
      console.warn('Shared editor document hydration failed.', error)
      setSharedResourceError(messageFromError(error))
      consumeTarget()
    })
    return () => controller.abort()
  }, [
    contentClientOptions,
    dispatch,
    isDemoMode,
    locale,
    registerOpenedServerDocument,
    sharedOpenTarget,
  ])
  const {
    bodyLoadStates: assetBodyLoadStates,
    error: assetSyncError,
    ensureAssetBodiesLoaded,
    noteServerAssetRecord,
    observationStopped: assetObservationStopped,
    retryUpload: retryServerAssetUpload,
  } = useAssetHistoryApi({
    apiKey: apiKey.trim() || undefined,
    dispatch,
    fileAssets: state.fileAssets,
    fileGroups: state.fileGroups,
    fileLibrarySections: state.fileLibrarySections,
    projectEpoch: state.projectEpoch,
    syncActive: projectSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  noteServerAssetRecordRef.current = noteServerAssetRecord
  const pendingChips = useMemo(
    () => chatAttachmentChipsFromRefs(
      chatResolveState,
      combinedChatRefs,
      {
        allowLocalFiles: isIncognitoChat,
        bodyLoadStates: assetBodyLoadStates,
      },
    ),
    [
      assetBodyLoadStates,
      chatResolveState,
      combinedChatRefs,
      isIncognitoChat,
    ],
  )
  const pendingChatAssetIds = useMemo(
    () => assetIdsFromChatRefs(chatResolveState, combinedChatRefs),
    [chatResolveState, combinedChatRefs],
  )
  useEffect(() => {
    if (pendingChatAssetIds.length === 0) return
    void ensureAssetBodiesLoaded(pendingChatAssetIds).catch(() => undefined)
  }, [ensureAssetBodiesLoaded, pendingChatAssetIds])
  const pendingAttachmentBudget = evaluateBudget(
    chatAttachmentsFromRefs(chatResolveState, combinedChatRefs).map((attachment) => ({
      content: attachment.contentMarkdown,
      label: attachment.label ?? attachment.title,
    })),
  )
  const attachmentBudgetNotice = shouldShowAttachmentBudgetNotice(pendingAttachmentBudget)
    ? t.chat.attachmentBudgetWarning
    : null
  const retryAttachmentUpload = useCallback((chip: ChatAttachmentChipModel) => {
    const assetIds = chip.retryAssetIds.filter((assetId) => Boolean(state.fileAssets[assetId]))
    if (assetIds.length === 0) return
    void Promise.all(assetIds.map(async (assetId) => {
      const pending = uploadRegistry.get(assetId)
      dispatch({ assetId, pending: true, type: 'setFileAssetUploadPending' })
      try {
        if (pending && serverFileUpload) {
          const targetReady = await ensureUploadTarget(
            pending.binding.sectionId,
            pending.binding.groupId,
          )
          if (!targetReady) {
            throw new Error(
              locale === 'de'
                ? 'Zielordner konnte nicht auf dem Server reserviert werden'
                : 'The destination could not be reserved on the server',
            )
          }
          const result = await serverFileUpload(pending.file, pending.binding)
          dispatch({ assetId, ...result, type: 'adoptFileAssetUploadLifecycle' })
          if (result.status === 'ready' && result.serverFileId) {
            uploadRegistry.delete(assetId)
          }
          return
        }
        await retryServerAssetUpload(assetId)
      } catch (error) {
        dispatch({
          assetId,
          message: serverUploadFailureMessage(error),
          type: 'failFileAssetUpload',
        })
      }
    }))
  }, [
    dispatch,
    ensureUploadTarget,
    locale,
    retryServerAssetUpload,
    serverFileUpload,
    state.fileAssets,
    uploadRegistry,
  ])
  const {
    acknowledgeServerDeletion: acknowledgeVectorIndexServerDeletion,
    error: vectorIndexSyncError,
  } = useVectorIndexHistoryApi({
    apiKey: apiKey.trim() || undefined,
    dispatch,
    vectorIndexes: state.vectorIndexes,
    projectEpoch: state.projectEpoch,
    syncActive: projectSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  const {
    deleteSession: deletePersistedKnowledgeSession,
    error: knowledgeSessionSyncError,
    isSelectedSessionItemsLoading: knowledgeItemsLoading,
    observationStopped: knowledgeSessionObservationStopped,
    prefetchSessionItems,
    retrySessionDeletion: retryKnowledgeSessionDeletion,
  } = useKnowledgeSessionsApi({
    apiKey: apiKey.trim() || undefined,
    dispatch,
    itemOrder: state.knowledgeItemOrder,
    items: state.knowledgeItems,
    projectEpoch: state.projectEpoch,
    selectedSessionId: state.selectedKnowledgeSessionId,
    sessionGroupMemberships: state.knowledgeSessionGroupMemberships,
    sessionGroups: state.knowledgeSessionGroups,
    sessionOrder: state.knowledgeSessionOrder,
    sessions: state.knowledgeSessions,
    syncActive: projectSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  // Account preferences (theme/locale/contrast/bubble tone) are an ACCOUNT tier, not
  // project data: they sync on a real per-user session (cookieAuthed) + the
  // durable capability, independent of the project's server-sync opt-in, and
  // are NOT part of the project import. "Account wins on login".
  const accountSyncActive = projectPersistenceAvailable && cookieAuthed
  const { error: accountPreferencesError } = useAccountPreferences({
    apiKey: apiKey.trim() || undefined,
    applyPreferences: applyProjectPreferences,
    preferences: currentPreferences,
    syncActive: accountSyncActive,
    workspaceId: effectiveWorkspaceId,
  })
  const { importPending: projectImportPending, importToServer } =
    useProjectServerImport({
      apiKey: apiKey.trim() || undefined,
      canPersist: projectPersistenceAvailable,
      dispatch,
      state,
      workspaceId: effectiveWorkspaceId,
    })
  // Reports loaded from a project file are pushed to the durable run tier so
  // they survive reload + follow the user (the runs analogue of the project
  // import-up), gated on the same server-sync activation as the project hooks.
  const { error: runImportError } = useResearchRunImport({
    apiKey: apiKey.trim() || undefined,
    enabled: projectSyncActive,
    researchRuns: state.researchRuns,
    researchRunOrder: state.researchRunOrder,
    onImported: handleImportedRun,
    workspaceId: effectiveWorkspaceId,
  })
  // Any entity's background autosave failure surfaces on the one badge.
  const conflictingTitle = [...Object.values(state.chatThreads), ...Object.values(state.agentSessions)]
    .find((record) => record.titleConflict)?.titleConflict
  const serverSyncError =
    sharedResourceError ?? chatSyncError ?? editorSyncError
    ?? (assetObservationStopped ? t.common.observationStopped(assetSyncError) : assetSyncError)
    ?? vectorIndexSyncError
    ?? (knowledgeSessionObservationStopped
      ? t.common.observationStopped(knowledgeSessionSyncError)
      : knowledgeSessionSyncError)
    ?? knowledgeCollectionsApi.error
    ?? userInvalidation.error ?? accountPreferencesError ?? runImportError
    ?? (conflictingTitle ? t.sessionTitles.conflict(conflictingTitle) : null)
  const [selectedKnowledgeIndexIds, setSelectedKnowledgeIndexIds] = useState<string[]>([])
  const knowledgeIndexOptions = useMemo<KnowledgeIndexOption[] | null>(() => {
    if (!knowledgeAvailable) return null
    return liveKnowledgeCollectionOptions
  }, [knowledgeAvailable, liveKnowledgeCollectionOptions])
  useEffect(() => {
    if (!knowledgeIndexOptions) {
      if (selectedKnowledgeIndexIds.length > 0) setSelectedKnowledgeIndexIds([])
      return
    }
    const available = new Set(knowledgeIndexOptions.map((option) => option.id))
    if (selectedKnowledgeIndexIds.some((id) => !available.has(id))) {
      setSelectedKnowledgeIndexIds((current) => current.filter((id) => available.has(id)))
    }
  }, [knowledgeIndexOptions, selectedKnowledgeIndexIds])

  // --- "Wissen" workspace (knowledge Q&A + Finden + reader) ----------------
  // Run-tracking wiring choice: knowledge asks are native runs submitted
  // through the EXISTING useResearchRunApi machinery (createResearchRun +
  // SSE stream + result fetch). The reducer routes the same
  // appendApiRunEvent / attachApiRunResult / markApiRunError actions into
  // the knowledgeItems thread slice by run id, so no second event pipeline
  // exists; the only additions are `select: false` (a knowledge ask must
  // not hijack the research workspace selection) and the knowledge-mode
  // filter in projectResearchJobs (the thread is its surface, not a job
  // card). Demo asks reuse the identical event path with synthetic events.
  const knowledgeWorkspaceVisible = isDemoMode || capabilities?.features.knowledge === true
  const agentWorkspaceVisible = isDemoMode || capabilities?.features.workspace_agent === true
  // Agent composer state is lifted so drafts survive view switches
  // (the KnowledgeComposer draft pattern): the text, and beside it the
  // document with its capsules, the attachments and the run setup.
  const [agentDraftQuestion, setAgentDraftQuestion] = useState('')
  const [agentComposerDraft, setAgentComposerDraft] = useState(emptyAgentComposerDraft)
  const [agentAutonomy, setAgentAutonomy] = useState<string | null>(null)
  // Thoroughness: lifted like autonomy, so Deep genuinely
  // stays on across view switches until the user toggles it off.
  const [agentDepth, setAgentDepth] = useState<'normal' | 'deep' | null>(null)
  // Stufe (speed/depth ladder); lifted like depth so it survives view
  // switches. null = server default from capabilities.
  const [agentTier, setAgentTier] = useState<
    import('@/features/researchRuns/types').AgentTierId | null
  >(null)
  // Collection chips belong to their agent session (as in the Knowledge Desk);
  // the chips of a first question move to the session it creates.
  const [agentCollectionIdsBySession, setAgentCollectionIdsBySession] = useState<
    Record<string, string[]>
  >({})
  const agentCollectionIds = useMemo(
    () => collectionIdsForSession(agentCollectionIdsBySession, state.selectedAgentSessionId),
    [agentCollectionIdsBySession, state.selectedAgentSessionId],
  )
  const setAgentCollectionIds = useCallback((collectionIds: string[]) => {
    setAgentCollectionIdsBySession((current) => withSessionCollectionIds(
      current,
      state.selectedAgentSessionId,
      collectionIds,
    ))
  }, [state.selectedAgentSessionId])
  const previousAgentSessionIdRef = useRef(state.selectedAgentSessionId)
  useEffect(() => {
    const previous = previousAgentSessionIdRef.current
    previousAgentSessionIdRef.current = state.selectedAgentSessionId
    // Switching sessions ends the draft's knowledge switch: a session that
    // never asked shows no glyph, whatever the previous session switched.
    // Held here, not in the workspace: there it ran on every remount too.
    setAgentComposerDraft((draft) => (
      draft.knowledgeSwitch === undefined ? draft : { ...draft, knowledgeSwitch: undefined }
    ))
    if (previous === null && state.selectedAgentSessionId !== null) {
      setAgentCollectionIdsBySession((current) => carryCollectionsToNewSession(
        current,
        previous,
        state.selectedAgentSessionId,
      ))
    }
  }, [state.selectedAgentSessionId])
  const [agentDocumentId, setAgentDocumentId] = useState<string | null>(null)
  const effectiveAgentAutonomy = agentAutonomy
    ?? capabilities?.agent?.default_autonomy
    ?? 'balanced'
  const effectiveDepth = effectiveAgentDepth(
    agentDepth,
    capabilities?.agent?.default_depth,
  )
  const projectStateRef = useRef(state)
  projectStateRef.current = state
  const agentDemo = useMemo(
    () =>
      isDemoMode
        ? createAgentDemo(
          dispatch,
          (documentId) =>
            projectStateRef.current.editorDocuments[documentId]
              ?.contentMarkdown ?? null,
          // Where the server reads its rule catalog, the demo reads the
          // project's own agent rules — by the same ids the composer sent.
          (ruleId) => {
            const rule = agentRuleSources(
              chatRuleOptionsFromRules(projectChatRules(projectStateRef.current), 'agent'),
              { localIds: true },
            ).find((candidate) => candidate.id === ruleId)
            // A demo rule was never saved to a server: its first revision.
            return rule ? { label: rule.label, revision: rule.revision ?? 1, title: rule.title } : null
          },
        )
        : null,
    [isDemoMode],
  )
  useEffect(() => () => agentDemo?.dispose(), [agentDemo])
  const [knowledgeMode, setKnowledgeMode] = useState<KnowledgeMode>('ask')
  const [knowledgeQuestion, setKnowledgeQuestion] = useState('')
  const [knowledgeCollectionIds, setKnowledgeCollectionIds] = useState<string[]>([])
  const [knowledgeProfileId, setKnowledgeProfileId] = useState<string | null>(null)
  const [knowledgeTopK, setKnowledgeTopK] = useState<number | null>(null)
  const [knowledgeFinalK, setKnowledgeFinalK] = useState<number | null>(null)
  const knowledgeComposerProjectionKeyRef = useRef<string | null>(null)
  const [knowledgeAskError, setKnowledgeAskError] = useState<string | null>(null)
  const [isIncognitoKnowledge, setIsIncognitoKnowledge] = useState(false)
  const [incognitoKnowledgeAskError, setIncognitoKnowledgeAskError] = useState<string | null>(null)
  const [incognitoKnowledgeItems, setIncognitoKnowledgeItems] = useState<KnowledgeThreadItemRecord[]>([])
  const knowledgeDemoTimeoutsRef = useRef<number[]>([])
  const incognitoKnowledgeRunIdsRef = useRef<Set<string>>(new Set())
  const incognitoKnowledgeItemsRef = useRef<KnowledgeThreadItemRecord[]>([])
  const knowledgeCollections = useMemo<KnowledgeCollectionOption[]>(() => {
    if (!knowledgeWorkspaceVisible) return []
    if (!isDemoMode) return liveKnowledgeCollectionOptions
    return projectKnowledgeIndexes.map((index) => ({
      collectionId: index.id,
      id: index.id,
      title: index.title,
    }))
  }, [
    isDemoMode,
    knowledgeWorkspaceVisible,
    liveKnowledgeCollectionOptions,
    projectKnowledgeIndexes,
  ])
  // Patchable editor documents: in demo every local document works; live
  // requires the server-persisted tier (the id IS the server id there).
  const agentDocumentOptions = useMemo(
    () =>
      isDemoMode || state.serverSyncEnabled
        ? projectAgentTargetEditorDocuments(state)
          .map((document) => ({ id: document.id, title: document.title }))
        : [],
    [
      isDemoMode,
      state.editorDocumentOrder,
      state.editorDocuments,
      state.serverSyncEnabled,
    ],
  )
  // An editing target whose document vanished is not reset here: its
  // capsule stays in the agent draft, marked as no longer available, and
  // the composer holds the send until the user removes it.
  // Agent scope mentions submit SERVER collection ids (knowledge_filters).
  const agentCollectionOptions = useMemo(
    () =>
      knowledgeCollections.map((option) => ({
        id: option.collectionId,
        title: option.title,
      })),
    [knowledgeCollections],
  )
  useEffect(() => {
    const available = new Set(knowledgeCollections.map((option) => option.id))
    setKnowledgeCollectionIds((current) => (
      current.some((id) => !available.has(id))
        ? current.filter((id) => available.has(id))
        : current
    ))
  }, [knowledgeCollections])
  const knowledgeProfileOptions = useMemo(
    () => knowledgeProfileOptionsFromManifest(
      isDemoMode ? DEMO_KNOWLEDGE_PROFILE_MANIFEST : capabilities?.knowledge?.profiles,
    ),
    [capabilities, isDemoMode],
  )
  const knowledgeServerDefaultProfileId = isDemoMode
    ? DEMO_KNOWLEDGE_DEFAULT_PROFILE
    : capabilities?.knowledge?.default_profile ?? null
  const knowledgeDefaultProfileId = useMemo(
    () => resolveKnowledgeDefaultProfileId(knowledgeProfileOptions, knowledgeServerDefaultProfileId),
    [knowledgeProfileOptions, knowledgeServerDefaultProfileId],
  )
  const knowledgeDefaultTopK = isDemoMode
    ? DEMO_KNOWLEDGE_DEFAULT_TOP_K
    : capabilities?.knowledge?.default_top_k ?? DEMO_KNOWLEDGE_DEFAULT_TOP_K
  const knowledgeEvidenceKMax = isDemoMode
    ? DEMO_KNOWLEDGE_EVIDENCE_K_MAX
    : capabilities?.knowledge?.evidence_k_max ?? DEMO_KNOWLEDGE_EVIDENCE_K_MAX
  const knowledgeRerankerProvider = isDemoMode
    ? DEMO_KNOWLEDGE_RERANKER_PROVIDER
    : capabilities?.knowledge?.reranker_provider ?? null
  const knowledgeItems = useMemo(
    () => projectKnowledgeItems(state),
    [state.knowledgeItemOrder, state.knowledgeItems, state.selectedKnowledgeSessionId],
  )
  // Seed the working selection from the account preference while the user has
  // not picked anything in this session. The preference arrives asynchronously
  // (device cache first, server row on login), so this cannot happen at boot.
  // The guard is the user's own choice: once either field is set, this stops
  // touching it — a preference must never overrule a deliberate pick.
  // Chat seeding waits until the selected thread is locally known: the list
  // hydrate carries the stored pick, so before the thread row arrives, "no
  // pick" and "not loaded yet" are indistinguishable (the agent seeding rule).
  const activeChatThread = state.ui.selectedChatThreadId
    ? state.chatThreads[state.ui.selectedChatThreadId] ?? null
    : null
  const chatSeedReady = state.ui.selectedChatThreadId === null
    || (activeChatThread !== null && !activeChatThread.modelSelection)
  const chatSelectionUntouched
    = state.ui.selectedChatModelTier === null && state.ui.selectedChatModel === null
  useEffect(() => {
    if (!chatSelectionUntouched || !chatSeedReady) return
    if (!chatModelTier) return
    dispatch({ tier: chatModelTier, type: 'seedChatModelTierFromPreference' })
  }, [chatModelTier, chatSeedReady, chatSelectionUntouched])
  // Agent seeding waits for the active session's DETAIL: the session list is
  // deliberately metadata-only, so before the items_json fetch lands, "no
  // pick stored" and "pick not loaded yet" are indistinguishable. Seeding in
  // that gap — and worse, writing the seed into the session — is exactly what
  // broke the first attempt at session stickiness.
  const activeAgentSession = state.ui.selectedAgentSessionId
    ? state.agentSessions[state.ui.selectedAgentSessionId] ?? null
    : null
  const agentSeedReady = activeAgentSession === null
    || (activeAgentSession.metadataHydrated === true
      && !activeAgentSession.modelSelection)
  const agentSelectionUntouched
    = state.ui.selectedAgentModelTier === null && state.ui.selectedAgentModel === null
  useEffect(() => {
    if (!agentSelectionUntouched || !agentSeedReady) return
    if (!agentModelTier) return
    dispatch({ tier: agentModelTier, type: 'seedAgentModelTierFromPreference' })
  }, [agentModelTier, agentSeedReady, agentSelectionUntouched])
  useEffect(() => {
    if (isIncognitoKnowledge) {
      knowledgeComposerProjectionKeyRef.current = null
      return
    }
    if (!isDemoMode && !knowledgeCollectionsApi.loaded) return
    if (knowledgeProfileOptions.length === 0) return

    const sessionId = state.selectedKnowledgeSessionId
    const context = sessionId
      ? knowledgeComposerContextForSession({
          availableCollectionIds: knowledgeCollections.map((collection) => collection.id),
          availableProfileIds: knowledgeProfileOptions.map((profile) => profile.id),
          evidenceKMax: knowledgeEvidenceKMax,
          itemOrder: state.knowledgeItemOrder,
          items: state.knowledgeItems,
          sessionId,
        })
      : null
    // The source identity, rather than live option values, makes this a
    // one-time projection. Subsequent user choices survive background
    // progress updates; the existing availability effect still removes a
    // collection immediately when access disappears.
    const projectionKey = JSON.stringify([sessionId, context?.sourceItemId ?? null])
    if (knowledgeComposerProjectionKeyRef.current === projectionKey) return
    knowledgeComposerProjectionKeyRef.current = projectionKey
    setKnowledgeCollectionIds(context?.collectionIds ?? [])
    setKnowledgeProfileId(context?.profileId ?? null)
    setKnowledgeTopK(context?.topK ?? null)
    setKnowledgeFinalK(context?.finalK ?? null)
  }, [
    isDemoMode,
    isIncognitoKnowledge,
    knowledgeCollections,
    knowledgeCollectionsApi.loaded,
    knowledgeEvidenceKMax,
    knowledgeProfileOptions,
    state.knowledgeItemOrder,
    state.knowledgeItems,
    state.selectedKnowledgeSessionId,
  ])
  // An explicit shared-resource open is the later intent in this render. It
  // runs after session projection so the target collection wins once and the
  // projection key prevents a subsequent background sync from undoing it.
  useEffect(() => {
    if (sharedOpenTarget?.resource_type !== 'knowledge_collection') return
    const option = knowledgeCollections.find(
      (collection) => collection.collectionId === sharedOpenTarget.resource_id,
    )
    if (!option) return
    knowledgeComposerProjectionKeyRef.current = JSON.stringify([
      state.selectedKnowledgeSessionId,
      knowledgeItems.at(-1)?.id ?? null,
    ])
    setKnowledgeCollectionIds([option.id])
    setSharedOpenTarget(null)
  }, [knowledgeCollections, knowledgeItems, sharedOpenTarget, state.selectedKnowledgeSessionId])
  const knowledgeAllItems = useMemo(
    () => projectAllKnowledgeItems(state),
    [state.knowledgeItemOrder, state.knowledgeItems],
  )
  const displayedKnowledgeItems = isIncognitoKnowledge ? incognitoKnowledgeItems : knowledgeItems
  const displayedKnowledgeAllItems = isIncognitoKnowledge ? incognitoKnowledgeItems : knowledgeAllItems
  const displayedKnowledgeAskError = isIncognitoKnowledge ? incognitoKnowledgeAskError : knowledgeAskError
  const knowledgeSessions = useMemo(
    () => projectKnowledgeSessions(state),
    [state.knowledgeSessionOrder, state.knowledgeSessions],
  )
  const knowledgeSessionSections = useMemo(
    () => projectKnowledgeSessionSections(state),
    [
      state.knowledgeSessionGroupMemberships,
      state.knowledgeSessionGroupOrder,
      state.knowledgeSessionGroups,
      state.knowledgeSessionOrder,
      state.knowledgeSessions,
      state.ui.explorerSort.knowledge,
    ],
  )
  const isKnowledgeAskRunning = displayedKnowledgeAllItems.some((item) => item.status === 'running')
  const knowledgeDataSource = useMemo<KnowledgeDataSource>(() => {
    if (isDemoMode) return createDemoKnowledgeDataSource()
    const clientOptions = { apiKey: apiKey.trim() || undefined, workspaceId: effectiveWorkspaceId }
    const fileContentAvailable = serverFilesAvailable || projectPersistenceAvailable
    return {
      canLoadFileContent: fileContentAvailable
        ? async (fileId) => {
          try {
            await fetchServerFileInfo(fileId, clientOptions)
            return true
          } catch (error) {
            if (hasHttpStatus(error, 404)) return false
            throw error
          }
        }
        : null,
      loadDocumentText: (documentId) => fetchKnowledgeDocumentText(documentId, clientOptions),
      // An original file exists whenever it was uploaded to the files server —
      // independent of the knowledge `features.files` flag — so gate this the
      // SAME way as the file-library preview (which works on the persistence
      // tier). Gating on serverFilesAvailable alone hid the "Quelle" PDF tab on
      // deployments that have persistence but not the files capability.
      loadFileContent: fileContentAvailable
        ? (fileId) => fetchServerFileContent(fileId, clientOptions)
        : null,
      search: (query, collectionIds, topK) =>
        searchKnowledge({ collectionIds, query, topK }, clientOptions),
    }
  }, [apiKey, isDemoMode, serverFilesAvailable, projectPersistenceAvailable, effectiveWorkspaceId, knowledgeRefreshToken])

  useEffect(() => {
    incognitoKnowledgeItemsRef.current = incognitoKnowledgeItems
  }, [incognitoKnowledgeItems])

  // Falls back to research when the knowledge capability disappears
  // (e.g. backend switch) while the view is active.
  useEffect(() => {
    if (state.ui.activeView === 'knowledge' && !knowledgeWorkspaceVisible) {
      dispatch({ type: 'setActiveView', view: 'research' })
    }
  }, [knowledgeWorkspaceVisible, state.ui.activeView])

  useEffect(() => {
    if (state.ui.activeView === 'agent' && !agentWorkspaceVisible) {
      dispatch({ type: 'setActiveView', view: 'research' })
    }
  }, [agentWorkspaceVisible, state.ui.activeView])

  useEffect(() => {
    const timeouts = knowledgeDemoTimeoutsRef.current
    return () => {
      for (const timeoutId of timeouts) window.clearTimeout(timeoutId)
    }
  }, [])

  useEffect(() => {
    if (knowledgeWorkspaceVisible) return
    discardServerKnowledgeRuns(
      knowledgeRunIdsFromThreadItems(incognitoKnowledgeItemsRef.current),
      { cancelIfActive: true },
    )
    incognitoKnowledgeRunIdsRef.current.clear()
    setIncognitoKnowledgeItems([])
    setIncognitoKnowledgeAskError(null)
    setIsIncognitoKnowledge(false)
  }, [knowledgeWorkspaceVisible])

  function discardServerKnowledgeRuns(
    runIds: string[],
    options: { cancelIfActive?: boolean } = {},
  ) {
    const uniqueRunIds = [...new Set(runIds)].filter(Boolean)
    if (uniqueRunIds.length === 0 || isDemoMode || !discoveryReady || !authUnlocked) return
    void Promise.all(uniqueRunIds.map(async (runId) => {
      try {
        await deleteRun(runId, { cancelIfActive: options.cancelIfActive })
      } catch (error) {
        console.warn('Inqtrix knowledge run delete failed.', error)
      }
    }))
  }

  function deleteLocalResearchRunRecords(runIds: string[]) {
    for (const runId of new Set(runIds)) {
      if (state.researchRuns[runId]) dispatch({ jobId: runId, type: 'deleteJob' })
    }
  }

  function retireKnowledgeRunRecords(
    runIds: string[],
    options: { cancelIfActive?: boolean } = {},
  ) {
    deleteLocalResearchRunRecords(runIds)
    discardServerKnowledgeRuns(runIds, options)
  }

  function updateIncognitoKnowledgeItem(
    runId: string,
    update: (item: KnowledgeThreadItemRecord) => KnowledgeThreadItemRecord,
  ) {
    if (!incognitoKnowledgeRunIdsRef.current.has(runId)) return
    setIncognitoKnowledgeItems((current) =>
      current.map((item) => (item.runId === runId ? update(item) : item)),
    )
  }

  function startIncognitoKnowledgeAsk(item: KnowledgeThreadItemRecord, replaceItemId?: string) {
    incognitoKnowledgeRunIdsRef.current.add(item.runId ?? item.id)
    setIncognitoKnowledgeItems((current) => {
      if (!replaceItemId) return [...current, item]
      let replaced = false
      const next = current.map((existing) => {
        if (existing.id !== replaceItemId) return existing
        replaced = true
        return { ...item, id: existing.id, sessionId: existing.sessionId }
      })
      return replaced ? next : [...current, item]
    })
  }

  function applyIncognitoKnowledgeEvent(event: ResearchRunEvent) {
    const terminalExit = event.type === 'inqtrix.run.failed' || event.type === 'inqtrix.run.cancelled'
    const terminalStatus = event.type === 'inqtrix.run.cancelled' ? 'cancelled' : 'failed'
    updateIncognitoKnowledgeItem(event.run_id, (item) => {
      if (item.status !== 'running') return item
      const progress = applyKnowledgeRunEvent(item.progress, event)
      if (terminalExit) {
        const error = typeof (event.data.error as { message?: unknown } | undefined)?.message === 'string'
          ? String((event.data.error as { message?: unknown }).message)
          : item.error
        return { ...item, error, progress, status: terminalStatus }
      }
      return progress === item.progress ? item : { ...item, progress }
    })
    if (terminalExit) {
      incognitoKnowledgeRunIdsRef.current.delete(event.run_id)
      discardServerKnowledgeRuns([event.run_id])
    }
  }

  function completeIncognitoKnowledgeRun(runId: string, answer: KnowledgeAnswerRecord) {
    const completedAt = new Date().toISOString()
    updateIncognitoKnowledgeItem(runId, (item) => ({
      ...item,
      answer: knowledgeAnswerWithRunProgress(answer, item.progress),
      completedAt,
      progress: {
        ...item.progress,
        steps: item.progress.steps.map((step) => (
          step.status === 'done' ? step : { ...step, status: 'done' as const }
        )),
      },
      status: 'completed',
    }))
    incognitoKnowledgeRunIdsRef.current.delete(runId)
    discardServerKnowledgeRuns([runId])
  }

  function failIncognitoKnowledgeRun(runId: string, message: string) {
    updateIncognitoKnowledgeItem(runId, (item) => (
      item.status === 'completed' ? item : { ...item, error: message, status: 'failed' }
    ))
    incognitoKnowledgeRunIdsRef.current.delete(runId)
    discardServerKnowledgeRuns([runId], { cancelIfActive: true })
  }

  function setKnowledgeIncognito(enabled: boolean) {
    if (!enabled) {
      discardServerKnowledgeRuns(
        knowledgeRunIdsFromThreadItems(incognitoKnowledgeItemsRef.current),
        { cancelIfActive: true },
      )
    }
    incognitoKnowledgeRunIdsRef.current.clear()
    setIncognitoKnowledgeItems([])
    setIncognitoKnowledgeAskError(null)
    setKnowledgeAskError(null)
    setIsIncognitoKnowledge(enabled)
    clearScrollMemory('knowledge:incognito')
  }

  function clearKnowledgeAskSession() {
    if (isIncognitoKnowledge) {
      discardServerKnowledgeRuns(
        knowledgeRunIdsFromThreadItems(incognitoKnowledgeItemsRef.current),
        { cancelIfActive: true },
      )
      incognitoKnowledgeRunIdsRef.current.clear()
      setIncognitoKnowledgeItems([])
      setIncognitoKnowledgeAskError(null)
      clearScrollMemory('knowledge:incognito')
      return
    }
    if (state.selectedKnowledgeSessionId) {
      clearScrollMemory(`knowledge:${state.selectedKnowledgeSessionId}`)
      retireKnowledgeRunRecords(knowledgeRunIdsFromThreadItems(knowledgeItems), { cancelIfActive: true })
      dispatch({ sessionId: state.selectedKnowledgeSessionId, type: 'clearKnowledgeSession' })
    }
  }

  function deleteKnowledgeAskItems(itemIds: string[]) {
    const itemIdSet = new Set(itemIds)
    if (isIncognitoKnowledge) {
      const deletedItems = incognitoKnowledgeItemsRef.current.filter((item) => itemIdSet.has(item.id))
      discardServerKnowledgeRuns(knowledgeRunIdsFromThreadItems(deletedItems), { cancelIfActive: true })
      for (const item of deletedItems) {
        if (item.runId) incognitoKnowledgeRunIdsRef.current.delete(item.runId)
      }
      setIncognitoKnowledgeItems((current) => current.filter((item) => !itemIdSet.has(item.id)))
      return
    }
    const deletedItems = itemIds.flatMap((itemId) => {
      const item = state.knowledgeItems[itemId]
      return item ? [item] : []
    })
    retireKnowledgeRunRecords(knowledgeRunIdsFromThreadItems(deletedItems), { cancelIfActive: true })
    dispatch({ itemIds, type: 'deleteKnowledgeItems' })
  }

  function deleteKnowledgeAskSession(sessionId: string) {
    clearScrollMemory(`knowledge:${sessionId}`)
    void deletePersistedKnowledgeSession(sessionId)
  }

  async function handleKnowledgeAsk(
    question: string,
    options: KnowledgeAskOptions = {},
  ) {
    if (isKnowledgeAskRunning) return
    if (!isDemoMode && !authUnlocked) return
    const replaceItemId = options.replaceItemId
    const setActiveAskError = isIncognitoKnowledge ? setIncognitoKnowledgeAskError : setKnowledgeAskError
    const selectedIds = options.collectionIds ?? knowledgeCollectionIds
    const selectedProfileId = options.profileId ?? knowledgeProfileId ?? knowledgeDefaultProfileId
    const selectedTopK = Object.prototype.hasOwnProperty.call(options, 'topK')
      ? options.topK ?? null
      : knowledgeTopK
    const selectedFinalK = Object.prototype.hasOwnProperty.call(options, 'finalK')
      ? options.finalK ?? null
      : knowledgeFinalK
    const selected = knowledgeCollections.filter((collection) =>
      selectedIds.includes(collection.id))
    if (selected.length === 0) {
      setActiveAskError(t.knowledge.collectionsRequired)
      return
    }
    setActiveAskError(null)

    const collectionTitles = selected.map((collection) => collection.title)
    const backendCollectionIds = selected.map((collection) => collection.collectionId)
    const replacedItem = replaceItemId
      ? isIncognitoKnowledge
        ? incognitoKnowledgeItems.find((item) => item.id === replaceItemId) ?? null
        : state.knowledgeItems[replaceItemId] ?? null
      : null
    const sessionId =
      replacedItem?.sessionId
      ?? (isIncognitoKnowledge ? 'ks-incognito' : state.selectedKnowledgeSessionId)
      ?? state.knowledgeSessionOrder[0]
      ?? createClientId('ks')
    const buildItem = (runId: string): KnowledgeThreadItemRecord => ({
      collectionIds: selected.map((collection) => collection.id),
      collectionTitles,
      createdAt: new Date().toISOString(),
      id: createClientId('kn'),
      progress: { steps: [] },
      question,
      requestedProfile: selectedProfileId,
      runId,
      sessionId,
      status: 'running',
      topK: selectedTopK ?? null,
      finalK: selectedFinalK ?? null,
    })
    const startPersistedItem = (item: KnowledgeThreadItemRecord) => {
      if (replaceItemId) {
        dispatch({ item, replacedItemId: replaceItemId, type: 'restartKnowledgeAsk' })
        return
      }
      dispatch({ item, type: 'startKnowledgeAsk' })
    }

    if (isDemoMode) {
      const runId = createClientId('kn-demo')
      const item = buildItem(runId)
      if (isIncognitoKnowledge) {
        startIncognitoKnowledgeAsk(item, replaceItemId)
      } else {
        startPersistedItem(item)
      }
      if (replaceItemId && replacedItem?.runId) {
        if (isIncognitoKnowledge) {
          discardServerKnowledgeRuns([replacedItem.runId], { cancelIfActive: true })
        } else {
          retireKnowledgeRunRecords([replacedItem.runId], { cancelIfActive: true })
        }
      }
      const script = buildDemoAskScript(runId)
      let elapsed = 0
      for (const step of script.steps) {
        elapsed += step.delayMs
        knowledgeDemoTimeoutsRef.current.push(window.setTimeout(() => {
          if (isIncognitoKnowledge) {
            applyIncognitoKnowledgeEvent(step.event)
          } else {
            dispatch({ event: step.event, type: 'appendApiRunEvent' })
          }
        }, elapsed))
      }
      knowledgeDemoTimeoutsRef.current.push(window.setTimeout(() => {
        if (isIncognitoKnowledge) {
          completeIncognitoKnowledgeRun(runId, script.answer)
        } else {
          dispatch({ answer: script.answer, runId, type: 'completeKnowledgeItem' })
        }
      }, elapsed + script.completeAfterMs))
      return
    }

    const messages = buildKnowledgeAskMessages(displayedKnowledgeItems, question, { replaceItemId })
    const summary = await submitRun(
      {
        knowledgeFilters: {
          collectionIds: backendCollectionIds,
          ...(selectedProfileId ? { profile: selectedProfileId } : {}),
          ...(selectedTopK ? { topK: selectedTopK } : {}),
          ...(selectedFinalK ? { finalK: selectedFinalK } : {}),
        },
        messages,
        mode: 'knowledge',
        question,
        uiLanguage: locale,
        ...(isIncognitoKnowledge ? { incognito: true } : { sessionId }),
      },
      {
        callbacks: isIncognitoKnowledge
          ? {
            onEvent: applyIncognitoKnowledgeEvent,
            onResult: (result) => completeIncognitoKnowledgeRun(result.run_id, knowledgeAnswerFromRunResult(result)),
            onRunError: failIncognitoKnowledgeRun,
          }
          : undefined,
        onCreated: (created) => {
          const item = buildItem(created.run_id)
          if (isIncognitoKnowledge) {
            startIncognitoKnowledgeAsk(item, replaceItemId)
          } else {
            startPersistedItem(item)
          }
          if (replaceItemId && replacedItem?.runId) {
            if (isIncognitoKnowledge) {
              discardServerKnowledgeRuns([replacedItem.runId], { cancelIfActive: true })
            } else {
              retireKnowledgeRunRecords([replacedItem.runId], { cancelIfActive: true })
            }
          }
        },
        select: false,
        suppressSummary: isIncognitoKnowledge,
      },
    )
    if (!summary) {
      setActiveAskError(t.knowledge.askFailed)
    }
  }

  async function handleStopKnowledgeAsk() {
    const runningItem = displayedKnowledgeAllItems.find((item) => item.status === 'running' && item.runId)
    if (!runningItem?.runId) return
    const runId = runningItem.runId
    const wasIncognito = isIncognitoKnowledge
    const setActiveAskError = wasIncognito ? setIncognitoKnowledgeAskError : setKnowledgeAskError
    const cancelEvent: ResearchRunEvent = {
      created_at: Math.floor(Date.now() / 1000),
      data: {
        message: t.knowledge.runCancelled,
        status: 'cancelled',
      },
      run_id: runId,
      sequence: Date.now(),
      type: 'inqtrix.run.cancelled',
    }
    const applyCancelEvent = () => {
      if (wasIncognito) {
        applyIncognitoKnowledgeEvent(cancelEvent)
        return
      }
      dispatch({ event: cancelEvent, type: 'appendApiRunEvent' })
    }

    setActiveAskError(null)
    if (isDemoMode) {
      for (const timeoutId of knowledgeDemoTimeoutsRef.current) window.clearTimeout(timeoutId)
      knowledgeDemoTimeoutsRef.current = []
      applyCancelEvent()
      return
    }
    if (!authUnlocked) return

    try {
      await cancelRun(runId)
      applyCancelEvent()
    } catch (error) {
      setActiveAskError(messageFromError(error))
    }
  }

  function handleKnowledgeDemoAsk() {
    if (!isDemoMode || isKnowledgeAskRunning) return
    const demoCollection =
      knowledgeCollections.find((collection) => collection.id === 'vector-index-eu-recht')
      ?? knowledgeCollections[0]
      ?? null
    if (!demoCollection) {
      if (isIncognitoKnowledge) {
        setIncognitoKnowledgeAskError(t.knowledge.collectionsRequired)
      } else {
        setKnowledgeAskError(t.knowledge.collectionsRequired)
      }
      return
    }
    setKnowledgeCollectionIds([demoCollection.id])
    setKnowledgeProfileId('tief')
    setKnowledgeTopK(DEMO_KNOWLEDGE_DEFAULT_TOP_K)
    void handleKnowledgeAsk(t.knowledge.demoSearchQuestion, {
      collectionIds: [demoCollection.id],
      profileId: 'tief',
      topK: DEMO_KNOWLEDGE_DEFAULT_TOP_K,
    })
  }

  function createKnowledgeSession(title = t.knowledge.newSession): KnowledgeSessionRecord {
    const now = new Date().toISOString()
    return {
      createdAt: now,
      id: createClientId('ks'),
      title,
      updatedAt: now,
    }
  }
  const effectiveStackOptions = isDemoMode
    ? stackOptions
    : stackDiscoveryStatus === 'available'
      ? apiStackNames
      : []
  const displayedSelectedStack = !isDemoMode && stackDiscoveryStatus === 'unsupported'
    ? singleStackLabel
    : state.ui.selectedStack
  const textImprovementStack = !isDemoMode && stackDiscoveryStatus === 'available'
    ? state.ui.selectedStack
    : undefined
  const chatModelDiscoveryStack = stackDiscoveryStatus === 'available'
    ? state.ui.selectedStack
    : defaultStackName
  const chatModelOptionsState = useMemo(
    () => resolveChatModelOptions(effectiveHealth, chatModelDiscoveryStack, apiStacks),
    [apiStacks, chatModelDiscoveryStack, effectiveHealth],
  )
  const chatModelOptions = chatModelOptionsState.options
  const chatModelCatalog = useMemo(
    () => resolveModelCatalog(effectiveHealth, chatModelDiscoveryStack, apiStacks),
    [apiStacks, chatModelDiscoveryStack, effectiveHealth],
  )
  const defaultChatModel = useMemo(
    () => resolveDefaultChatModel(effectiveHealth, chatModelDiscoveryStack, apiStacks),
    [apiStacks, chatModelDiscoveryStack, effectiveHealth],
  )
  const selectedChatCard = chatModelCatalog.find(
    (entry) => entry.model_id === state.ui.selectedChatModel,
  )?.card ?? null
  // Per-category token estimate for the composer meter (the composer draft is
  // added live inside ChatWorkspace). The attachment content is the same the
  // request will carry; history mirrors buildChatMessages' last-20 window.
  const chatContextBase = useMemo(() => {
    const attachments = chatAttachmentsFromRefs(chatResolveState, combinedChatRefs)
    let documents = 0
    let reports = 0
    let rules = 0
    for (const attachment of attachments) {
      const tokens = estimateTokensFromText(attachment.contentMarkdown ?? '')
      if (attachment.kind === 'research-report') reports += tokens
      else if (attachment.kind === 'chat-rule') rules += tokens
      else documents += tokens
    }
    const history = (displayedChatThread?.messages ?? []).slice(-20)
    const conversation = history.reduce(
      (sum, message) => sum + estimateTokensFromText(message.contentMarkdown ?? ''),
      0,
    )
    return { documents, reports, rules, conversation }
  }, [chatResolveState, combinedChatRefs, displayedChatThread])
  const chatContextCapacity = {
    contextWindowTokens: selectedChatCard?.context_window_tokens ?? null,
    reservedOutputTokens: selectedChatCard?.max_output_tokens ?? 0,
  }
  const [settingsRequestedSection, setSettingsRequestedSection] =
    useState<'security' | null>(null)
  // Feed the cookie session's authenticated state back to the run-list hook
  // (declared above) so an in-app local login re-hydrates prior runs.
  useEffect(() => {
    setCookieAuthed(isCookieMode && authSession.status === 'authenticated')
  }, [isCookieMode, authSession.status])
  // Server-first persistence for an authenticated (cookie-session) user: derive
  // serverSyncEnabled from the live session + durable capability instead of the
  // manual import button, so the project auto-hydrates on every boot/reload and
  // auto-saves with no opt-in. Drives the flag both ways: a login enables sync
  // (the empty boot state then hydrates from the server), a logout disables it
  // (the hooks reset to empty so the prior user's data is not shown). persistLocal
  // is false: the flag is session-derived, never written to the local manifest or
  // marked dirty. ONLY the cookie tier (local/oidc = a real per-user
  // identity); the apikey / local-first / demo tiers keep the manual opt-in
  // (this effect no-ops outside cookie mode), so their behaviour is unchanged.
  useEffect(() => {
    if (!isCookieMode) return
    const shouldSync = projectPersistenceAvailable && cookieAuthed
    if (state.serverSyncEnabled !== shouldSync) {
      dispatch({ enabled: shouldSync, persistLocal: false, type: 'setServerSyncEnabled' })
    }
  }, [isCookieMode, projectPersistenceAvailable, cookieAuthed, state.serverSyncEnabled])
  // No lock flash while the very first session probe is in flight.
  const isAuthLocked =
    !isDemoMode
    && !authUnlocked
    && (!isCookieMode || authSession.status !== 'unknown')
  // Exactly the silent window the lock screen deliberately leaves open
  // (cookie mode, first session probe in flight): submission is visibly
  // disabled there instead of handleComposerSubmit no-op'ing a click.
  const isAuthResolving = !isDemoMode && !authUnlocked && !isAuthLocked
  const canImproveText = !isDemoMode && authUnlocked
  // Sharing exists on the oidc surface with a live session; the capability
  // flag keeps none/apikey deployments byte-identical. Demo mode simulates it
  // from seeded data so the feature is visible offline (like the quota meter).
  const sharingEnabled = isSharingEnabled({
    authMode,
    capabilities,
    isDemo: isDemoMode,
    sessionStatus: authSession.status,
  })
  // Resolved once here so the settings panel and the nav badge share one
  // source of truth (and one fetch); `null` when sharing is off hides the
  // settings section entirely.
  const sharingInbox = useSharingInbox({
    demo: isDemoMode,
    enabled: sharingEnabled,
    onResourcesChanged: requestResourceRefresh,
    refreshToken: sharingListRefreshToken,
  })
  // "Open" retains the entity id until its authoritative list has hydrated;
  // each workspace then consumes the same one-shot target when it can focus it.
  const openSharedResource = useCallback((share: InboxShare) => {
    setSharedResourceError(null)
    setSharedOpenTarget({
      resource_id: share.resource_id,
      resource_type: share.resource_type,
    })
    requestResourceRefresh()
    setSettingsRequestedSection(null)
    dispatch({
      type: 'setActiveView',
      view: sharedResourceDestination(share.resource_type),
    })
  }, [dispatch, requestResourceRefresh])
  // The quota meter follows the same cookie-session + capability gate;
  // demo mode shows seeded figures so the feature is visible offline.
  const quotaMeterEnabled =
    isDemoMode
    || (capabilities?.features.quota === true
      && isCookieMode
      && authSession.status === 'authenticated')
  const [shareTarget, setShareTarget] = useState<
    {
      collaborationGeneration?: number
      document?: EditorDocumentRecord
      documentDetails?: EditorDocumentDetailsSummary
      /** Why the dialog opened — decides its landing tab and whether the
       * recipient search takes focus. Only editor documents set it. */
      intent?: 'details' | 'share'
      returnFocusTarget?: HTMLElement
      resourceId: string
      resourceType: string
      title: string
    } | null
  >(null)
  const handleShareEditorDocument = useCallback((
    document: EditorDocumentRecord,
    documentDetails: EditorDocumentDetailsSummary,
    intent: 'details' | 'share' = 'share',
    returnFocusTarget?: HTMLElement | null,
  ) => {
    setShareTarget({
      document,
      documentDetails,
      intent,
      ...(returnFocusTarget ? { returnFocusTarget } : {}),
      resourceId: document.id,
      resourceType: 'editor_document',
      title: document.title,
      ...(document.collaboration
        ? { collaborationGeneration: document.collaboration.generation }
        : {}),
    })
  }, [])
  const incomingShareTarget = useMemo(() => {
    const document = shareTarget?.document
    if (!document || document.access?.mode !== 'shared') return null
    return sharingInbox.state.accepted.find((share) => (
      share.resource_type === 'editor_document'
      && share.resource_id === document.id
    )) ?? null
  }, [shareTarget?.document, sharingInbox.state.accepted])
  const shareCountByRunId = useMemo(
    () => outgoingShareCounts(sharingInbox.state.outgoing, 'run'),
    [sharingInbox.state.outgoing],
  )
  // Prompt templates persist server-side whenever the capability is
  // live and the caller is unlocked (works in apikey/none too);
  // demo mode stays browser-local.
  const templatesEnabled =
    !isDemoMode
    && capabilities?.features.prompt_templates === true
    && authUnlocked
  const templateSync = useTemplateSync({
    clientOptions: contentClientOptions,
    dispatch,
    enabled: templatesEnabled,
    localRules: chatRules,
    refreshToken: templatesRefreshToken,
  })
  const flushScheduledChatContent = useCallback((threadId: string) => {
    const scheduled = scheduledChatContentByThreadIdRef.current.get(threadId)
    if (!scheduled) return
    scheduledChatContentByThreadIdRef.current.delete(threadId)
    if (scheduled.threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => setThreadAssistantMessageContent(
        current,
        scheduled.assistantMessageId,
        scheduled.contentMarkdown,
      ))
      return
    }
    dispatch({
      assistantMessageId: scheduled.assistantMessageId,
      contentMarkdown: scheduled.contentMarkdown,
      threadId: scheduled.threadId,
      type: 'setChatAssistantMessageContent',
    })
  }, [])

  const flushPendingChatContent = useCallback((threadId: string) => {
    const frame = chatFlushFrameByThreadIdRef.current.get(threadId)
    if (frame !== undefined) {
      window.cancelAnimationFrame(frame)
      chatFlushFrameByThreadIdRef.current.delete(threadId)
    }
    flushScheduledChatContent(threadId)
  }, [flushScheduledChatContent])

  const scheduleChatContent = useCallback((
    threadId: string,
    assistantMessageId: string,
    contentMarkdown: string,
  ) => {
    scheduledChatContentByThreadIdRef.current.set(threadId, {
      assistantMessageId,
      contentMarkdown,
      threadId,
    })
    if (chatFlushFrameByThreadIdRef.current.has(threadId)) return
    const frame = window.requestAnimationFrame(() => {
      chatFlushFrameByThreadIdRef.current.delete(threadId)
      flushScheduledChatContent(threadId)
    })
    chatFlushFrameByThreadIdRef.current.set(threadId, frame)
  }, [flushScheduledChatContent])

  useEffect(() => {
    return () => {
      for (const controller of chatControllerByThreadIdRef.current.values()) {
        controller.abort()
      }
      for (const frame of chatFlushFrameByThreadIdRef.current.values()) {
        window.cancelAnimationFrame(frame)
      }
      chatControllerByThreadIdRef.current.clear()
      chatSubmittingThreadIdsRef.current.clear()
      chatFlushFrameByThreadIdRef.current.clear()
      chatStreamContentByThreadIdRef.current.clear()
      scheduledChatContentByThreadIdRef.current.clear()
    }
  }, [])

  function reportProjectActionError(error: unknown) {
    logProjectActionError(error)
    if (error instanceof DOMException && error.name === 'AbortError') {
      // Cancelling the directory dialog is a user decision, not a failure, but
      // it still ends the action. Saying nothing leaves the user believing a
      // backup was written.
      setProjectActionError(t.topbar.projectActionCancelled)
      return
    }
    setProjectActionError(`${t.topbar.projectActionFailed}: ${messageFromError(error)}`)
  }

  async function handleImportProjectToServer() {
    // The import is the one user-facing chat-sync action that runs BEFORE
    // the project is server-synced, so the "Synced" badge (which carries
    // sync errors) is not yet mounted to show a failure. Surface it through
    // the same red banner as the other project actions (No Silent Fallbacks).
    setProjectActionError(null)
    try {
      await importToServer()
    } catch (error) {
      reportProjectActionError(error)
    }
  }

  async function handleLoadProject() {
    setProjectActionError(null)
    try {
      const loadedState = await loadProject({ onWorkStart: () => setProjectAction('load') })
      const nextState = prepareProjectFileImport(
        loadedState,
        effectiveWorkspaceId,
        accountSyncActive,
      )
      abortAllChatRequests()
      // Account preferences (theme/locale/contrast/bubble tone) are an account tier, not
      // project data: while a real per-user session drives them, a loaded
      // project file must NOT bleed its embedded prefs into the live theme
      // (which the account autosave would then PUT, clobbering the account
      // from an unrelated file). Project-embedded prefs apply only offline /
      // when there is no account sync (the local-first case).
      if (!accountSyncActive) applyProjectPreferences(nextState.preferences)
      setIsIncognitoChat(false)
      resetIncognitoSession()
      setActiveChatRequestsByThreadId({})
      dispatch({ state: nextState, type: 'hydrateProject' })
    } catch (error) {
      reportProjectActionError(error)
    } finally {
      setProjectAction(null)
    }
  }

  async function handleExportProject() {
    setProjectActionError(null)
    try {
      setProjectAction('export')
      const prepared = await prepareCollaborationProjectExport(
        projectStateWithPreferences(state, currentPreferences),
        contentClientOptions,
        activeCollaborationControllerRef.current,
      )
      if (prepared.staleDocuments.length > 0 && !confirmStaleCollaborationExport(
        prepared.staleDocuments,
        locale,
      )) {
        setProjectActionError(t.topbar.projectActionCancelled)
        return
      }
      const result = await exportProject(
        prepared.state,
        { onWorkStart: () => setProjectAction('export') },
      )
      dispatch({
        connection: result.connection,
        preferences: currentPreferences,
        savedAt: result.savedAt,
        type: 'markProjectSaved',
      })
    } catch (error) {
      reportProjectActionError(error)
    } finally {
      setProjectAction(null)
    }
  }

  async function handleSaveProject() {
    setProjectActionError(null)
    try {
      setProjectAction('save')
      const prepared = await prepareCollaborationProjectExport(
        projectStateWithPreferences(state, currentPreferences),
        contentClientOptions,
        activeCollaborationControllerRef.current,
      )
      if (prepared.staleDocuments.length > 0 && !confirmStaleCollaborationExport(
        prepared.staleDocuments,
        locale,
      )) {
        setProjectActionError(t.topbar.projectActionCancelled)
        return
      }
      const result = await saveProject(
        prepared.state,
        { onWorkStart: () => setProjectAction('save') },
      )
      dispatch({
        connection: result.connection,
        preferences: currentPreferences,
        savedAt: result.savedAt,
        type: 'markProjectSaved',
      })
    } catch (error) {
      reportProjectActionError(error)
    } finally {
      setProjectAction(null)
    }
  }

  async function handleComposerSubmit(
    request: CreateResearchRunRequest,
  ): Promise<ResearchSubmissionOutcome> {
    if (isDemoMode) {
      dispatch({ request, type: 'createLocalRun' })
      return { status: 'accepted' }
    }
    if (!authUnlocked) {
      return {
        message: t.composer.sessionExpired,
        recoverability: 'login',
        status: 'rejected',
      }
    }

    let rejection: unknown
    const summary = await submitRun(
      stackDiscoveryStatus === 'unsupported'
        ? { ...request, stack: undefined }
        : request,
      {
        onRejected: (error) => {
          rejection = error
        },
        reloadOnUnauthorized: false,
      },
    )
    if (summary) return { status: 'accepted' }
    if (hasHttpStatus(rejection, 401)) {
      return {
        message: t.composer.sessionExpired,
        recoverability: 'login',
        status: 'rejected',
      }
    }
    return {
      message: t.composer.submitFailed,
      recoverability: 'retry',
      status: 'rejected',
    }
  }

  function handleResearchAuthenticationRequired() {
    setAuthLockError(t.composer.sessionExpired)
    if (isCookieMode) {
      void refreshAuthSession()
    } else if (authMode === 'apikey') {
      setApiKey('')
    }
  }

  async function handleChatMessageSubmit(
    contentMarkdown: string,
    inlineAttachmentRefs: ChatContextReferenceRecord[] = [],
    options: ChatSendOptions = {},
  ): Promise<boolean> {
    const trimmedContent = contentMarkdown.trim()
    if (!trimmedContent) return false
    if (!isDemoMode && !authUnlocked) return false

    const selectedThread = isIncognitoChat
      ? incognitoThread
      : state.ui.selectedChatThreadId
      ? state.chatThreads[state.ui.selectedChatThreadId]
      : chatThreads[0] ?? undefined
    const threadId = isIncognitoChat
      ? INCOGNITO_THREAD_ID
      : selectedThread?.id ?? createClientId('chat')
    if (
      chatControllerByThreadIdRef.current.has(threadId)
      || chatSubmittingThreadIdsRef.current.has(threadId)
    ) return false
    if (
      chatControllerByThreadIdRef.current.size
      + chatSubmittingThreadIdsRef.current.size
      >= MAX_PARALLEL_CHAT_REQUESTS
    ) {
      setChatNoticeByThreadId((current) => ({
        ...current,
        [threadId]: t.chat.parallelLimitReached,
      }))
      return false
    }
    chatSubmittingThreadIdsRef.current.add(threadId)
    const releaseSubmissionLatch = () => {
      chatSubmittingThreadIdsRef.current.delete(threadId)
    }

    const userMessageId = createClientId('msg')
    const assistantMessageId = createClientId('msg')
    const createdAt = new Date().toISOString()
    const useStreaming = chatStreamingEnabled
    const modelTier = options.modelTier ?? state.ui.selectedChatModelTier
    const explicitModel = options.model ?? state.ui.selectedChatModel
    const explicitEffort = options.effort ?? state.ui.selectedChatEffort
    const chatStack = stackDiscoveryStatus === 'available' ? state.ui.selectedStack : undefined
    const knowledgeCollectionIds = options.knowledgeCollectionIds ?? []
    const requestContext = chatRequestContextForKnowledge(knowledgeCollectionIds)
    const modelResolution = explicitModel
      ? explicitModelResolution(explicitModel, explicitEffort)
      : chatMessageModelResolutionForTier(
          chatModelOptionsState,
          defaultChatModel,
          modelTier,
        )
    const messageAttachmentRefs = dedupeChatContextRefs([
      ...inlineAttachmentRefs,
      ...pendingChatRefs,
    ])
    const metadataReadiness = attachmentContextReadiness(
      chatResolveState,
      messageAttachmentRefs,
      {
        allowLocalFiles: isIncognitoChat,
        bodyLoadStates: assetBodyLoadStates,
      },
    )
    if (metadataReadiness.status !== 'ready') {
      setChatErrorByThreadId((current) => ({
        ...current,
        [threadId]: metadataReadiness.error
          ?? (metadataReadiness.status === 'pending'
            ? t.chat.attachmentContextPending
            : t.chat.attachmentContextFailed),
      }))
      releaseSubmissionLatch()
      return false
    }
    // Guarantee any attached file-asset bodies are loaded before they are read
    // synchronously into the outgoing attachments (load-on-use). The
    // prefetch on attach usually made this instant; the returned map overrides
    // the state snapshot, which the just-dispatched bodies have not reached. A
    // failed fetch must surface (No-Silent-Fallbacks) and abort the send rather
    // than ship an empty attachment — nothing has been mutated yet, so the
    // draft is preserved and the user can retry.
    let assetBodies: Map<string, string>
    try {
      assetBodies = await ensureAssetBodiesLoaded(
        assetIdsFromChatRefs(chatResolveState, messageAttachmentRefs),
      )
    } catch (error) {
      setChatErrorByThreadId((current) => ({
        ...current,
        [threadId]: `${t.chat.requestFailed}: ${messageFromError(error)}`,
      }))
      releaseSubmissionLatch()
      return false
    }
    const messageAttachments = chatAttachmentsFromRefs(
      chatResolveState,
      messageAttachmentRefs,
      assetBodies,
    )
    const contentReadiness = attachmentContextReadiness(
      chatResolveState,
      messageAttachmentRefs,
      {
        allowLocalFiles: isIncognitoChat,
        assetBodyOverride: assetBodies,
        requireContent: true,
      },
    )
    if (contentReadiness.status !== 'ready') {
      setChatErrorByThreadId((current) => ({
        ...current,
        [threadId]: contentReadiness.error ?? t.chat.attachmentContextFailed,
      }))
      releaseSubmissionLatch()
      return false
    }
    const requestMessages = buildChatMessages(
      selectedThread?.messages ?? [],
      trimmedContent,
      messageAttachments,
    )

    if (isIncognitoChat) {
      setIncognitoThread((current) => appendChatExchangeToThread(
        current,
        {
          assistantMessageId,
          attachments: messageAttachments,
          contentMarkdown: trimmedContent,
          createdAt,
          modelResolution,
          requestContext,
          userMessageId,
        },
      ))
      // Clear only the draft refs; keep incognitoAssets so the same file can be
      // re-referenced within the session (mirrors how a normal chat keeps its
      // asset in state.fileAssets after sending).
      setIncognitoAttachmentRefs([])
    } else {
      dispatch({
        assistantMessageId,
        contentMarkdown: trimmedContent,
        createdAt,
        attachmentRefs: messageAttachmentRefs,
        modelResolution,
        requestContext,
        threadId,
        type: 'startChatExchange',
        titleLanguage: locale, titleStack: chatStack ?? null,
        userMessageId,
      })
    }
    setChatErrorByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setChatNoticeByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })

    const controller = new AbortController()
    chatControllerByThreadIdRef.current.set(threadId, controller)
    releaseSubmissionLatch()
    chatStreamContentByThreadIdRef.current.set(threadId, '')
    setActiveChatRequestsByThreadId((current) => ({
      ...current,
      [threadId]: {
        assistantMessageId,
        phase: 'submitted',
        threadId,
      },
    }))

    const chainTemplates = state.ui.chatChainingEnabled && knowledgeCollectionIds.length === 0
      ? chatFunctionChainTemplatesFromRefs(state.chatRules, messageAttachmentRefs)
      : []

    if (chainTemplates.length > 0) {
      void runChatChainRequest({
        assistantMessageId,
        controller,
        history: selectedThread?.messages ?? [],
        modelTier,
        model: explicitModel,
        effort: explicitEffort,
        sourceAttachments: chatAttachmentsFromRefs(
          chatResolveState,
          messageAttachmentRefs.filter((ref) => isPillKind(ref.kind)),
          assetBodies,
        ),
        stack: chatStack,
        templates: chainTemplates,
        threadId,
        useStreaming,
        userText: trimmedContent,
      })
    } else {
      void runChatAssistantRequest({
        assistantMessageId,
        controller,
        requestMessages,
        modelTier,
        model: explicitModel,
        effort: explicitEffort,
        knowledgeCollectionIds,
        stack: chatStack,
        threadId,
        useStreaming,
      })
    }
    return true
  }

  async function runChatChainRequest({
    assistantMessageId,
    controller,
    history,
    modelTier,
    model,
    effort,
    sourceAttachments,
    stack,
    templates,
    threadId,
    useStreaming,
    userText,
  }: {
    assistantMessageId: string
    controller: AbortController
    history: ChatMessageRecord[]
    modelTier: ChatModelTier | null
    model: string | null
    effort: string | null
    sourceAttachments: ChatMessageAttachmentRecord[]
    stack?: string
    templates: { instruction: string; label: string }[]
    threadId: string
    useStreaming: boolean
    userText: string
  }) {
    const trace: ChatChainStepRecord[] = []
    let running = userText
    let modelResolution: ChatMessageModelResolutionRecord | undefined

    try {
      if (isDemoMode) throw new Error(t.chat.demoModeDisabled)

      for (let index = 0; index < templates.length; index += 1) {
        const template = templates[index]
        const isFinal = index === templates.length - 1
        const stepMessages = buildChainStepMessages(
          index === 0 ? history : [],
          template.instruction,
          running,
          index === 0 ? sourceAttachments : [],
        )
        const baseStepRequest = chatCompletionBase({
          agentOverrides: chatAgentOverrides(modelTier, model, effort),
          messages: stepMessages,
          stack,
          threadId,
        })
        const stepTimeout = globalThis.setTimeout(() => controller.abort(), chatStepTimeoutMsRef.current)
        try {
          if (isFinal && useStreaming) {
            chatStreamContentByThreadIdRef.current.set(threadId, '')
            await streamChatCompletion(
              { ...baseStepRequest, stream: true },
              {
                apiKey: apiKey.trim() || undefined,
                signal: controller.signal,
                workspaceId: effectiveWorkspaceId,
                onDelta: (delta) => {
                  const nextContent = `${chatStreamContentByThreadIdRef.current.get(threadId) ?? ''}${delta}`
                  chatStreamContentByThreadIdRef.current.set(threadId, nextContent)
                  setActiveChatRequestsByThreadId((current) => {
                    const request = current[threadId]
                    if (request?.assistantMessageId !== assistantMessageId) return current
                    return { ...current, [threadId]: { ...request, phase: 'streaming' } }
                  })
                  scheduleChatContent(threadId, assistantMessageId, nextContent)
                },
                onModelResolution: (resolution) => {
                  const normalized = normalizeChatMessageModelResolution(resolution)
                  if (normalized) modelResolution = normalized
                },
              },
            )
            flushPendingChatContent(threadId)
            running = (chatStreamContentByThreadIdRef.current.get(threadId) ?? '').trim() || t.chat.emptyResponse
          } else {
            const response = await createChatCompletion(
              { ...baseStepRequest, stream: false },
              {
                apiKey: apiKey.trim() || undefined,
                signal: controller.signal,
                workspaceId: effectiveWorkspaceId,
              },
            )
            running = response.choices[0]?.message.content.trim() || t.chat.emptyResponse
            if (isFinal) {
              modelResolution = normalizeChatMessageModelResolution(response.inqtrix?.model_resolution) ?? modelResolution
            }
          }
        } finally {
          globalThis.clearTimeout(stepTimeout)
        }

        if (controller.signal.aborted) {
          trace.push({ label: template.label, output: running, status: 'stopped' })
          setChatAssistantMessageContent(
            threadId,
            assistantMessageId,
            stoppedChatContent(isFinal ? running : '', t.chat.generationStopped),
            modelResolution,
            trace,
          )
          return
        }
        trace.push({ label: template.label, output: running, status: 'ok' })
        setChatAssistantMessageContent(
          threadId,
          assistantMessageId,
          isFinal ? running : (chatStreamContentByThreadIdRef.current.get(threadId) ?? ''),
          modelResolution,
          trace,
        )
      }
    } catch (error) {
      flushPendingChatContent(threadId)
      if (controller.signal.aborted) {
        setChatAssistantMessageContent(
          threadId,
          assistantMessageId,
          stoppedChatContent(running, t.chat.generationStopped),
          modelResolution,
          trace,
        )
      } else {
        const errorMessage = `${t.chat.requestFailed}: ${messageFromError(error)}`
        const failedTrace: ChatChainStepRecord[] = [
          ...trace,
          { label: templates[trace.length]?.label ?? t.chat.chainStepFailed, output: errorMessage, status: 'error' },
        ]
        setChatAssistantMessageContent(threadId, assistantMessageId, errorMessage, modelResolution, failedTrace)
        setChatErrorByThreadId((current) => ({ ...current, [threadId]: errorMessage }))
      }
    } finally {
      if (chatControllerByThreadIdRef.current.get(threadId) === controller) {
        chatControllerByThreadIdRef.current.delete(threadId)
      }
      chatStreamContentByThreadIdRef.current.delete(threadId)
      setActiveChatRequestsByThreadId((current) => removeChatRequest(current, threadId, assistantMessageId))
    }
  }

  async function runChatAssistantRequest({
    assistantMessageId,
    controller,
    modelTier,
    model,
    effort,
    knowledgeCollectionIds = [],
    requestMessages,
    stack,
    threadId,
    useStreaming,
  }: {
    assistantMessageId: string
    controller: AbortController
    modelTier: ChatModelTier | null
    model: string | null
    effort: string | null
    knowledgeCollectionIds?: string[]
    requestMessages: ChatCompletionMessage[]
    stack?: string
    threadId: string
    useStreaming: boolean
  }) {
    const knowledgeMode = knowledgeCollectionIds.length > 0
    const baseChatRequest = chatCompletionBase({
      agentOverrides: chatAgentOverrides(modelTier, model, effort),
      knowledgeCollectionIds,
      messages: requestMessages,
      stack,
      threadId,
      uiLanguage: locale,
    })

    async function writeBlockingAnswer() {
      const response = await createChatCompletion(
        {
          ...baseChatRequest,
          stream: false,
        },
        {
          apiKey: apiKey.trim() || undefined,
          signal: controller.signal,
          workspaceId: effectiveWorkspaceId,
        },
      )
      const answer = response.choices[0]?.message.content.trim() || t.chat.emptyResponse
      setChatAssistantMessageContent(
        threadId,
        assistantMessageId,
        answer,
        normalizeChatMessageModelResolution(response.inqtrix?.model_resolution),
      )
    }

    try {
      if (isDemoMode) {
        throw new Error(t.chat.demoModeDisabled)
      }

      // Knowledge mode answers over the request/response transport; the
      // server cancels the run when this request is aborted, so Stop is
      // effective on both transports.
      if (useStreaming && !knowledgeMode) {
        await streamChatCompletion(
          {
            ...baseChatRequest,
            stream: true,
          },
          {
            apiKey: apiKey.trim() || undefined,
            signal: controller.signal,
            workspaceId: effectiveWorkspaceId,
            onDelta: (delta) => {
              const nextContent = `${chatStreamContentByThreadIdRef.current.get(threadId) ?? ''}${delta}`
              chatStreamContentByThreadIdRef.current.set(threadId, nextContent)
              setActiveChatRequestsByThreadId((current) => {
                const request = current[threadId]
                if (request?.assistantMessageId !== assistantMessageId) return current
                return {
                  ...current,
                  [threadId]: { ...request, phase: 'streaming' },
                }
              })
              scheduleChatContent(threadId, assistantMessageId, nextContent)
            },
            onModelResolution: (resolution) => {
              const modelResolution = normalizeChatMessageModelResolution(resolution)
              if (!modelResolution) return
              setChatAssistantMessageContent(
                threadId,
                assistantMessageId,
                chatStreamContentByThreadIdRef.current.get(threadId) ?? '',
                modelResolution,
              )
            },
          },
        )
        flushPendingChatContent(threadId)
        const streamedContent = chatStreamContentByThreadIdRef.current.get(threadId) ?? ''
        if (!streamedContent.trim()) {
          setChatStreamingEnabled(false)
          setChatNoticeByThreadId((current) => ({
            ...current,
            [threadId]: t.chat.streamingFallback,
          }))
          await writeBlockingAnswer()
        }
      } else {
        await writeBlockingAnswer()
      }
    } catch (error) {
      flushPendingChatContent(threadId)
      let chatFailure = error
      if (
        useStreaming
        && !controller.signal.aborted
        && !(chatStreamContentByThreadIdRef.current.get(threadId) ?? '').trim()
      ) {
        try {
          setChatStreamingEnabled(false)
          setChatNoticeByThreadId((current) => ({
            ...current,
            [threadId]: t.chat.streamingFallback,
          }))
          await writeBlockingAnswer()
          return
        } catch (fallbackError) {
          chatFailure = fallbackError
        }
      }

      const streamContent = chatStreamContentByThreadIdRef.current.get(threadId) ?? ''
      const partialContent = streamContent.trim()
      const errorMessage = `${t.chat.requestFailed}: ${messageFromError(chatFailure)}`
      const content = controller.signal.aborted
        ? stoppedChatContent(streamContent, t.chat.generationStopped)
        : partialContent
          ? `${streamContent.trimEnd()}\n\n_${errorMessage}_`
        : errorMessage

      setChatAssistantMessageContent(threadId, assistantMessageId, content)
      if (!controller.signal.aborted) {
        setChatErrorByThreadId((current) => ({
          ...current,
          [threadId]: errorMessage,
        }))
      }
    } finally {
      if (chatControllerByThreadIdRef.current.get(threadId) === controller) {
        chatControllerByThreadIdRef.current.delete(threadId)
      }
      chatStreamContentByThreadIdRef.current.delete(threadId)
      setActiveChatRequestsByThreadId((current) => removeChatRequest(current, threadId, assistantMessageId))
    }
  }

  async function handleAnswerLastUserMessage(threadId: string, messageId: string) {
    if (!isDemoMode && !authUnlocked) return

    const selectedThread = threadId === INCOGNITO_THREAD_ID
      ? incognitoThread
      : state.chatThreads[threadId]
    const lastMessage = selectedThread?.messages[selectedThread.messages.length - 1]
    if (
      !selectedThread
      || !lastMessage
      || lastMessage.id !== messageId
      || lastMessage.role !== 'user'
      || !lastMessage.contentMarkdown.trim()
      || chatControllerByThreadIdRef.current.has(threadId)
    ) {
      return
    }
    if (chatControllerByThreadIdRef.current.size >= MAX_PARALLEL_CHAT_REQUESTS) {
      setChatNoticeByThreadId((current) => ({
        ...current,
        [threadId]: t.chat.parallelLimitReached,
      }))
      return
    }

    const assistantMessageId = createClientId('msg')
    const createdAt = new Date().toISOString()
    const useStreaming = chatStreamingEnabled
    const modelTier = state.ui.selectedChatModelTier
    const explicitModel = state.ui.selectedChatModel
    const explicitEffort = state.ui.selectedChatEffort
    const modelResolution = explicitModel
      ? explicitModelResolution(explicitModel, explicitEffort)
      : chatMessageModelResolutionForTier(
          chatModelOptionsState,
          defaultChatModel,
          modelTier,
        )
    const requestMessages = buildChatMessages(
      selectedThread.messages.slice(0, -1),
      lastMessage.contentMarkdown,
      lastMessage.attachments ?? [],
    )

    if (threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => appendAssistantResponseToLastUserMessage(
        current,
        {
          assistantMessageId,
          createdAt,
          modelResolution,
          userMessageId: messageId,
        },
      ))
    } else {
      dispatch({
        assistantMessageId,
        createdAt,
        modelResolution,
        threadId,
        type: 'startChatAssistantResponse',
        userMessageId: messageId,
      })
    }
    setChatErrorByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setChatNoticeByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })

    const controller = new AbortController()
    chatControllerByThreadIdRef.current.set(threadId, controller)
    chatStreamContentByThreadIdRef.current.set(threadId, '')
    setActiveChatRequestsByThreadId((current) => ({
      ...current,
      [threadId]: {
        assistantMessageId,
        phase: 'submitted',
        threadId,
      },
    }))

    await runChatAssistantRequest({
      assistantMessageId,
      controller,
      modelTier,
      model: explicitModel,
      effort: explicitEffort,
      requestMessages,
      stack: stackDiscoveryStatus === 'available' ? state.ui.selectedStack : undefined,
      threadId,
      useStreaming,
    })
  }

  async function handleRetryAssistantMessage(
    threadId: string,
    assistantMessageId: string,
    mode: ChatRetryMode,
    options: ChatRetryOptions = {},
  ) {
    if (!isDemoMode && !authUnlocked) return

    const selectedThread = threadId === INCOGNITO_THREAD_ID
      ? incognitoThread
      : state.chatThreads[threadId]
    const retryTarget = selectedThread
      ? findAssistantRetryTarget(selectedThread.messages, assistantMessageId)
      : null
    if (!selectedThread || !retryTarget || chatControllerByThreadIdRef.current.has(threadId)) return
    if (chatControllerByThreadIdRef.current.size >= MAX_PARALLEL_CHAT_REQUESTS) {
      setChatNoticeByThreadId((current) => ({
        ...current,
        [threadId]: t.chat.parallelLimitReached,
      }))
      return
    }

    const hasRetryModelOverride = (
      Object.prototype.hasOwnProperty.call(options, 'model')
      || Object.prototype.hasOwnProperty.call(options, 'modelTier')
      || Object.prototype.hasOwnProperty.call(options, 'effort')
    )
    const modelTier = hasRetryModelOverride
      ? options.modelTier ?? null
      : state.ui.selectedChatModelTier
    const explicitModel = hasRetryModelOverride
      ? options.model ?? null
      : state.ui.selectedChatModel
    const explicitEffort = hasRetryModelOverride
      ? options.effort ?? null
      : state.ui.selectedChatEffort
    const modelResolution = explicitModel
      ? explicitModelResolution(explicitModel, explicitEffort)
      : chatMessageModelResolutionForTier(
          chatModelOptionsState,
          defaultChatModel,
          modelTier,
        )
    const knowledgeCollectionIds = retryTarget.assistantMessage.requestContext?.knowledgeCollectionIds ?? []
    const requestContext = chatRequestContextForKnowledge(knowledgeCollectionIds)
    const nextAssistantMessageId = createClientId('msg')
    const createdAt = new Date().toISOString()

    if (threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => retryAssistantResponseInThread(
        current,
        {
          assistantMessageId: nextAssistantMessageId,
          createdAt,
          modelResolution,
          requestContext,
          replacedAssistantMessageId: assistantMessageId,
        },
      ))
    } else {
      dispatch({
        assistantMessageId: nextAssistantMessageId,
        createdAt,
        modelResolution,
        requestContext,
        replacedAssistantMessageId: assistantMessageId,
        threadId,
        type: 'startChatAssistantRetry',
      })
    }
    setChatErrorByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setChatNoticeByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })

    const controller = new AbortController()
    chatControllerByThreadIdRef.current.set(threadId, controller)
    chatStreamContentByThreadIdRef.current.set(threadId, '')
    setActiveChatRequestsByThreadId((current) => ({
      ...current,
      [threadId]: {
        assistantMessageId: nextAssistantMessageId,
        phase: 'submitted',
        threadId,
      },
    }))

    await runChatAssistantRequest({
      assistantMessageId: nextAssistantMessageId,
      controller,
      effort: explicitEffort,
      knowledgeCollectionIds,
      model: explicitModel,
      modelTier,
      requestMessages: buildChatRetryMessages(retryTarget, mode),
      stack: stackDiscoveryStatus === 'available' ? state.ui.selectedStack : undefined,
      threadId,
      useStreaming: chatStreamingEnabled,
    })
  }

  function handleStopChatGeneration() {
    if (!activeChatThreadId) return
    chatControllerByThreadIdRef.current.get(activeChatThreadId)?.abort()
  }

  function setChatAssistantMessageContent(
    threadId: string,
    assistantMessageId: string,
    contentMarkdown: string,
    modelResolution?: ChatMessageModelResolutionRecord,
    chainTrace?: ChatChainStepRecord[],
  ) {
    if (threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => setThreadAssistantMessageContent(
        current,
        assistantMessageId,
        contentMarkdown,
        modelResolution,
        chainTrace,
      ))
      return
    }

    dispatch({
      assistantMessageId,
      chainTrace,
      contentMarkdown,
      modelResolution,
      threadId,
      type: 'setChatAssistantMessageContent',
    })
  }

  function discardChatRequestRuntime(threadId: string) {
    chatControllerByThreadIdRef.current.get(threadId)?.abort()
    chatControllerByThreadIdRef.current.delete(threadId)
    chatStreamContentByThreadIdRef.current.delete(threadId)
    scheduledChatContentByThreadIdRef.current.delete(threadId)
    const frame = chatFlushFrameByThreadIdRef.current.get(threadId)
    if (frame !== undefined) {
      window.cancelAnimationFrame(frame)
      chatFlushFrameByThreadIdRef.current.delete(threadId)
    }
  }

  function abortAllChatRequests() {
    for (const threadId of Array.from(chatControllerByThreadIdRef.current.keys())) {
      discardChatRequestRuntime(threadId)
    }
  }

  function handleCreateChatThread(groupId?: string | null) {
    if (groupId) {
      if (isIncognitoChat && chatControllerByThreadIdRef.current.has(INCOGNITO_THREAD_ID)) return
      if (isIncognitoChat) {
        setIsIncognitoChat(false)
        resetIncognitoSession()
      }
      dispatch({
        groupId,
        modelTier: chatModelTier || null,
        preview: t.chat.empty,
        title: t.chat.new,
        type: 'createChatThread',
      })
      return
    }

    if (isIncognitoChat) {
      if (chatControllerByThreadIdRef.current.has(INCOGNITO_THREAD_ID)) return
      setChatErrorByThreadId((current) => {
        const next = { ...current }
        delete next[INCOGNITO_THREAD_ID]
        return next
      })
      setChatNoticeByThreadId((current) => {
        const next = { ...current }
        delete next[INCOGNITO_THREAD_ID]
        return next
      })
      resetIncognitoSession()
      dispatch({ type: 'clearChatDraftAttachment' })
      return
    }

    dispatch({
      modelTier: chatModelTier || null,
      preview: t.chat.empty,
      title: t.chat.new,
      type: 'createChatThread',
    })
  }

  function handleClearChatThread() {
    const threadId = isIncognitoChat ? INCOGNITO_THREAD_ID : state.ui.selectedChatThreadId
    if (threadId && chatControllerByThreadIdRef.current.has(threadId)) return
    if (isIncognitoChat) {
      resetIncognitoSession()
      setChatErrorByThreadId((current) => {
        const next = { ...current }
        delete next[INCOGNITO_THREAD_ID]
        return next
      })
      setChatNoticeByThreadId((current) => {
        const next = { ...current }
        delete next[INCOGNITO_THREAD_ID]
        return next
      })
      dispatch({ type: 'clearChatDraftAttachment' })
      return
    }
    if (!threadId) return
    clearScrollMemory(`chat:${threadId}`)
    dispatch({ emptyPreview: t.chat.empty, threadId, type: 'clearChatThread' })
    setChatErrorByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setChatNoticeByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
  }

  // Start a fresh incognito session: blank thread plus discard the local-only
  // attachments (assets + draft refs). Called on every enter/leave/switch/load
  // so no incognito file survives into another session.
  function resetIncognitoSession() {
    setIncognitoThread(createIncognitoThread(t.chat.incognitoTitle, t.chat.incognitoPreview))
    setIncognitoAssets({})
    setIncognitoAttachmentRefs([])
    clearScrollMemory('chat:incognito')
  }

  function handleIncognitoChange(enabled: boolean) {
    if (activeChatThreadId && chatControllerByThreadIdRef.current.has(activeChatThreadId)) return
    setIsIncognitoChat(enabled)
    resetIncognitoSession()
    dispatch({ type: 'clearChatDraftAttachment' })
  }

  function handleSelectChatThread(threadId: string) {
    if (isIncognitoChat && chatControllerByThreadIdRef.current.has(INCOGNITO_THREAD_ID)) return
    if (isIncognitoChat) {
      setIsIncognitoChat(false)
      resetIncognitoSession()
    }
    dispatch({ threadId, type: 'selectChatThread' })
  }

  function handleDeleteChatThread(threadId: string) {
    discardChatRequestRuntime(threadId)
    clearScrollMemory(`chat:${threadId}`)
    // The removal itself belongs to the history API: it deletes on the
    // server first and only then drops the row, so a failed deletion stays
    // visible instead of reappearing at the next reload.
    void deleteChatThreadOnServer(threadId)
    setChatErrorByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setChatNoticeByThreadId((current) => {
      const next = { ...current }
      delete next[threadId]
      return next
    })
    setActiveChatRequestsByThreadId((current) => removeChatRequest(current, threadId))
  }

  function handleDeleteChatMessages(threadId: string, messageIds: string[]) {
    if (messageIds.length === 0 || chatControllerByThreadIdRef.current.has(threadId)) return
    if (threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => removeMessagesFromThread(current, messageIds))
      return
    }

    dispatch({
      emptyPreview: t.chat.empty,
      messageIds,
      threadId,
      type: 'deleteChatMessages',
    })
  }

  function handleEditChatMessage(threadId: string, messageId: string, contentMarkdown: string) {
    if (chatControllerByThreadIdRef.current.has(threadId)) return
    if (threadId === INCOGNITO_THREAD_ID) {
      setIncognitoThread((current) => editUserMessageInThread(current, messageId, contentMarkdown))
      return
    }

    dispatch({ contentMarkdown, messageId, threadId, type: 'editChatUserMessage' })
  }

  function handleBranchChatThreadFromMessage(threadId: string, messageId: string) {
    if (threadId === INCOGNITO_THREAD_ID || chatControllerByThreadIdRef.current.has(threadId)) return
    dispatch({ messageId, threadId, type: 'branchChatThreadFromMessage' })
  }

  function handleCreateChatThreadGroup() {
    dispatch({ title: t.chat.newGroupTitle, type: 'createChatThreadGroup' })
  }

  async function handleAuthUnlock(token: string) {
    const trimmedToken = token.trim()
    if (!trimmedToken) {
      setAuthLockError(t.authLock.tokenRequired)
      return
    }

    setIsAuthSubmitting(true)
    setAuthLockError(null)
    try {
      await listResearchRuns({ apiKey: trimmedToken, workspaceId: state.workspaceId })
      setApiKey(trimmedToken)
      setApiKeyDraft(trimmedToken)
    } catch (error) {
      setAuthLockError(hasHttpStatus(error, 401) || hasHttpStatus(error, 403)
        ? t.authLock.tokenRejected
        : t.authLock.tokenCheckFailed)
    } finally {
      setIsAuthSubmitting(false)
    }
  }

  function handleSettingsApiKeyChange(nextApiKey: string) {
    setApiKeyDraft(nextApiKey)
    setApiKey(nextApiKey.trim())
    setAuthLockError(null)
  }

  async function handleCredentialLogin() {
    const identifier = authIdentifier.trim()
    if (!identifier || !authPassword) {
      setAuthLockError(t.authLock.credentialRequired)
      return
    }
    setIsAuthSubmitting(true)
    setAuthLockError(null)
    try {
      const credentials = { identifier, password: authPassword }
      await loginLocal(credentials)
      setAuthPassword('')
      // A document reload is the only account-switch boundary that also
      // discards every reducer, draft, cache, and live stream.
      reloadApplication()
    } catch (error) {
      setAuthLockError(
        hasHttpStatus(error, 429)
          ? t.authLock.credentialRateLimited
          : hasHttpStatus(error, 401) || hasHttpStatus(error, 403)
            ? t.authLock.credentialRejected
            : t.authLock.credentialCheckFailed,
      )
    } finally {
      setIsAuthSubmitting(false)
    }
  }

  function applyProjectPreferences(preferences: ProjectPreferences) {
    setAgentMemoryEnabled(preferences.agentMemoryEnabled)
    setAgentModelTier(preferences.agentModelTier)
    setChatModelTier(preferences.chatModelTier)
    setContrastMode(preferences.contrastMode)
    setLocale(preferences.locale)
    setTheme(preferences.theme)
    setThemePreset(preferences.themePreset)
    setUserBubbleTone(preferences.userBubbleTone)
  }

  async function handleCancelJob(runId: string) {
    const run = state.researchRuns[runId]
    if (!run || (run.status !== 'running' && run.status !== 'queued')) return

    setCancelErrorByRunId((current) => {
      const next = { ...current }
      delete next[runId]
      return next
    })

    if (isDemoMode || run.source !== 'api') {
      dispatch({ runId, type: 'cancelLocalRun' })
      return
    }

    setCancelSubmittingRunIds((current) => new Set(current).add(runId))
    try {
      await cancelRun(runId)
    } catch (error) {
      setCancelErrorByRunId((current) => ({
        ...current,
        [runId]: messageFromError(error),
      }))
    } finally {
      setCancelSubmittingRunIds((current) => {
        const next = new Set(current)
        next.delete(runId)
        return next
      })
    }
  }

  async function handleDeleteJob(runId: string) {
    const run = state.researchRuns[runId]
    if (!run) return
    // API and imported runs are durable on the server; deleting them locally
    // alone lets a reload re-hydrate them from the store (the "deleted report
    // comes back" bug). Mock/demo runs and the no-server case have no durable
    // record, so they delete locally only. The server delete must succeed
    // BEFORE the local removal — except a 404, which deleteRun treats as
    // already-gone, so it resolves and the local removal proceeds.
    const serverBacked = !isDemoMode
      && (run.source === 'api' || run.source === 'imported')
      && discoveryReady
      && authUnlocked
    if (!serverBacked) {
      dispatch({ jobId: runId, type: 'deleteJob' })
      return
    }
    try {
      // cancelIfActive: deleting an active run cancels it first and waits
      // (bounded) for the terminal transition before deleting.
      await deleteRun(runId, { cancelIfActive: true })
      dispatch({ jobId: runId, type: 'deleteJob' })
    } catch (error) {
      if (error instanceof RunStillCancellingError) {
        // The cancel is accepted but the worker has not stopped within the
        // bounded wait: keep the run visible (it carries the "cancelling"
        // badge) and explain on the card why the delete has to be retried.
        setCancelErrorByRunId((current) => ({
          ...current,
          [runId]: t.runCard.deletePendingCancel,
        }))
        return
      }
      // Other real failures are surfaced via runError -> the apiError
      // banner; keep the run in the list so it stays visible.
    }
  }

  return (
    <QuotaMeterProvider demo={isDemoMode} enabled={Boolean(quotaMeterEnabled)}>
    <main
      className="flex h-svh flex-col overflow-hidden bg-canvas text-foreground"
      // While the lock screen covers the app, the shell behind it must be
      // unreachable for keyboard and assistive technology — not merely
      // invisible. `inert` is the native, single-source way to do that;
      // a JS focus trap would still leave the shell in the a11y tree.
      inert={isAuthLocked}
    >
      <Topbar
        activeView={state.ui.activeView}
        canPersistProject={projectPersistenceAvailable}
        dirty={state.dirty}
        importPending={projectImportPending}
        isProjectActionPending={projectAction !== null}
        onDismissProjectActionError={() => setProjectActionError(null)}
        onExportProject={() => void handleExportProject()}
        onImportProjectToServer={() => void handleImportProjectToServer()}
        onLoadProject={() => void handleLoadProject()}
        onSaveProject={() => void handleSaveProject()}
        projectActionError={projectActionError}
        projectConnection={state.connection}
        projectName={state.project.name}
        serverSyncEnabled={state.serverSyncEnabled}
        serverSyncError={serverSyncError}
      />
      {discoveryError && (
        <details className="border-b border-warning/30 bg-warning-subtle px-4 py-2 t-meta text-warning" role="status">
          <summary className="cursor-pointer">{t.topbar.discoveryRetry}</summary>
          <p className="mt-1 break-words">{discoveryError}</p>
        </details>
      )}
      <div className="flex min-h-0 w-full flex-1 overflow-hidden">
        <AppRail
          activeView={state.ui.activeView}
          onViewChange={(view) => {
            setSettingsRequestedSection(null)
            dispatch({ type: 'setActiveView', view })
          }}
          settingsBadgeCount={sharingInbox.pendingCount}
          showAgent={agentWorkspaceVisible}
          showKnowledge={knowledgeWorkspaceVisible}
          profileSlot={
            <ProfileAvatar
              authMode={authMode}
              isDemo={isDemoMode}
              session={authSession}
              onLogin={ssoLogin}
              onLogout={() => void handleLogout()}
              onOpenSecuritySettings={() => {
                setSettingsRequestedSection('security')
                dispatch({ type: 'setActiveView', view: 'settings' })
              }}
            />
          }
        />
        <div className="min-h-0 min-w-0 flex-1">
          {/* The deferred key keeps workspace lifecycle boundaries explicit.
              Cached desks swap without mount motion; only genuinely cold,
              bounded regions may use StructuralLoadBoundary. */}
          <div
            className="flex h-full min-h-0 w-full min-w-0 flex-col"
            key={deferredActiveView}
          >
          {deferredActiveView === 'research' ? (
            <ResearchWorkspace
              activeFilter={state.ui.activeFilter}
              allJobs={allJobs}
              authenticatedUserId={authSession.status === 'authenticated'
                ? authSession.user?.id ?? null
                : null}
              expandedJobId={state.ui.expandedJobId}
              isComposerVisible={state.ui.isComposerVisible}
              isDesktop={isDesktop}
              isReportVisible={state.ui.isReportVisible}
              isSubmitDisabled={isAuthResolving}
              jobs={visibleJobs}
              // Silhouette instead of the empty hero while the SERVER listing
              // is pending: mirrors useResearchRunApi's canList gate, so demo
              // and local-first (which never list) keep the hero.
              runsLoading={!isDemoMode && discoveryReady && authUnlocked && !runsHydrated}
              cancelErrorByRunId={cancelErrorByRunId}
              cancelSubmittingRunIds={cancelSubmittingRunIds}
              onActiveFilterChange={(filter) => dispatch({ filter, type: 'setActiveFilter' })}
              onAuthenticationRequired={handleResearchAuthenticationRequired}
              onComposerSubmit={handleComposerSubmit}
              researchQuestion={researchQuestion}
              onResearchQuestionChange={setResearchQuestion}
              onComposerVisibleChange={(isVisible) => dispatch({
                isVisible,
                type: 'setComposerVisible',
              })}
              onDeleteJob={(jobId) => void handleDeleteJob(jobId)}
              onCancelJob={(jobId) => void handleCancelJob(jobId)}
              onReportVisibleChange={handleReportVisibleChange}
              onReportPanelSizeChange={(size) => dispatch({
                key: 'researchReport',
                size,
                type: 'setPanelLayoutSize',
              })}
              onSelectJob={(jobId) => dispatch({ jobId, type: 'selectJob' })}
              onToggleJob={(jobId) => dispatch({ jobId, type: 'toggleJob' })}
              onSetReportAutocomplete={handleSetReportAutocomplete}
              onRetryReportResult={(runId) => void retryRunResult(runId)}
              onUseReportInChat={handleUseReportInChat}
              onShareJob={sharingEnabled
                ? (jobId) => setShareTarget({
                  resourceId: jobId,
                  resourceType: 'run',
                  title: state.researchRuns[jobId]?.summary.title ?? '',
                })
                : undefined}
              reduceMotion={reduceMotion}
              reportPanelSize={state.ui.panelLayout.researchReport}
              selectedJobId={state.ui.selectedJobId}
              selectedRun={selectedRun}
              selectedStack={displayedSelectedStack}
              shareCountByRunId={shareCountByRunId}
            />
          ) : deferredActiveView === 'chat' ? (
            <ChatWorkspace
              activeAssistantMessageId={activeChatRequest?.assistantMessageId ?? null}
              chatModelOptions={chatModelOptions}
              chatModelOptionsStatus={chatModelOptionsState.status}
              chatHistorySections={chatHistorySections}
              chatHistoryHasMore={chatHistoryHasMore}
              chatHistoryLoadingMore={chatHistoryLoadingMore}
              onLoadMoreChatHistory={loadMoreChatHistory}
              onPrefetchThread={(threadId) => void prefetchThreadMessages(threadId)}
              isHistoryPending={chatHistoryPending}
              isMessagesLoading={chatMessagesLoading}
              messagesLoadError={chatMessagesLoadError}
              onRetryMessagesLoad={() => void retrySelectedThreadMessages()}
              defaultChatModel={defaultChatModel}
              historyPanelSize={state.ui.panelLayout.chatHistory}
              isDesktop={isDesktop}
              isHistoryVisible={state.ui.isChatHistoryVisible}
              isIncognito={isIncognitoChat}
              isSending={activeChatRequest !== null}
              onAttachContext={(ref) => {
                // Incognito pins the ref locally (it references existing library
                // data, but the reducer's pending refs are ignored in incognito);
                // the normal path stores it in the reducer draft.
                if (isIncognitoChat) {
                  setIncognitoAttachmentRefs((current) =>
                    current.some((existing) => chatContextRefKey(existing) === chatContextRefKey(ref))
                      ? current
                      : [...current, ref],
                  )
                } else {
                  dispatch({ ref, type: 'attachChatContextToDraft' })
                }
                // Prefetch the file body in the background so it is already in
                // hand when the message is sent (load-on-use). The shared
                // loader projects loading/failure into the chip; the send
                // guard awaits the same de-duplicated request.
                void ensureAssetBodiesLoaded(assetIdsFromChatRefs(chatResolveState, [ref])).catch(() => {})
              }}
              onAttachFiles={(files) => void handleAttachChatFiles(files)}
              onPillRefsChange={setChatPillRefs}
              chatDraft={chatDraft}
              onChatDraftChange={setChatDraft}
              onAnswerLastUserMessage={(threadId, messageId) => void handleAnswerLastUserMessage(threadId, messageId)}
              onClearThread={handleClearChatThread}
              onCreateThread={handleCreateChatThread}
              onCreateThreadGroup={handleCreateChatThreadGroup}
              onBranchFromMessage={handleBranchChatThreadFromMessage}
              onDeleteMessages={handleDeleteChatMessages}
              onDeleteThreadGroup={(groupId) => dispatch({ groupId, type: 'deleteChatThreadGroup' })}
              onDeleteThread={handleDeleteChatThread}
              onEditMessage={handleEditChatMessage}
              onRetryAssistantMessage={(threadId, messageId, mode, options) =>
                void handleRetryAssistantMessage(threadId, messageId, mode, options)}
              chainingEnabled={state.ui.chatChainingEnabled}
              onChainingEnabledChange={(enabled) => dispatch({ enabled, type: 'setChatChainingEnabled' })}
              onIncognitoChange={handleIncognitoChange}
              onHistoryPanelSizeChange={(size) => dispatch({
                key: 'chatHistory',
                size,
                type: 'setPanelLayoutSize',
              })}
              onHistoryVisibleChange={(isVisible) => dispatch({
                isVisible,
                type: 'setChatHistoryVisible',
              })}
              onOpenPromptLibrary={() => dispatch({ type: 'setActiveView', view: 'prompt-library' })}
              onRenameThread={(threadId, title) => dispatch({ threadId, title, type: 'renameChatThread' })}
              onRenameThreadGroup={(groupId, title) => dispatch({ groupId, title, type: 'renameChatThreadGroup' })}
              onMoveThreadGroup={(groupId, targetIndex) => dispatch({
                groupId,
                targetIndex,
                type: 'moveChatThreadGroup',
              })}
              onMoveThreadToGroup={(threadId, groupId, targetIndex) => dispatch({
                groupId,
                targetIndex,
                threadId,
                type: 'moveChatThreadToGroup',
              })}
              onAdoptVisibleOrder={(itemIds, folderIds) => dispatch({
                desk: 'chat',
                folderIds,
                itemIds,
                type: 'adoptExplorerOrder',
              })}
              onChangeSortMode={(mode) => dispatch({ desk: 'chat', mode, type: 'setExplorerSortMode' })}
              sortMode={state.ui.explorerSort.chat}
              onRemoveContext={(ref) => {
                if (isIncognitoChat) {
                  setIncognitoAttachmentRefs((current) =>
                    current.filter((existing) => chatContextRefKey(existing) !== chatContextRefKey(ref)),
                  )
                } else {
                  dispatch({ ref, type: 'removeChatContextFromDraft' })
                }
              }}
              onRetryAttachment={retryAttachmentUpload}
              onReorderContext={(fromIndex, toIndex) => {
                if (isIncognitoChat) {
                  setIncognitoAttachmentRefs((current) => moveItem(current, fromIndex, toIndex))
                } else {
                  dispatch({ fromIndex, toIndex, type: 'reorderChatContextInDraft' })
                }
              }}
              pendingReorderKeys={pendingChatRefs.map(chatContextRefKey)}
              pillKeys={chatPillRefs.map(chatContextRefKey)}
              onSendMessage={handleChatMessageSubmit}
              onSelectThread={handleSelectChatThread}
              onTogglePinnedThread={(threadId) => dispatch({ threadId, type: 'togglePinnedChatThread' })}
              onSelectedModelTierChange={(tier) => dispatch({ tier, type: 'setSelectedChatModelTier' })}
              chatModelCatalog={chatModelCatalog}
              selectedChatModel={state.ui.selectedChatModel}
              selectedChatEffort={state.ui.selectedChatEffort}
              onSelectedChatModelChange={(model) => dispatch({ model, type: 'setSelectedChatModel' })}
              onSelectedChatEffortChange={(effort) => dispatch({ effort, type: 'setSelectedChatEffort' })}
              chatContextBase={chatContextBase}
              chatContextCapacity={chatContextCapacity}
              knowledgeIndexOptions={knowledgeIndexOptions}
              selectedKnowledgeIndexIds={selectedKnowledgeIndexIds}
              onSelectedKnowledgeIndexIdsChange={setSelectedKnowledgeIndexIds}
              onStopGenerating={handleStopChatGeneration}
              onStreamingEnabledChange={setChatStreamingEnabled}
              attachmentBudgetNotice={attachmentBudgetNotice}
              pendingChips={pendingChips}
              pinnedThreadIds={state.ui.pinnedExplorer.chatThreadIds}
              reduceMotion={reduceMotion}
              requestError={activeChatThreadId
                ? chatErrorByThreadId[activeChatThreadId] ?? null
                : null}
              requestNotice={activeChatThreadId
                ? chatNoticeByThreadId[activeChatThreadId] ?? null
                : null}
              fileGroupOptions={fileGroupOptions}
              fileOptions={fileOptions}
              reportOptions={reportOptions}
              runningThreadIds={runningChatThreadIds}
              ruleOptions={ruleOptions}
              selectedModelTier={state.ui.selectedChatModelTier}
              selectedThreadId={state.ui.selectedChatThreadId}
              streamingEnabled={chatStreamingEnabled}
              temporaryThread={incognitoThread}
              textImprovement={{
                apiKey: apiKey.trim() || undefined,
                enabled: canImproveText,
                selectedStack: textImprovementStack,
                workspaceId: effectiveWorkspaceId,
              }}
              threads={chatThreads}
            />
          ) : deferredActiveView === 'editor' ? (
            <EditorWorkspace
              apiKey={apiKey.trim() || undefined}
              assetBodyLoadStates={assetBodyLoadStates}
              capabilities={capabilities}
              chatModelOptions={chatModelOptions}
              chatModelOptionsStatus={chatModelOptionsState.status}
              chatModelCatalog={chatModelCatalog}
              defaultChatModel={defaultChatModel}
              dispatch={dispatch}
              documentBodyHydration={selectedDocumentBodyHydration}
              ensureAssetBodiesLoaded={ensureAssetBodiesLoaded}
              ensureUploadTarget={projectPersistenceAvailable ? ensureUploadTarget : undefined}
              onCollaborationControllerChange={handleCollaborationControllerChange}
              onFlushDocumentForShare={projectSyncActive ? flushDocumentForShare : undefined}
              onRecoveryCaptureProviderChange={setEditorRecoveryCaptureProvider}
              onShareDocument={sharingEnabled ? handleShareEditorDocument : undefined}
              onServerDocumentObserved={registerOpenedServerDocument}
              prefetchDocumentBody={prefetchDocumentBody}
              reportOptions={reportOptions}
              selectedModelTier={state.ui.selectedChatModelTier}
              serverFileUpload={serverFileUpload}
              serverParserAvailable={serverParserAvailable}
              onRetryAttachment={retryAttachmentUpload}
              state={state}
              textImprovement={{
                apiKey: apiKey.trim() || undefined,
                enabled: canImproveText,
                selectedStack: textImprovementStack,
                workspaceId: effectiveWorkspaceId,
              }}
              workspaceId={effectiveWorkspaceId}
              uploadRegistry={uploadRegistry}
            />
          ) : deferredActiveView === 'agent' ? (
            <AgentWorkspace
              apiKey={apiKey.trim() || undefined}
              cancelRun={cancelRun}
              submitError={runError}
              pollingRunIds={pollingRunIds}
              runsHydrated={runsHydrated}
              canvasPanelSize={state.ui.panelLayout.agentCanvas}
              capabilities={capabilities}
              collections={agentCollectionOptions}
              dispatch={dispatch}
              knowledgeDataSource={knowledgeDataSource}
              documents={agentDocumentOptions}
              composerDraft={agentComposerDraft}
              onComposerDraftChange={setAgentComposerDraft}
              draftQuestion={agentDraftQuestion}
              memoryEnabled={agentMemoryEnabled}
              skillsApi={skillsEnabled ? skillsApi : null}
              onAutonomyChange={setAgentAutonomy}
              onDepthChange={setAgentDepth}
              modelCatalog={chatModelCatalog}
              modelOptions={chatModelOptions}
              modelOptionsStatus={chatModelOptionsState.status}
              defaultChatModel={defaultChatModel}
              onCanvasPanelSizeChange={(size) =>
                dispatch({ key: 'agentCanvas', size, type: 'setPanelLayoutSize' })}
              onDraftQuestionChange={setAgentDraftQuestion}
              onEditorDocumentsChanged={requestResourceRefresh}
              onSelectedCollectionIdsChange={setAgentCollectionIds}
              onSelectedDocumentIdChange={setAgentDocumentId}
              onSessionsPanelSizeChange={(size) =>
                dispatch({ key: 'agentSessions', size, type: 'setPanelLayoutSize' })}
              selectedAutonomy={effectiveAgentAutonomy}
              selectedDepth={effectiveDepth}
              selectedTier={agentTier}
              onTierChange={setAgentTier}
              selectedCollectionIds={agentCollectionIds}
              selectedDocumentId={agentDocumentId}
              serverEnabled={!isDemoMode}
              titleStack={stackDiscoveryStatus === 'available' ? state.ui.selectedStack : undefined}
              demo={agentDemo}
              state={state}
              submitRun={(request) => submitRun(request)}
              workspaceId={effectiveWorkspaceId}
            />
          ) : deferredActiveView === 'knowledge' ? (
            <KnowledgeWorkspace
              collections={knowledgeCollections}
              composerNotice={displayedKnowledgeAskError}
              dataSource={knowledgeDataSource}
              defaultProfileId={knowledgeDefaultProfileId}
              defaultTopK={knowledgeDefaultTopK}
              evidenceKMax={knowledgeEvidenceKMax}
              rerankerProvider={knowledgeRerankerProvider}
              historyItems={knowledgeAllItems}
              historyPanelSize={state.ui.panelLayout.knowledgeHistory}
              isAskDisabled={!isDemoMode && !authUnlocked}
              isAskRunning={isKnowledgeAskRunning}
              isHistoryVisible={state.ui.isKnowledgeHistoryVisible}
              isIncognito={isIncognitoKnowledge}
              isItemsLoading={knowledgeItemsLoading}
              items={displayedKnowledgeItems}
              sessionSections={knowledgeSessionSections}
              sessions={knowledgeSessions}
              mode={knowledgeMode}
              onClearSession={clearKnowledgeAskSession}
              onCreateSession={(groupId) => {
                const session = createKnowledgeSession()
                dispatch({ session, type: 'createKnowledgeSession' })
                if (groupId) {
                  dispatch({ groupId, sessionId: session.id, targetIndex: 0, type: 'moveKnowledgeSessionToGroup' })
                }
              }}
              onCreateSessionGroup={() => dispatch({ title: t.knowledge.newFolder, type: 'createKnowledgeSessionGroup' })}
              onDeleteSessionGroup={(groupId) => dispatch({ groupId, type: 'deleteKnowledgeSessionGroup' })}
              onDeleteSession={deleteKnowledgeAskSession}
              onRetrySessionDeletion={(sessionId) => {
                void retryKnowledgeSessionDeletion(sessionId)
              }}
              onDeleteItems={deleteKnowledgeAskItems}
              onDemoAsk={isDemoMode ? handleKnowledgeDemoAsk : undefined}
              onHistoryPanelSizeChange={(size) => dispatch({
                key: 'knowledgeHistory',
                size,
                type: 'setPanelLayoutSize',
              })}
              onHistoryVisibleChange={(isVisible) => dispatch({ isVisible, type: 'setKnowledgeHistoryVisible' })}
              onIncognitoChange={setKnowledgeIncognito}
              onOpenDatabase={() => dispatch({ type: 'setActiveView', view: 'database' })}
              onAsk={(question, options) => void handleKnowledgeAsk(question, options)}
              knowledgeQuestion={knowledgeQuestion}
              onKnowledgeQuestionChange={setKnowledgeQuestion}
              onModeChange={setKnowledgeMode}
              onMoveSessionGroup={(groupId, targetIndex) => dispatch({ groupId, targetIndex, type: 'moveKnowledgeSessionGroup' })}
              onMoveSessionToGroup={(sessionId, groupId, targetIndex) =>
                dispatch({ groupId, sessionId, targetIndex, type: 'moveKnowledgeSessionToGroup' })}
              onAdoptVisibleOrder={(itemIds, folderIds) => dispatch({
                desk: 'knowledge',
                folderIds,
                itemIds,
                type: 'adoptExplorerOrder',
              })}
              onChangeSortMode={(mode) => dispatch({ desk: 'knowledge', mode, type: 'setExplorerSortMode' })}
              sortMode={state.ui.explorerSort.knowledge}
              onPrefetchSession={(sessionId) => void prefetchSessionItems(sessionId)}
              onProfileChange={setKnowledgeProfileId}
              onRenameSessionGroup={(groupId, title) => dispatch({ groupId, title, type: 'renameKnowledgeSessionGroup' })}
              onRenameSession={(sessionId, title) => dispatch({ sessionId, title, type: 'renameKnowledgeSession' })}
              onSelectSession={(sessionId) => dispatch({ sessionId, type: 'selectKnowledgeSession' })}
              onStopAsk={() => void handleStopKnowledgeAsk()}
              onTogglePinnedSession={(sessionId) => dispatch({ sessionId, type: 'togglePinnedKnowledgeSession' })}
              onSelectedCollectionIdsChange={setKnowledgeCollectionIds}
              onTopKChange={setKnowledgeTopK}
              onFinalKChange={setKnowledgeFinalK}
              onSourcePanelSizeChange={(size) => dispatch({
                key: 'knowledgeSource',
                size,
                type: 'setPanelLayoutSize',
              })}
              profileId={knowledgeProfileId}
              profileOptions={knowledgeProfileOptions}
              pinnedSessionIds={state.ui.pinnedExplorer.knowledgeSessionIds}
              selectedCollectionIds={knowledgeCollectionIds}
              selectedSessionId={state.selectedKnowledgeSessionId}
              sourcePanelSize={state.ui.panelLayout.knowledgeSource}
              topK={knowledgeTopK}
              finalK={knowledgeFinalK}
            />
          ) : deferredActiveView === 'prompt-library' ? (
            <PromptLibraryWorkspace
              dispatch={dispatch}
              onRequestedResourceHandled={() => setSharedOpenTarget(null)}
              requestedResource={sharedOpenTarget?.resource_type === 'prompt_template'
                || sharedOpenTarget?.resource_type === 'skill_template'
                ? {
                  resourceId: sharedOpenTarget.resource_id,
                  resourceType: sharedOpenTarget.resource_type,
                }
                : null}
              skillsApi={skillsEnabled ? skillsApi : null}
              sharing={sharingEnabled
                ? {
                  onShareRule: (rule) => {
                    if (!rule.serverTemplateId) return
                    setShareTarget({
                      resourceId: rule.serverTemplateId,
                      resourceType: 'prompt_template',
                      title: rule.title,
                    })
                  },
                  onShareSkill: (skill) => {
                    if (skill.access.mode !== 'owner') return
                    setShareTarget({
                      resourceId: skill.id,
                      resourceType: 'skill_template',
                      title: skill.title,
                    })
                  },
                }
                : null}
              state={state}
              templateSync={templateSync}
              textImprovement={{
                apiKey: apiKey.trim() || undefined,
                enabled: canImproveText,
                selectedStack: textImprovementStack,
                workspaceId: effectiveWorkspaceId,
              }}
            />
          ) : deferredActiveView === 'database' ? (
            <FileLibraryWorkspace
              assetDeletionApiOptions={projectSyncActive ? fileApiOptions : null}
              deletionRefreshToken={deletionOperationsRefreshToken}
              deletionScopeKey={[
                effectiveWorkspaceId,
                state.projectEpoch,
                fileApiOptions?.baseUrl ?? '',
                authSession.user?.id ?? authMode,
              ].join('\u001f')}
              dispatch={dispatch}
              embedModels={embedCatalog}
              ensureAssetBodiesLoaded={ensureAssetBodiesLoaded}
              ensureUploadTarget={projectPersistenceAvailable ? ensureUploadTarget : undefined}
              fileApiOptions={fileApiOptions}
              knowledgeSync={knowledgeSyncOptions}
              knowledgeUnavailableReason={knowledgeUnavailableReason}
              onRefreshServerCollections={knowledgeCollectionsApi.refresh}
              onVectorIndexServerDeleted={acknowledgeVectorIndexServerDeletion}
              onShareServerCollection={sharingEnabled
                ? (collection) => {
                  if (collection.access.mode !== 'owner') return
                  setShareTarget({
                    resourceId: collection.id,
                    resourceType: 'knowledge_collection',
                    title: collection.name,
                  })
                }
                : undefined}
              serverCollections={knowledgeCollectionsApi.collections}
              serverCollectionsLoaded={knowledgeCollectionsApi.loaded}
              serverCollectionsRefreshToken={knowledgeRefreshToken}
              contextualRetrievalEnabled={
                isDemoMode || !capabilities
                  ? null
                  : capabilities.features.contextual_retrieval
              }
              serverFeatureLabels={serverFeatureLabels}
              serverFileUpload={serverFileUpload}
              serverParserAvailable={serverParserAvailable}
              retryServerUpload={retryServerAssetUpload}
              uploadRegistry={uploadRegistry}
              state={state}
            />
          ) : (
            <SettingsWorkspace
              apiCapabilities={capabilities}
              apiError={apiError}
              apiHealth={health}
              apiKey={apiKeyDraft}
              authMode={authMode}
              authSession={authSession}
              patAvailable={authConfig?.pat_available}
              onSsoLogin={ssoLogin}
              onSsoLogout={() => void handleLogout()}
              logoutError={logoutError}
              isDemoMode={isDemoMode}
              onApiKeyChange={handleSettingsApiKeyChange}
              onDemoModeChange={(enabled) => dispatch({ enabled, type: 'setDemoMode' })}
              onStackChange={(stack) => dispatch({ stack, type: 'setSelectedStack' })}
              selectedStack={displayedSelectedStack}
              requestedSection={settingsRequestedSection}
              sharing={sharingEnabled ? sharingInbox : null}
              sharingRefreshToken={sharingListRefreshToken}
              onOpenSharedResource={openSharedResource}
              stackDiscoveryStatus={stackDiscoveryStatus}
              stackOptions={effectiveStackOptions}
            />
          )}
          </div>
        </div>
      </div>
      {sharingEnabled && shareTarget && (
        <ShareDialog
          {...(shareTarget.collaborationGeneration !== undefined
            ? { collaborationGeneration: shareTarget.collaborationGeneration }
            : {})}
          demo={isDemoMode}
          initialTab={shareTarget.intent === 'details' ? 'overview' : 'access'}
          guestLinksEnabled={
            capabilities?.features.editor_guest_links === true
              && shareTarget.document?.access?.mode !== 'shared'
          }
          documentDetails={shareTarget.documentDetails}
          onLeave={incomingShareTarget
            ? async () => {
                await sharingInbox.drop(incomingShareTarget.id)
                requestResourceRefresh()
              }
            : undefined}
          onChanged={() => {
            requestResourceRefresh()
          }}
          onClose={() => setShareTarget(null)}
          ownerEmail={isDemoMode ? DEMO_OWNER.email : authSession.user?.email ?? null}
          ownerName={isDemoMode ? DEMO_OWNER.displayName : authSession.user?.displayName ?? null}
          recipientAccess={shareTarget.document?.access?.mode === 'shared'
            ? {
                ownerId: shareTarget.document.access.owner?.id
                  ?? incomingShareTarget?.granted_by_user_id
                  ?? '',
                ownerName: shareTarget.document.access.owner?.name
                  ?? incomingShareTarget?.granted_by_display_name
                  ?? (locale === 'de' ? 'Unbekannter Eigentümer' : 'Unknown owner'),
                permission: shareTarget.document.access.permission,
              }
            : undefined}
          refreshToken={shareDialogRefreshToken}
          resourceId={shareTarget.resourceId}
          resourceTitle={shareTarget.title}
          resourceType={shareTarget.resourceType}
          returnFocusTarget={shareTarget.returnFocusTarget}
        />
      )}
    </main>
    {/* Sibling of the shell, never a child: the shell carries `inert`
        while locked, and a nested lock screen would inert ITSELF —
        leaving the user with an unusable sign-in form. */}
    {isAuthLocked && (
      <AuthLockScreen
        error={authLockError}
        identifier={authIdentifier}
        isSubmitting={isAuthSubmitting}
        mode={
          authMode === 'oidc'
            ? 'sso'
            : authMode === 'local'
              ? 'local'
              : 'apikey'
        }
        onCredentialSubmit={() => void handleCredentialLogin()}
        onIdentifierChange={(value) => {
          setAuthIdentifier(value)
          setAuthLockError(null)
        }}
        onPasswordChange={(value) => {
          setAuthPassword(value)
          setAuthLockError(null)
        }}
        onSsoLogin={ssoLogin}
        providerName={authConfig?.provider_name}
        onSubmit={(token) => void handleAuthUnlock(token)}
        onTokenChange={(token) => {
          setApiKeyDraft(token)
          setAuthLockError(null)
        }}
        password={authPassword}
        reduceMotion={reduceMotion}
        token={apiKeyDraft}
      />
    )}
    </QuotaMeterProvider>
  )
}

function createIncognitoThread(title: string, preview: string): ChatThreadRecord {
  const now = new Date().toISOString()
  return {
    createdAt: now,
    id: INCOGNITO_THREAD_ID,
    messages: [],
    preview,
    source: 'api',
    title,
    updatedAt: now,
  }
}

function knowledgeRunIdsFromThreadItems(items: readonly KnowledgeThreadItemRecord[]) {
  return items.flatMap((item) => (item.runId ? [item.runId] : []))
}

function appendChatExchangeToThread(
  thread: ChatThreadRecord,
  options: {
    assistantMessageId: string
    attachments: ChatMessageAttachmentRecord[]
    contentMarkdown: string
    createdAt: string
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    userMessageId: string
  },
): ChatThreadRecord {
  const userMessage: ChatMessageRecord = {
    attachments: options.attachments.length > 0 ? options.attachments : undefined,
    contentMarkdown: options.contentMarkdown,
    createdAt: options.createdAt,
    id: options.userMessageId,
    role: 'user',
  }
  const assistantMessage: ChatMessageRecord = {
    contentMarkdown: '',
    createdAt: options.createdAt,
    id: options.assistantMessageId,
    modelResolution: options.modelResolution,
    requestContext: options.requestContext,
    role: 'assistant',
  }

  return {
    ...thread,
    messages: [...thread.messages, userMessage, assistantMessage],
    preview: options.contentMarkdown,
    title: thread.messages.some((message) => message.role === 'user')
      ? thread.title
      : titleFromChatMessage(options.contentMarkdown),
    updatedAt: options.createdAt,
  }
}

function appendAssistantResponseToLastUserMessage(
  thread: ChatThreadRecord,
  options: {
    assistantMessageId: string
    createdAt: string
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    userMessageId: string
  },
): ChatThreadRecord {
  const lastMessage = thread.messages[thread.messages.length - 1]
  if (!lastMessage || lastMessage.id !== options.userMessageId || lastMessage.role !== 'user') {
    return thread
  }

  return threadWithMessages(
    thread,
    [
      ...thread.messages,
      {
        contentMarkdown: '',
        createdAt: options.createdAt,
        id: options.assistantMessageId,
        modelResolution: options.modelResolution,
        requestContext: options.requestContext,
        role: 'assistant',
      },
    ],
  )
}

function retryAssistantResponseInThread(
  thread: ChatThreadRecord,
  options: {
    assistantMessageId: string
    createdAt: string
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    replacedAssistantMessageId: string
  },
): ChatThreadRecord {
  const assistantIndex = thread.messages.findIndex((message) => message.id === options.replacedAssistantMessageId)
  const assistantMessage = assistantIndex >= 0 ? thread.messages[assistantIndex] : undefined
  const userMessage = assistantIndex > 0 ? thread.messages[assistantIndex - 1] : undefined
  if (
    !assistantMessage
    || assistantMessage.role !== 'assistant'
    || !userMessage
    || userMessage.role !== 'user'
    || !userMessage.contentMarkdown.trim()
  ) {
    return thread
  }

  return {
    ...threadWithMessages(
      thread,
      [
        ...thread.messages.slice(0, assistantIndex),
        {
          contentMarkdown: '',
          createdAt: options.createdAt,
          id: options.assistantMessageId,
          modelResolution: options.modelResolution,
          requestContext: options.requestContext,
          role: 'assistant',
        },
      ],
    ),
    updatedAt: options.createdAt,
  }
}

function setThreadAssistantMessageContent(
  thread: ChatThreadRecord,
  assistantMessageId: string,
  contentMarkdown: string,
  modelResolution?: ChatMessageModelResolutionRecord,
  chainTrace?: ChatChainStepRecord[],
): ChatThreadRecord {
  return {
    ...thread,
    messages: thread.messages.map((message) => (
      message.id === assistantMessageId
        ? {
          ...message,
          chainTrace: chainTrace ?? message.chainTrace,
          contentMarkdown,
          modelResolution: modelResolution ?? message.modelResolution,
        }
        : message
    )),
    updatedAt: new Date().toISOString(),
  }
}

function removeMessagesFromThread(
  thread: ChatThreadRecord,
  messageIds: readonly string[],
): ChatThreadRecord {
  const messageIdSet = new Set(messageIds)
  const messages = thread.messages.filter((message) => !messageIdSet.has(message.id))
  if (messages.length === thread.messages.length) return thread
  return threadWithMessages(thread, messages)
}

function editUserMessageInThread(
  thread: ChatThreadRecord,
  messageId: string,
  contentMarkdown: string,
): ChatThreadRecord {
  const nextContent = contentMarkdown.trim()
  const currentMessage = thread.messages.find((message) => message.id === messageId)
  if (!nextContent || !currentMessage || currentMessage.role !== 'user') return thread
  if (currentMessage.contentMarkdown === nextContent) return thread

  const messages = thread.messages.map((message) => (
    message.id === messageId
      ? { ...message, contentMarkdown: nextContent }
      : message
  ))
  const autoTitle = titleFromChatMessage(currentMessage.contentMarkdown)
  const nextTitle = thread.title === autoTitle
    ? titleFromChatMessage(nextContent)
    : thread.title

  return {
    ...threadWithMessages(thread, messages),
    title: nextTitle,
  }
}

function threadWithMessages(
  thread: ChatThreadRecord,
  messages: ChatMessageRecord[],
): ChatThreadRecord {
  return {
    ...thread,
    messages,
    preview: chatPreviewFromMessages(messages) ?? thread.preview,
    updatedAt: new Date().toISOString(),
  }
}

function chatPreviewFromMessages(messages: readonly ChatMessageRecord[]) {
  return [...messages].reverse().find((message) => message.role === 'user')?.contentMarkdown
}

function titleFromChatMessage(contentMarkdown: string) {
  return contentMarkdown
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 72) || 'Incognito chat'
}

function projectStateWithPreferences(
  state: ReturnType<typeof initializeResearchDeskState>,
  preferences: ProjectPreferences,
) {
  return {
    ...state,
    preferences,
  }
}

async function prepareCollaborationProjectExport(
  state: ProjectState,
  clientOptions: ClientOptions,
  activeController: {
    controller: CollaborationProjectionController
    documentId: string
  } | null,
): Promise<{ staleDocuments: ProjectState['editorDocuments'][string][]; state: ProjectState }> {
  const editorDocuments = { ...state.editorDocuments }
  const staleDocuments: ProjectState['editorDocuments'][string][] = []
  for (const documentId of state.editorDocumentOrder) {
    const document = state.editorDocuments[documentId]
    if (
      !document
      || document.contentMode !== 'collaboration'
      || document.access?.mode === 'shared'
    ) continue
    try {
      const localController = activeController?.documentId === document.id
        ? activeController.controller
        : null
      const projection = await flushCollaborationProjectionBarrier({
        clientOptions,
        controller: localController,
        documentId: document.id,
        generation: document.collaboration?.generation,
        // Require the local barrier exactly when a controller exists to satisfy
        // it. The persisted UI selection is not evidence of a mounted editor:
        // exporting from any other view leaves the last-opened document with a
        // requirement nothing can meet, and the whole export fails there.
        requireLocal: localController !== null,
      })
      editorDocuments[document.id] = {
        ...document,
        collaboration: document.collaboration
          ? {
              ...document.collaboration,
              persistedSequence: projection.sequence,
              projectionSequence: projection.sequence,
              projectionUpdatedAt: projection.confirmedAt,
            }
          : document.collaboration,
        contentMarkdown: projection.markdown,
      }
    } catch (error) {
      console.warn('Collaboration projection flush failed before project export.', error)
      if (!confirmedProjectionFallback(document)) throw error
      staleDocuments.push(document)
    }
  }
  return {
    staleDocuments,
    state: { ...state, editorDocuments },
  }
}

function confirmStaleCollaborationExport(
  documents: ProjectState['editorDocuments'][string][],
  locale: 'de' | 'en',
): boolean {
  const details = documents
    .map((document) => {
      const timestamp = document.collaboration!.projectionUpdatedAt!
      return `${document.title} (${new Date(timestamp).toLocaleString(locale)})`
    })
    .join('\n')
  const message = locale === 'de'
    ? `Die Live-Version konnte nicht abgerufen werden. Letzten gespeicherten Stand exportieren?\n\n${details}`
    : `The live version could not be retrieved. Export the last saved state?\n\n${details}`
  return window.confirm(message)
}

function buildChatMessages(
  history: ChatMessageRecord[],
  contentMarkdown: string,
  attachments: ChatMessageAttachmentRecord[],
): ChatCompletionMessage[] {
  const messages = history
    .filter((message) => message.contentMarkdown.trim().length > 0)
    .slice(-20)
    .map((message) => ({
      content: message.contentMarkdown,
      role: message.role,
    }))

  messages.push({
    content: attachments.length > 0
      ? contentWithAttachmentContext(contentMarkdown, attachments)
      : contentMarkdown,
    role: 'user',
  })

  return messages
}

/**
 * Build the request messages for one prompt-chaining step: apply a template's
 * instruction to the running text. Thread history and attached sources are only
 * supplied for the first step (the others transform the previous step's result).
 */
function buildChainStepMessages(
  history: ChatMessageRecord[],
  instruction: string,
  runningText: string,
  sources: ChatMessageAttachmentRecord[],
): ChatCompletionMessage[] {
  const messages = history
    .filter((message) => message.contentMarkdown.trim().length > 0)
    .slice(-20)
    .map((message) => ({ content: message.contentMarkdown, role: message.role }))
  const subject = sources.length > 0
    ? contentWithAttachmentContext(runningText, sources)
    : runningText
  messages.push({
    content: `${instruction}\n\n---\n${subject}`,
    role: 'user',
  })
  return messages
}

function directChatModelResolution(
  nodeModels: Record<string, NodeModelResolution> | undefined,
): NodeModelResolution | null {
  if (!nodeModels) return null
  return nodeModels.direct_chat ?? null
}

function resolveChatModelOptions(
  health: InqtrixHealth | null,
  selectedStackName: string | null,
  stacks: InqtrixStack[],
): ChatModelOptionsState {
  const selectedStackModels = selectedStackName
    ? stacks.find((stack) => stack.name === selectedStackName)?.models
    : undefined
  const options = selectedStackName
    ? selectedStackModels?.chat_model_options
    : health?.chat_model_options
  if (!options || options.length === 0) {
    return { options: [], status: 'missing' }
  }
  const hasEveryTier = chatModelTierOrder.every((tier) => (
    options.some((option) => option.tier === tier)
  ))
  if (!hasEveryTier) {
    return { options, status: 'missing' }
  }
  if (options.some((option) => !option.model?.trim())) {
    return { options, status: 'unresolved' }
  }
  return { options, status: 'available' }
}

function resolveModelCatalog(
  health: InqtrixHealth | null,
  selectedStackName: string | null,
  stacks: InqtrixStack[],
): ModelCatalogEntry[] {
  const selectedStackModels = selectedStackName
    ? stacks.find((stack) => stack.name === selectedStackName)?.models
    : undefined
  const catalog = selectedStackName
    ? selectedStackModels?.models_catalog
    : health?.models_catalog
  return catalog ?? []
}

function resolveDefaultChatModel(
  health: InqtrixHealth | null,
  selectedStackName: string | null,
  stacks: InqtrixStack[],
): NodeModelResolution | null {
  const selectedStackModels = selectedStackName
    ? stacks.find((stack) => stack.name === selectedStackName)?.models
    : undefined
  if (selectedStackName) {
    return directChatModelResolution(selectedStackModels?.node_models)
  }
  return directChatModelResolution(health?.node_models)
}

const chatModelTierOrder: ChatModelTier[] = ['high', 'mid', 'fast']

// Picker-selection -> overrides slice: the shared
// modelOverridesFromSelection (features/researchRuns/modelSelection.ts)
// is the single implementation; this alias keeps the call sites short.
const chatAgentOverrides = modelOverridesFromSelection

function chatRequestContextForKnowledge(
  knowledgeCollectionIds: readonly string[],
): ChatMessageRequestContextRecord | undefined {
  const ids = knowledgeCollectionIds.filter((id) => id.trim().length > 0)
  return ids.length > 0 ? { knowledgeCollectionIds: ids } : undefined
}

function explicitModelResolution(
  model: string,
  effort: string | null,
): ChatMessageModelResolutionRecord {
  return {
    effort: effort ?? '',
    effortSource: effort ? 'explicit_request' : '',
    model,
    modelSource: 'explicit_request',
    requestedTier: '',
    tier: '',
  }
}

function chatMessageModelResolutionForTier(
  optionsState: ChatModelOptionsState,
  defaultModel: NodeModelResolution | null,
  selectedTier: ChatModelTier | null,
): ChatMessageModelResolutionRecord | undefined {
  if (!selectedTier) {
    return normalizeChatMessageModelResolution(defaultModel)
  }
  if (optionsState.status !== 'available') return undefined
  return normalizeChatMessageModelResolution(
    optionsState.options.find((option) => option.tier === selectedTier),
  )
}

function normalizeChatMessageModelResolution(
  resolution: NodeModelResolution | null | undefined,
): ChatMessageModelResolutionRecord | undefined {
  if (!resolution) return undefined
  const model = resolution.model?.trim()
  if (!model) return undefined
  return {
    effort: resolution.effort?.trim() ?? '',
    effortSource: resolution.effort_source?.trim() ?? '',
    model,
    modelSource: resolution.model_source?.trim() ?? '',
    requestedTier: resolution.requested_tier?.trim() ?? '',
    tier: resolution.tier?.trim() ?? '',
  }
}

function resolveSingleStackLabel(health: InqtrixHealth | null) {
  if (!health) return 'Server default'
  const providerParts = [
    health.llm.provider,
    health.search.provider,
  ].filter(Boolean)
  const providerLabel = providerParts.length > 0
    ? providerParts.join(' + ')
    : 'Server default'
  return health.reasoning_model
    ? `${providerLabel} · ${health.reasoning_model}`
    : providerLabel
}

function stoppedChatContent(partialContent: string, stoppedLabel: string) {
  const trimmed = partialContent.trim()
  if (!trimmed) return stoppedLabel
  return `${partialContent.trimEnd()}\n\n_${stoppedLabel}_`
}

function logProjectActionError(error: unknown) {
  if (error instanceof DOMException && error.name === 'AbortError') return
  console.warn('Inqtrix project action failed.', error)
}
