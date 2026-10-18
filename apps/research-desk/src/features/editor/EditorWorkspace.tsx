import {
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  type ReactNode,
} from 'react'
import type { Editor } from '@tiptap/react'
import { createSecureUuid, EDITOR_SCHEMA_VERSION } from '@inqtrix/editor-schema'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import {
  StructuralLoadBoundary,
  type StructuralLoadPhase,
  type StructuralVisibilityChange,
  useStructuralRenderBlocker,
} from '@/motion/StructuralLoadBoundary'
import { Skeleton } from '@/components/ui/skeleton'
import {
  buildLoginUrl,
  enableEditorDocumentCollaboration,
} from '@/api/inqtrixClient'
import {
  AlertTriangle,
  Anchor,
  Bold,
  BookOpen,
  Check,
  Code2,
  Eye,
  FileDown,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Info,
  LoaderCircle,
  LockKeyhole,
  MessageSquareText,
  MessagesSquare,
  MoreHorizontal,
  PanelBottomClose,
  PanelBottomOpen,
  Paperclip,
  PencilLine,
  Pin,
  PinOff,
  Redo2,
  RefreshCw,
  SendHorizontal,
  Scale,
  Sparkles,
  Share2,
  SquarePen,
  Trash2,
  Undo2,
  Users,
  X,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { AnimatedPanelBody, AnimatedResizableHandle } from '@/components/ui/animated-panel'
import { useAnimatedResizablePanelCollapse } from '@/components/ui/animated-panel-motion'
import { ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { PanelToggle } from '@/components/ui/panel-toggle'
import { ResponsiveSidePanel } from '@/components/ui/responsive-side-panel'
import { WelcomeState } from '@/components/ui/welcome-state'
import { ComposerDisclosureHint } from '@/features/composer/ComposerDisclosureHint'
import { ComposerIconButton } from '@/features/composer/ComposerIconButton'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  EXPLORER_SECTION_REVEAL_STEP,
  ExplorerFolderRow,
  ExplorerFolderToggle,
  ExplorerHistoryRow,
  ExplorerHistoryTitleInput,
  ExplorerRevealControls,
  ExplorerRunningIndicator,
  ExplorerSearchField,
  ExplorerSectionLabel,
  isExplorerActionTarget,
  isPastExplorerDragThreshold,
} from '@/components/ui/explorer-list'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ComposerStopButton } from '@/features/composer/ComposerStopButton'
import {
  assetIdsFromChatRefs,
  chatAttachmentChipsFromRefs,
  chatAttachmentsFromRefs,
  chatContextRefKey,
  chatRuleOptions,
  dedupeChatContextRefs,
  displayRelativeAge,
  editorCommentsForDocument,
  fileGroupMentionOptions,
  fileMentionOptions,
  projectEditorDocuments,
  projectEditorFolders,
  projectFileAssets,
  selectedEditorDocument,
  type ChatAttachmentChipModel,
  type ChatRuleOption,
  type CompletedReportOption,
  type FileGroupMentionOption,
  type FileMentionOption,
} from '@/features/project/selectors'
import type {
  ChatModelOption,
  ModelCatalogEntry,
  ChatModelTier,
  InqtrixCapabilities,
  NodeModelResolution,
} from '@/features/researchRuns/types'
import { ModelTierPicker } from '@/features/researchRuns/ModelTierPicker'
import { ContextTokenMeter } from '@/features/composer/ContextTokenMeter'
import { QuotaMeter } from '@/features/quota/QuotaMeter'
import { QuotaUsageFooter } from '@/features/quota/QuotaUsageFooter'
import {
  buildContextTokenModel,
  estimateTokensFromText,
  type ContextCategoryInput,
} from '@/features/files/contextTokens'
import type {
  ChatContextReferenceRecord,
  FileAssetBodyLoadState,
  EditorCommentAnchorRecord,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorDocumentRecoveryReason,
  FileAssetRecord,
  EditorFolderRecord,
  EditorSuggestionRecord,
  ProjectState,
} from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { sharePermissionLabel } from '@/features/sharing/shareModel'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { ExplorerSortMenu } from '@/components/ui/explorer-sort-menu'
import { orderPinnedExplorerItems, sortExplorerFolders, sortExplorerItems } from '@/features/project/explorerSort'
import type { ExplorerSortMode } from '@/features/project/explorerSort'
import { useMediaQuery } from '@/features/researchDesk/hooks/useMediaQuery'
import { cn } from '@/lib/utils'
import {
  MarkdownEditorSurface,
  collaborationSurfaceAcceptsLocalEdits,
  isCollaborationSurfaceBound,
  isCollaborationSurfaceSynced,
} from './core/MarkdownEditorSurface'
import {
  canRunEditorHistoryCommand,
  runEditorHistoryCommand,
} from './core/editorHistoryCommands'
import {
  editorForSurfaceIdentity,
  updateEditorSurfaceRegistration,
  type EditorSurfaceIdentity,
  type EditorSurfaceRegistration,
} from './core/editorSurfaceRegistration'
import { isAuthoredComment } from './commentKinds'
import { editorCopy, type EditorCopy } from './editorCopy'
import { escapeCssIdentifier } from './editorDom'
import { suggestionDiffPlan } from './suggestionDiff'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import {
  TextImproveButton,
  TextImproveFloatingLayer,
  useTextImprovement,
  type TextImprovementApiOptions,
} from '@/features/textImprove'
import {
  handedOverDiscussions,
  useHandedOverDiscussions,
  type HandedOverDiscussion,
} from './assistantHandoff'
import { EditorDocumentChangesSection } from './EditorDocumentChangesSection'
import { EditorInstructionFeedbackCard } from './EditorInstructionFeedbackCard'
import { SuggestionErrorLine } from './SuggestionErrorLine'
import {
  beginEditorCollaborationAuthorityGuard,
  editorCollaborationActionDisabledReason,
  useEditorSuggestions,
  type CollaborationSuggestionPublication,
  type EditorInstructionFeedback,
} from './useEditorSuggestions'
import {
  type MentionCategoryLabels,
  type MentionSources,
} from '@/features/composer/mention'
import { ContextChipLegend } from '@/features/composer/ContextChipLegend'
import { MentionComposer, type MentionComposerHandle } from '@/features/composer/MentionComposer'
import { improvedMentionText, type LabelResolver } from '@/features/composer/mentionDoc'
import { moveItem } from '@/features/composer/reorder'
import { Dropzone } from '@/features/files/Dropzone'
import {
  createFileAssetPlaceholders,
  runFileIngestPipeline,
  type FileUploadRegistry,
  type ServerFileUpload,
  uploadBindingForRecord,
} from '@/features/files/ingest'
import { createDefaultFileParser } from '@/features/files/parsing'
import { temporaryFileSectionId } from '@/features/files/sections'
import { COLLABORATION_NOTICES } from './collaborationNotices'
import {
  CollaborationEditRefusalLine,
  CollaborationRecoveryCopyNotice,
  CollaborationRejectionBar,
  downloadCollaborationBackup,
  replacedGenerationRecoveryCopy,
} from './CollaborationNoticeBars'
import {
  collaborationDocumentLifecycleHasUnconfirmedChanges,
  retireCollaborationDocumentLifecycle,
  useCollaborationDocument,
} from './useCollaborationDocument'
import {
  clearCollaborationCommentDrafts,
  useCollaborationComments,
} from './useCollaborationComments'
import {
  EditorCollaborationStatus,
  EditorInspector,
  EditorWriteModeControl,
} from './inspector/EditorInspector'
import { EditorTopBarLayout } from './inspector/EditorTopBarLayout'
import {
  CollaborationActivationDialog,
  collaborationActivationConflictError,
  collaborationActivationFallbackError,
} from './inspector/CollaborationActivationDialog'
import {
  useInspectorCollaborationActivity,
  useInspectorCollaborationSnapshot,
  useInspectorOpenSuggestionIds,
} from './inspector/collaborationAdapter'
import {
  applyCollaborationEditorPolicy,
  collaborationEditorPolicyUpdate,
  type CollaborationEditorPolicyInput,
} from './inspector/editorPolicy'
import {
  resolveEditorCollaborationAnchor,
  resolveCollaborationCommentAnchorWithStatus,
  serializeEditorCollaborationAnchor,
  serializeCollaborationCommentAnchor,
} from './inspector/relativeAnchors'
import { TeamCommentsPanel } from './inspector/TeamCommentsPanel'
import {
  effectiveEditorWriteMode,
  filterInspectorChanges,
  buildEditorCollaborationStatusModel,
  COLLABORATION_CHANGES_UNREADABLE_COPY,
  localizeCollaborationEditRefusal,
  localizeEditorCollaborationNotice,
  beginCollaborationPublicationFocus,
  consumeCollaborationPublicationFocus,
  isCollaborationPublicationFocusCurrent,
  pendingCollaborationPublicationFocusForDocument,
  registerCollaborationPublicationFocus,
  editorDocumentRailCapabilities,
  editorDocumentRowKind,
  isOwnedEditorDocument,
  partitionEditorDocumentsByAccess,
  type EditorChangesView,
  type EditorCollaborationStatusModel,
  type EditorInspectorTab,
  type InspectorHistoryFilters,
  type InspectorOpenFilters,
} from './inspector/model'
import {
  serializeEditorFinalProjectionMarkdown,
  serializeEditorMarkdown,
  type CollaborationReviewDisplay,
} from './tiptap'
import {
  collaborationProjectionController,
  confirmedProjectionFallback,
  decideCollaborationPatchesAfterBarrier,
  flushCollaborationProjectionBarrier,
  type CollaborationProjectionController,
} from './collaborationProjection'
import { persistCollaborationDiffAnchor } from './collaborationDiffAnchor'
import { resolveMaterializedAnchor } from './anchoring'
import type {
  EditorDocumentBodyHydration,
  EditorDocumentPushOutcome,
} from './useEditorHistoryApi'
import { readerError } from '@/api/requestErrorText'

type EditorWorkspaceProps = {
  apiKey?: string
  /** Server capability manifest; forwarded to useEditorSuggestions so the
   * client editor-run abort tracks the server wait. */
  capabilities: InqtrixCapabilities | null
  chatModelOptions: ChatModelOption[]
  chatModelOptionsStatus: 'available' | 'missing' | 'unresolved'
  chatModelCatalog?: ModelCatalogEntry[]
  defaultChatModel: NodeModelResolution | null
  dispatch: Dispatch<ResearchDeskAction>
  /** Authoritative body state from the persistence owner. Tiptap readiness is
   * combined with it locally before the structural boundary may publish. */
  documentBodyHydration: EditorDocumentBodyHydration
  /** Shared, idempotent body loader used by pointer/focus intent and open. */
  prefetchDocumentBody?: (
    documentId: string,
    options?: { retry?: boolean },
  ) => Promise<void>
  /** Loads attached file-asset bodies on demand before an AI run reads them,
   * so a body is fetched at use time rather than up front; forwarded to
   * useEditorSuggestions. */
  ensureAssetBodiesLoaded?: (assetIds: readonly string[]) => Promise<Map<string, string>>
  assetBodyLoadStates?: Readonly<Record<string, FileAssetBodyLoadState>>
  /** Persist the target hierarchy before a bound original-file upload. */
  ensureUploadTarget?: (sectionId: string, groupId: string | null) => Promise<boolean>
  /** Shared bound upload service used by chat, editor, and the file library. */
  serverFileUpload?: ServerFileUpload
  /** Session-local File handles shared across upload entry points so changing
   * desks does not remove the explicit retry affordance. */
  uploadRegistry?: FileUploadRegistry
  /** Whether a ready upload can hand parsing to the shared server parser. */
  serverParserAvailable?: boolean
  /** Retry through the root-owned durable UploadOperation/byte registry. */
  onRetryAttachment?: (chip: ChatAttachmentChipModel) => void
  /** Starts the common background server parser after an accepted upload. */
  onAssetsIngested?: (assets: FileAssetRecord[]) => void
  /** Opens the parent-owned global ShareDialog after collaboration is active. */
  onShareDocument?: (
    document: EditorDocumentRecord,
    details: EditorDocumentDetailsSummary,
    /** `'share'` opened the dialog to grant access (it focuses the recipient
     * search); `'details'` only wants to read the document's facts. */
    intent: 'details' | 'share',
    returnFocusTarget?: HTMLElement | null,
  ) => void
  /** Pushes the document's current body to the server before collaboration
   * activation and returns the fresh server revisions (null = a concurrent
   * writer won; retry). With this wired, the share flow does not depend on
   * the GLOBAL project dirty flag — which server-synced deployments never
   * clear, so depending on it would leave the Share button permanently
   * disabled. */
  onFlushDocumentForShare?: (
    documentId: string,
  ) => Promise<EditorDocumentPushOutcome>
  onCollaborationControllerChange?: (
    registration: { controller: CollaborationProjectionController; documentId: string } | null,
  ) => void
  onRecoveryCaptureProviderChange?: (
    provider: EditorRecoveryCaptureProvider | null,
  ) => void
  onServerDocumentObserved?: (
    document: EditorDocumentRecord,
    provenance: 'exact_detail' | 'metadata',
  ) => void
  reportOptions: CompletedReportOption[]
  selectedModelTier: ChatModelTier | null
  state: ProjectState
  textImprovement: Omit<TextImprovementApiOptions, 'locale'>
  /** Canonical authenticated project namespace. This may differ from the
   * browser-local state.workspaceId on a new device or browser profile. */
  workspaceId: string
}

export type EditorDocumentDetailsSummary = {
  createdAt: string
  openCommentCount: number | null
  openSuggestionCount: number | null
  participantCount: number | null
  updatedAt: string
  wordCount: number
}

export type EditorRecoveryCaptureProvider = (
  documentId: string,
) => { contentMarkdown: string } | null

export function editorDocumentStructuralLoadPhase(
  activeDocumentId: string | null,
  hydration: EditorDocumentBodyHydration,
): StructuralLoadPhase {
  if (!activeDocumentId) return 'empty'
  return hydration.documentId === activeDocumentId
    ? hydration.phase
    : 'pending'
}

export type EditorIdentityPublication = {
  displayedIdentity: string
  visible: boolean
}

/** Keeps identity-owned editor chrome on the same committed document as the
 * body boundary. A hidden edge makes the retained chrome inert; only the
 * matching visible edge publishes the requested identity. Late edges from an
 * aborted document switch are ignored. */
export function advanceEditorIdentityPublication(
  current: EditorIdentityPublication,
  requestedIdentity: string,
  change: StructuralVisibilityChange,
): EditorIdentityPublication {
  if (change.identity !== requestedIdentity) return current
  if (!change.visible) {
    return current.visible ? { ...current, visible: false } : current
  }
  if (current.displayedIdentity === change.identity && current.visible) return current
  return { displayedIdentity: change.identity, visible: true }
}

function useEditorIdentityOwnedContent(identity: string, content: ReactNode) {
  const requestedRef = useRef({ content, identity })
  requestedRef.current = { content, identity }
  const displayedContentRef = useRef(content)
  const [publication, setPublication] = useState<EditorIdentityPublication>({
    displayedIdentity: identity,
    visible: false,
  })

  // Updates within the already published document (for example a title save
  // or collaboration status) stay live. A new document's chrome remains in
  // requestedRef until its body has reached the same visibility edge.
  if (publication.displayedIdentity === identity) {
    displayedContentRef.current = content
  }

  const handleVisibilityChange = useCallback((change: StructuralVisibilityChange) => {
    const requested = requestedRef.current
    if (change.identity !== requested.identity) return
    if (change.visible) displayedContentRef.current = requested.content
    setPublication((current) => advanceEditorIdentityPublication(
      current,
      requested.identity,
      change,
    ))
  }, [])

  return {
    content: displayedContentRef.current,
    inert: !publication.visible || publication.displayedIdentity !== identity,
    onVisibilityChange: handleVisibilityChange,
    visibleIdentity: publication.displayedIdentity,
  }
}

type EditorDocumentDropTarget = {
  folderId: string | null
  targetIndex: number
}

const EDITOR_TREE_PANEL_ID = 'editor-file-tree-panel'
const EDITOR_COMMENTS_PANEL_ID = 'editor-comments-panel'
// The center column and the center+comments subtree of the nested resizable
// groups. The tree collapses in the OUTER group, the comments panel in the
// INNER group -- mirroring the Knowledge Desk so the two side panels never
// share one percentage space and fight during independent collapse.
const EDITOR_CENTER_PANEL_ID = 'editor-center-panel'
const EDITOR_CENTER_COMMENTS_PANEL_ID = 'editor-center-comments-panel'

export default function EditorWorkspace({
  apiKey,
  assetBodyLoadStates = {},
  capabilities,
  chatModelOptions,
  chatModelOptionsStatus,
  chatModelCatalog,
  defaultChatModel,
  dispatch,
  documentBodyHydration,
  ensureAssetBodiesLoaded,
  ensureUploadTarget,
  onCollaborationControllerChange,
  onFlushDocumentForShare,
  onRecoveryCaptureProviderChange,
  onShareDocument,
  onServerDocumentObserved,
  prefetchDocumentBody,
  onAssetsIngested,
  onRetryAttachment,
  reportOptions,
  selectedModelTier,
  serverFileUpload,
  serverParserAvailable = false,
  uploadRegistry,
  state,
  textImprovement,
  workspaceId,
}: EditorWorkspaceProps) {
  const { locale } = useLocale()
  const copy = editorCopy[locale]
  const isDesktop = useMediaQuery('(min-width: 1024px)')
  const folders = useMemo(() => projectEditorFolders(state), [state.editorFolderOrder, state.editorFolders])
  const documents = useMemo(() => projectEditorDocuments(state), [state.editorDocumentOrder, state.editorDocuments])
  const activeDocument = selectedEditorDocument(state)
  const collaborationFeatureAvailable =
    capabilities?.features.collaboration === true
  const activeDocumentIdRef = useRef(activeDocument?.id ?? null)
  activeDocumentIdRef.current = activeDocument?.id ?? null
  // Live mirror of the reducer's document map (the useEditorHistoryApi
  // pattern): async handlers re-read the CURRENT record instead of a
  // dialog-open snapshot, which the awaited work may already have superseded.
  const editorDocumentsRef = useRef(state.editorDocuments)
  editorDocumentsRef.current = state.editorDocuments
  const activeSurfaceIdentity = useMemo<EditorSurfaceIdentity | null>(() => (
    activeDocument
      ? {
          documentId: activeDocument.id,
          generation: activeDocument.collaboration?.generation ?? null,
        }
      : null
  ), [activeDocument?.collaboration?.generation, activeDocument?.id])
  const activeSurfaceKey = activeSurfaceIdentity
    ? `${activeSurfaceIdentity.documentId}:${activeSurfaceIdentity.generation ?? 'local'}`
    : 'editor:empty'
  const activeDocumentLoadPhase = editorDocumentStructuralLoadPhase(
    activeDocument?.id ?? null,
    documentBodyHydration,
  )
  const [editorRegistration, setEditorRegistration] = useState<EditorSurfaceRegistration | null>(null)
  const activeEditor = editorForSurfaceIdentity(editorRegistration, activeSurfaceIdentity)
  const handleEditorReady = useCallback((editor: Editor | null) => {
    if (!activeSurfaceIdentity) return
    setEditorRegistration((current) => updateEditorSurfaceRegistration(
      current,
      activeSurfaceIdentity,
      editor,
    ))
  }, [activeSurfaceIdentity])
  const collaborationActive = activeDocument?.contentMode === 'collaboration'
  const collaborationSessionActive =
    collaborationActive && collaborationFeatureAvailable
  const collaboration = useCollaborationDocument({
    active: collaborationSessionActive,
    apiKey,
    document: activeDocument,
    workspaceId,
  })
  const teamComments = useCollaborationComments({
    active: collaborationSessionActive,
    apiKey,
    documentId: activeDocument?.id ?? null,
    eventVersion: collaboration.commentEventVersion,
    generation: activeDocument?.collaboration?.generation ?? null,
    initialRevision: activeDocument?.collaboration?.commentRevision ?? 0,
    locale,
    mentionEventVersion: collaboration.commentMentionEventVersion,
    workspaceId,
  })
  const recoverySnapshotsRef = useRef(new Map<string, {
    commentDrafts: string[]
    documentMarkdown: string
    generation: number
    locale: 'de' | 'en'
    title: string
    workspaceId: string
  }>())
  useEffect(() => {
    const documentId = activeDocument?.id
    const generation = activeDocument?.collaboration?.generation
    if (
      !documentId
      || generation === undefined
      || activeDocument.contentMode !== 'collaboration'
    ) return
    const commentDrafts = Object.values(teamComments.drafts)
      .filter((value) => value.trim().length > 0)
    if (
      !collaboration.hasUnconfirmedLocalChanges
      && commentDrafts.length === 0
    ) {
      recoverySnapshotsRef.current.delete(documentId)
      return
    }
    const liveMarkdown = collaboration.hasUnconfirmedLocalChanges
      ? activeEditor && !activeEditor.isDestroyed
        ? serializeEditorMarkdown(activeEditor)
        : activeDocument.contentMarkdown
      : ''
    recoverySnapshotsRef.current.set(documentId, {
      commentDrafts,
      documentMarkdown: liveMarkdown,
      generation,
      locale,
      title: activeDocument.title,
      workspaceId,
    })
  }, [
    activeDocument,
    activeEditor,
    collaboration,
    locale,
    teamComments.drafts,
    workspaceId,
  ])
  const consumeRecoveryCapture = useCallback<EditorRecoveryCaptureProvider>((
    documentId,
  ) => {
    const snapshot = recoverySnapshotsRef.current.get(documentId)
    if (!snapshot) return null
    recoverySnapshotsRef.current.delete(documentId)
    const hasUnconfirmedDocumentChanges =
      collaborationDocumentLifecycleHasUnconfirmedChanges({
        documentId,
        generation: snapshot.generation,
        workspaceId: snapshot.workspaceId,
      })
    if (
      !hasUnconfirmedDocumentChanges
      && snapshot.commentDrafts.length === 0
    ) return null
    clearCollaborationCommentDrafts({
      documentId,
      generation: snapshot.generation,
      workspaceId: snapshot.workspaceId,
    })
    retireCollaborationDocumentLifecycle({
      documentId,
      generation: snapshot.generation,
      workspaceId: snapshot.workspaceId,
    })
    return {
      contentMarkdown: editorRecoveryMarkdown({
        commentDrafts: snapshot.commentDrafts,
        documentMarkdown: hasUnconfirmedDocumentChanges
          ? snapshot.documentMarkdown
          : '',
        locale: snapshot.locale,
        title: snapshot.title,
      }),
    }
  }, [])
  useEffect(() => {
    onRecoveryCaptureProviderChange?.(consumeRecoveryCapture)
    return () => {
      onRecoveryCaptureProviderChange?.(
        recoverySnapshotsRef.current.size > 0 ? consumeRecoveryCapture : null,
      )
    }
  }, [consumeRecoveryCapture, onRecoveryCaptureProviderChange])
  const collaborationController = useMemo(
    () => collaborationProjectionController(
      collaboration,
      activeDocument?.id ?? null,
      activeDocument?.collaboration?.generation ?? null,
    ),
    [activeDocument?.collaboration?.generation, activeDocument?.id, collaboration],
  )
  const openCollaborationSuggestionIds = useInspectorOpenSuggestionIds(activeEditor)
  const [inspectorTab, setInspectorTab] = useState<EditorInspectorTab>('assistant')
  const [isMobileCommentsOpen, setIsMobileCommentsOpen] = useState(false)
  // The activity feeds the changes tab (its history and the authors of open
  // changes); it is read only while that tab of an open inspector shows it.
  const inspectorActivityVisible = inspectorTab === 'changes'
    && (isDesktop ? state.editorUi.isCommentPanelVisible : isMobileCommentsOpen)
  const collaborationActivity = useInspectorCollaborationActivity({
    active: collaborationSessionActive,
    activityRevision: collaboration.activityRevision,
    apiKey,
    authorityRevision: collaboration.authorityRevision,
    documentId: activeDocument?.id ?? null,
    generation: activeDocument?.collaboration?.generation ?? null,
    lifecycleKey: collaboration.lifecycleKey,
    locale,
    openSuggestionIds: openCollaborationSuggestionIds,
    readAuthority: collaboration.readAuthority,
    visible: inspectorActivityVisible,
    workspaceId,
  })
  const collaborationSnapshot = useInspectorCollaborationSnapshot(
    activeEditor,
    collaboration,
    collaborationActivity.suggestionAuthors,
  )
  const [selectedTeamCommentId, setSelectedTeamCommentId] = useState<string | null>(null)
  const [changesView, setChangesView] = useState<EditorChangesView>('open')
  const [reviewDisplay, setReviewDisplay] = useState<CollaborationReviewDisplay>('simple')
  const [openChangeFilters, setOpenChangeFilters] = useState<InspectorOpenFilters>({
    authorId: null,
    type: null,
  })
  const [historyFilters, setHistoryFilters] = useState<InspectorHistoryFilters>({
    actorId: null,
    type: null,
  })
  const [selectedCollaborationChangeId, setSelectedCollaborationChangeId] = useState<string | null>(null)
  const [requestedWriteModes, setRequestedWriteModes] = useState<
    Record<string, 'comment' | 'edit' | 'suggest'>
  >({})
  const [decisionState, setDecisionState] = useState<{
    documentId: string
    error: string | null
    pending: boolean
  } | null>(null)
  const decisionInFlightRef = useRef(false)
  const [pendingPublicationFocusByDocument, setPendingPublicationFocusByDocument] = useState<
    Record<string, ReturnType<typeof beginCollaborationPublicationFocus>>
  >({})
  const [collaborationAnchorError, setCollaborationAnchorError] = useState<{
    documentId: string
    message: string
  } | null>(null)
  const [collaborationActivation, setCollaborationActivation] = useState<{
    document: EditorDocumentRecord
    error: string | null
    pending: boolean
  } | null>(null)
  const [diffAnchorOperation, setDiffAnchorOperation] = useState<{
    documentId: string
    error: string | null
    pending: boolean
  } | null>(null)
  const requestedWriteMode = activeDocument
    ? requestedWriteModes[activeDocument.id] ?? 'edit'
    : 'edit'
  const permissionWriteMode = effectiveEditorWriteMode(
    collaboration.access,
    collaboration.canEdit,
    requestedWriteMode,
  )
  const editorWriteMode = collaborationActive && state.editorUi.viewMode === 'source'
    ? 'view'
    : permissionWriteMode
  // The review policy gates every local transaction behind the surface. While
  // the bound surface only reconnects, the surface's own editability is the
  // input gate; the policy keeps the permission's mode then.
  const collaborationAcceptsLocalEdits = collaborationSurfaceAcceptsLocalEdits(collaboration)
  const reviewWriteMode = collaborationActive && state.editorUi.viewMode === 'source'
    ? 'view'
    : effectiveEditorWriteMode(
        collaboration.access,
        collaborationAcceptsLocalEdits,
        requestedWriteMode,
      )
  const collaborationDecisionDisabledReason = editorCollaborationActionDisabledReason(
    activeDocument,
    collaboration,
    'decision',
    locale,
  )
  const canDecideCollaborationChanges = collaborationActive
    && collaborationDecisionDisabledReason === null
  const diffAnchorAuthorityDisabledReason = editorCollaborationActionDisabledReason(
    activeDocument,
    collaboration,
    'write',
    locale,
  )
  const filteredCollaborationChanges = useMemo(
    () => filterInspectorChanges(collaborationSnapshot.changes, openChangeFilters),
    [collaborationSnapshot.changes, openChangeFilters],
  )
  // The bound editor holds the live document even while it reconnects; the
  // projection is older than what the editor shows then.
  const collaborationSourceMarkdown = useMemo(
    () => activeEditor && isCollaborationSurfaceBound(collaboration)
      ? serializeEditorFinalProjectionMarkdown(activeEditor)
      : activeDocument?.contentMarkdown ?? '',
    [
      activeDocument?.contentMarkdown,
      activeEditor,
      collaboration.liveDocumentReady,
      collaborationSnapshot,
    ],
  )
  const [isMobileTreeOpen, setIsMobileTreeOpen] = useState(false)

  useEffect(() => {
    setSelectedTeamCommentId(null)
    if (!collaborationActive) setInspectorTab('assistant')
  }, [activeDocument?.id, collaborationActive])

  useEffect(() => {
    const inspectorVisible = isDesktop
      ? state.editorUi.isCommentPanelVisible
      : isMobileCommentsOpen
    if (
      !collaborationActive
      || inspectorTab !== 'comments'
      || !inspectorVisible
      || teamComments.revision <= teamComments.lastReadRevision
    ) return
    const timer = window.setTimeout(() => {
      void teamComments.markRead()
    }, 600)
    return () => window.clearTimeout(timer)
  }, [
    collaborationActive,
    inspectorTab,
    isDesktop,
    isMobileCommentsOpen,
    state.editorUi.isCommentPanelVisible,
    teamComments.lastReadRevision,
    teamComments.markRead,
    teamComments.revision,
  ])

  useEffect(() => {
    if (!onCollaborationControllerChange) return
    if (!collaborationActive || !activeDocument || !collaborationController) {
      onCollaborationControllerChange(null)
      return
    }
    onCollaborationControllerChange({
      controller: collaborationController,
      documentId: activeDocument.id,
    })
    return () => onCollaborationControllerChange(null)
  }, [
    activeDocument?.id,
    collaborationActive,
    collaborationController,
    onCollaborationControllerChange,
  ])
  const comments = useMemo(
    () => editorCommentsForDocument(state, activeDocument?.id ?? null),
    [activeDocument?.id, state.editorComments],
  )
  const surfaceCommentAnchors = useMemo(() => {
    if (!collaborationActive || !activeEditor || !collaboration.document) {
      return { comments, degraded: false }
    }
    const resolved = comments.map((comment) => resolveCollaborationCommentAnchorWithStatus(
      activeEditor,
      collaboration.document!,
      comment,
    ))
    return {
      comments: resolved.map((item) => item.comment),
      degraded: resolved.some((item) => item.status === 'degraded'),
    }
  }, [activeEditor, collaboration.document, collaborationActive, collaborationSnapshot, comments])
  const surfaceTeamComments = useMemo(() => {
    const orphanedThreadIds = new Set<string>()
    const positionByThreadId = new Map<string, number>()
    if (!activeEditor || !collaboration.document) {
      return { orphanedThreadIds, positionByThreadId, threads: teamComments.threads }
    }
    const threads = teamComments.threads.map((thread) => {
      const resolved = resolveEditorCollaborationAnchor(
        activeEditor,
        collaboration.document!,
        thread.anchor as EditorCommentAnchorRecord,
      )
      const materialized = resolveMaterializedAnchor(activeEditor, resolved.anchor)
      if (resolved.status === 'degraded' && !materialized) {
        orphanedThreadIds.add(thread.id)
      }
      if (materialized) {
        positionByThreadId.set(thread.id, materialized.range.from)
      }
      return { ...thread, anchor: resolved.anchor }
    })
    return { orphanedThreadIds, positionByThreadId, threads }
  }, [activeEditor, collaboration.document, teamComments.threads])
  const ruleOptions = useMemo(() => chatRuleOptions(state, 'editor'), [state.chatRuleOrder, state.chatRules])
  const fileOptions = useMemo(() => fileMentionOptions(state), [state.fileAssetOrder, state.fileAssets])
  const fileGroupOptions = useMemo(() => fileGroupMentionOptions(state), [state.fileGroupOrder, state.fileGroups])
  const [attachedCommentIds, setAttachedCommentIds] = useState<string[]>([])
  // Editor attachments come from two sources: positional `[N]` pills owned by the
  // composer (files/groups) and rules/dropped files attached out-of-band. They
  // merge -- pills first -- into one deduplicated ref list, mirroring the chat.
  const [pillRefs, setPillRefs] = useState<ChatContextReferenceRecord[]>([])
  const [extraRefs, setExtraRefs] = useState<ChatContextReferenceRecord[]>([])
  const attachedRefs = useMemo(
    () => dedupeChatContextRefs([...pillRefs, ...extraRefs]),
    [pillRefs, extraRefs],
  )
  // Team threads handed over with "Use with assistant": context for the next
  // request, attached after the composer's own references and built from the
  // thread as it is when the request goes out.
  const {
    clear: clearHandedOverDiscussions,
    handOver: handOverDiscussion,
    remove: removeHandedOverDiscussion,
    threadIds: handedOverThreadIds,
  } = useHandedOverDiscussions(activeDocument?.id ?? null)
  const discussions = useMemo(
    () => handedOverDiscussions(handedOverThreadIds, teamComments.threads, locale),
    [handedOverThreadIds, locale, teamComments.threads],
  )
  const attachmentChips = useMemo(
    () => chatAttachmentChipsFromRefs(
      state,
      attachedRefs,
      { bodyLoadStates: assetBodyLoadStates },
    ),
    [assetBodyLoadStates, state, attachedRefs],
  )
  useEffect(() => {
    if (!ensureAssetBodiesLoaded) return
    const assetIds = assetIdsFromChatRefs(state, attachedRefs)
    if (assetIds.length === 0) return
    // The shared loader owns de-duplication plus visible loading/error state.
    // A prefetch rejection is represented by that state; run admission still
    // awaits the same promise and refuses an empty context.
    void ensureAssetBodiesLoaded(assetIds).catch(() => undefined)
  }, [attachedRefs, ensureAssetBodiesLoaded, state])
  const editorSelectedCard = chatModelCatalog?.find(
    (entry) => entry.model_id === state.ui.selectedChatModel,
  )?.card ?? null
  // Per-category token estimate for the editor composer meter. The whole
  // document is always sent as context, so it is the `conversation` category;
  // the composer draft is added live inside EditorAssistantComposer.
  const editorContextBase = useMemo(() => {
    const attachments = chatAttachmentsFromRefs(state, attachedRefs)
    let documents = 0
    let reports = 0
    let rules = 0
    for (const attachment of attachments) {
      const tokens = estimateTokensFromText(attachment.contentMarkdown ?? '')
      if (attachment.kind === 'research-report') reports += tokens
      else if (attachment.kind === 'chat-rule') rules += tokens
      else documents += tokens
    }
    for (const discussion of discussions) {
      documents += estimateTokensFromText(discussion.document?.content ?? '')
    }
    const conversation = estimateTokensFromText(activeDocument?.contentMarkdown ?? '')
    return { documents, reports, rules, conversation }
  }, [state, attachedRefs, activeDocument, discussions])
  const editorContextCapacity = {
    contextWindowTokens: editorSelectedCard?.context_window_tokens ?? null,
    reservedOutputTokens: editorSelectedCard?.max_output_tokens ?? 0,
  }
  const composerRef = useRef<MentionComposerHandle | null>(null)
  const [isAttachActive, setIsAttachActive] = useState(false)

  const collaborationEditorPolicy = useMemo<CollaborationEditorPolicyInput>(() => ({
      changesView,
      collaboration: collaborationActive,
      display: reviewDisplay,
      documentId: activeDocument?.id ?? null,
      inspectorTab,
      selectedChangeId: selectedCollaborationChangeId,
      visibleChanges: filteredCollaborationChanges,
      writeAuthorId: collaboration.user?.id ?? null,
      writeMode: reviewWriteMode,
  }), [
    activeDocument?.id,
    changesView,
    collaboration.user?.id,
    collaborationActive,
    filteredCollaborationChanges,
    inspectorTab,
    reviewDisplay,
    reviewWriteMode,
    selectedCollaborationChangeId,
  ])

  useLayoutEffect(() => {
    applyCollaborationEditorPolicy(activeEditor, collaborationEditorPolicy)
  }, [
    activeEditor,
    collaborationEditorPolicy,
  ])

  const handleWriteModeChange = useCallback((
    mode: 'comment' | 'edit' | 'suggest',
  ) => {
    if (!activeDocument) return
    applyCollaborationEditorPolicy(activeEditor, {
      ...collaborationEditorPolicy,
      writeMode: effectiveEditorWriteMode(
        collaboration.access,
        collaborationAcceptsLocalEdits,
        mode,
      ),
    })
    setRequestedWriteModes((current) => ({ ...current, [activeDocument.id]: mode }))
  }, [
    activeDocument,
    activeEditor,
    collaboration.access,
    collaborationAcceptsLocalEdits,
    collaborationEditorPolicy,
  ])

  const runCollaborationDecision = useCallback(async (
    decision: 'accept' | 'reject',
    patchIds: string[],
  ): Promise<void> => {
    if (!activeDocument || patchIds.length === 0) {
      throw new Error(locale === 'de'
        ? 'Für diese Änderung ist keine gültige Entscheidung verfügbar.'
        : 'No valid decision is available for this change.')
    }
    const documentId = activeDocument.id
    if (decisionInFlightRef.current) {
      throw new Error(locale === 'de'
        ? 'Eine Änderungsentscheidung wird bereits verarbeitet.'
        : 'A change decision is already in progress.')
    }
    let authorityGuard
    try {
      authorityGuard = beginEditorCollaborationAuthorityGuard(
        activeDocument,
        collaboration,
        'decision',
        locale,
      )
    } catch (error) {
      setDecisionState({
        documentId,
        error: error instanceof Error ? error.message : collaborationDecisionDisabledReason,
        pending: false,
      })
      throw error
    }
    decisionInFlightRef.current = true
    setDecisionState({ documentId, error: null, pending: true })
    try {
      await decideCollaborationPatchesAfterBarrier({
        authorityGuard,
        clientOptions: { apiKey, workspaceId },
        controller: collaborationController,
        decision,
        decisionId: createSecureUuid(),
        documentId,
        generation: activeDocument.collaboration?.generation,
        patchIds,
      })
      setDecisionState({ documentId, error: null, pending: false })
    } catch (error) {
      const decisionError = error instanceof Error
        ? error
        : new Error(locale === 'de'
          ? 'Die Änderung konnte nicht entschieden werden.'
          : 'The change could not be decided.')
      setDecisionState({
        documentId,
        error: decisionError.message,
        pending: false,
      })
      throw decisionError
    } finally {
      decisionInFlightRef.current = false
    }
  }, [
    activeDocument,
    apiKey,
    collaboration,
    collaborationController,
    collaborationDecisionDisabledReason,
    locale,
    workspaceId,
  ])

  const handleCollaborationDecision = useCallback((
    decision: 'accept' | 'reject',
    patchIds: string[],
  ) => {
    void runCollaborationDecision(decision, patchIds).catch(() => undefined)
  }, [runCollaborationDecision])

  const handleCollaborationSuggestionUndo = useCallback(async (
    patchId: string,
  ): Promise<void> => {
    const userId = collaboration.user?.id
    const patch = collaborationSnapshot.changes.find((change) => change.id === patchId)
    if (!userId || !patch || patch.author.id !== userId) {
      throw new Error(locale === 'de'
        ? 'Die letzte eigene Änderung ist nicht mehr als offener Vorschlag verfügbar.'
        : 'The latest local change is no longer available as an open suggestion.')
    }
    await runCollaborationDecision('reject', [patchId])
  }, [
    collaboration.user?.id,
    collaborationSnapshot.changes,
    locale,
    runCollaborationDecision,
  ])

  const handleShareDocument = useCallback((
    document: EditorDocumentRecord,
    intent: 'details' | 'share' = 'share',
    returnFocusTarget?: HTMLElement | null,
  ) => {
    if (!onShareDocument || document.recovery) return
    const isActiveDocument = activeDocument?.id === document.id
    const details: EditorDocumentDetailsSummary = {
      createdAt: document.createdAt,
      openCommentCount: isActiveDocument && document.contentMode === 'collaboration'
        ? teamComments.threads.filter((thread) => thread.status === 'open').length
        : null,
      openSuggestionCount: isActiveDocument && document.contentMode === 'collaboration'
        ? collaborationSnapshot.changes.length
        : null,
      participantCount: isActiveDocument && document.contentMode === 'collaboration'
        ? collaborationSnapshot.participants.length
        : null,
      updatedAt: document.updatedAt,
      wordCount: countEditorDocumentWords(document.contentMarkdown),
    }
    if (!isOwnedEditorDocument(document)) {
      onShareDocument(document, details, intent, returnFocusTarget)
      return
    }
    if (document.contentMode === 'collaboration') {
      onShareDocument(document, details, intent, returnFocusTarget)
      return
    }
    // Direct editor sharing requires an atomic Markdown-to-Yjs conversion.
    // A deployment may still expose sharing for runs/templates while its
    // collaboration module is deliberately disabled or temporarily degraded.
    // Never offer a conversion that can only end in a route-level 404/503.
    if (!collaborationFeatureAvailable) return
    // Without a flush seam the legacy gates apply: the document must already
    // be durably on the server and nothing may be pending. With the seam,
    // the confirm step flushes THIS document itself, so neither the global
    // project dirty flag nor a missing metadataRevision blocks the dialog.
    if (
      !onFlushDocumentForShare
      && (state.dirty || document.metadataRevision === undefined)
    ) {
      return
    }
    setCollaborationActivation({ document, error: null, pending: false })
  }, [
    activeDocument?.id,
    collaborationSnapshot.changes.length,
    collaborationSnapshot.participants.length,
    collaborationFeatureAvailable,
    onFlushDocumentForShare,
    onShareDocument,
    state.dirty,
    teamComments.threads,
  ])

  const handleEnableCollaboration = useCallback(async () => {
    if (!collaborationActivation || !onShareDocument || collaborationActivation.pending) return
    const document = collaborationActivation.document
    setCollaborationActivation((current) => current
      ? { ...current, error: null, pending: true }
      : null)
    try {
      let expectedRevision = document.revision
      let expectedMetadataRevision = document.metadataRevision
      if (onFlushDocumentForShare) {
        // Der Rumpf wird zuerst geschrieben, damit die Umwandlung auf dem
        // aktuellen Inhalt geschieht. Der Flush liest den Record selbst und
        // innerhalb seiner Sperre -- hier wird nur die Id gereicht.
        const outcome = await onFlushDocumentForShare(document.id)
        // 'rebased' ist der EINZIGE Fall, in dem "gleichzeitig geaendert"
        // wahr ist: ein anderer Schreiber hat den Revisionsschutz gewonnen,
        // der lokale Stand wurde nachgezogen, und der naechste Klick setzt
        // darauf auf. Ein einziges `null` fuer alle Ausgaenge wuerde vier
        // Bedeutungen tragen und allen diese Meldung geben -- auch denen, in
        // denen Wiederholen nichts aendert.
        if (outcome.kind === 'rebased') {
          throw new Error(collaborationActivationConflictError(locale))
        }
        if (outcome.kind !== 'saved') {
          throw new Error(collaborationActivationFallbackError(locale))
        }
        expectedRevision = outcome.revision
        expectedMetadataRevision = outcome.metadataRevision
      }
      if (expectedMetadataRevision === undefined) {
        throw new Error(collaborationActivationFallbackError(locale))
      }
      const response = await enableEditorDocumentCollaboration(
        document.id,
        {
          expected_metadata_revision: expectedMetadataRevision,
          expected_revision: expectedRevision,
          schema_version: EDITOR_SCHEMA_VERSION,
        },
        { apiKey, workspaceId },
      )
      // The dialog snapshot is only the ADDRESS — the record is re-read
      // LIVE here: a rebase during the flush has already advanced
      // body/title/updatedAt, and the two observer
      // callbacks below must never republish the pre-rebase snapshot
      // (a stale updatedAt fingerprint would misdescribe the server view
      // that registerOpenedServerDocument records).
      const liveDocument =
        editorDocumentsRef.current[document.id] ?? document
      const activatedDocument: EditorDocumentRecord = {
        ...liveDocument,
        collaboration: {
          commentRevision: 0,
          generation: response.generation,
          persistedSequence: response.persisted_sequence,
          projectionSequence: response.projection_sequence,
          schemaVersion: response.schema_version,
        },
        contentMode: response.content_mode,
        metadataRevision: expectedMetadataRevision + 1,
        revision: expectedRevision,
      }
      dispatch({
        collaboration: activatedDocument.collaboration!,
        documentId: activatedDocument.id,
        metadataRevision: activatedDocument.metadataRevision!,
        type: 'activateEditorDocumentCollaboration',
      })
      onServerDocumentObserved?.(activatedDocument, 'metadata')
      setCollaborationActivation(null)
      onShareDocument(activatedDocument, {
        createdAt: activatedDocument.createdAt,
        openCommentCount: 0,
        openSuggestionCount: 0,
        participantCount: 1,
        updatedAt: activatedDocument.updatedAt,
        wordCount: countEditorDocumentWords(
          activatedDocument.contentMarkdown,
        ),
      }, 'share')
    } catch (error: unknown) {
      setCollaborationActivation((current) => current
        ? {
            ...current,
            error: error instanceof Error
              ? error.message
              : collaborationActivationFallbackError(locale),
            pending: false,
          }
        : null)
    }
  }, [
    apiKey,
    collaborationActivation,
    dispatch,
    locale,
    onFlushDocumentForShare,
    onShareDocument,
    onServerDocumentObserved,
    workspaceId,
  ])

  const resolveWordExportMarkdown = useCallback(async (): Promise<string> => {
    if (!activeDocument) throw new Error(copy.exportWordFailed)
    if (activeDocument.contentMode !== 'collaboration') {
      return state.editorUi.viewMode === 'live' && activeEditor
        ? serializeEditorMarkdown(activeEditor)
        : activeDocument.contentMarkdown
    }
    try {
      const projection = await flushCollaborationProjectionBarrier({
        clientOptions: { apiKey, workspaceId },
        controller: collaborationController,
        documentId: activeDocument.id,
        generation: activeDocument.collaboration?.generation,
      })
      dispatch({
        confirmedAt: projection.confirmedAt,
        contentMarkdown: projection.markdown,
        documentId: activeDocument.id,
        sequence: projection.sequence,
        type: 'confirmEditorCollaborationProjection',
      })
      return projection.markdown
    } catch (error) {
      const fallback = confirmedProjectionFallback(activeDocument)
      if (!fallback || !confirmProjectionFallbackExport(fallback.confirmedAt, locale)) {
        throw error
      }
      return fallback.markdown
    }
  }, [
    activeDocument,
    activeEditor,
    apiKey,
    collaborationController,
    copy.exportWordFailed,
    dispatch,
    locale,
    state.editorUi.viewMode,
    workspaceId,
  ])

  const handleSetDiffAnchor = useCallback(() => {
    if (!activeDocument || diffAnchorOperation?.pending) return
    const documentId = activeDocument.id
    if (activeDocument.contentMode !== 'collaboration') {
      dispatch({
        contentMarkdown: state.editorUi.viewMode === 'live' && activeEditor
          ? serializeEditorMarkdown(activeEditor)
          : activeDocument.contentMarkdown,
        documentId,
        type: 'setEditorDiffAnchor',
      })
      setDiffAnchorOperation(null)
      return
    }
    if (!isOwnedEditorDocument(activeDocument) || activeDocument.metadataRevision === undefined) {
      setDiffAnchorOperation({
        documentId,
        error: locale === 'de'
          ? 'Nur die Eigentümerin oder der Eigentümer kann den Vergleichsstand ändern.'
          : 'Only the owner can change the comparison anchor.',
        pending: false,
      })
      return
    }
    let authorityGuard
    try {
      authorityGuard = beginEditorCollaborationAuthorityGuard(
        activeDocument,
        collaboration,
        'write',
        locale,
      )
    } catch (error) {
      setDiffAnchorOperation({
        documentId,
        error: error instanceof Error ? error.message : copy.setDiffAnchor,
        pending: false,
      })
      return
    }
    if (!authorityGuard) return
    setDiffAnchorOperation({ documentId, error: null, pending: true })
    void persistCollaborationDiffAnchor({
      authorityGuard,
      clientOptions: { apiKey, workspaceId },
      controller: collaborationController,
      documentId,
      expectedMetadataRevision: activeDocument.metadataRevision,
      locale,
      onAdoptMetadataRevision: (metadataRevision) => {
        dispatch({
          documentId,
          metadataRevision,
          type: 'adoptEditorDocumentMetadataRevision',
        })
      },
      onDocumentSaved: (document) => {
        dispatch({ document, type: 'setServerEditorDocumentDetail' })
      },
      onServerDocumentObserved: onServerDocumentObserved
        ? (document) => onServerDocumentObserved(document, 'metadata')
        : undefined,
    }).then(() => {
      setDiffAnchorOperation({ documentId, error: null, pending: false })
    }).catch((error: unknown) => {
      setDiffAnchorOperation({
        documentId,
        error: error instanceof Error ? error.message : copy.setDiffAnchor,
        pending: false,
      })
    })
  }, [
    activeDocument,
    activeEditor,
    apiKey,
    collaboration,
    collaborationController,
    copy.setDiffAnchor,
    diffAnchorOperation?.pending,
    dispatch,
    locale,
    onServerDocumentObserved,
    state.editorUi.viewMode,
    workspaceId,
  ])

  const handleCreateComment = useCallback((comment: EditorCommentThreadRecord) => {
    if (!collaborationActive) {
      dispatch({ comment, type: 'createEditorComment' })
      return
    }
    if (!activeEditor || !collaboration.document) {
      setCollaborationAnchorError({
        documentId: comment.documentId,
        message: locale === 'de'
          ? 'Der Kommentar konnte nicht stabil verankert werden.'
          : 'The comment could not be anchored safely.',
      })
      return
    }
    try {
      const anchoredComment = serializeCollaborationCommentAnchor(
        activeEditor,
        collaboration.document,
        comment,
      )
      setCollaborationAnchorError(null)
      dispatch({ comment: anchoredComment, type: 'createEditorComment' })
    } catch (error) {
      console.error('Inqtrix collaboration comment anchor failed.', error)
      setCollaborationAnchorError({
        documentId: comment.documentId,
        message: locale === 'de'
          ? 'Der Kommentar konnte nicht stabil verankert werden.'
          : 'The comment could not be anchored safely.',
      })
    }
  }, [activeEditor, collaboration.document, collaborationActive, dispatch, locale])

  const handleCreateTeamComment = useCallback((input: {
    anchor: EditorCommentAnchorRecord
    bodyMarkdown: string
    mentionUserIds: string[]
    quote: string
  }) => {
    if (
      !activeDocument
      || !activeEditor
      || !collaboration.document
      || !collaborationActive
    ) return
    try {
      const anchor = serializeEditorCollaborationAnchor(
        activeEditor,
        collaboration.document,
        input.anchor,
      )
      setCollaborationAnchorError(null)
      setInspectorTab('comments')
      if (isDesktop) {
        dispatch({ isVisible: true, type: 'setEditorCommentPanelVisible' })
      } else {
        setIsMobileCommentsOpen(true)
      }
      void teamComments.createThread({
        anchor,
        bodyMarkdown: input.bodyMarkdown,
        mentionUserIds: input.mentionUserIds,
        quote: input.quote,
      }).then((thread) => {
        setSelectedTeamCommentId(thread.id)
      }).catch((error: unknown) => {
        setCollaborationAnchorError({
          documentId: activeDocument.id,
          message: error instanceof Error
            ? error.message
            : locale === 'de'
              ? 'Der Team-Kommentar konnte nicht gespeichert werden.'
              : 'The team comment could not be saved.',
        })
      })
    } catch (error) {
      console.error('Inqtrix team comment anchor failed.', error)
      setCollaborationAnchorError({
        documentId: activeDocument.id,
        message: locale === 'de'
          ? 'Der Team-Kommentar konnte nicht stabil verankert werden.'
          : 'The team comment could not be anchored safely.',
      })
    }
  }, [
    activeDocument,
    activeEditor,
    collaboration.document,
    collaborationActive,
    dispatch,
    isDesktop,
    locale,
    teamComments.createThread,
  ])

  const handleSelectTeamComment = useCallback((threadId: string | null) => {
    setSelectedTeamCommentId(threadId)
    if (!threadId) return
    setInspectorTab('comments')
    if (isDesktop) {
      dispatch({ isVisible: true, type: 'setEditorCommentPanelVisible' })
    } else {
      setIsMobileCommentsOpen(true)
    }
  }, [dispatch, isDesktop])

  // The thread becomes a chip beside the composer; the composer's text stays
  // the user's own, and the user writes what to do with the discussion.
  const handleUseTeamCommentWithAssistant = useCallback((
    thread: (typeof teamComments.threads)[number],
  ) => {
    handOverDiscussion(thread.id)
    if (!state.editorUi.isAssistantVisible) {
      dispatch({ isVisible: true, type: 'setEditorAssistantVisible' })
    }
    setInspectorTab('assistant')
    window.requestAnimationFrame(() => composerRef.current?.focus())
  }, [
    dispatch,
    handOverDiscussion,
    state.editorUi.isAssistantVisible,
  ])

  const addExtraRef = useCallback((ref: ChatContextReferenceRecord) => {
    setExtraRefs((prev) => dedupeChatContextRefs([...prev, ref]))
  }, [])

  const clearEditorAttachments = useCallback(() => {
    setPillRefs([])
    setExtraRefs([])
    composerRef.current?.clear()
  }, [])

  const handleRemoveEditorChip = useCallback((ref: ChatContextReferenceRecord) => {
    composerRef.current?.removeRef(ref)
    setExtraRefs((prev) => prev.filter((item) => chatContextRefKey(item) !== chatContextRefKey(ref)))
  }, [])

  const editorPillKeys = useMemo(() => pillRefs.map(chatContextRefKey), [pillRefs])
  const editorPendingKeys = useMemo(() => extraRefs.map(chatContextRefKey), [extraRefs])

  const handleReorderEditorPill = useCallback((fromIndex: number, toIndex: number) => {
    composerRef.current?.reorderPill(fromIndex, toIndex)
  }, [])

  const handleReorderEditorPending = useCallback((fromIndex: number, toIndex: number) => {
    setExtraRefs((prev) => moveItem(prev, fromIndex, toIndex))
  }, [])

  const handleCollaborationPublication = useCallback((
    documentId: string,
    publication: CollaborationSuggestionPublication,
  ) => {
    const pending = beginCollaborationPublicationFocus(documentId, publication.patchId)
    setPendingPublicationFocusByDocument((current) => (
      registerCollaborationPublicationFocus(current, documentId, publication.patchId)
    ))
    if (!isCollaborationPublicationFocusCurrent(pending, activeDocumentIdRef.current)) return
    setInspectorTab('changes')
    setChangesView('open')
    setOpenChangeFilters({ authorId: null, type: null })
    if (isDesktop) {
      dispatch({ isVisible: true, type: 'setEditorCommentPanelVisible' })
    } else {
      setIsMobileCommentsOpen(true)
    }
  }, [dispatch, isDesktop])

  const handleAttachEditorFiles = useCallback(async (files: File[]) => {
    if (files.length === 0) return
    const existingLabels = projectFileAssets(state).map((asset) => asset.label)
    const sectionId = temporaryFileSectionId(Object.values(state.fileLibrarySections))
    const { queue, records } = createFileAssetPlaceholders(
      files,
      { kind: 'editor', sectionId },
      existingLabels,
      Boolean(serverFileUpload),
    )
    dispatch({ assets: records, type: 'ingestFileAssets' })
    for (const asset of records) addExtraRef({ fileId: asset.id, kind: 'file-asset' })
    const recordsById = new Map(records.map((record) => [record.id, record]))
    const bindings = new Map(records.map((record) => [record.id, uploadBindingForRecord(record)]))
    for (const item of queue) {
      const binding = bindings.get(item.assetId)
      if (binding) uploadRegistry?.register(item.assetId, { binding, file: item.file })
    }
    const parser = createDefaultFileParser()
    void (async () => {
      const bindingReady = serverFileUpload && ensureUploadTarget
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
          uploadRegistry?.delete(assetId)
          const record = recordsById.get(assetId)
          if (record) onAssetsIngested?.([{ ...record, serverFileId: result.serverFileId }])
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
  }, [
    addExtraRef,
    dispatch,
    ensureUploadTarget,
    onAssetsIngested,
    serverFileUpload,
    serverParserAvailable,
    state,
    uploadRegistry,
  ])

  const {
    aiReadOnlyReason,
    clearInstructionFeedback,
    documentSuggestions,
    handleAcceptSuggestionGroup,
    handleAcceptSuggestion,
    handleEditSuggestionProposal,
    handleGlobalRun,
    handleInstructionRun,
    handleMarkSuggestionStale,
    handleRefineSuggestion,
    handleRejectSuggestion,
    handleRejectSuggestionGroup,
    handleRunComment,
    handleStopRun,
    handleStopSuggestionRun,
    instructionFeedback,
    isGlobalRunning,
    replaceableSuggestionCount,
    runErrors,
    runningCommentIds,
    savingCommentDraftIds,
    runningSuggestionIds,
    suggestionPublishDisabledReason,
    suggestionErrors,
  } = useEditorSuggestions({
    activeDocument,
    activeEditor,
    apiKey,
    attachedCommentIds,
    attachedRefs,
    capabilities,
    collaboration,
    comments,
    dispatch,
    ensureAssetBodiesLoaded,
    handedOverDiscussions: discussions,
    locale,
    onCollaborationPublication: handleCollaborationPublication,
    onGlobalSuccess: () => {
      setAttachedCommentIds([])
      clearEditorAttachments()
      clearHandedOverDiscussions()
      setIsAttachActive(false)
    },
    selectedModelTier,
    state,
  })
  const surfaceSuggestions = useMemo(() => {
    if (!collaborationActive || !activeEditor || !collaboration.document) {
      return documentSuggestions
    }
    return documentSuggestions.map((suggestion) => {
      const resolved = resolveEditorCollaborationAnchor(
        activeEditor,
        collaboration.document!,
        suggestion.anchor,
      )
      return resolved.status === 'relative'
        ? { ...suggestion, anchor: resolved.anchor }
        : suggestion
    })
  }, [
    activeEditor,
    collaboration.document,
    collaborationActive,
    collaborationSnapshot,
    documentSuggestions,
  ])

  const handleSelectSuggestion = useCallback((suggestionId: string) => {
    if (state.editorUi.isDiffVisible) {
      dispatch({ isVisible: false, type: 'setEditorDiffVisible' })
    }
    globalThis.setTimeout(() => {
      const escapedId = escapeCssIdentifier(suggestionId)
      const target = globalThis.document?.querySelector<HTMLElement>(
        `[data-suggestion-block-card="${escapedId}"], [data-suggestion-id="${escapedId}"]`,
      )
      if (!target) return
      target.scrollIntoView({ block: 'center', behavior: 'smooth' })
      target.classList.add('suggestion-scroll-pulse')
      globalThis.setTimeout(() => target.classList.remove('suggestion-scroll-pulse'), 900)
    }, 0)
  }, [dispatch, state.editorUi.isDiffVisible])

  const handleSelectCollaborationChange = useCallback((changeId: string | null) => {
    setSelectedCollaborationChangeId(changeId)
    if (!changeId) return
    const suggestionId = collaborationSnapshot.changes
      .find((change) => change.id === changeId)
      ?.suggestionIds[0]
    if (!suggestionId) return
    globalThis.setTimeout(() => {
      const target = globalThis.document?.querySelector<HTMLElement>(
        `[data-suggestion-id="${escapeCssIdentifier(suggestionId)}"]`,
      )
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 0)
  }, [collaborationSnapshot.changes])

  useEffect(() => {
    const activeDocumentId = activeDocument?.id ?? null
    const pendingPublicationFocus = pendingCollaborationPublicationFocusForDocument(
      pendingPublicationFocusByDocument,
      activeDocumentId,
    )
    const result = consumeCollaborationPublicationFocus(
      pendingPublicationFocus,
      activeDocumentId,
      collaborationSnapshot.changes,
    )
    if (!result.focusId || !activeDocumentId || !pendingPublicationFocus) return
    handleSelectCollaborationChange(result.focusId)
    setPendingPublicationFocusByDocument((current) => {
      if (current[activeDocumentId]?.patchId !== pendingPublicationFocus.patchId) return current
      const next = { ...current }
      delete next[activeDocumentId]
      return next
    })
  }, [
    activeDocument?.id,
    collaborationSnapshot.changes,
    handleSelectCollaborationChange,
    pendingPublicationFocusByDocument,
  ])

  useEffect(() => {
    const collision = collaborationSnapshot.collision
    if (!collision) return
    setInspectorTab('changes')
    setChangesView('open')
    setOpenChangeFilters({ authorId: null, type: null })
    handleSelectCollaborationChange(collision.patchId)
    if (isDesktop) {
      dispatch({ isVisible: true, type: 'setEditorCommentPanelVisible' })
    } else {
      setIsMobileCommentsOpen(true)
    }
  }, [
    collaborationSnapshot.collision,
    dispatch,
    handleSelectCollaborationChange,
    isDesktop,
  ])

  // Both side panels drag-resize + persist their width (like Knowledge/Chat),
  // while the existing `isTreeVisible`/`isCommentPanelVisible` booleans keep
  // driving collapse/expand (already persisted). Nested groups: the tree lives
  // in the outer group, the comments panel in the inner group.
  const reduceMotion = useReducedMotion()
  const treeVisible = state.editorUi.isTreeVisible
  const commentsVisible = state.editorUi.isCommentPanelVisible
  const treeSize = state.ui.panelLayout.editorTree
  const commentsSize = state.ui.panelLayout.editorComments
  const treePanelMotion = useAnimatedResizablePanelCollapse({
    expanded: treeVisible,
    expandedSize: treeSize,
    reduceMotion,
  })
  const commentsPanelMotion = useAnimatedResizablePanelCollapse({
    expanded: commentsVisible,
    expandedSize: commentsSize,
    reduceMotion,
  })
  const treeLayout = {
    [EDITOR_CENTER_PANEL_ID]: treeVisible ? 100 - treeSize : 100,
    [EDITOR_TREE_PANEL_ID]: treeVisible ? treeSize : 0,
  }
  const commentsLayout = {
    [EDITOR_CENTER_COMMENTS_PANEL_ID]: commentsVisible ? 100 - commentsSize : 100,
    [EDITOR_COMMENTS_PANEL_ID]: commentsVisible ? commentsSize : 0,
  }

  const handleTreeVisibleChange = useCallback((isVisible: boolean) => {
    if (isDesktop) {
      dispatch({ isVisible, type: 'setEditorTreeVisible' })
      return
    }
    setIsMobileTreeOpen(isVisible)
  }, [dispatch, isDesktop])

  const handleCommentsVisibleChange = useCallback((isVisible: boolean) => {
    if (isDesktop) {
      dispatch({ isVisible, type: 'setEditorCommentPanelVisible' })
      return
    }
    setIsMobileCommentsOpen(isVisible)
  }, [dispatch, isDesktop])

  const fileTreePanel = (
    <EditorFileTree
      activeDocumentId={activeDocument?.id ?? null}
      copy={copy}
      dispatch={dispatch}
      documents={documents}
      folders={folders}
      onDocumentDetails={(document) => handleShareDocument(document, 'details')}
      onPrefetchDocument={prefetchDocumentBody
        ? (documentId) => {
            void prefetchDocumentBody(documentId)
          }
        : undefined}
      pinnedDocumentIds={state.ui.pinnedExplorer.editorDocumentIds}
      reportOptions={reportOptions}
      sortMode={state.ui.explorerSort.editor}
      runningDocumentId={
        (isGlobalRunning || runningCommentIds.length > 0 || runningSuggestionIds.length > 0)
          ? activeDocument?.id ?? null
          : null
      }
    />
  )

  const assistantPanel = (
    <EditorCommentsPanel
      aiReadOnlyReason={aiReadOnlyReason}
      anchorError={collaborationAnchorError?.documentId === activeDocument?.id
        ? collaborationAnchorError?.message ?? null
        : surfaceCommentAnchors.degraded
          ? locale === 'de'
            ? 'Mindestens ein Kommentarziel wurde gelöscht oder verschoben. Die Textsuche wird als Ersatz verwendet.'
            : 'At least one comment target was deleted or moved. Quote matching is being used as a fallback.'
          : null}
      comments={comments}
      copy={copy}
      dispatch={dispatch}
      onAcceptSuggestionGroup={handleAcceptSuggestionGroup}
      onAcceptSuggestion={handleAcceptSuggestion}
      onRejectSuggestionGroup={handleRejectSuggestionGroup}
      onRejectSuggestion={handleRejectSuggestion}
      attachedCommentIds={attachedCommentIds}
      onRunComment={handleRunComment}
      onSelectSuggestion={handleSelectSuggestion}
      onToggleCollect={(commentId) => {
        setAttachedCommentIds((current) => (current.includes(commentId)
          ? current.filter((id) => id !== commentId)
          : [...current, commentId]))
      }}
      runErrors={runErrors}
      runningCommentIds={runningCommentIds}
      savingCommentDraftIds={savingCommentDraftIds}
      selectedCommentId={state.editorUi.selectedCommentId}
      suggestionErrors={suggestionErrors}
      suggestionPublishDisabledReason={suggestionPublishDisabledReason}
      suggestions={documentSuggestions}
    />
  )
  const activeDecisionState = decisionState?.documentId === activeDocument?.id
    ? decisionState
    : null
  const collaborationChangeError = collaborationSnapshot.changesUnreadable
    ? COLLABORATION_CHANGES_UNREADABLE_COPY[locale]
    : collaborationActivity.attributionWarning
  const collaborationEditRefusal = collaborationActive
    ? localizeCollaborationEditRefusal(collaborationSnapshot.error, locale)
    : null
  const collaborationConnectionError = collaborationActive
    && !collaborationFeatureAvailable
    ? locale === 'de'
      ? 'Live-Kollaboration wurde von der Administration deaktiviert. Angezeigt wird der letzte bestätigte Stand.'
      : 'Live collaboration was disabled by an administrator. The last confirmed version is shown.'
    : localizeEditorCollaborationNotice(
        collaboration.blockingFailure
          ?? collaboration.error
          ?? (collaboration.durabilityDelayed ? COLLABORATION_NOTICES.ack_delayed : null),
        locale,
      )
  const collaborationStatus = useMemo(() => buildEditorCollaborationStatusModel({
    access: collaboration.access,
    active: collaborationActive,
    canEdit: collaboration.canEdit,
    connectionStatus: collaboration.connectionStatus,
    durabilityDelayed: collaboration.durabilityDelayed,
    durabilityStatus: collaboration.durabilityStatus,
    hasUnconfirmedLocalChanges: collaboration.hasUnconfirmedLocalChanges,
    nextReconnectAt: collaboration.nextReconnectAt,
    notice: collaborationConnectionError,
    participants: collaborationSnapshot.participants,
    projectionUpdatedAt: activeDocument?.collaboration?.projectionUpdatedAt,
    reconnectAttempt: collaboration.reconnectAttempt,
    recoverability: collaboration.recoverability,
    synced: isCollaborationSurfaceSynced(collaboration),
    unconfirmedSince: collaboration.unconfirmedSince,
  }), [
    activeDocument?.collaboration?.projectionUpdatedAt,
    collaboration,
    collaborationActive,
    collaborationConnectionError,
    collaborationSnapshot.participants,
  ])
  const handleCollaborationLogin = useCallback(() => {
    const next = `${window.location.pathname}${window.location.search}${window.location.hash}`
    window.location.assign(buildLoginUrl(undefined, next))
  }, [])
  // A rejected update needs only this document started over from the
  // service's state; every other reload state needs the page.
  const handleCollaborationReload = useCallback(() => {
    if (collaboration.updateRejected) {
      collaboration.reloadDocument()
      return
    }
    window.location.reload()
  }, [collaboration])
  const handleCollaborationBackupDownload = useCallback(() => {
    if (!activeEditor || activeEditor.isDestroyed || !activeDocument) return
    downloadCollaborationBackup(activeEditor, activeDocument.title, locale)
  }, [activeDocument, activeEditor, locale])
  const teamCommentsPanel = (
    <TeamCommentsPanel
      canComment={
        collaboration.canEdit
        && (collaboration.access === 'edit' || collaboration.access === 'suggest')
      }
      comments={teamComments}
      currentUserId={collaboration.user?.id ?? null}
      onSelectThread={handleSelectTeamComment}
      onUseWithAssistant={handleUseTeamCommentWithAssistant}
      orphanedThreadIds={surfaceTeamComments.orphanedThreadIds}
      positionByThreadId={surfaceTeamComments.positionByThreadId}
      selectedThreadId={selectedTeamCommentId}
    />
  )
  const commentsPanel = (
    <EditorInspector
      activeTab={inspectorTab}
      assistant={assistantPanel}
      canDecide={canDecideCollaborationChanges}
      changes={collaborationSnapshot.changes}
      changesError={collaborationChangeError}
      changesView={changesView}
      collaborationActive={collaborationActive}
      collaborationStatus={collaborationStatus}
      commentCount={teamComments.threads.length}
      comments={teamCommentsPanel}
      commentUnreadCount={teamComments.unreadCount}
      decisionError={activeDecisionState?.error ?? null}
      display={reviewDisplay}
      history={collaborationActivity.entries}
      historyError={collaborationActivity.error}
      historyFilters={historyFilters}
      historyLoading={collaborationActivity.isLoading}
      isDecisionPending={activeDecisionState?.pending ?? false}
      onActiveTabChange={setInspectorTab}
      onChangesViewChange={setChangesView}
      onClose={() => handleCommentsVisibleChange(false)}
      onDecision={handleCollaborationDecision}
      onDisplayChange={setReviewDisplay}
      onHistoryFiltersChange={setHistoryFilters}
      onLogin={handleCollaborationLogin}
      onOpenFiltersChange={setOpenChangeFilters}
      onReconnect={collaboration.retryConnection}
      onReload={handleCollaborationReload}
      onSelectedChangeIdChange={handleSelectCollaborationChange}
      openFilters={openChangeFilters}
      selectedChangeId={selectedCollaborationChangeId}
    />
  )

  const requestedDocumentHeader = activeDocument ? (
    <>
      <EditorTopBar
        collaborationAccess={collaboration.access}
        collaborationActive={collaborationActive}
        collaborationCanEdit={collaboration.canEdit}
        collaborationStatus={collaborationStatus}
        copy={copy}
        dispatch={dispatch}
        document={activeDocument}
        editor={activeEditor}
        isCommentPanelVisible={isDesktop ? state.editorUi.isCommentPanelVisible : isMobileCommentsOpen}
        isDiffVisible={state.editorUi.isDiffVisible}
        isDirty={state.dirty}
        canFlushForShare={onFlushDocumentForShare !== undefined}
        isTreeVisible={isDesktop ? state.editorUi.isTreeVisible : isMobileTreeOpen}
        diffAnchorError={diffAnchorOperation?.documentId === activeDocument.id
          ? diffAnchorOperation.error
          : null}
        diffAnchorPending={diffAnchorOperation?.documentId === activeDocument.id
          ? diffAnchorOperation.pending
          : false}
        diffAnchorDisabledReason={diffAnchorAuthorityDisabledReason}
        onCommentPanelVisibleChange={handleCommentsVisibleChange}
        onExportWordMarkdown={resolveWordExportMarkdown}
        onSetDiffAnchor={handleSetDiffAnchor}
        onShareDocument={(document, returnFocusTarget) => {
          handleShareDocument(document, 'share', returnFocusTarget)
        }}
        onTreeVisibleChange={handleTreeVisibleChange}
        onWriteModeChange={handleWriteModeChange}
        onCollaborationLogin={handleCollaborationLogin}
        onCollaborationReconnect={collaboration.retryConnection}
        onCollaborationReload={handleCollaborationReload}
        sharingAvailable={
          onShareDocument !== undefined
          && (collaborationActive || collaborationFeatureAvailable)
        }
        viewMode={state.editorUi.viewMode}
        writeMode={editorWriteMode}
      />
      <EditorDocumentRecoveryNotice
        dispatch={dispatch}
        document={activeDocument}
        documents={state.editorDocuments}
        locale={locale}
      />
      {collaborationActive
        && collaboration.recoverability === 'retry'
        && collaboration.reconnectAttempt >= 3 ? (
          <CollaborationRecoveryBar
            hasUnconfirmedLocalChanges={collaboration.hasUnconfirmedLocalChanges}
            locale={locale}
            onReconnect={collaboration.retryConnection}
          />
        ) : null}
      {collaborationActive && collaboration.updateRejected ? (
        <CollaborationRejectionBar
          locale={locale}
          notice={localizeEditorCollaborationNotice(
            collaboration.error ?? COLLABORATION_NOTICES.update_rejected,
            locale,
          ) ?? COLLABORATION_NOTICES.update_rejected}
          onDownloadBackup={activeEditor ? handleCollaborationBackupDownload : null}
          onReload={collaboration.reloadDocument}
        />
      ) : collaborationEditRefusal ? (
        <CollaborationEditRefusalLine message={collaborationEditRefusal} />
      ) : null}
    </>
  ) : null
  const documentHeaderPublication = useEditorIdentityOwnedContent(
    activeSurfaceKey,
    requestedDocumentHeader,
  )

  const editorContent = (
    <main className="flex h-full w-full min-w-0 flex-col bg-background">
      {activeDocument ? (
        <>
          <div
            aria-hidden={documentHeaderPublication.inert || undefined}
            data-editor-document-header=""
            data-editor-requested-identity={activeSurfaceKey}
            data-editor-visible-identity={documentHeaderPublication.visibleIdentity}
            inert={documentHeaderPublication.inert || undefined}
          >
            {documentHeaderPublication.content}
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            <StructuralLoadBoundary
              className="min-h-0 flex-1"
              fallback={<EditorDocumentSkeleton />}
              identity={activeSurfaceKey}
              onVisibilityChange={documentHeaderPublication.onVisibilityChange}
              phase={activeDocumentLoadPhase}
            >
              {activeDocumentLoadPhase === 'error' ? (
                <EditorDocumentBodyError
                  error={documentBodyHydration.error}
                  locale={locale}
                  onRetry={prefetchDocumentBody
                      ? () => {
                        void prefetchDocumentBody(activeDocument.id, { retry: true })
                      }
                    : undefined}
                />
              ) : activeDocumentLoadPhase === 'pending' ? (
                <div aria-hidden className="min-h-0 flex-1" />
              ) : (
                <div className="relative min-h-0 flex-1">
                  <div
                    aria-hidden={collaborationActive && state.editorUi.viewMode === 'source'}
                    className={cn(
                      'absolute inset-0 flex min-h-0 flex-col',
                      collaborationActive && state.editorUi.viewMode === 'source'
                        && 'invisible pointer-events-none',
                    )}
                  >
                    <EditorDocumentSurfaceReadiness ready={activeEditor !== null}>
                      <MarkdownEditorSurface
                        collaboration={collaborationActive ? collaboration : null}
                        collaborationReviewPolicy={collaborationEditorPolicyUpdate(collaborationEditorPolicy)}
                        comments={inspectorTab === 'assistant' ? surfaceCommentAnchors.comments : []}
                        copy={copy}
                        document={activeDocument}
                        diffAnchorMarkdown={activeDocument.diffAnchorMarkdown ?? null}
                        isDiffVisible={state.editorUi.isDiffVisible}
                        mode={state.editorUi.viewMode}
                        onChange={(contentMarkdown) => {
                          dispatch({
                            contentMarkdown,
                            documentId: activeDocument.id,
                            type: 'updateEditorDocumentMarkdown',
                          })
                        }}
                        onCreateComment={handleCreateComment}
                        onCreateTeamComment={collaborationActive
                          ? handleCreateTeamComment
                          : undefined}
                        onCollaborationSuggestionUndo={collaborationActive
                          ? handleCollaborationSuggestionUndo
                          : undefined}
                        onEditorReady={handleEditorReady}
                        onAcceptSuggestion={handleAcceptSuggestion}
                        onEditSuggestion={handleEditSuggestionProposal}
                        onMarkSuggestionStale={handleMarkSuggestionStale}
                        onRefineSuggestion={handleRefineSuggestion}
                        onRejectSuggestion={handleRejectSuggestion}
                        onSelectComment={(commentId) => dispatch({ commentId, type: 'selectEditorComment' })}
                        onSelectTeamComment={handleSelectTeamComment}
                        onStopSuggestion={handleStopSuggestionRun}
                        onTeamCommentDraftChange={(value) => teamComments.setDraft('new', value)}
                        runningSuggestionIds={runningSuggestionIds}
                        selectedCommentId={inspectorTab === 'assistant' ? state.editorUi.selectedCommentId : null}
                        suggestionActionsDisabled={Boolean(suggestionPublishDisabledReason)}
                        suggestionErrors={suggestionErrors}
                        suggestions={inspectorTab === 'assistant'
                          ? surfaceSuggestions.filter((suggestion) => suggestion.status === 'pending')
                          : []}
                        selectedTeamCommentId={inspectorTab === 'comments'
                          ? selectedTeamCommentId
                          : null}
                        teamCommentParticipants={teamComments.participants}
                        teamCommentDraft={teamComments.drafts.new ?? ''}
                        teamComments={surfaceTeamComments.threads}
                        textImprovement={textImprovement}
                      />
                    </EditorDocumentSurfaceReadiness>
                  </div>
                  {collaborationActive && state.editorUi.viewMode === 'source' ? (
                    <ScrollArea className="absolute inset-0 bg-background">
                      <pre
                        aria-label={copy.sourceEditor}
                        className="t-body min-h-full whitespace-pre-wrap break-words px-10 py-8 font-mono text-foreground"
                      >
                        {collaborationSourceMarkdown}
                      </pre>
                    </ScrollArea>
                  ) : null}
                </div>
              )}
            </StructuralLoadBoundary>
            <EditorAssistantComposer
              aiReadOnlyReason={aiReadOnlyReason}
              attachedCommentIds={attachedCommentIds}
              attachmentChips={attachmentChips}
              chatModelOptions={chatModelOptions}
              chatModelOptionsStatus={chatModelOptionsStatus}
              comments={comments}
              composerRef={composerRef}
              copy={copy}
              defaultChatModel={defaultChatModel}
              dispatch={dispatch}
              draft={state.editorUi.assistantDraft}
              fileGroupOptions={fileGroupOptions}
              fileOptions={fileOptions}
              handedOverDiscussions={discussions}
              reportOptions={reportOptions}
              isAttachActive={isAttachActive}
              instructionFeedback={instructionFeedback}
              isRunning={isGlobalRunning}
              isVisible={state.editorUi.isAssistantVisible}
              isWideCanvas={isDesktop ? !state.editorUi.isTreeVisible && !state.editorUi.isCommentPanelVisible : true}
              onAttachFiles={(files) => void handleAttachEditorFiles(files)}
              onAttachRule={(ruleId) => addExtraRef({ kind: 'chat-rule', ruleId })}
              onRefsChange={setPillRefs}
              onReorderPending={handleReorderEditorPending}
              onReorderPill={handleReorderEditorPill}
              onRemoveAttachedComment={(commentId) =>
                setAttachedCommentIds((prev) => prev.filter((id) => id !== commentId))}
              onRemoveChip={handleRemoveEditorChip}
              onRemoveHandedOverDiscussion={removeHandedOverDiscussion}
              onRetryAttachment={onRetryAttachment}
              pendingKeys={editorPendingKeys}
              pillKeys={editorPillKeys}
              replaceableSuggestionCount={replaceableSuggestionCount}
              onToggleAttach={() => {
                const openIds = comments
                  .filter((comment) => comment.status === 'open' && isAuthoredComment(comment))
                  .map((comment) => comment.id)
                setAttachedCommentIds(isAttachActive ? [] : openIds)
                setIsAttachActive(!isAttachActive)
              }}
              onDismissInstructionFeedback={clearInstructionFeedback}
              onSend={() => {
                const instruction = composerRef.current?.getInstructionText().trim() ?? ''
                const hasAttachedComments = comments.some((comment) =>
                  comment.status === 'open'
                  && isAuthoredComment(comment)
                  && attachedCommentIds.includes(comment.id))
                if (hasAttachedComments) {
                  void handleGlobalRun(instruction)
                } else {
                  void handleInstructionRun(instruction)
                }
              }}
              onStop={handleStopRun}
              ruleOptions={ruleOptions}
              selectedModelTier={selectedModelTier}
              chatModelCatalog={chatModelCatalog}
              selectedModel={state.ui.selectedChatModel}
              selectedEffort={state.ui.selectedChatEffort}
              editorContextBase={editorContextBase}
              editorContextCapacity={editorContextCapacity}
              textImprovement={textImprovement}
            />
          </div>
        </>
      ) : (
        <div className="relative flex min-h-0 flex-1 bg-background">
          <header className="absolute inset-x-0 top-0 z-10 grid inqtrix-panel-header grid-cols-[auto_1fr_auto] items-center gap-2 border-b border-border bg-background px-3">
            <EditorPanelToggle
              copy={copy}
              dispatch={dispatch}
              onToggle={handleTreeVisibleChange}
              side="left"
              visible={isDesktop ? state.editorUi.isTreeVisible : isMobileTreeOpen}
            />
            <span aria-hidden />
            <EditorPanelToggle
              copy={copy}
              dispatch={dispatch}
              onToggle={handleCommentsVisibleChange}
              side="right"
              visible={isDesktop ? state.editorUi.isCommentPanelVisible : isMobileCommentsOpen}
            />
          </header>
          <EditorEmptyState copy={copy} dispatch={dispatch} reportOptions={reportOptions} />
        </div>
      )}
    </main>
  )

  return (
    <div className="relative flex h-full min-h-0 min-w-0 overflow-hidden bg-canvas text-foreground">
      <ResizablePanelGroup
        className="min-h-0 min-w-0 flex-1 overflow-hidden"
        defaultLayout={treeLayout}
        elementRef={treePanelMotion.groupRef}
        onLayoutChanged={(layout) => {
          const size = layout[EDITOR_TREE_PANEL_ID]
          if (
            treeVisible
            && !treePanelMotion.isProgrammaticLayoutChange()
            && Number.isFinite(size)
            && size > 0
          ) {
            dispatch({ key: 'editorTree', size, type: 'setPanelLayoutSize' })
          }
        }}
        orientation="horizontal"
      >
        {isDesktop && (
          <>
            <ResizablePanel
              className="min-h-0 min-w-0 overflow-hidden"
              collapsedSize="0%"
              collapsible
              defaultSize={treeLayout[EDITOR_TREE_PANEL_ID]}
              id={EDITOR_TREE_PANEL_ID}
              maxSize="34%"
              minSize={treeVisible ? '16%' : '0%'}
              panelRef={treePanelMotion.panelRef}
            >
              <AnimatedPanelBody expanded={treeVisible} side="left">
                {fileTreePanel}
              </AnimatedPanelBody>
            </ResizablePanel>
            <AnimatedResizableHandle aria-label={copy.resizeTree} expanded={treeVisible} />
          </>
        )}
        <ResizablePanel
          className="min-h-0 min-w-0 overflow-hidden"
          defaultSize={treeLayout[EDITOR_CENTER_PANEL_ID]}
          id={EDITOR_CENTER_PANEL_ID}
          // Identity anchor — must stay unconditional so the TipTap editor
          // (instance, selection, undo history) survives the desktop/mobile flip.
          key={EDITOR_CENTER_PANEL_ID}
          minSize="40%"
        >
          <ResizablePanelGroup
            className="min-h-0 w-full overflow-hidden"
            defaultLayout={commentsLayout}
            elementRef={commentsPanelMotion.groupRef}
            onLayoutChanged={(layout) => {
              const size = layout[EDITOR_COMMENTS_PANEL_ID]
              if (
                commentsVisible
                && !commentsPanelMotion.isProgrammaticLayoutChange()
                && Number.isFinite(size)
                && size > 0
              ) {
                dispatch({ key: 'editorComments', size, type: 'setPanelLayoutSize' })
              }
            }}
            orientation="horizontal"
          >
            <ResizablePanel
              className="min-h-0 min-w-0 overflow-hidden"
              defaultSize={commentsLayout[EDITOR_CENTER_COMMENTS_PANEL_ID]}
              id={EDITOR_CENTER_COMMENTS_PANEL_ID}
              // Identity anchor — must stay unconditional (see above).
              key={EDITOR_CENTER_COMMENTS_PANEL_ID}
              minSize="50%"
            >
              {editorContent}
            </ResizablePanel>
            {isDesktop && (
              <>
                <AnimatedResizableHandle aria-label={copy.resizeComments} expanded={commentsVisible} />
                <ResizablePanel
                  className="min-h-0 min-w-0 overflow-hidden"
                  collapsedSize="0%"
                  collapsible
                  defaultSize={commentsLayout[EDITOR_COMMENTS_PANEL_ID]}
                  id={EDITOR_COMMENTS_PANEL_ID}
                  maxSize="38%"
                  minSize={commentsVisible ? '20%' : '0%'}
                  panelRef={commentsPanelMotion.panelRef}
                >
                  <AnimatedPanelBody expanded={commentsVisible} side="right">
                    {commentsPanel}
                  </AnimatedPanelBody>
                </ResizablePanel>
              </>
            )}
          </ResizablePanelGroup>
        </ResizablePanel>
      </ResizablePanelGroup>
      {!isDesktop && (
        <>
          <ResponsiveSidePanel
            closeLabel={copy.hideTree}
            controlsId={EDITOR_TREE_PANEL_ID}
            onOpenChange={setIsMobileTreeOpen}
            open={isMobileTreeOpen}
            showHeader={false}
            side="left"
            title={copy.documents}
          >
            {fileTreePanel}
          </ResponsiveSidePanel>
          <ResponsiveSidePanel
            closeLabel={copy.hideAssistant}
            controlsId={EDITOR_COMMENTS_PANEL_ID}
            onOpenChange={setIsMobileCommentsOpen}
            open={isMobileCommentsOpen}
            showHeader={false}
            side="right"
            title={copy.assistant}
          >
            {commentsPanel}
          </ResponsiveSidePanel>
        </>
      )}
      <CollaborationActivationDialog
        error={collaborationActivation?.error ?? null}
        onCancel={() => {
          if (!collaborationActivation?.pending) setCollaborationActivation(null)
        }}
        onConfirm={() => {
          void handleEnableCollaboration()
        }}
        open={collaborationActivation !== null}
        pending={collaborationActivation?.pending ?? false}
      />
    </div>
  )
}

function EditorFileTree({
  activeDocumentId,
  copy,
  dispatch,
  documents,
  folders,
  onDocumentDetails,
  onPrefetchDocument,
  pinnedDocumentIds,
  reportOptions,
  runningDocumentId,
  sortMode,
}: {
  activeDocumentId: string | null
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  documents: EditorDocumentRecord[]
  folders: EditorFolderRecord[]
  onDocumentDetails: (document: EditorDocumentRecord) => void
  onPrefetchDocument?: (documentId: string) => void
  pinnedDocumentIds: readonly string[]
  reportOptions: CompletedReportOption[]
  runningDocumentId: string | null
  sortMode: ExplorerSortMode
}) {
  const { locale, t } = useLocale()
  const [expandedFolderIds, setExpandedFolderIds] = useState<ReadonlySet<string>>(() => new Set(folders.map((folder) => folder.id)))
  const [visibleDocumentCounts, setVisibleDocumentCounts] = useState<Record<string, number>>({})
  const [draggedDocumentId, setDraggedDocumentId] = useState<string | null>(null)
  const [draggedFolderId, setDraggedFolderId] = useState<string | null>(null)
  const [documentDropTarget, setDocumentDropTarget] = useState<EditorDocumentDropTarget | null>(null)
  const [editingDocumentId, setEditingDocumentId] = useState<string | null>(null)
  const [editingFolderId, setEditingFolderId] = useState<string | null>(null)
  const [folderDropTargetIndex, setFolderDropTargetIndex] = useState<number | null>(null)
  const [documentTitleDraft, setDocumentTitleDraft] = useState('')
  const [folderTitleDraft, setFolderTitleDraft] = useState('')
  const documentTitleInputRef = useRef<HTMLInputElement | null>(null)
  const folderTitleInputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const suppressDocumentOpenClickRef = useRef(false)
  const suppressFolderToggleClickRef = useRef(false)
  const pinnedDocumentIdSet = new Set(pinnedDocumentIds)
  const partitionedDocuments = partitionEditorDocumentsByAccess(documents)
  // Sort order: automatic modes order every section here (documents by
  // activity/name, folders alphabetical); manual keeps the explicit
  // arrays and the user's drag placement.
  const documentTime = (document: EditorDocumentRecord) => document.updatedAt
  const documentTitle = (document: EditorDocumentRecord) => document.title
  const orderedOwned = sortExplorerItems(partitionedDocuments.owned, sortMode, documentTime, documentTitle)
  const orderedFolders = sortExplorerFolders(folders, sortMode, (folder) => folder.title)
  const sharedDocuments = sortExplorerItems(partitionedDocuments.shared, sortMode, documentTime, documentTitle)
  const pinnedDocuments = orderPinnedExplorerItems(
    orderedOwned.filter((document) => pinnedDocumentIdSet.has(document.id)),
    pinnedDocumentIds,
    sortMode,
    (document) => document.id,
  )
  const treeDocuments = orderedOwned.filter((document) => !pinnedDocumentIdSet.has(document.id))
  const ungroupedDocuments = treeDocuments.filter((document) => !document.folderId || !folders.some((folder) => folder.id === document.folderId))
  const hasFolders = folders.length > 0
  const adoptVisibleOrderIfAutomatic = () => {
    if (sortMode === 'manual') return
    dispatch({
      desk: 'editor',
      folderIds: orderedFolders.map((folder) => folder.id),
      itemIds: [
        ...pinnedDocuments.map((document) => document.id),
        // Shared section renders between Pinned and the folders — adopt
        // its sorted order too, or it reshuffles at the manual switch.
        ...sharedDocuments.map((document) => document.id),
        ...orderedFolders.flatMap((folder) =>
          treeDocuments.filter((document) => document.folderId === folder.id).map((document) => document.id)),
        ...ungroupedDocuments.map((document) => document.id),
      ],
      type: 'adoptExplorerOrder',
    })
  }
  const adoptVisibleOrderRef = useRef(adoptVisibleOrderIfAutomatic)
  adoptVisibleOrderRef.current = adoptVisibleOrderIfAutomatic
  const [searchQuery, setSearchQuery] = useState('')
  const trimmedQuery = searchQuery.trim().toLowerCase()
  const isSearching = trimmedQuery.length > 0
  // Title search runs over the already-loaded documents (the tree hydrates all
  // document metadata up front; only bodies are lazy), so it is a client-side
  // filter across every folder — no server round-trip. Matches keep the
  // document order so the result list reads top-to-bottom like the tree.
  const searchResults = useMemo(
    () => (isSearching
      ? documents.filter((document) => document.title.toLowerCase().includes(trimmedQuery))
      : []),
    [documents, isSearching, trimmedQuery],
  )

  useEffect(() => {
    setExpandedFolderIds((current) => new Set([...current, ...folders.map((folder) => folder.id)]))
  }, [folders])

  function visibleDocumentCount(sectionId: string) {
    return visibleDocumentCounts[sectionId] ?? EXPLORER_SECTION_REVEAL_STEP
  }

  function showMoreDocuments(sectionId: string, total: number) {
    setVisibleDocumentCounts((current) => ({
      ...current,
      [sectionId]: Math.min((current[sectionId] ?? EXPLORER_SECTION_REVEAL_STEP) + EXPLORER_SECTION_REVEAL_STEP, total),
    }))
  }

  function showLessDocuments(sectionId: string) {
    setVisibleDocumentCounts((current) => ({
      ...current,
      [sectionId]: EXPLORER_SECTION_REVEAL_STEP,
    }))
  }

  useLayoutEffect(() => {
    if (!editingDocumentId) return
    documentTitleInputRef.current?.focus()
    documentTitleInputRef.current?.select()
  }, [editingDocumentId])

  useLayoutEffect(() => {
    if (!editingFolderId) return
    folderTitleInputRef.current?.focus()
    folderTitleInputRef.current?.select()
  }, [editingFolderId])

  function toggleFolder(folderId: string) {
    if (suppressFolderToggleClickRef.current) {
      suppressFolderToggleClickRef.current = false
      return
    }
    setExpandedFolderIds((current) => {
      const next = new Set(current)
      if (next.has(folderId)) {
        next.delete(folderId)
      } else {
        next.add(folderId)
      }
      return next
    })
  }

  function openDocumentFromTree(documentId: string) {
    if (suppressDocumentOpenClickRef.current) {
      suppressDocumentOpenClickRef.current = false
      return
    }
    dispatch({ documentId, type: 'openEditorDocument' })
  }

  function startDocumentTitleEdit(document: EditorDocumentRecord) {
    setEditingDocumentId(document.id)
    setDocumentTitleDraft(document.title)
  }

  function commitDocumentTitleEdit() {
    if (!editingDocumentId) return
    const title = documentTitleDraft.trim()
    if (title) {
      dispatch({ documentId: editingDocumentId, title, type: 'renameEditorDocument' })
    }
    setEditingDocumentId(null)
    setDocumentTitleDraft('')
  }

  function cancelDocumentTitleEdit() {
    setEditingDocumentId(null)
    setDocumentTitleDraft('')
  }

  function startFolderTitleEdit(folder: EditorFolderRecord) {
    setEditingFolderId(folder.id)
    setFolderTitleDraft(folder.title)
  }

  function commitFolderTitleEdit() {
    if (!editingFolderId) return
    const title = folderTitleDraft.trim()
    if (title) {
      dispatch({ folderId: editingFolderId, title, type: 'renameEditorFolder' })
    }
    setEditingFolderId(null)
    setFolderTitleDraft('')
  }

  function cancelFolderTitleEdit() {
    setEditingFolderId(null)
    setFolderTitleDraft('')
  }

  function readFolderDropTarget(clientY: number, excludedFolderId = draggedFolderId) {
    const container = listRef.current
    if (!container) return null
    const folderElements = Array.from(container.querySelectorAll<HTMLElement>('[data-editor-draggable-folder-id]'))
      .filter((folderElement) => (
        folderElement.dataset.editorDraggableFolderId !== excludedFolderId
        && folderElement.getBoundingClientRect().height > 0
      ))
    for (const [index, folderElement] of folderElements.entries()) {
      const rect = folderElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return index
    }
    return folderElements.length
  }

  function beginFolderDrag(event: ReactPointerEvent<HTMLElement>, folderId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function startDrag(moveEvent: PointerEvent) {
      didStartDrag = true
      suppressFolderToggleClickRef.current = true
      setDraggedFolderId(folderId)
      setFolderDropTargetIndex(readFolderDropTarget(moveEvent.clientY, folderId))
    }

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        startDrag(moveEvent)
      }
      moveEvent.preventDefault()
      setFolderDropTargetIndex(readFolderDropTarget(moveEvent.clientY, folderId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const nextDropTarget = didStartDrag ? readFolderDropTarget(upEvent.clientY, folderId) : null
      cleanupPointerDrag()
      if (nextDropTarget === null) return
      adoptVisibleOrderRef.current()
      dispatch({ folderId, targetIndex: nextDropTarget, type: 'moveEditorFolder' })
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cleanupPointerDrag)
      setDraggedFolderId(null)
      setFolderDropTargetIndex(null)
      if (didStartDrag) {
        window.setTimeout(() => {
          suppressFolderToggleClickRef.current = false
        }, 0)
      }
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cleanupPointerDrag)
  }

  function readDocumentDropTarget(clientY: number, excludedDocumentId = draggedDocumentId): EditorDocumentDropTarget | null {
    const container = listRef.current
    if (!container) return null
    const sectionElements = Array.from(container.querySelectorAll<HTMLElement>('[data-editor-document-section]'))
      .filter((sectionElement) => sectionElement.getBoundingClientRect().height > 0)
    if (sectionElements.length === 0) return null
    const sectionElement = sectionElements.find((candidate) => {
      const rect = candidate.getBoundingClientRect()
      return clientY >= rect.top - 8 && clientY <= rect.bottom + 8
    }) ?? sectionElements.reduce((nearest, candidate) => {
      const nearestRect = nearest.getBoundingClientRect()
      const candidateRect = candidate.getBoundingClientRect()
      const nearestDistance = Math.min(Math.abs(clientY - nearestRect.top), Math.abs(clientY - nearestRect.bottom))
      const candidateDistance = Math.min(Math.abs(clientY - candidateRect.top), Math.abs(clientY - candidateRect.bottom))
      return candidateDistance < nearestDistance ? candidate : nearest
    })
    const folderKey = sectionElement.dataset.editorFolderId ?? '__ungrouped__'
    const folderId = folderKey === '__ungrouped__' ? null : folderKey
    const documentElements = Array.from(sectionElement.querySelectorAll<HTMLElement>('[data-editor-document-id]'))
      .filter((documentElement) => (
        documentElement.dataset.editorDocumentId !== excludedDocumentId
        && documentElement.getBoundingClientRect().height > 0
      ))
    for (const [index, documentElement] of documentElements.entries()) {
      const rect = documentElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return { folderId, targetIndex: index }
    }
    return { folderId, targetIndex: documentElements.length }
  }

  function beginDocumentDrag(event: ReactPointerEvent<HTMLElement>, documentId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function startDrag(moveEvent: PointerEvent) {
      didStartDrag = true
      suppressDocumentOpenClickRef.current = true
      setDraggedDocumentId(documentId)
      setDocumentDropTarget(readDocumentDropTarget(moveEvent.clientY, documentId))
    }

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        startDrag(moveEvent)
      }
      moveEvent.preventDefault()
      setDocumentDropTarget(readDocumentDropTarget(moveEvent.clientY, documentId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const nextDropTarget = didStartDrag ? readDocumentDropTarget(upEvent.clientY, documentId) : null
      cleanupPointerDrag()
      if (!nextDropTarget) return
      adoptVisibleOrderRef.current()
      dispatch({
        documentId,
        folderId: nextDropTarget.folderId,
        targetIndex: nextDropTarget.targetIndex,
        type: 'moveEditorDocumentToFolder',
      })
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cleanupPointerDrag)
      setDraggedDocumentId(null)
      setDocumentDropTarget(null)
      if (didStartDrag) {
        window.setTimeout(() => {
          suppressDocumentOpenClickRef.current = false
        }, 0)
      }
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cleanupPointerDrag)
  }

  return (
    <aside className="inqtrix-contained-panel flex h-full w-full min-w-0 flex-col bg-surface">
      <div className="flex inqtrix-panel-header items-center justify-between border-b border-border px-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="t-section truncate">{copy.documents}</h2>
        </div>
        <div className="flex items-center gap-1">
          <ImportReportMenu copy={copy} dispatch={dispatch} reportOptions={reportOptions} />
          <ExplorerSortMenu
            mode={sortMode}
            onChangeMode={(mode) => dispatch({ desk: 'editor', mode, type: 'setExplorerSortMode' })}
          />
          <TooltipButton
            label={copy.createFolder}
            onClick={() => dispatch({ title: copy.createFolder, type: 'createEditorFolder' })}
          >
            <FolderPlus className="size-4" />
          </TooltipButton>
          <TooltipButton
            label={copy.createDocument}
            onClick={() => dispatch({ type: 'createEditorDocument' })}
          >
            <SquarePen className="size-4" />
          </TooltipButton>
        </div>
      </div>
      <ExplorerSearchField
        clearLabel={copy.searchClear}
        label={copy.searchDocuments}
        onChange={setSearchQuery}
        onClear={() => setSearchQuery('')}
        placeholder={copy.searchDocuments}
        value={searchQuery}
      />
      <ScrollArea className="min-h-0 flex-1">
        <div className="inqtrix-explorer-list space-y-1 p-2" ref={listRef}>
          {isSearching ? (
            searchResults.length > 0 ? (
              <div className="space-y-0.5">
                {searchResults.map((document) => (
                  <EditorDocumentTreeItem
                    beginDocumentDrag={beginDocumentDrag}
                    cancelTitleEdit={cancelDocumentTitleEdit}
                    commitTitleEdit={commitDocumentTitleEdit}
                    copy={copy}
                    document={document}
                    isActive={activeDocumentId === document.id}
                    isDragging={false}
                    isEditing={editingDocumentId === document.id}
                    isNested={false}
                    isPinned={pinnedDocumentIdSet.has(document.id)}
                    isRunning={runningDocumentId === document.id}
                    key={document.id}
                    onDelete={() => dispatch({ documentId: document.id, type: 'deleteEditorDocument' })}
                    onDetails={() => onDocumentDetails(document)}
                    onDraftChange={setDocumentTitleDraft}
                    onOpen={() => openDocumentFromTree(document.id)}
                    onPrefetch={() => onPrefetchDocument?.(document.id)}
                    onTogglePinned={() => dispatch({ documentId: document.id, type: 'togglePinnedEditorDocument' })}
                    showAfterIndicator={false}
                    showBeforeIndicator={false}
                    startTitleEdit={startDocumentTitleEdit}
                    titleDraft={documentTitleDraft}
                    titleInputRef={documentTitleInputRef}
                  />
                ))}
              </div>
            ) : (
              <p className="px-2 py-6 text-center t-meta-sm text-muted-foreground">{copy.searchNoResults}</p>
            )
          ) : (
          <>
          {pinnedDocuments.length > 0 ? (
            <section className="space-y-0.5">
              <ExplorerSectionLabel className="pt-0">{copy.pinned}</ExplorerSectionLabel>
              {pinnedDocuments.map((document) => (
                <EditorDocumentTreeItem
                  beginDocumentDrag={beginDocumentDrag}
                  cancelTitleEdit={cancelDocumentTitleEdit}
                  commitTitleEdit={commitDocumentTitleEdit}
                  copy={copy}
                  document={document}
                  isActive={activeDocumentId === document.id}
                  isDragging={draggedDocumentId === document.id}
                  isEditing={editingDocumentId === document.id}
                  isNested={false}
                  isPinned
                  isRunning={runningDocumentId === document.id}
                  key={document.id}
                  onDelete={() => dispatch({ documentId: document.id, type: 'deleteEditorDocument' })}
                  onDetails={() => onDocumentDetails(document)}
                  onDraftChange={setDocumentTitleDraft}
                  onOpen={() => openDocumentFromTree(document.id)}
                  onPrefetch={() => onPrefetchDocument?.(document.id)}
                  onTogglePinned={() => dispatch({ documentId: document.id, type: 'togglePinnedEditorDocument' })}
                  showAfterIndicator={false}
                  showBeforeIndicator={false}
                  startTitleEdit={startDocumentTitleEdit}
                  titleDraft={documentTitleDraft}
                  titleInputRef={documentTitleInputRef}
                />
              ))}
            </section>
          ) : null}
          {sharedDocuments.length > 0 ? (
            <section className="space-y-0.5" data-editor-shared-documents>
              <ExplorerSectionLabel className="flex items-center justify-between">
                <span>{t.sharing.sharedWithMe}</span>
                <span
                  className="t-hint tabular-nums text-muted-foreground"
                >
                  <span aria-hidden="true">{sharedDocuments.length}</span>
                  <span className="sr-only">
                    {sharedDocuments.length} {t.sharing.sharedBadge}
                  </span>
                </span>
              </ExplorerSectionLabel>
              {/* No cap: shared documents are their own list, not a folder. */}
              {sharedDocuments.map((document) => (
                  <EditorDocumentTreeItem
                    beginDocumentDrag={beginDocumentDrag}
                    cancelTitleEdit={cancelDocumentTitleEdit}
                    commitTitleEdit={commitDocumentTitleEdit}
                    copy={copy}
                    document={document}
                    isActive={activeDocumentId === document.id}
                    isDragging={false}
                    isEditing={false}
                    isNested={false}
                    isPinned={false}
                    isRunning={runningDocumentId === document.id}
                    key={document.id}
                    onDelete={() => undefined}
                    onDetails={() => onDocumentDetails(document)}
                    onDraftChange={setDocumentTitleDraft}
                    onOpen={() => openDocumentFromTree(document.id)}
                    onPrefetch={() => onPrefetchDocument?.(document.id)}
                    onTogglePinned={() => undefined}
                    showAfterIndicator={false}
                    showBeforeIndicator={false}
                    startTitleEdit={startDocumentTitleEdit}
                    titleDraft={documentTitleDraft}
                    titleInputRef={documentTitleInputRef}
                  />
                ))}
            </section>
          ) : null}
          <div
            className={cn(
              'space-y-1',
              sharedDocuments.length > 0 && 'mt-2 border-t border-border pt-1',
            )}
            data-editor-owned-documents
          >
            <ExplorerSectionLabel>
              {locale === 'de' ? 'Meine Dokumente' : 'My documents'}
            </ExplorerSectionLabel>
          {orderedFolders.map((folder, folderIndex) => {
            const isExpanded = expandedFolderIds.has(folder.id)
            const isDraggingFolder = draggedFolderId === folder.id
            const showFolderBeforeIndicator = folderDropTargetIndex === folderIndex
            const showFolderAfterIndicator = folderDropTargetIndex === folders.length && folderIndex === folders.length - 1
            const showDropFrame = documentDropTarget?.folderId === folder.id
            const folderDocuments = treeDocuments.filter((document) => document.folderId === folder.id)
            const visibleCount = visibleDocumentCount(folder.id)
            return (
              <section
                className={cn(
                  'relative transition-colors',
                  showDropFrame && 'bg-brand-subtle/45',
                  isDraggingFolder && 'scale-[0.995] opacity-80 shadow-[0_8px_20px_var(--shadow-soft)] ring-1 ring-ring/40',
                )}
                data-editor-document-section
                data-editor-draggable-folder-id={folder.id}
                data-editor-folder-id={folder.id}
                key={folder.id}
              >
                {showFolderBeforeIndicator ? <DropIndicator className="-top-1" /> : null}
                {showFolderAfterIndicator ? <DropIndicator className="-bottom-1" /> : null}
                <ExplorerFolderRow
                  onPointerDown={(event) => beginFolderDrag(event, folder.id)}
                  actions={(
                    <>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            aria-label={copy.options}
                            className="size-6 shrink-0 text-foreground/55 hover:text-foreground"
                            size="icon"
                            type="button"
                            variant="ghost"
                          >
                            <MoreHorizontal className="icon-sm" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-56">
                          <DropdownMenuItem onSelect={() => startFolderTitleEdit(folder)}>
                            <PencilLine className="icon-sm" />
                            {copy.renameFolder}
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            className="text-destructive focus:text-destructive"
                            onSelect={() => dispatch({ folderId: folder.id, type: 'deleteEditorFolder' })}
                          >
                            <Trash2 className="icon-sm" />
                            {copy.deleteFolder}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                      <button
                        aria-label={copy.createDocument}
                        className="grid size-6 shrink-0 place-items-center rounded-sm text-foreground/50 transition hover:bg-surface hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        onClick={() => dispatch({ folderId: folder.id, type: 'createEditorDocument' })}
                        type="button"
                      >
                        <SquarePen className="icon-sm" />
                      </button>
                    </>
                  )}
                >
                  {editingFolderId === folder.id ? (
                    <span className="flex min-h-8 min-w-0 items-center gap-1.5" data-explorer-action>
                      <FolderOpen className="icon-sm shrink-0 text-muted-foreground" />
                      <input
                        aria-label={copy.renameFolder}
                        className="min-w-0 flex-1 rounded-sm border-0 bg-background/85 px-1.5 py-0.5 t-list text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        onBlur={commitFolderTitleEdit}
                        onChange={(event) => setFolderTitleDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.preventDefault()
                            commitFolderTitleEdit()
                          }
                          if (event.key === 'Escape') {
                            event.preventDefault()
                            cancelFolderTitleEdit()
                          }
                        }}
                        ref={folderTitleInputRef}
                        value={folderTitleDraft}
                      />
                    </span>
                  ) : (
                    <ExplorerFolderToggle
                      count={folderDocuments.length}
                      expanded={isExpanded}
                      icon={isExpanded ? <FolderOpen className="icon-sm shrink-0" /> : <Folder className="icon-sm shrink-0" />}
                      label={`${isExpanded ? copy.hideTree : copy.showTree}: ${folder.title}`}
                      onDoubleClick={(event) => {
                        event.preventDefault()
                        startFolderTitleEdit(folder)
                      }}
                      onToggle={() => toggleFolder(folder.id)}
                      title={folder.title}
                    />
                  )}
                </ExplorerFolderRow>
                {isExpanded ? (
                  <div className="space-y-0.5">
                    {folderDocuments.slice(0, visibleCount).map((document, index) => (
                      <EditorDocumentTreeItem
                        beginDocumentDrag={beginDocumentDrag}
                        cancelTitleEdit={cancelDocumentTitleEdit}
                        commitTitleEdit={commitDocumentTitleEdit}
                        copy={copy}
                        document={document}
                        isActive={activeDocumentId === document.id}
                        isDragging={draggedDocumentId === document.id}
                        isEditing={editingDocumentId === document.id}
                        isNested
                        isPinned={false}
                        isRunning={runningDocumentId === document.id}
                        key={document.id}
                        onDelete={() => dispatch({ documentId: document.id, type: 'deleteEditorDocument' })}
                        onDetails={() => onDocumentDetails(document)}
                        onDraftChange={setDocumentTitleDraft}
                        onOpen={() => openDocumentFromTree(document.id)}
                        onPrefetch={() => onPrefetchDocument?.(document.id)}
                        onTogglePinned={() => dispatch({ documentId: document.id, type: 'togglePinnedEditorDocument' })}
                        showAfterIndicator={documentDropTarget?.folderId === folder.id && documentDropTarget.targetIndex === Math.min(visibleCount, folderDocuments.length) && index === Math.min(visibleCount, folderDocuments.length) - 1}
                        showBeforeIndicator={documentDropTarget?.folderId === folder.id && documentDropTarget.targetIndex === index}
                        startTitleEdit={startDocumentTitleEdit}
                        titleDraft={documentTitleDraft}
                        titleInputRef={documentTitleInputRef}
                      />
                    ))}
                    <ExplorerRevealControls
                      onShowLess={() => showLessDocuments(folder.id)}
                      onShowMore={() => showMoreDocuments(folder.id, folderDocuments.length)}
                      showLessLabel={copy.showLess}
                      showMoreLabel={copy.showMore}
                      total={folderDocuments.length}
                      visibleCount={visibleCount}
                    />
                    {folderDocuments.length === 0 ? (
                      <p className="rounded-md px-2 py-1.5 t-meta-sm font-medium text-muted-foreground">{copy.dropIntoFolder}</p>
                    ) : null}
                  </div>
                ) : null}
              </section>
            )
          })}
          {ungroupedDocuments.length > 0 || documents.length === 0 || hasFolders ? (
            <section
              className={cn('relative space-y-0.5 rounded-md', documentDropTarget?.folderId === null && 'bg-brand-subtle/30')}
              data-editor-document-section
              data-editor-folder-id="__ungrouped__"
            >
              {/* No cap: the ungrouped list is the rail itself. */}
              {ungroupedDocuments.map((document, index) => (
                <EditorDocumentTreeItem
                  beginDocumentDrag={beginDocumentDrag}
                  cancelTitleEdit={cancelDocumentTitleEdit}
                  commitTitleEdit={commitDocumentTitleEdit}
                  copy={copy}
                  document={document}
                  isActive={activeDocumentId === document.id}
                  isDragging={draggedDocumentId === document.id}
                  isEditing={editingDocumentId === document.id}
                  isNested={false}
                  isPinned={false}
                  isRunning={runningDocumentId === document.id}
                  key={document.id}
                  onDelete={() => dispatch({ documentId: document.id, type: 'deleteEditorDocument' })}
                  onDetails={() => onDocumentDetails(document)}
                  onDraftChange={setDocumentTitleDraft}
                  onOpen={() => openDocumentFromTree(document.id)}
                  onPrefetch={() => onPrefetchDocument?.(document.id)}
                  onTogglePinned={() => dispatch({ documentId: document.id, type: 'togglePinnedEditorDocument' })}
                  showAfterIndicator={documentDropTarget?.folderId === null && documentDropTarget.targetIndex === ungroupedDocuments.length && index === ungroupedDocuments.length - 1}
                  showBeforeIndicator={documentDropTarget?.folderId === null && documentDropTarget.targetIndex === index}
                  startTitleEdit={startDocumentTitleEdit}
                  titleDraft={documentTitleDraft}
                  titleInputRef={documentTitleInputRef}
                />
              ))}
              {documents.length === 0 ? (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">{copy.noDocuments}</p>
              ) : null}
            </section>
          ) : null}
          </div>
          </>
          )}
        </div>
      </ScrollArea>
      <QuotaUsageFooter dimensions={['llm_tokens']} />
    </aside>
  )
}

function EditorDocumentTreeItem({
  beginDocumentDrag,
  cancelTitleEdit,
  commitTitleEdit,
  copy,
  document,
  isActive,
  isDragging,
  isEditing,
  isNested,
  isPinned,
  isRunning,
  onDelete,
  onDetails,
  onDraftChange,
  onOpen,
  onPrefetch,
  onTogglePinned,
  showAfterIndicator,
  showBeforeIndicator,
  startTitleEdit,
  titleDraft,
  titleInputRef,
}: {
  beginDocumentDrag: (event: ReactPointerEvent<HTMLElement>, documentId: string) => void
  cancelTitleEdit: () => void
  commitTitleEdit: () => void
  copy: EditorCopy
  document: EditorDocumentRecord
  isActive: boolean
  isDragging: boolean
  isEditing: boolean
  isNested: boolean
  isPinned: boolean
  isRunning: boolean
  onDelete: () => void
  onDetails: () => void
  onDraftChange: (value: string) => void
  onOpen: () => void
  onPrefetch?: () => void
  onTogglePinned: () => void
  showAfterIndicator: boolean
  showBeforeIndicator: boolean
  startTitleEdit: (document: EditorDocumentRecord) => void
  titleDraft: string
  titleInputRef: RefObject<HTMLInputElement | null>
}) {
  const { locale, t } = useLocale()
  const timeLabel = displayRelativeAge(document.updatedAt, locale)
  const permission = sharePermissionLabel(document.access?.permission ?? 'view', locale, {
    edit: t.sharing.permissionEdit,
    view: t.sharing.permissionView,
  })
  const ownerName = document.access?.owner?.name
    ?? (locale === 'de' ? 'Unbekannter Nutzer' : 'Unknown user')
  const shareDetails = t.sharing.sharedBy
    .replace('{name}', ownerName)
    .concat(` · ${permission}`)
  // One grammar for every row: the leading icon says who is involved, the
  // trailing hover actions say what can be done — and the details action
  // appears exactly where a details page exists.
  const rowKind = editorDocumentRowKind(document)
  const railCapabilities = editorDocumentRailCapabilities(document)
  const detailsLabel = locale === 'de' ? 'Dokumentdetails' : 'Document details'
  const title = (
    <span
      className="flex min-w-0 items-center gap-1.5"
      title={document.recovery
        ? locale === 'de' ? 'Lokales Recovery-Artefakt' : 'Local recovery artifact'
        : rowKind === 'shared-with-me' ? shareDetails : undefined}
    >
      {railCapabilities.leadingRole === 'file' ? (
        <FileText aria-hidden="true" className="icon-sm shrink-0 text-muted-foreground" />
      ) : (
        <Users
          aria-label={rowKind === 'owned-shared' ? t.sharing.collaborationBadge : t.sharing.sharedBadge}
          className="icon-sm shrink-0 text-muted-foreground"
        />
      )}
      <span className="min-w-0 truncate">{document.title}</span>
      {document.recovery ? (
        <span className="shrink-0 rounded-sm border border-warning/40 bg-warning-subtle px-1 py-0.5 t-hint text-foreground">
          Recovery
        </span>
      ) : null}
    </span>
  )
  const detailsAction = {
    ariaLabel: `${detailsLabel}: ${document.title}`,
    icon: <Info className="icon-sm" />,
    label: detailsLabel,
    onSelect: onDetails,
  }
  const ownerActions = [
    // A private document has no details page — offering the icon would open
    // an unrelated activation gate, so it only appears once details exist.
    ...(railCapabilities.canOpenDetails ? [detailsAction] : []),
    {
      ariaLabel: `${isPinned ? copy.unpinDocument : copy.pinDocument}: ${document.title}`,
      icon: isPinned ? <PinOff className="icon-sm" /> : <Pin className="icon-sm" />,
      label: isPinned ? copy.unpinDocument : copy.pinDocument,
      onSelect: onTogglePinned,
    },
    {
      ariaLabel: `${copy.deleteDocument}: ${document.title}`,
      destructive: true,
      icon: <Trash2 className="icon-sm" />,
      label: copy.deleteDocument,
      onSelect: onDelete,
    },
  ]

  return (
    <div
      className="group/editor-document relative"
      data-editor-document-id={document.id}
      onFocusCapture={onPrefetch}
      onPointerEnter={onPrefetch}
    >
      {showBeforeIndicator ? <DropIndicator className="-top-1" /> : null}
      {showAfterIndicator ? <DropIndicator className="-bottom-1" /> : null}
      <ExplorerHistoryRow
        actions={railCapabilities.canDelete ? ownerActions : [detailsAction]}
        active={isActive}
        dragging={isDragging}
        indicator={isRunning ? <ExplorerRunningIndicator label={copy.runningSuggestion} /> : undefined}
        nested={isNested}
        onPointerDown={railCapabilities.canDrag
          ? (event) => beginDocumentDrag(event, document.id)
          : undefined}
        onSelect={onOpen}
        onStartRename={railCapabilities.canRename ? () => startTitleEdit(document) : undefined}
        renameEditor={railCapabilities.canRename && isEditing ? (
          <ExplorerHistoryTitleInput
            inputRef={titleInputRef}
            label={copy.renameDocument}
            onCancel={cancelTitleEdit}
            onChange={onDraftChange}
            onCommit={commitTitleEdit}
            value={titleDraft}
          />
        ) : undefined}
        renameLabel={railCapabilities.canRename ? copy.renameDocument : undefined}
        timeLabel={timeLabel}
        title={title}
      />
    </div>
  )
}

function DropIndicator({ className }: { className?: string }) {
  return (
    <span className={cn('pointer-events-none absolute left-1 right-1 z-20 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]', className)} />
  )
}

function countEditorDocumentWords(markdown: string): number {
  return markdown.match(/[\p{L}\p{N}]+(?:['’\u2010-\u2015][\p{L}\p{N}]+)*/gu)
    ?.length ?? 0
}

function ImportReportMenu({
  copy,
  dispatch,
  reportOptions,
  triggerClassName,
  variant = 'icon',
}: {
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  reportOptions: CompletedReportOption[]
  triggerClassName?: string
  variant?: 'button' | 'icon'
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={copy.importReport}
          className={cn(
            variant === 'icon' && 'size-8 rounded-md',
            variant === 'button' && 'justify-center',
            triggerClassName,
          )}
          size={variant === 'button' ? 'default' : 'icon'}
          type="button"
          variant={variant === 'button' ? 'outline' : 'ghost'}
        >
          <BookOpen className="size-4" />
          {variant === 'button' ? <span className="min-w-0 truncate">{copy.importReport}</span> : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-64 max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl p-0 shadow-lg"
      >
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
          <span className="t-meta-sm font-medium text-muted-foreground">{copy.importReport}</span>
          <span className="ml-auto t-hint tabular-nums text-muted-foreground/50">
            {reportOptions.length}
          </span>
        </div>
        <div className="py-1">
          {reportOptions.length === 0 ? (
            <div className="px-2.5 py-2 t-meta text-muted-foreground">
              {copy.noReports}
            </div>
          ) : reportOptions.map((report) => (
            <DropdownMenuItem
              className="group relative w-full min-w-0 items-start gap-2.5 rounded-none px-2.5 py-1.5 hover:bg-accent/50 focus:bg-accent/80 data-[highlighted]:bg-accent/80"
              key={report.runId}
              onSelect={() => dispatch({ runId: report.runId, type: 'importResearchReportToEditor' })}
            >
              <span className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-brand opacity-0 transition-opacity group-hover:opacity-100 group-focus:opacity-100 group-data-[highlighted]:opacity-100" />
              <FileText className="mt-0.5 icon-md shrink-0 text-muted-foreground/70 transition-colors group-hover:text-brand group-focus:text-brand group-data-[highlighted]:text-brand" />
              <span className="min-w-0 flex-1">
                <span className="block max-w-full truncate t-list text-foreground">
                  @research:{report.label}
                </span>
                <span className="block max-w-full truncate t-meta-sm text-muted-foreground">
                  {report.title}
                </span>
              </span>
            </DropdownMenuItem>
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * The single header control that collapses/expands one editor side panel (file
 * tree on the left, comments on the right). Lives at the leading/trailing edge
 * of the editor header and stays put across the document/empty-state swap, so
 * the toggles never move and a collapsed panel always has a reopen path.
 */
function EditorPanelToggle({
  copy,
  dispatch,
  onToggle,
  side,
  visible,
}: {
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  onToggle?: (visible: boolean) => void
  side: 'left' | 'right'
  visible: boolean
}) {
  return side === 'left' ? (
    <PanelToggle
      collapseLabel={copy.hideTree}
      controlsId={EDITOR_TREE_PANEL_ID}
      expandLabel={copy.showTree}
      expanded={visible}
      onToggle={(next) => {
        if (onToggle) {
          onToggle(next)
          return
        }
        dispatch({ isVisible: next, type: 'setEditorTreeVisible' })
      }}
      side="left"
    />
  ) : (
    <PanelToggle
      collapseLabel={copy.hideComments}
      controlsId={EDITOR_COMMENTS_PANEL_ID}
      expandLabel={copy.showComments}
      expanded={visible}
      onToggle={(next) => {
        if (onToggle) {
          onToggle(next)
          return
        }
        dispatch({ isVisible: next, type: 'setEditorCommentPanelVisible' })
      }}
      side="right"
    />
  )
}

export function editorRecoveryMarkdown({
  commentDrafts,
  documentMarkdown,
  locale,
  title,
}: {
  commentDrafts: readonly string[]
  documentMarkdown: string
  locale: 'de' | 'en'
  title: string
}): string {
  const normalizedTitle = title.replace(/\s+/g, ' ').trim()
  const body = documentMarkdown.trim()
    || `# ${normalizedTitle || (locale === 'de' ? 'Wiederhergestelltes Dokument' : 'Recovered document')}`
  if (commentDrafts.length === 0) return body
  const sectionTitle = locale === 'de'
    ? 'Nicht gesendete Kommentarentwürfe'
    : 'Unsent comment drafts'
  const drafts = commentDrafts.map((draft, index) => (
    `### ${locale === 'de' ? 'Entwurf' : 'Draft'} ${index + 1}\n\n${draft}`
  ))
  return `${body}\n\n---\n\n## ${sectionTitle}\n\n${drafts.join('\n\n')}`
}

/** A local recovery copy: why it exists, and the two decisions it offers. */
export function EditorRecoveryBar({
  locale,
  onDiscard,
  onSaveAsNew,
  reason,
}: {
  locale: 'de' | 'en'
  onDiscard: () => void
  onSaveAsNew: () => void
  reason: EditorDocumentRecoveryReason
}) {
  const copy = editorCopy[locale]
  return (
    <div
      className="flex flex-wrap items-center gap-2 border-b border-warning/40 bg-warning-subtle px-3 py-2"
      data-editor-recovery-artifact={reason}
      role="alert"
    >
      <AlertTriangle className="icon-sm shrink-0 text-warning" />
      <p className="t-meta min-w-64 flex-1 text-foreground">
        {reason === 'generation_replaced'
          ? copy.recoveryGenerationReplaced
          : copy.recoveryRemoteDeleted}
      </p>
      <Button onClick={onSaveAsNew} size="sm" type="button" variant="outline">
        {copy.recoverySaveAsNew}
      </Button>
      <Button onClick={onDiscard} size="sm" type="button" variant="ghost">
        {copy.recoveryDiscard}
      </Button>
    </div>
  )
}

/** What an open document says about local recovery copies: a recovery copy
 * shows its own bar; the original of a replaced generation whose unsaved
 * state became a copy shows the notice that opens it; every other document
 * shows nothing. */
export function EditorDocumentRecoveryNotice({
  dispatch,
  document,
  documents,
  locale,
}: {
  dispatch: Dispatch<ResearchDeskAction>
  document: EditorDocumentRecord
  documents: Readonly<Record<string, EditorDocumentRecord>>
  locale: 'de' | 'en'
}) {
  const recoveryCopy = useMemo(() => (
    document.recovery ? null : replacedGenerationRecoveryCopy(documents, document.id)
  ), [document.id, document.recovery, documents])
  if (document.recovery) {
    return (
      <EditorRecoveryBar
        locale={locale}
        reason={document.recovery.reason}
        onDiscard={() => dispatch({
          documentId: document.id,
          type: 'deleteEditorDocument',
        })}
        onSaveAsNew={() => dispatch({
          documentId: document.id,
          type: 'promoteEditorRecoveryDocument',
        })}
      />
    )
  }
  if (!recoveryCopy) return null
  return (
    <CollaborationRecoveryCopyNotice
      locale={locale}
      onOpenCopy={() => dispatch({
        documentId: recoveryCopy.id,
        type: 'openEditorDocument',
      })}
    />
  )
}

function CollaborationRecoveryBar({
  hasUnconfirmedLocalChanges,
  locale,
  onReconnect,
}: {
  hasUnconfirmedLocalChanges: boolean
  locale: 'de' | 'en'
  onReconnect: () => Promise<void>
}) {
  const [pending, setPending] = useState(false)
  const reconnect = () => {
    if (pending) return
    setPending(true)
    void onReconnect().finally(() => setPending(false))
  }
  return (
    <div
      className="flex min-h-9 items-center gap-2 border-b border-border bg-warning-subtle px-3 py-1"
      role="alert"
    >
      <AlertTriangle className="icon-sm shrink-0 text-warning" />
      <p className="t-meta min-w-0 flex-1 text-foreground">
        {locale === 'de'
          ? hasUnconfirmedLocalChanges
            ? 'Die Verbindung ist unterbrochen. Nicht bestätigte Änderungen bleiben erhalten.'
            : 'Die Verbindung konnte mehrfach nicht wiederhergestellt werden.'
          : hasUnconfirmedLocalChanges
            ? 'The connection is interrupted. Unconfirmed changes remain preserved.'
            : 'The connection could not be restored after several attempts.'}
      </p>
      <Button
        disabled={pending}
        onClick={reconnect}
        size="sm"
        type="button"
        variant="outline"
      >
        <RefreshCw className={pending ? 'animate-spin' : undefined} />
        {locale === 'de' ? 'Jetzt erneut verbinden' : 'Reconnect now'}
      </Button>
    </div>
  )
}

export function EditorTopBar({
  collaborationAccess,
  collaborationActive,
  collaborationCanEdit,
  collaborationStatus,
  copy,
  canFlushForShare,
  diffAnchorError,
  diffAnchorDisabledReason,
  diffAnchorPending,
  dispatch,
  document,
  editor,
  isCommentPanelVisible,
  isDiffVisible,
  isDirty,
  isTreeVisible,
  onCommentPanelVisibleChange,
  onCollaborationLogin,
  onCollaborationReconnect,
  onCollaborationReload,
  onExportWordMarkdown,
  onSetDiffAnchor,
  onShareDocument,
  onTreeVisibleChange,
  onWriteModeChange,
  sharingAvailable,
  viewMode,
  writeMode,
}: {
  /** The share flow flushes the document itself before converting, so the
   * dirty/server-ready gates on the Share button do not apply. */
  canFlushForShare: boolean
  collaborationAccess: 'comment' | 'edit' | 'suggest' | 'view' | null
  collaborationActive: boolean
  collaborationCanEdit: boolean
  collaborationStatus: EditorCollaborationStatusModel
  copy: EditorCopy
  diffAnchorError: string | null
  diffAnchorDisabledReason: string | null
  diffAnchorPending: boolean
  dispatch: Dispatch<ResearchDeskAction>
  document: EditorDocumentRecord
  editor: Editor | null
  isCommentPanelVisible: boolean
  isDiffVisible: boolean
  isDirty: boolean
  isTreeVisible: boolean
  onCommentPanelVisibleChange?: (visible: boolean) => void
  onCollaborationLogin?: () => void
  onCollaborationReconnect?: () => Promise<void>
  onCollaborationReload?: () => void
  onExportWordMarkdown: () => Promise<string>
  onSetDiffAnchor: () => void
  onShareDocument: (
    document: EditorDocumentRecord,
    returnFocusTarget?: HTMLElement | null,
  ) => void
  onTreeVisibleChange?: (visible: boolean) => void
  onWriteModeChange: (mode: 'comment' | 'edit' | 'suggest') => void
  sharingAvailable: boolean
  viewMode: ProjectState['editorUi']['viewMode']
  writeMode: 'comment' | 'edit' | 'suggest' | 'view'
}) {
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState(document.title)
  const titleInputRef = useRef<HTMLInputElement | null>(null)
  const overflowTriggerRef = useRef<HTMLButtonElement | null>(null)
  const shareDialogRequestedRef = useRef(false)
  const [isExporting, setIsExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)
  const ownerControls = isOwnedEditorDocument(document)
  const { locale } = useLocale()

  useLayoutEffect(() => {
    if (!isEditingTitle) return
    titleInputRef.current?.focus()
    titleInputRef.current?.select()
  }, [isEditingTitle])

  useEffect(() => {
    if (isEditingTitle) return
    setTitleDraft(document.title)
  }, [document.title, isEditingTitle])

  function commitTitleEdit() {
    const title = titleDraft.trim()
    if (title) {
      dispatch({ documentId: document.id, title, type: 'renameEditorDocument' })
    } else {
      setTitleDraft(document.title)
    }
    setIsEditingTitle(false)
  }

  async function handleExportWord() {
    if (isExporting) return
    setExportError(null)
    setIsExporting(true)
    try {
      const { exportMarkdownToDocx } = await import('./export/docxExport')
      const markdown = await onExportWordMarkdown()
      await exportMarkdownToDocx(markdown, document.title)
    } catch (error) {
      setExportError(error instanceof Error ? error.message : copy.exportWordFailed)
      console.error('Inqtrix Word export failed.', error)
    } finally {
      setIsExporting(false)
    }
  }

  const provenanceLabel = document.sourceRunId
    ? document.source === 'agent-artifact'
      ? copy.importedFromAgent
      : document.source === 'imported-research-report'
        ? copy.importedFrom
        : null
    : null
  const documentStatusText = provenanceLabel
    ? `${provenanceLabel} ${document.sourceRunId} · ${copy.updated} ${formatEditorTime(document.updatedAt)}`
    : `${copy.updated} ${formatEditorTime(document.updatedAt)}`
  const shareDisabled = editorDocumentShareDisabled({
    canFlushForShare,
    document,
    isDirty,
    sharingAvailable,
  })
  const diffAnchorDisabled = !ownerControls
    || diffAnchorPending
    || Boolean(diffAnchorDisabledReason)
  const overflowLabel = locale === 'de' ? 'Weitere Dokumentaktionen' : 'More document actions'
  const shareLabel = locale === 'de' ? 'Dokument teilen' : 'Share document'

  return (
    <EditorTopBarLayout
      actions={(
        <>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={overflowLabel}
                className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
                data-editor-topbar-overflow
                ref={overflowTriggerRef}
                size="icon"
                title={overflowLabel}
                type="button"
                variant="ghost"
              >
                <MoreHorizontal className="icon-sm" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-56"
              onCloseAutoFocus={(event) => {
                if (!shareDialogRequestedRef.current) return
                shareDialogRequestedRef.current = false
                event.preventDefault()
              }}
            >
              {ownerControls ? (
                <DropdownMenuItem
                  disabled={shareDisabled}
                  onSelect={() => {
                    shareDialogRequestedRef.current = true
                    overflowTriggerRef.current?.focus({ preventScroll: true })
                    onShareDocument(document, overflowTriggerRef.current)
                  }}
                >
                  <Share2 />
                  {shareLabel}
                </DropdownMenuItem>
              ) : null}
              <DropdownMenuItem
                onSelect={() => dispatch({
                  mode: viewMode === 'source' ? 'live' : 'source',
                  type: 'setEditorViewMode',
                })}
              >
                {viewMode === 'source' ? <Eye /> : <Code2 />}
                {viewMode === 'source' ? copy.live : copy.source}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={diffAnchorDisabled} onSelect={onSetDiffAnchor}>
                <Anchor />
                {diffAnchorError ?? diffAnchorDisabledReason ?? copy.setDiffAnchor}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => dispatch({
                  isVisible: !isDiffVisible,
                  type: 'setEditorDiffVisible',
                })}
              >
                <Scale />
                {isDiffVisible ? copy.hideDiff : copy.showDiff}
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={isExporting}
                onSelect={() => { void handleExportWord() }}
              >
                <FileDown />
                {exportError ?? copy.exportWord}
              </DropdownMenuItem>
              {ownerControls ? (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    className="text-destructive focus:text-destructive"
                    onSelect={() => dispatch({
                      documentId: document.id,
                      type: 'deleteEditorDocument',
                    })}
                  >
                    <Trash2 />
                    {copy.deleteDocument}
                  </DropdownMenuItem>
                </>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
          <EditorPanelToggle
            copy={copy}
            dispatch={dispatch}
            onToggle={onCommentPanelVisibleChange}
            side="right"
            visible={isCommentPanelVisible}
          />
        </>
      )}
      leading={(
        <>
        <EditorPanelToggle
          copy={copy}
          dispatch={dispatch}
          onToggle={onTreeVisibleChange}
          side="left"
          visible={isTreeVisible}
        />
        <div className="min-w-0" data-editor-topbar-title title={documentStatusText}>
          <div className="flex min-w-0 items-center gap-1.5">
            {isEditingTitle ? (
              <input
                aria-label={copy.renameDocument}
                className="t-section min-w-0 flex-1 rounded-sm border-0 bg-transparent px-0 text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                onBlur={commitTitleEdit}
                onChange={(event) => setTitleDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    commitTitleEdit()
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    setTitleDraft(document.title)
                    setIsEditingTitle(false)
                  }
                }}
                ref={titleInputRef}
                value={titleDraft}
              />
            ) : (
              <button
                className={cn(
                  't-section min-w-0 flex-1 truncate rounded-sm text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
                  ownerControls && 'hover:text-brand',
                )}
                disabled={!ownerControls}
                onDoubleClick={() => {
                  if (ownerControls) setIsEditingTitle(true)
                }}
                title={`${copy.renameDocument} · ${documentStatusText}`}
                type="button"
              >
                {document.title}
              </button>
            )}
            {isDirty ? (
              // role makes the label count: a bare span carries no name for
              // assistive technology, so "unsaved changes" existed only as a
              // coloured dot. status also announces the change when it appears.
              <span
                className="size-1.5 shrink-0 rounded-full bg-brand"
                role="status"
                aria-label="Unsaved changes"
              />
            ) : null}
          </div>
        </div>
        </>
      )}
      primary={(
        <>
          <EditorWriteModeControl
            access={collaborationAccess}
            canEdit={collaborationCanEdit}
            collaborationActive={collaborationActive}
            mode={writeMode}
            onModeChange={onWriteModeChange}
            sourceReadOnly={viewMode === 'source'}
          />
          {collaborationActive ? (
            <EditorCollaborationStatus
              collaborationExpected
              model={collaborationStatus}
              onLogin={onCollaborationLogin}
              onReconnect={onCollaborationReconnect}
              onReload={onCollaborationReload}
              variant="topbar"
            />
          ) : null}
        </>
      )}
      toolbar={(
        <EditorCommandToolbar
          editor={editor}
          isSource={viewMode === 'source' || writeMode === 'comment' || writeMode === 'view'}
        />
      )}
    />
  )
}

export function editorDocumentShareDisabled({
  canFlushForShare,
  document,
  isDirty,
  sharingAvailable,
}: {
  canFlushForShare: boolean
  document: EditorDocumentRecord
  isDirty: boolean
  sharingAvailable: boolean
}): boolean {
  return !isOwnedEditorDocument(document)
    || document.recovery !== undefined
    || (!canFlushForShare && isDirty)
    || (!canFlushForShare && document.metadataRevision === undefined)
    || !sharingAvailable
}

function EditorCommandToolbar({
  editor,
  isSource,
}: {
  editor: Editor | null
  isSource: boolean
}) {
  const disabled = !editor || isSource
  const undoDisabled = disabled
    || !canRunEditorHistoryCommand(editor, 'undo')
  const redoDisabled = disabled
    || !canRunEditorHistoryCommand(editor, 'redo')

  return (
    <div className="flex min-w-0 items-center justify-center gap-0.5 px-1">
      <ToolbarButton
        disabled={undoDisabled}
        icon={Undo2}
        label="Undo"
        onClick={() => runEditorHistoryCommand(editor, 'undo')}
      />
      <ToolbarButton
        disabled={redoDisabled}
        icon={Redo2}
        label="Redo"
        onClick={() => runEditorHistoryCommand(editor, 'redo')}
      />
    </div>
  )
}

export function EditorAssistantComposer({
  aiReadOnlyReason,
  attachedCommentIds,
  attachmentChips,
  chatModelOptions,
  chatModelOptionsStatus,
  comments,
  composerRef,
  copy,
  defaultChatModel,
  dispatch,
  draft,
  fileGroupOptions,
  fileOptions,
  handedOverDiscussions,
  reportOptions,
  isAttachActive,
  instructionFeedback,
  isRunning,
  isVisible,
  isWideCanvas,
  onAttachFiles,
  onAttachRule,
  onRefsChange,
  onReorderPending,
  onReorderPill,
  onRemoveAttachedComment,
  onRemoveChip,
  onRemoveHandedOverDiscussion,
  onRetryAttachment,
  pendingKeys,
  pillKeys,
  replaceableSuggestionCount,
  onSend,
  onDismissInstructionFeedback,
  onStop,
  onToggleAttach,
  ruleOptions,
  selectedModelTier,
  chatModelCatalog,
  selectedModel,
  selectedEffort,
  editorContextBase,
  editorContextCapacity,
  textImprovement,
}: {
  aiReadOnlyReason: string | null
  attachedCommentIds: string[]
  attachmentChips: ChatAttachmentChipModel[]
  chatModelOptions: ChatModelOption[]
  chatModelOptionsStatus: 'available' | 'missing' | 'unresolved'
  comments: EditorCommentThreadRecord[]
  composerRef: RefObject<MentionComposerHandle | null>
  copy: EditorCopy
  defaultChatModel: NodeModelResolution | null
  dispatch: Dispatch<ResearchDeskAction>
  draft: string
  fileGroupOptions: FileGroupMentionOption[]
  fileOptions: FileMentionOption[]
  /** Team threads handed over with "Use with assistant". They are context,
   * not a request: they never make the composer sendable on their own. */
  handedOverDiscussions: readonly HandedOverDiscussion[]
  reportOptions: CompletedReportOption[]
  isAttachActive: boolean
  instructionFeedback: EditorInstructionFeedback | null
  isRunning: boolean
  isVisible: boolean
  isWideCanvas: boolean
  onAttachFiles: (files: File[]) => void
  onAttachRule: (ruleId: string) => void
  onRefsChange: (refs: ChatContextReferenceRecord[]) => void
  onReorderPending: (fromIndex: number, toIndex: number) => void
  onReorderPill: (fromIndex: number, toIndex: number) => void
  onRemoveAttachedComment: (commentId: string) => void
  onRemoveChip: (ref: ChatContextReferenceRecord) => void
  onRemoveHandedOverDiscussion: (threadId: string) => void
  onRetryAttachment?: (chip: ChatAttachmentChipModel) => void
  pendingKeys: string[]
  pillKeys: string[]
  /** The open suggestions the next instruction replaces, named above the
   * input while the assistant may write. */
  replaceableSuggestionCount: number
  onSend: () => void
  onDismissInstructionFeedback: () => void
  onStop: () => void
  onToggleAttach: () => void
  ruleOptions: ChatRuleOption[]
  selectedModelTier: ChatModelTier | null
  chatModelCatalog?: ModelCatalogEntry[]
  selectedModel: string | null
  selectedEffort: string | null
  editorContextBase: { documents: number; reports: number; rules: number; conversation: number }
  editorContextCapacity: { contextWindowTokens: number | null; reservedOutputTokens: number }
  textImprovement: Omit<TextImprovementApiOptions, 'locale'>
}) {
  const { locale, t } = useLocale()
  const reduceMotion = useReducedMotion()
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [improveError, setImproveError] = useState<string | null>(null)
  const deferredDraft = useDeferredValue(draft)
  const composerTokens = useMemo(() => estimateTokensFromText(deferredDraft), [deferredDraft])
  const contextTokenModel = buildContextTokenModel(
    [
      { key: 'documents', tone: 'file', tokens: editorContextBase.documents },
      { key: 'reports', tone: 'success', tokens: editorContextBase.reports },
      { key: 'rules', tone: 'success', tokens: editorContextBase.rules },
      { key: 'conversation', tone: 'warning', tokens: editorContextBase.conversation },
      { key: 'composer', tone: 'brand', tokens: composerTokens },
    ] satisfies ContextCategoryInput[],
    editorContextCapacity,
  )
  // Confirm-on-overflow guard, mirroring the chat composer: fire only on a real
  // estimated overflow (capacity already nets out reserved output + safety).
  const contextOverflow =
    contextTokenModel.usedFraction != null && contextTokenModel.usedFraction > 1
  const contextOverflowPct = Math.round((contextTokenModel.usedFraction ?? 0) * 100)
  const [overflowConfirmOpen, setOverflowConfirmOpen] = useState(false)
  const assistantTextImprove = useTextImprovement({
    ...textImprovement,
    locale,
    messages: {
      requestFailed: (message) => `${t.textImprove.requestFailed}: ${message}`,
      sensitiveText: t.textImprove.sensitiveText,
      unavailable: t.textImprove.unavailable,
    },
  })

  const attachedComments = comments.filter(
    (comment) => comment.status === 'open'
      && isAuthoredComment(comment)
      && attachedCommentIds.includes(comment.id),
  )
  const hasUnreadyAttachment = attachmentChips.some((chip) => chip.readiness !== 'ready')
  const canEditorSend = !aiReadOnlyReason
    && (
      isRunning
      || (!hasUnreadyAttachment && (attachedComments.length > 0 || draft.trim().length > 0))
    )
  const guardedSend = () => {
    if (aiReadOnlyReason) return
    if (!isRunning && hasUnreadyAttachment) return
    if (!isRunning && contextOverflow && canEditorSend) {
      setOverflowConfirmOpen(true)
      return
    }
    onSend()
  }
  const commentNumbers = buildCommentNumbers(comments)
  const mentionCategoryLabels: MentionCategoryLabels = {
    files: t.chat.mentionFilesCategory,
    filegroups: t.chat.mentionFilegroupsCategory,
    research: t.chat.mentionResearchCategory,
    rules: t.chat.mentionRulesCategory,
  }
  const mentionSources: MentionSources = { fileGroupOptions, fileOptions, reportOptions, ruleOptions }
  const resolveMentionLabel: LabelResolver = (kind, label) => {
    if (kind === 'file-asset') {
      const option = fileOptions.find((file) => file.label === label)
      return option ? { id: option.fileId, label: option.label } : null
    }
    if (kind === 'file-group') {
      const option = fileGroupOptions.find((group) => group.label === label)
      return option ? { id: option.groupId, label: option.label } : null
    }
    const option = reportOptions.find((report) => report.label === label)
    return option ? { id: option.runId, label: option.label } : null
  }

  function handleComposerChange() {
    dispatch({ draft: composerRef.current?.getMentionText() ?? '', type: 'setEditorAssistantDraft' })
    setImproveError(null)
    assistantTextImprove.clearProposal()
  }

  async function improveDraft() {
    setImproveError(null)
    if (aiReadOnlyReason) {
      setImproveError(aiReadOnlyReason)
      return
    }
    try {
      await assistantTextImprove.improve('chat_input', composerRef.current?.getMentionText() ?? draft)
    } catch (error) {
      setImproveError(messageFromUnknown(error))
    }
  }

  function acceptDraftImprovement(text: string) {
    // The composer can hold text the user did not write (a paste): the
    // model's text references only what the composer did.
    const next = improvedMentionText(text, composerRef.current?.getMentionText() ?? draft)
    composerRef.current?.setMentionText(next)
    dispatch({ draft: composerRef.current?.getMentionText() ?? next, type: 'setEditorAssistantDraft' })
    assistantTextImprove.clearProposal()
    setImproveError(null)
    window.requestAnimationFrame(() => composerRef.current?.focus())
  }

  // Restore the persisted draft (with its `[N]` pills) whenever the composer
  // editor mounts: the assistant draft survives hiding/showing the panel and
  // switching documents, mirroring the previous textarea behaviour. Hiding
  // unmounts the editor, so the next showing mounts an empty one again.
  const didRestoreRef = useRef(false)
  useEffect(() => {
    if (!isVisible) {
      didRestoreRef.current = false
      return
    }
    if (didRestoreRef.current) return
    didRestoreRef.current = true
    if (draft) composerRef.current?.setMentionText(draft)
  }, [draft, composerRef, isVisible])

  if (!isVisible) {
    return (
      <div className="shrink-0 px-4 pb-4 pt-2">
        <Button
          className="h-8 rounded-md"
          onClick={() => dispatch({ isVisible: true, type: 'setEditorAssistantVisible' })}
          size="sm"
          type="button"
          variant="outline"
        >
          <PanelBottomOpen className="size-4" />
          {copy.showAssistant}
        </Button>
      </div>
    )
  }

  return (
    <div className="relative z-10 shrink-0 px-4 pb-2 pt-2">
      {isWideCanvas ? (
        <>
          <div
            aria-hidden="true"
            className="pointer-events-none absolute bottom-0 left-4 top-2 hidden rounded-r-xl bg-background/30 backdrop-blur-xl xl:block xl:right-[calc(50%+28rem)]"
          />
          <div
            aria-hidden="true"
            className="pointer-events-none absolute bottom-0 right-4 top-2 hidden rounded-l-xl bg-background/30 backdrop-blur-xl xl:left-[calc(50%+28rem)] xl:block"
          />
        </>
      ) : null}
      <div className="relative mx-auto max-w-4xl">
        {aiReadOnlyReason ? (
          <p className="t-meta-sm mb-2 flex items-center gap-1.5 text-muted-foreground" role="note">
            <Eye className="icon-sm shrink-0" />
            <span>{aiReadOnlyReason}</span>
          </p>
        ) : null}
        <AnimatePresence initial={false}>
          {isAttachActive ? (
            <motion.div
              animate={{ height: 'auto', opacity: 1 }}
              className="mb-2 overflow-hidden rounded-md border border-brand/30 bg-brand-subtle/30"
              exit={{ height: 0, opacity: 0 }}
              initial={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18 }}
            >
              <div className="flex items-center gap-1.5 px-3 pb-1 pt-2 t-meta-sm font-semibold text-brand">
                <MessagesSquare className="size-3.5" />
                {attachedComments.length} {copy.attachedComments}
              </div>
              <div className="flex flex-wrap gap-1.5 px-3 pb-2.5">
                <AnimatePresence initial={false}>
                  {attachedComments.map((comment) => (
                    <motion.span
                      animate={{ opacity: 1, scale: 1 }}
                      className="inline-flex max-w-full items-center gap-1 rounded-full border border-brand/30 bg-background px-2 py-0.5 t-meta-sm text-foreground"
                      exit={{ opacity: 0, scale: 0.85 }}
                      initial={{ opacity: 0, scale: 0.85 }}
                      key={comment.id}
                      layout
                      transition={{ duration: 0.15 }}
                    >
                      <span className="grid size-4 shrink-0 place-items-center rounded-[4px] bg-brand-subtle t-hint font-semibold tabular-nums text-brand">
                        {commentNumbers.get(comment.id) ?? 0}
                      </span>
                      <span className="max-w-40 truncate text-muted-foreground">“{comment.anchor.selectedText}”</span>
                      <button
                        aria-label={copy.removeFromQueue}
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => onRemoveAttachedComment(comment.id)}
                        type="button"
                      >
                        <X className="size-3" />
                      </button>
                    </motion.span>
                  ))}
                </AnimatePresence>
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
        <AnimatePresence initial={false}>
          {handedOverDiscussions.length > 0 ? (
            <motion.div
              animate={{ height: 'auto', opacity: 1 }}
              aria-label={copy.handedOverDiscussions}
              className="mb-2 overflow-hidden rounded-md border border-brand/30 bg-brand-subtle/30"
              exit={{ height: 0, opacity: 0 }}
              initial={{ height: 0, opacity: 0 }}
              role="group"
              transition={{ duration: 0.18 }}
            >
              <div className="flex flex-wrap gap-1.5 px-3 py-2">
                <AnimatePresence initial={false}>
                  {handedOverDiscussions.map((discussion) => (
                    <motion.span
                      animate={{ opacity: 1, scale: 1 }}
                      className="inline-flex max-w-full items-center gap-1 rounded-full border border-brand/30 bg-background px-2 py-0.5 t-meta-sm text-foreground"
                      exit={{ opacity: 0, scale: 0.85 }}
                      initial={{ opacity: 0, scale: 0.85 }}
                      key={discussion.threadId}
                      layout
                      transition={{ duration: 0.15 }}
                    >
                      <MessagesSquare className="size-3.5 shrink-0 text-brand" />
                      <span className="shrink-0 font-semibold text-brand">{discussion.label}</span>
                      {discussion.document ? (
                        <>
                          <span className="max-w-40 truncate text-muted-foreground">“{discussion.quote}”</span>
                          <span className="shrink-0 tabular-nums text-muted-foreground">
                            {countLabel(
                              discussion.messageCount,
                              copy.handedOverDiscussionMessagesOne,
                              copy.handedOverDiscussionMessages,
                            )}
                          </span>
                        </>
                      ) : (
                        <span className="shrink-0 text-destructive">{copy.handedOverDiscussionGone}</span>
                      )}
                      <button
                        aria-label={`${copy.removeHandedOverDiscussion}: ${discussion.label}`}
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => onRemoveHandedOverDiscussion(discussion.threadId)}
                        type="button"
                      >
                        <X className="size-3" />
                      </button>
                    </motion.span>
                  ))}
                </AnimatePresence>
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
        <EditorInstructionFeedbackCard
          feedback={instructionFeedback}
          labels={{
            assistantDone: copy.assistantDone,
            assistantThinking: copy.assistantThinking,
            dismiss: copy.reject,
            hide: copy.hideAssistant,
            show: copy.showAssistant,
          }}
          onDismiss={onDismissInstructionFeedback}
        />
        <AnimatePresence initial={false}>
          {replaceableSuggestionCount > 0 && !aiReadOnlyReason ? (
            <motion.p
              animate={{ height: 'auto', opacity: 1 }}
              className="mb-2 overflow-hidden px-1 t-meta text-muted-foreground"
              exit={{ height: 0, opacity: 0 }}
              initial={{ height: 0, opacity: 0 }}
              role="status"
              transition={{ duration: 0.18 }}
            >
              {countLabel(
                replaceableSuggestionCount,
                copy.assistantReplacesSuggestionsOne,
                copy.assistantReplacesSuggestions,
              )}
            </motion.p>
          ) : null}
        </AnimatePresence>
        <input
          className="hidden"
          multiple
          onChange={(event) => {
            const files = Array.from(event.target.files ?? [])
            if (files.length > 0) onAttachFiles(files)
            event.target.value = ''
          }}
          ref={fileInputRef}
          type="file"
        />
        <Dropzone disabled={isRunning} label={t.chat.dropFiles} onFiles={onAttachFiles}>
        <div className="relative rounded-xl border border-border bg-card px-3 py-2 shadow-[0_8px_28px_-12px_var(--shadow-soft)] transition-[border-color,box-shadow] duration-150 focus-within:border-brand/60 focus-within:ring-2 focus-within:ring-brand/15">
          <TextImproveFloatingLayer
            labels={{
              accept: t.textImprove.accept,
              changes: t.textImprove.changes,
              noChanges: t.textImprove.noChanges,
              reject: t.textImprove.reject,
              title: t.textImprove.title,
              warnings: t.textImprove.warnings,
            }}
            onAccept={acceptDraftImprovement}
            onReject={assistantTextImprove.clearProposal}
            proposal={assistantTextImprove.proposal}
            reduceMotion={reduceMotion}
          />
          <ContextChipLegend
            chips={attachmentChips}
            labels={{
              attachmentFailed: t.chat.attachmentContextFailed,
              attachmentPending: t.chat.attachmentContextPending,
              attachmentRetry: t.chat.attachmentContextRetry,
              removeContext: copy.removeFromQueue,
              reorderHint: t.chat.reorderContextHint,
            }}
            onRemove={onRemoveChip}
            onReorderPending={onReorderPending}
            onReorderPill={onReorderPill}
            onRetry={onRetryAttachment}
            pendingKeys={pendingKeys}
            pillKeys={pillKeys}
          />
          <MentionComposer
            ariaLabel={copy.assistantPlaceholder}
            categoryLabels={mentionCategoryLabels}
            contentClassName="min-h-16 pb-2 pl-2 pr-9 pt-2 text-sm leading-6"
            enabledKinds={['research', 'rules', 'files', 'filegroups']}
            maxRows={6}
            mentionSources={mentionSources}
            onAttachRule={onAttachRule}
            onChange={handleComposerChange}
            onRefsChange={onRefsChange}
            onSubmit={guardedSend}
            placeholder={handedOverDiscussions.length > 0
              ? copy.handedOverDiscussionPlaceholder
              : copy.assistantPlaceholder}
            ref={composerRef}
            resolveLabel={resolveMentionLabel}
          />
          <TextImproveButton
            className="absolute right-3 top-2"
            disabled={Boolean(aiReadOnlyReason) || !draft.trim() || isRunning}
            isLoading={assistantTextImprove.isImproving}
            label={t.textImprove.improve}
            loadingLabel={t.textImprove.improving}
            onClick={() => void improveDraft()}
            reduceMotion={reduceMotion}
          />
          {improveError ? (
            <p className="t-meta-sm mb-1 rounded-md border border-destructive/20 bg-destructive/5 px-2 py-1 text-destructive">
              {improveError}
            </p>
          ) : null}
          <div className="flex items-center justify-between gap-2 border-t border-border/70 pt-1.5">
            <div className="flex min-w-0 items-center gap-1">
              <ComposerIconButton
                icon={PanelBottomClose}
                label={copy.hideAssistant}
                onClick={() => dispatch({ isVisible: false, type: 'setEditorAssistantVisible' })}
              />
              <ComposerIconButton
                active={isAttachActive}
                icon={MessageSquareText}
                label={copy.attachComments}
                onClick={onToggleAttach}
              />
              <ComposerIconButton
                icon={Paperclip}
                label={t.chat.attachFiles}
                onClick={() => fileInputRef.current?.click()}
              />
              <ModelTierPicker
                defaultModel={defaultChatModel}
                disabled={false}
                onChange={(tier) => dispatch({ tier, type: 'setSelectedChatModelTier' })}
                options={chatModelOptions}
                optionsStatus={chatModelOptionsStatus}
                selectedTier={selectedModelTier}
                modelCatalog={chatModelCatalog}
                selectedModel={selectedModel}
                selectedEffort={selectedEffort}
                onModelChange={(model) => dispatch({ model, type: 'setSelectedChatModel' })}
                onEffortChange={(effort) => dispatch({ effort, type: 'setSelectedChatEffort' })}
              />
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <QuotaMeter />
              <ContextTokenMeter
                conversationLabel={t.chat.contextCatDocument}
                disabled={false}
                model={contextTokenModel}
              />
            {!isRunning && contextOverflow && canEditorSend ? (
              <DropdownMenu onOpenChange={setOverflowConfirmOpen} open={overflowConfirmOpen}>
                <DropdownMenuTrigger asChild>
                  <Button
                    aria-label={copy.send}
                    className="size-7 rounded-md bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
                    size="icon"
                    type="button"
                    variant="default"
                  >
                    <SendHorizontal className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64 rounded-xl p-0 shadow-lg" side="top" sideOffset={8}>
                  <div className="px-2.5 py-2">
                    <p className="flex items-center gap-1.5 t-meta-sm font-medium text-warning">
                      <AlertTriangle className="size-3.5 shrink-0" />
                      {t.chat.contextOverflowConfirmTitle(contextOverflowPct)}
                    </p>
                    <p className="mt-1 t-hint text-muted-foreground/80">
                      {t.chat.contextOverflowConfirmBody}
                    </p>
                  </div>
                  <div className="flex items-center justify-end gap-1.5 border-t border-border px-2 py-1.5">
                    <Button onClick={() => setOverflowConfirmOpen(false)} size="sm" type="button" variant="ghost">
                      {t.chat.contextOverflowCancel}
                    </Button>
                    <Button
                      className="bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
                      onClick={() => {
                        setOverflowConfirmOpen(false)
                        guardedSend()
                      }}
                      size="sm"
                      type="button"
                      variant="default"
                    >
                      {t.chat.contextOverflowConfirmSend}
                    </Button>
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              isRunning ? (
                <ComposerStopButton label={copy.stopRun} onClick={onStop} />
              ) : (
                <Button
                  aria-label={copy.send}
                  className={cn(
                    'size-7 rounded-md',
                    attachedComments.length === 0 && draft.trim().length === 0
                      ? 'text-muted-foreground/45'
                      : 'bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground',
                  )}
                  disabled={Boolean(aiReadOnlyReason) || (attachedComments.length === 0 && draft.trim().length === 0)}
                  onClick={guardedSend}
                  size="icon"
                  type="button"
                  variant={attachedComments.length === 0 && draft.trim().length === 0 ? 'ghost' : 'default'}
                >
                  <SendHorizontal className="size-4" />
                </Button>
              )
            )}
            </div>
          </div>
        </div>
        </Dropzone>
        <ComposerDisclosureHint />
      </div>
    </div>
  )
}


function EditorCommentsPanel({
  aiReadOnlyReason,
  anchorError,
  attachedCommentIds,
  comments,
  copy,
  dispatch,
  onAcceptSuggestionGroup,
  onAcceptSuggestion,
  onRejectSuggestionGroup,
  onRejectSuggestion,
  onRunComment,
  onSelectSuggestion,
  onToggleCollect,
  runErrors,
  runningCommentIds,
  savingCommentDraftIds,
  selectedCommentId,
  suggestionErrors,
  suggestionPublishDisabledReason,
  suggestions,
}: {
  aiReadOnlyReason: string | null
  anchorError: string | null
  attachedCommentIds: string[]
  comments: EditorCommentThreadRecord[]
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  onAcceptSuggestionGroup: (groupId: string) => void
  onAcceptSuggestion: (suggestion: EditorSuggestionRecord) => void
  onRejectSuggestionGroup: (groupId: string) => void
  onRejectSuggestion: (suggestionId: string) => void
  onRunComment: (comment: EditorCommentThreadRecord) => void
  onToggleCollect: (commentId: string) => void
  onSelectSuggestion: (suggestionId: string) => void
  runErrors: Record<string, string>
  runningCommentIds: readonly string[]
  savingCommentDraftIds: readonly string[]
  suggestionErrors: Record<string, string>
  selectedCommentId: string | null
  suggestionPublishDisabledReason: string | null
  suggestions: EditorSuggestionRecord[]
}) {
  const { locale } = useLocale()
  const [statusTab, setStatusTab] = useState<'open' | 'resolved'>('open')

  // Maschinentraeger sind keine Notizen des Nutzers. Sie existieren nur,
  // damit ein privater Entwurf einen Anker hat, und gehoeren weder in die
  // Liste noch in die Zaehler -- sonst faende der Nutzer nach jedem
  // Assistentenlauf N Eintraege vor, die er nie geschrieben hat.
  const authoredComments = comments.filter(isAuthoredComment)
  const openComments = authoredComments.filter((comment) => comment.status !== 'resolved')
  const resolvedComments = authoredComments.filter((comment) => comment.status === 'resolved')
  // No kind filter: with one kind there is nothing to filter BY, and the
  // status tabs above already split the list the only way that carries
  // meaning.
  const visibleComments = statusTab === 'open' ? openComments : resolvedComments
  const commentNumbers = buildCommentNumbers(authoredComments)
  // Genau die Vorschlaege eines Anweisungslaufs. Im Markdown-Modus haben sie
  // keinen Traeger ('global_run' ohne commentId), im Kollaborationsmodus
  // haengen sie an einer Maschinenzeile ('assistant_edit'). Ein 'global_run'
  // MIT commentId stammt dagegen aus einer Sammel-Notiz des Nutzers und
  // gehoert in deren Karte -- hier erschiene er ein zweites Mal, mit einem
  // zweiten Uebernehmen-Knopf.
  const documentChangeSuggestions = suggestions
    .filter((suggestion) =>
      (suggestion.status === 'pending' || suggestion.status === 'stale')
      && (
        suggestion.origin.kind === 'assistant_edit'
        || (suggestion.origin.kind === 'global_run' && !suggestion.origin.commentId)
      ))
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
  const actionRestriction = aiReadOnlyReason ?? (
    suggestions.some((suggestion) => suggestion.status === 'pending')
      ? suggestionPublishDisabledReason
      : null
  )

  return (
    <div className="flex h-full w-full min-w-0 flex-col bg-background">
      <div className="border-b border-border px-3 py-2">
        <div className="flex items-center gap-1.5 t-meta font-semibold text-foreground">
          <LockKeyhole className="icon-sm text-muted-foreground" />
          <span>{copy.comments}</span>
        </div>
        <p className="mt-0.5 t-hint text-muted-foreground">
          {locale === 'de'
            ? 'Nur für mich sichtbar · niemals automatisch mit dem Team oder der KI geteilt'
            : 'Visible only to me · never shared with the team or AI automatically'}
        </p>
      </div>
      <div className="flex flex-col gap-2 border-b border-border px-3 py-2">
        <div className="grid grid-cols-2 gap-0.5 rounded-md bg-muted/60 p-0.5">
          <CommentStatusTab active={statusTab === 'open'} count={openComments.length} label={copy.tabOpen} onClick={() => setStatusTab('open')} />
          <CommentStatusTab active={statusTab === 'resolved'} count={resolvedComments.length} label={copy.tabResolved} onClick={() => setStatusTab('resolved')} />
        </div>
        {anchorError ? (
          <p className="t-meta flex items-start gap-1.5 text-destructive" role="alert">
            <AlertTriangle className="icon-sm mt-0.5 shrink-0" />
            <span>{anchorError}</span>
          </p>
        ) : null}
        {actionRestriction ? (
          <p className="t-meta flex items-start gap-1.5 text-muted-foreground" role="note">
            <Eye className="icon-sm mt-0.5 shrink-0" />
            <span>{actionRestriction}</span>
          </p>
        ) : null}
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-2 p-3">
          <EditorDocumentChangesSection
            labels={{
              accept: copy.accept,
              acceptAll: copy.acceptAll,
              documentChanges: copy.documentChanges,
              proposedChange: copy.proposedChange,
              reject: copy.reject,
              rejectAll: copy.rejectAll,
              stale: copy.suggestionStale,
            }}
            onAcceptGroup={onAcceptSuggestionGroup}
            onAcceptSuggestion={onAcceptSuggestion}
            onRejectGroup={onRejectSuggestionGroup}
            onRejectSuggestion={onRejectSuggestion}
            onSelectSuggestion={onSelectSuggestion}
            publishDisabledReason={suggestionPublishDisabledReason}
            suggestionErrors={suggestionErrors}
            suggestions={documentChangeSuggestions}
          />
          {visibleComments.length === 0 ? (
            <p className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">{copy.noComments}</p>
          ) : visibleComments.map((comment) => {
            const commentSuggestion = activeSuggestionFor(suggestions, comment.id)
            return (
              <EditorCommentCard
                aiReadOnlyReason={aiReadOnlyReason}
                comment={comment}
                commentNumber={commentNumbers.get(comment.id) ?? 0}
                copy={copy}
                dispatch={dispatch}
                isCollected={attachedCommentIds.includes(comment.id)}
                isRunning={runningCommentIds.includes(comment.id)}
                isSavingDraft={savingCommentDraftIds.includes(comment.id)}
                isSelected={selectedCommentId === comment.id}
                key={comment.id}
                onAcceptSuggestion={onAcceptSuggestion}
                onRejectSuggestion={onRejectSuggestion}
                onRunComment={onRunComment}
                onToggleCollect={onToggleCollect}
                runError={runErrors[comment.id]}
                suggestion={commentSuggestion}
                suggestionError={commentSuggestion ? suggestionErrors[commentSuggestion.id] : undefined}
                suggestionPublishDisabledReason={suggestionPublishDisabledReason}
              />
            )
          })}
        </div>
      </ScrollArea>
    </div>
  )
}

function CommentStatusTab({ active, count, label, onClick }: {
  active: boolean
  count: number
  label: string
  onClick: () => void
}) {
  return (
    <button
      className={cn(
        'flex h-7 items-center justify-center gap-1.5 rounded-[5px] text-xs font-medium transition-colors',
        active ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={onClick}
      type="button"
    >
      {label}
      <span className="t-hint tabular-nums text-muted-foreground/80">{count}</span>
    </button>
  )
}

function EditorCommentCard({
  aiReadOnlyReason,
  comment,
  commentNumber,
  copy,
  dispatch,
  isCollected,
  isRunning,
  isSavingDraft,
  isSelected,
  onAcceptSuggestion,
  onRejectSuggestion,
  onRunComment,
  onToggleCollect,
  runError,
  suggestion,
  suggestionError,
  suggestionPublishDisabledReason,
}: {
  aiReadOnlyReason: string | null
  comment: EditorCommentThreadRecord
  commentNumber: number
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  isCollected: boolean
  isRunning: boolean
  isSavingDraft: boolean
  isSelected: boolean
  onAcceptSuggestion: (suggestion: EditorSuggestionRecord) => void
  onRejectSuggestion: (suggestionId: string) => void
  onRunComment: (comment: EditorCommentThreadRecord) => void
  onToggleCollect: (commentId: string) => void
  runError?: string
  suggestion?: EditorSuggestionRecord
  suggestionError?: string
  suggestionPublishDisabledReason: string | null
}) {
  const isResolved = comment.status === 'resolved'
  const [isEditing, setIsEditing] = useState(false)
  const [editDraft, setEditDraft] = useState(comment.commentMarkdown)
  const editTextareaRef = useRef<HTMLTextAreaElement | null>(null)

  useLayoutEffect(() => {
    if (!isEditing) return
    resizeTextareaToRows(editTextareaRef.current, 6)
  }, [editDraft, isEditing])

  function startEditing() {
    setEditDraft(comment.commentMarkdown)
    setIsEditing(true)
    dispatch({ commentId: comment.id, type: 'selectEditorComment' })
  }

  function saveEdit() {
    dispatch({ commentId: comment.id, contentMarkdown: editDraft, type: 'updateEditorCommentText' })
    setIsEditing(false)
  }

  // A note is a QUOTE and a SENTENCE; everything else is chrome, and rows of
  // decoration make the panel read as long instead of compact. The row reads
  // number · time, the quote sits behind a quiet rule like an indented reply,
  // and the per-note buttons appear only while the note is open — the same
  // shape as a comment thread, not a settings card.
  return (
    <div
      className={cn(
        'group/note rounded-lg border px-2.5 py-2 text-sm transition-colors',
        isSelected
          ? 'border-brand/45 bg-brand-subtle/20'
          : 'border-border/70 bg-card hover:border-border',
        isResolved && 'opacity-60',
      )}
      onClick={() => dispatch({ commentId: comment.id, type: 'selectEditorComment' })}
    >
      <div className="flex items-center gap-1.5">
        <span className="t-hint shrink-0 tabular-nums text-muted-foreground/70">
          {commentNumber}
        </span>
        <span className="t-hint shrink-0 text-muted-foreground/70">
          {formatEditorTime(comment.updatedAt)}
        </span>
        <div className={cn(
          'ml-auto flex items-center gap-0.5 transition-opacity',
          isSelected ? 'opacity-100' : 'opacity-0 group-hover/note:opacity-100',
        )}
        >
          <Button
            aria-label={copy.editComment}
            className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation()
              startEditing()
            }}
            size="icon"
            type="button"
            variant="ghost"
          >
            <PencilLine className="size-3.5" />
          </Button>
          <Button
            aria-label={isResolved ? copy.reopen : copy.resolve}
            className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation()
              dispatch({ commentId: comment.id, status: isResolved ? 'open' : 'resolved', type: 'setEditorCommentStatus' })
            }}
            size="icon"
            type="button"
            variant="ghost"
          >
            {isResolved ? <Undo2 className="size-3.5" /> : <Check className="size-3.5" />}
          </Button>
          <Button
            aria-label={copy.deleteComment}
            className="size-6 shrink-0 text-muted-foreground hover:text-destructive"
            onClick={(event) => {
              event.stopPropagation()
              dispatch({ commentId: comment.id, type: 'deleteEditorComment' })
            }}
            size="icon"
            type="button"
            variant="ghost"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>
      <div className="pt-0.5">
        <p className={cn(
          'border-l border-border pl-2 t-meta text-muted-foreground/80',
          isSelected ? 'line-clamp-2' : 'line-clamp-1',
        )}
        >
          {compactCommentQuote(comment.anchor.selectedText, isSelected ? 180 : 96)}
        </p>
        {isEditing ? (
          <div className="mt-1.5" onClick={(event) => event.stopPropagation()}>
            <Textarea
              autoFocus
              className="t-body min-h-16 resize-none [scrollbar-width:thin]"
              onChange={(event) => setEditDraft(event.target.value)}
              ref={editTextareaRef}
              value={editDraft}
            />
            <div className="mt-1.5 flex items-center justify-end gap-1.5">
              <Button className="h-7" onClick={() => setIsEditing(false)} size="sm" type="button" variant="ghost">
                {copy.cancel}
              </Button>
              <Button className="h-7" disabled={!editDraft.trim()} onClick={saveEdit} size="sm" type="button">
                {copy.save}
              </Button>
            </div>
          </div>
        ) : (
          <p className={cn('mt-1 text-foreground', isSelected ? 'whitespace-pre-wrap' : 'line-clamp-1')}>
            {comment.commentMarkdown}
          </p>
        )}
        {isSelected && !isEditing ? (
          isRunning ? (
            <div className="mt-2.5 flex items-center justify-center gap-2 rounded-md border border-brand/25 bg-brand-subtle/30 py-2 t-meta-sm font-medium text-brand">
              <LoaderCircle className="size-3.5 animate-spin" />
              {isSavingDraft ? copy.savingPrivateDraft : copy.runningSuggestion}
            </div>
          ) : suggestion && suggestion.status === 'pending' ? (
            <SuggestionReview
              copy={copy}
              error={suggestionError}
              onAccept={onAcceptSuggestion}
              onReject={onRejectSuggestion}
              publishDisabledReason={suggestionPublishDisabledReason}
              suggestion={suggestion}
            />
          ) : suggestion && suggestion.status === 'stale' ? (
            <div
              className="mt-2.5 rounded-md border border-warning/30 bg-warning-subtle/40 p-2.5"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="t-meta-sm flex items-center gap-1.5 text-warning">
                <AlertTriangle className="size-3.5 shrink-0" />
                {copy.suggestionStale}
              </div>
              <div className="mt-2 flex items-center justify-end gap-1.5">
                <Button className="h-7" onClick={() => onRejectSuggestion(suggestion.id)} size="sm" type="button" variant="ghost">
                  <X className="size-3.5" />
                  {copy.reject}
                </Button>
                {(
                  <Button className="h-7" disabled={Boolean(aiReadOnlyReason)} onClick={() => onRunComment(comment)} size="sm" type="button" variant="outline">
                    <Sparkles className="size-3.5" />
                    {copy.regenerate}
                  </Button>
                )}
              </div>
            </div>
          ) : (
            <>
              {runError ? (
                <div
                  className="t-meta-sm mt-2.5 rounded-md border border-destructive/30 bg-destructive-subtle/40 p-2 text-destructive"
                  onClick={(event) => event.stopPropagation()}
                >
                  {runError}
                </div>
              ) : null}
              {/* Both actions sit on the FINISHED note: collect it for the
                  assistant, or run it right away. Neither is a property of the
                  note, so neither has to be decided before the first word is
                  typed. They read as two words, not two blocks — the note is
                  the content here. */}
              <div className="mt-1.5 flex items-center gap-3">
                <button
                  aria-pressed={isCollected}
                  className={cn(
                    't-meta-sm font-medium transition-colors',
                    isCollected ? 'text-brand' : 'text-muted-foreground hover:text-foreground',
                  )}
                  onClick={(event) => {
                    event.stopPropagation()
                    onToggleCollect(comment.id)
                  }}
                  type="button"
                >
                  {isCollected ? copy.collected : copy.collect}
                </button>
                <button
                  className="t-meta-sm font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50 disabled:hover:text-muted-foreground"
                  disabled={Boolean(aiReadOnlyReason)}
                  onClick={(event) => {
                    event.stopPropagation()
                    onRunComment(comment)
                  }}
                  type="button"
                >
                  {copy.runSuggestion}
                </button>
              </div>
            </>
          )
        ) : null}
      </div>
    </div>
  )
}

function buildCommentNumbers(comments: EditorCommentThreadRecord[]): Map<string, number> {
  const ordered = [...comments].sort((a, b) =>
    a.anchor.from - b.anchor.from || a.createdAt.localeCompare(b.createdAt))
  return new Map(ordered.map((comment, index) => [comment.id, index + 1]))
}

function compactCommentQuote(value: string, maxLength: number): string {
  const text = value.replace(/\s+/g, ' ').trim()
  if (text.length <= maxLength) return text
  return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`
}

function activeSuggestionFor(
  suggestions: EditorSuggestionRecord[],
  commentId: string,
): EditorSuggestionRecord | undefined {
  return suggestions
    .filter((suggestion) =>
      (suggestion.status === 'pending' || suggestion.status === 'stale')
      && suggestion.origin.commentId === commentId)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))[0]
}

function SuggestionReview({ copy, error, onAccept, onReject, publishDisabledReason, suggestion }: {
  copy: EditorCopy
  error: string | undefined
  onAccept: (suggestion: EditorSuggestionRecord) => void
  onReject: (suggestionId: string) => void
  publishDisabledReason: string | null
  suggestion: EditorSuggestionRecord
}) {
  const plan = suggestionDiffPlan(suggestion.originalText, suggestion.proposedText)
  const reviewInEditor = plan.reviewSurface === 'editor'
  return (
    <div
      className="mt-2.5 rounded-md border border-brand/25 bg-brand-subtle/30 p-2.5"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex items-center gap-1.5 t-meta-sm font-medium text-brand">
        <Sparkles className="size-3.5 shrink-0" />
        {reviewInEditor ? copy.reviewInEditor : copy.reviewInPanel}
      </div>
      {suggestion.privateDraft ? (
        <div
          className="mt-1.5 flex items-center gap-1.5 t-caption text-success"
          data-private-suggestion-draft-saved="true"
          role="status"
        >
          <Check className="size-3 shrink-0" />
          {copy.privateDraftSaved}
        </div>
      ) : null}
      <div className="mt-2 rounded-md border border-success/20 bg-success-subtle/25 p-2">
        <div className="t-caption text-success">{copy.proposedText}</div>
        <p className="t-meta mt-1 line-clamp-4 whitespace-pre-wrap text-foreground">{suggestion.proposedText}</p>
      </div>
      {suggestion.changeSummary?.length ? (
        <div className="mt-2 border-t border-border/60 pt-1.5">
          <div className="t-caption text-muted-foreground">{copy.changeSummary}</div>
          <ul className="mt-1 space-y-0.5">
            {suggestion.changeSummary.map((item, index) => (
              <li className="t-meta text-muted-foreground" key={`${index}-${item}`}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {suggestion.warnings?.length ? (
        <div className="mt-2 rounded-md border border-warning/25 bg-warning-subtle/35 p-2">
          <div className="t-caption flex items-center gap-1.5 text-warning">
            <AlertTriangle className="size-3" />
            {copy.warnings}
          </div>
          <ul className="mt-1 space-y-0.5">
            {suggestion.warnings.map((item, index) => (
              <li className="t-meta text-warning" key={`${index}-${item}`}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {reviewInEditor ? null : (
        <>
          {/* Der Fehlschlag steht bei den Knoepfen, die ihn ausloesen. Bei
              reviewInEditor traegt die Blockkarte im Editor beide. */}
          {error ? <SuggestionErrorLine message={error} /> : null}
        <div className="mt-2 flex items-center justify-end gap-1.5">
          <Button
            className="h-7"
            disabled={Boolean(publishDisabledReason)}
            onClick={() => onReject(suggestion.id)}
            size="sm"
            title={publishDisabledReason ?? undefined}
            type="button"
            variant="ghost"
          >
            <X className="size-3.5" />
            {copy.reject}
          </Button>
          <Button
            className="h-7 bg-brand text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
            disabled={Boolean(publishDisabledReason)}
            onClick={() => onAccept(suggestion)}
            size="sm"
            title={publishDisabledReason ?? undefined}
            type="button"
          >
            <Check className="size-3.5" />
            {copy.accept}
          </Button>
        </div>
        </>
      )}
    </div>
  )
}

function EditorEmptyState({
  copy,
  dispatch,
  reportOptions,
}: {
  copy: EditorCopy
  dispatch: Dispatch<ResearchDeskAction>
  reportOptions: CompletedReportOption[]
}) {
  return (
    <div className="flex min-h-0 w-full flex-1 items-center justify-center bg-background px-6 py-8">
      <WelcomeState
        actions={(
          <div className="flex flex-wrap justify-center gap-2">
            <Button
              className="h-8 gap-1.5 rounded-md bg-brand px-3 text-xs text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
              onClick={() => dispatch({ type: 'createEditorDocument' })}
              type="button"
              variant="default"
            >
              <SquarePen className="icon-sm" />
              {copy.createDocument}
            </Button>
            <ImportReportMenu
              copy={copy}
              dispatch={dispatch}
              reportOptions={reportOptions}
              triggerClassName="h-8 gap-1.5 rounded-md px-3 text-xs"
              variant="button"
            />
          </div>
        )}
        body={<p>{copy.emptyGuidance}</p>}
        example={copy.emptyExample}
        kicker={copy.emptyKicker}
        subtitle={copy.emptyBody}
        title={copy.emptyTitle}
      />
    </div>
  )
}

function ToolbarButton({
  active,
  disabled,
  icon: Icon,
  label,
  onClick,
}: {
  active?: boolean
  disabled?: boolean
  icon: typeof Bold
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={active}
          className={cn('size-6 rounded-md', active && 'bg-brand-subtle text-brand')}
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function TooltipButton({
  children,
  className,
  disabled = false,
  label,
  onClick,
}: {
  children: ReactNode
  className?: string
  disabled?: boolean
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className={cn('size-7 rounded-md', className)}
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function formatEditorTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function confirmProjectionFallbackExport(
  confirmedAt: string,
  locale: 'de' | 'en',
): boolean {
  const timestamp = new Date(confirmedAt).toLocaleString(locale)
  return window.confirm(locale === 'de'
    ? `Die Live-Projektion ist nicht verfügbar. Den zuletzt bestätigten Stand vom ${timestamp} exportieren?`
    : `The live projection is unavailable. Export the last confirmed version from ${timestamp}?`)
}

function messageFromUnknown(error: unknown) {
  if (error instanceof Error) return error.message
  return String(error)
}

/** Keeps the boundary staged until the matching Tiptap instance has committed.
 * The data body is checked before this child mounts, so an editor can never
 * initialise from a metadata-only empty string and become visible as ready. */
function EditorDocumentSurfaceReadiness({
  children,
  ready,
}: {
  children: ReactNode
  ready: boolean
}) {
  useStructuralRenderBlocker(!ready)
  return children
}

function EditorDocumentBodyError({
  error,
  locale,
  onRetry,
}: {
  error: string | null
  locale: 'de' | 'en'
  onRetry?: () => void
}) {
  return (
    <div
      className="flex min-h-0 flex-1 items-center justify-center bg-background px-8 py-10"
      data-editor-document-body-error=""
      role="alert"
    >
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <AlertTriangle aria-hidden="true" className="size-5 text-warning" />
        <p className="t-body font-medium text-foreground">
          {locale === 'de'
            ? 'Der Dokumentinhalt konnte nicht geladen werden.'
            : 'The document body could not be loaded.'}
        </p>
        {error ? <p className="t-meta text-muted-foreground">{error}</p> : null}
        {onRetry ? (
          <Button onClick={onRetry} size="sm" type="button" variant="outline">
            <RefreshCw aria-hidden="true" className="icon-sm" />
            {locale === 'de' ? 'Erneut versuchen' : 'Try again'}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** Document-shaped placeholder for the scrollable body only, using the live
 * surface's padding. It appears only after the shared structural delay; the
 * top bar, explorer, inspector, and composer remain stable. */
function EditorDocumentSkeleton() {
  const paragraph = (key: number, widths: readonly string[]) => (
    <div className="flex flex-col gap-2.5" key={key}>
      {widths.map((width, index) => (
        <Skeleton className={cn('h-4', width)} key={index} />
      ))}
    </div>
  )
  return (
    <div aria-hidden className="flex min-h-0 flex-1 flex-col gap-6 overflow-hidden px-10 py-8">
      <Skeleton className="h-7 w-1/2" />
      {paragraph(0, ['w-[94%]', 'w-[88%]', 'w-[91%]', 'w-[60%]'])}
      {paragraph(1, ['w-[85%]', 'w-[90%]', 'w-[93%]', 'w-[72%]'])}
      <Skeleton className="h-5 w-1/3" />
      {paragraph(2, ['w-[90%]', 'w-[86%]', 'w-[94%]', 'w-[64%]'])}
      {paragraph(3, ['w-[92%]', 'w-[87%]', 'w-[55%]'])}
      <Skeleton className="h-5 w-2/5" />
      {paragraph(4, ['w-[89%]', 'w-[93%]', 'w-[84%]', 'w-[68%]'])}
      {paragraph(5, ['w-[91%]', 'w-[88%]', 'w-[76%]'])}
    </div>
  )
}
