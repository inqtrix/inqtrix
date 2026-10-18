import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { KnowledgeDataSource } from '@/features/knowledge/types'
import { createReadyGate } from '@/features/composer/readyGate'
import type { Dispatch, SetStateAction } from 'react'
import { useReducedMotion } from 'motion/react'

import type { ClientOptions } from '@/api/inqtrixClient'
import { createProjectEntityId } from '@/features/project/entityId'
import { shouldRequestAgentTitle } from '@/features/sessionTitle/model'
import {
  chatRuleOptions,
  completedReportOptions,
  mentionableReportOptions,
} from '@/features/project/selectors'
import {
  reportComposedCharsMax,
  reportGuidanceMaxChars,
  reportRuleIdsMax,
} from '@/features/agent/reportRequirement'
import {
  agentRuleSources,
  attachedReportsMax,
  ruleIdsForSubmit,
  settleRuleIdsAfterSubmit,
} from './contextCategories'
import { gateRuleCatalog } from './plan/reportGuidance'
import { sendThenSettle } from './submitSettlement'
import { decideConversationAppend } from '@/features/scroll/conversationAppend'
import { clearScrollMemory } from '@/features/scroll/scrollMemory'
import { useScrollRestoration } from '@/features/scroll/useScrollRestoration'
import {
  BookOpen,
  FileText,
  Folder,
  Globe2,
  ListChecks,
  PenLine,
  Plus,
  Repeat2,
  Waypoints,
} from '@/components/icons'
import type { LucideIcon } from '@/components/icons'
import {
  AnimatedPanelBody,
  AnimatedResizableHandle,
} from '@/components/ui/animated-panel'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import { useAnimatedResizablePanelCollapse } from '@/components/ui/animated-panel-motion'
import { PanelToggle } from '@/components/ui/panel-toggle'
import {
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import { ResponsiveSidePanel } from '@/components/ui/responsive-side-panel'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ConversationSkeleton } from '@/components/ui/conversation-skeleton'
import { WelcomeState } from '@/components/ui/welcome-state'
import { CanvasHost } from '@/features/canvas/CanvasHost'
import { useCanvasFollow } from '@/features/canvas/useCanvasFollow'
import {
  activeCanvasView,
  type CanvasViewDescriptor,
} from '@/features/canvas/types'
import type { ProjectState } from '@/features/project/types'
import type {
  CreateResearchRunRequest,
  InqtrixCapabilities,
} from '@/features/researchRuns/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { useMediaQuery } from '@/features/researchDesk/hooks/useMediaQuery'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { StructuralLoadBoundary } from '@/motion/StructuralLoadBoundary'
import {
  AgentComposer,
  type AgentCanvasDocumentOption,
  type AgentCollectionOption,
  type AgentComposerSubmit,
  type AgentDocumentOption,
} from './AgentComposer'
import { AgentSessionRail } from './AgentSessionRail'
import { useDraftField, type AgentComposerDraft } from './composerDraft'
import { ComposerGateTray, pendingGate } from './ComposerGateTray'
import { AGENT_CANVAS_REGISTRY } from './canvas/views'
import { createCanvasSaveRegistry } from './canvas/saveRegistry'
import {
  AGENT_CANVAS_COMMENT_LIMIT,
  canvasContextFromSelection,
  pinAfterComment,
  settleCanvasQueueAfterSubmit,
  type AgentCanvasCommentDraft,
} from './canvas/commentQueue'
import {
  AgentCanvasReactContext,
  type AgentCanvasContextValue,
} from './canvas/context'
import { routeAgentRunToView } from './followTarget'
import { agentOverridesFromSelection } from '@/features/researchRuns/modelSelection'
import { assignArtifactFileNames } from './artifactNames'
import {
  agentCenterScreen,
  canEditAgentRun,
  isActiveAgentRun,
  resolveAgentArtifact,
  restoredAgentSessionId,
  type AgentRunRecord,
  type AgentSessionRecord,
} from './model'
import { agentComposerAvailability, startExclusive } from './sessionComposer'
import {
  vectorBackendDisplay,
  type PlanSourceInfo,
} from './plan/sourceLabel'
import type { ReportRuleOption } from './plan/PlanReviewBody'
import { agentScrollKey, agentTranscriptVersion, transcriptSettled } from './agentScroll'
import {
  DEMO_AGENT_ATTACHED_REPORTS,
  DEMO_AGENT_OVERVIEW_SOURCE,
  DEMO_AGENT_REPORT_REQUIREMENT,
  DEMO_AGENT_TIERS,
  type AgentDemoActions,
} from './demo'
import {
  sourceAvailabilityFromOverviewSource,
  buildAgentOverview,
  countAgentToolUse,
  defaultEngineMode,
  type AgentEngineMode,
} from './agentStatusOverview'
import { retainHydratedAgentRunIds } from './runPresentation'
import {
  DEFAULT_AGENT_SOURCE_POLICY,
  type AgentKnowledgeSwitch,
  normalizeAgentExecutionSnapshot,
  type AgentSourcePolicy,
} from './executionPolicy'
import { AgentRunTurn, type AgentTimelineActions } from './timeline/AgentTimeline'
import { useAgentControlApi } from './useAgentControlApi'
import { useAgentSessionsApi } from './useAgentSessionsApi'

const SESSIONS_PANEL_ID = 'agent-sessions-panel'
const CANVAS_PANEL_ID = 'agent-canvas-panel'
const AGENT_CENTER_PANEL_ID = 'agent-center-panel'
const AGENT_TIMELINE_PANEL_ID = 'agent-timeline-panel'

/**
 * The Agent Desk: session rail | timeline (command spine with the
 * docked composer) | polymorphic canvas. Follow-the-agent drives the canvas
 * from run state; every manual navigation pins. Focus mode collapses the
 * timeline and gives the canvas the full center.
 */
export function AgentWorkspace({
  apiKey,
  cancelRun,
  canvasPanelSize,
  capabilities,
  collections,
  knowledgeDataSource,
  dispatch,
  pollingRunIds,
  runsHydrated = true,
  documents = [],
  composerDraft,
  draftQuestion,
  memoryEnabled = false,
  skillsApi = null,
  onCanvasPanelSizeChange,
  onComposerDraftChange,
  onDraftQuestionChange,
  onEditorDocumentsChanged,
  onSelectedCollectionIdsChange,
  onSelectedDocumentIdChange,
  onSessionsPanelSizeChange,
  selectedAutonomy,
  onAutonomyChange,
  selectedDepth = 'normal',
  onDepthChange,
  selectedTier = null,
  onTierChange,
  modelCatalog = [],
  modelOptions = [],
  modelOptionsStatus = 'missing',
  defaultChatModel = null,
  selectedCollectionIds,
  selectedDocumentId,
  serverEnabled,
  titleStack,
  demo = null,
  state,
  submitError = null,
  submitRun,
  workspaceId,
}: {
  apiKey: string | undefined
  cancelRun: (runId: string) => Promise<unknown> | void
  /** Runs on the polling fallback — shown as a visible
   * degradation hint on their live status line. */
  pollingRunIds?: string[]
  /** Initial run-list hydration has settled (success or error). While
   * false, a selected session with no runs shows a loading skeleton
   * instead of the welcome state — the transcript may still be paging in. */
  runsHydrated?: boolean
  canvasPanelSize: number
  capabilities: InqtrixCapabilities | null
  collections: AgentCollectionOption[]
  /** Knowledge reader access for the knowledge-evidence canvas. Built
   * ONCE in the shell so the files/persistence gating and the demo
   * corpus stay in one place; required so a forgetful call site fails
   * loudly instead of silently hiding the document view. */
  knowledgeDataSource: KnowledgeDataSource
  dispatch: Dispatch<ResearchDeskAction>
  /** Patchable editor documents (server-synced or demo). */
  documents?: AgentDocumentOption[]
  /** The draft's document, attachments and run setup, held by the desk so
   * a view switch (which unmounts this workspace) brings them back. */
  composerDraft: AgentComposerDraft
  draftQuestion: string
  /** Account preference `enable_agent_memory` — the run overview shows it. */
  memoryEnabled?: boolean
  /** Skill library handle; null = feature off. */
  skillsApi?: import('@/features/skills/useSkillsApi').SkillsApiHandle | null
  onCanvasPanelSizeChange: (size: number) => void
  onComposerDraftChange: Dispatch<SetStateAction<AgentComposerDraft>>
  onDraftQuestionChange: (draft: string) => void
  onEditorDocumentsChanged: () => void
  onSelectedCollectionIdsChange: (ids: string[]) => void
  onSelectedDocumentIdChange: (id: string | null) => void
  onSessionsPanelSizeChange: (size: number) => void
  selectedAutonomy: string
  onAutonomyChange: (autonomy: string) => void
  /** User-picked Stufe; null = server default (capabilities). */
  selectedTier?: import('@/features/researchRuns/types').AgentTierId | null
  onTierChange?: (
    tier: import('@/features/researchRuns/types').AgentTierId | null,
  ) => void
  /** Thoroughness, lifted like autonomy — genuinely sticky
   * across view switches. */
  selectedDepth?: 'normal' | 'deep'
  onDepthChange?: (mode: 'normal' | 'deep') => void
  /** Model catalog for the agent's model override picker
   * (health-derived, same source as the chat picker; empty catalog ->
   * tier fallback). */
  modelCatalog?: import('@/features/researchRuns/types').ModelCatalogEntry[]
  modelOptions?: import('@/features/researchRuns/types').ChatModelOption[]
  modelOptionsStatus?: 'available' | 'missing' | 'unresolved'
  defaultChatModel?:
    | import('@/features/researchRuns/types').NodeModelResolution
    | null
  selectedCollectionIds: string[]
  selectedDocumentId: string | null
  /** Live server present (false in demo / local-only). */
  serverEnabled: boolean
  titleStack?: string
  /** Demo runtime: replaces the server actions in demo mode. */
  demo?: AgentDemoActions | null
  state: ProjectState
  submitRun: (request: CreateResearchRunRequest) => Promise<unknown> | void
  /** The last refused submission (server message), shown at the composer
   * until the next attempt clears it — a refusal is never silent. */
  submitError?: string | null
  workspaceId: string
}) {
  const { t, locale } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const isDesktop = useMediaQuery('(min-width: 1024px)')
  const [isMobileSessionsOpen, setIsMobileSessionsOpen] = useState(false)
  // The draft's document, attachments and run setup are held by the desk
  // (`composerDraft`): a view switch unmounts this workspace, and state
  // kept here came back empty while the draft text survived.
  const [draftDoc, setDraftDoc] = useDraftField(composerDraft, onComposerDraftChange, 'doc')
  // Output-form override: defaults to Auto — the agent's intake decides
  // unless the user forces a form.
  const [responseForm, setResponseForm] =
    useDraftField(composerDraft, onComposerDraftChange, 'responseForm')
  // Canvas comment queue: selection comments collected in the
  // document views, shown as chips above the composer, bundled into the
  // next submission's canvas_context — and emptied ONLY when the server
  // accepted that submission (settleCanvasQueueAfterSubmit).
  const [canvasCommentQueue, setCanvasCommentQueue] =
    useDraftField(composerDraft, onComposerDraftChange, 'canvasCommentQueue')
  // Mention-pinned canvas document: rides as comment-less
  // canvas_context; queued comments take the single-document channel
  // over (canvasContextFromSelection), so pinning clears on queue-add.
  const [pinnedCanvasArtifactId, setPinnedCanvasArtifactId] =
    useDraftField(composerDraft, onComposerDraftChange, 'pinnedCanvasArtifactId')
  // Canvas comment edit round-trip: the composer's pencil parks the
  // draft here, the matching document view scrolls to the anchor, opens
  // the popover prefilled, and clears the request.
  const [canvasCommentEdit, setCanvasCommentEdit] = useState<
    AgentCanvasCommentDraft | null
  >(null)
  const queueCanvasComment = useCallback(
    (draft: AgentCanvasCommentDraft) => {
      if (canvasCommentQueue.length >= AGENT_CANVAS_COMMENT_LIMIT) {
        return false
      }
      setCanvasCommentQueue((current) =>
        current.length >= AGENT_CANVAS_COMMENT_LIMIT
          ? current
          : [...current, draft])
      // Comments bind the channel to THEIR document (`pinAfterComment`).
      setPinnedCanvasArtifactId((pinned) => pinAfterComment(pinned, draft.artifactId))
      return true
    },
    [canvasCommentQueue.length, setCanvasCommentQueue, setPinnedCanvasArtifactId],
  )
  // Engine selection: user-selectable only when the
  // server registered the kernel; initialized from the published default
  // once capabilities arrive. The demo simulates a current server.
  const [engineMode, setEngineMode] =
    useDraftField(composerDraft, onComposerDraftChange, 'engineMode')
  // Attached skills; they go with an accepted submission.
  const [selectedSkillIds, setSelectedSkillIds] =
    useDraftField(composerDraft, onComposerDraftChange, 'skillIds')
  // Research reports attached as INPUT, cleared only on an ACCEPTED
  // submission.
  const [reportIds, setReportIds] =
    useDraftField(composerDraft, onComposerDraftChange, 'reportIds')
  // Library rules attached as the result requirement: inline pills that
  // hold for ONE task, like skills — cleared only on an ACCEPTED
  // submission (`settleSubmittedAttachments`).
  const [selectedRuleIds, setSelectedRuleIds] =
    useDraftField(composerDraft, onComposerDraftChange, 'ruleIds')
  /**
   * What a submission leaves attached — the one place the draft's
   * attachments settle. The canvas queue and the rules empty only on an
   * ACCEPTED submission and stay for the retry of a refused one; skills,
   * the canvas pin and the reports go with an accepted one. The server
   * path calls this through `sendThenSettle`, after the server answered.
   */
  const settleSubmittedAttachments = useCallback((accepted: boolean) => {
    setCanvasCommentQueue((current) => settleCanvasQueueAfterSubmit(current, accepted))
    setSelectedRuleIds((current) => settleRuleIdsAfterSubmit(current, accepted))
    if (!accepted) return
    setSelectedSkillIds([])
    setPinnedCanvasArtifactId(null)
    setReportIds([])
  }, [
    setCanvasCommentQueue,
    setPinnedCanvasArtifactId,
    setReportIds,
    setSelectedRuleIds,
    setSelectedSkillIds,
  ])
  // The source policy of a session that does not exist yet (first
  // message); a saved session carries its own value.
  const [draftSourcePolicy, setDraftSourcePolicy] =
    useDraftField(composerDraft, onComposerDraftChange, 'sourcePolicy')
  // The knowledge switch of a session that does not exist yet (first
  // message); a saved session carries its own value.
  const [draftKnowledgeSwitch, setDraftKnowledgeSwitch] =
    useDraftField(composerDraft, onComposerDraftChange, 'knowledgeSwitch')
  const saveRegistry = useRef(createCanvasSaveRegistry()).current
  const canvas = state.agentCanvas
  const agentBlock = capabilities?.agent ?? null
  // Published == enforced: the composer and the plan gate render the
  // SERVER's limits, so a text a surface accepts is never one the
  // submission is then refused for. The demo simulates a current server
  // and reads its mirror of the same block, so the caps show there too.
  const publishedRequirement = agentBlock?.report_requirement
    ?? (demo ? DEMO_AGENT_REPORT_REQUIREMENT : null)
  const reportGuidanceLimit = reportGuidanceMaxChars(publishedRequirement)
  const reportRuleLimit = reportRuleIdsMax(publishedRequirement)
  // No fallback number: a server that publishes no composed ceiling is
  // not pre-checked, and the rule menu says so.
  const reportComposedLimit = reportComposedCharsMax(publishedRequirement)
  // The report cap the same way: the server's number, or the demo's
  // mirror of it — and no number of our own, so a server that publishes
  // none is not pre-checked and the report category says so.
  const publishedAttachedReports = agentBlock?.attached_reports
    ?? (demo ? DEMO_AGENT_ATTACHED_REPORTS : null)
  const reportLimit = attachedReportsMax(publishedAttachedReports)
  const allAutonomyModes = agentBlock?.autonomy_modes ?? [
    'strict',
    'balanced',
    'autonomous',
  ]
  // Two-mode UI: servers publishing
  // mode_presets narrow the composer to Standard/Auto; the legacy
  // three-way control stays for older servers and when the operator
  // republishes it (advanced_autonomy). Wire vocabulary is unchanged.
  // The demo simulates a CURRENT server, so it presets too (the new UI
  // must be demo-visible).
  const modePresets =
    agentBlock?.mode_presets
    ?? (demo
      ? [
        { autonomy: 'balanced', id: 'standard' },
        { autonomy: 'autonomous', id: 'auto' },
      ]
      : undefined)
  const autonomyModes =
    modePresets && modePresets.length > 0 && !agentBlock?.advanced_autonomy
      ? modePresets
        .map((preset) => preset.autonomy)
        .filter((mode) => allAutonomyModes.includes(mode))
      : allAutonomyModes
  const agentAvailable =
    !serverEnabled || capabilities?.features.workspace_agent === true
  // Run overview (composer status menu): server facts, or the demo's
  // current-server mirror — new UI must be demo-visible. Recomputed on
  // mode switch so the approvals group always describes the SELECTED mode.
  const slashSkills = useMemo(
    () =>
      (skillsApi?.skills ?? [])
        .filter((skill) => skill.include_in_autocomplete)
        .map((skill) => ({
          id: skill.id,
          label: skill.label,
          description: skill.description,
          argument_hint: skill.argument_hint,
        })),
    [skillsApi],
  )
  const overviewSource = agentBlock ?? (demo ? DEMO_AGENT_OVERVIEW_SOURCE : null)
  const kernelSelectable =
    capabilities?.features.agent_kernel === true || Boolean(demo)
  // Deep toggle only when the server publishes it (feature detection);
  // the demo simulates a current server.
  const depthSelectable =
    Boolean(
      agentBlock?.depth_modes?.some((mode) => mode.id === 'deep'),
    ) || Boolean(demo)
  // Stufen (feature detection): published tiers replace the depth
  // toggle; the demo simulates a current server.
  const tiers = agentBlock?.tiers?.length
    ? agentBlock.tiers
    : demo
      ? DEMO_AGENT_TIERS
      : null
  const effectiveTier = tiers
    ? selectedTier ?? agentBlock?.default_tier ?? 'gruendlich'
    : null
  const sourceAvailability = useMemo(
    () => sourceAvailabilityFromOverviewSource(overviewSource),
    [overviewSource],
  )
  // Model override picker: agent-OWN selection (never the chat
  // fields — different cost profile), catalog from the same health
  // source the chat picker reads. Hidden without any catalog/options.
  const agentModelSelection = {
    tier: state.ui.selectedAgentModelTier,
    model: state.ui.selectedAgentModel,
    effort: state.ui.selectedAgentEffort,
  }
  const modelPicker =
    modelCatalog.length > 0 || modelOptions.length > 0
      ? {
        catalog: modelCatalog,
        options: modelOptions,
        optionsStatus: modelOptionsStatus,
        defaultModel: defaultChatModel,
        selectedTier: agentModelSelection.tier,
        selectedModel: agentModelSelection.model,
        selectedEffort: agentModelSelection.effort,
        onTierChange: (tier: import('@/features/researchRuns/types').ChatModelTier | null) =>
          dispatch({ tier, type: 'setSelectedAgentModelTier' }),
        onModelChange: (model: string | null) =>
          dispatch({ model, type: 'setSelectedAgentModel' }),
        onEffortChange: (effort: string | null) =>
          dispatch({ effort, type: 'setSelectedAgentEffort' }),
      }
      : null
  const effectiveEngineMode: AgentEngineMode =
    (kernelSelectable ? engineMode : null)
    ?? (demo
      ? 'agent_kernel'
      : defaultEngineMode(overviewSource, capabilities?.features ?? null))
  const overview = useMemo(
    () => buildAgentOverview({
      agent: overviewSource,
      autonomy: selectedAutonomy,
      kernelSelectable,
      mode: effectiveEngineMode,
    }),
    [effectiveEngineMode, kernelSelectable, overviewSource, selectedAutonomy],
  )

  const clientOptions = useMemo<ClientOptions | null>(
    () => (serverEnabled ? { apiKey, workspaceId } : null),
    [apiKey, serverEnabled, workspaceId],
  )

  const control = useAgentControlApi({
    apiKey,
    dispatch,
    enabled: serverEnabled,
    onEditorDocumentsChanged,
    runs: state.agentRuns,
    selectedSessionId: state.selectedAgentSessionId,
    sessionArtifacts: state.agentSessionArtifacts,
    workspaceId,
  })

  const {
    createSession: persistAgentSession,
    deleteSession: deletePersistedAgentSession,
    error: sessionSyncError,
    observationStopped: sessionObservationStopped,
    retrySessionDeletion,
    settled: sessionsSettled,
  } =
    useAgentSessionsApi({
      apiKey,
      dispatch,
      projectEpoch: state.projectEpoch,
      sessionGroups: state.agentSessionGroups,
      sessionOrder: state.agentSessionOrder,
      selectedSessionId: state.selectedAgentSessionId,
      sessions: state.agentSessions,
      syncActive: serverEnabled,
      workspaceId,
    })

  // Reload restore: once the session list settles, re-open the persisted
  // selection (or the most recent session) — a reload must land back in
  // the conversation, not the empty state. Once per HYDRATION IDENTITY
  // (workspace + project epoch), so loading a project file or switching
  // workspaces restores again; layout effect, so the selection commits
  // before paint (no one-frame welcome flash).
  const restoredForRef = useRef<string | null>(null)
  const hydrationIdentity = `${workspaceId}:${state.projectEpoch}`
  useLayoutEffect(() => {
    if (!sessionsSettled) return
    if (restoredForRef.current === hydrationIdentity) return
    restoredForRef.current = hydrationIdentity
    if (state.selectedAgentSessionId) return
    const sessionId = restoredAgentSessionId(
      state.ui.selectedAgentSessionId,
      state.agentSessionOrder,
      state.agentSessions,
      state.agentRuns,
    )
    if (sessionId) dispatch({ sessionId, type: 'selectAgentSession' })
  }, [
    dispatch,
    hydrationIdentity,
    sessionsSettled,
    state.agentRuns,
    state.agentSessionOrder,
    state.agentSessions,
    state.selectedAgentSessionId,
    state.ui.selectedAgentSessionId,
  ])

  const selectedSession = state.selectedAgentSessionId
    ? state.agentSessions[state.selectedAgentSessionId]
    : undefined
  const sourcePolicy = selectedSession?.sourcePolicy ?? draftSourcePolicy
  const knowledgeSwitch = selectedSession
    ? selectedSession.knowledgeSwitch
    : draftKnowledgeSwitch
  const handleKnowledgeSwitchChange = useCallback(
    (next: AgentKnowledgeSwitch) => {
      // The draft belongs to the session that does not exist yet; a saved
      // session owns its switch, so a click there never leaks into the
      // next new session.
      if (!selectedSession) {
        setDraftKnowledgeSwitch(next)
        return
      }
      dispatch({
        knowledgeSwitch: next,
        sessionId: selectedSession.id,
        type: 'setAgentSessionKnowledgeSwitch',
      })
    },
    [dispatch, selectedSession],
  )
  const handleSourcePolicyChange = useCallback(
    (next: AgentSourcePolicy) => {
      setDraftSourcePolicy(next)
      if (!selectedSession) return
      dispatch({
        sessionId: selectedSession.id,
        sourcePolicy: next,
        type: 'setAgentSessionSourcePolicy',
      })
    },
    [dispatch, selectedSession],
  )
  const sessionRuns = useMemo(() => {
    if (!selectedSession) return []
    return selectedSession.runIds
      .map((runId) => state.agentRuns[runId])
      .filter((run): run is AgentRunRecord => Boolean(run))
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
  }, [selectedSession, state.agentRuns])
  const centerScreen = agentCenterScreen({
    hasRuns: sessionRuns.length > 0,
    hasSelectedSession: Boolean(selectedSession),
    runsHydrated,
    serverEnabled,
    sessionsKnown: sessionsSettled,
  })
  const centerLoading = centerScreen === 'skeleton'
  const agentRevealKey = `agent:${hydrationIdentity}:${selectedSession?.id ?? 'empty'}`

  // The list hydrator commits its complete server snapshot before flipping
  // `runsHydrated`. Everything seen before that boundary is history, including
  // rows arriving after this component mounted. Later run ids are real turns.
  const runHistoryRef = useRef<{
    hydrationIdentity: string
    runIds: ReadonlySet<string>
  }>({
    hydrationIdentity,
    runIds: new Set(Object.keys(state.agentRuns)),
  })
  if (runHistoryRef.current.hydrationIdentity !== hydrationIdentity) {
    runHistoryRef.current = {
      hydrationIdentity,
      runIds: new Set(Object.keys(state.agentRuns)),
    }
  } else {
    runHistoryRef.current.runIds = retainHydratedAgentRunIds(
      runHistoryRef.current.runIds,
      Object.keys(state.agentRuns),
      runsHydrated,
    )
  }

  // Header and transcript publish as one identity. While the requested
  // session stages, the rail may select it but the bounded surface keeps the
  // previous title and body together (and the boundary makes the body inert).
  const requestedSessionByIdentityRef = useRef(new Map<
    string,
    { id: string | null; title: string }
  >())
  requestedSessionByIdentityRef.current.set(agentRevealKey, {
    id: selectedSession?.id ?? null,
    title: selectedSession?.title || t.navigation.agent,
  })
  const transcriptHydrationOutstanding = sessionRuns.some(
    (run) => !control.isTranscriptHydrated(run.runId),
  )
  const [committedSession, setCommittedSession] = useState(() => ({
    id: selectedSession?.id ?? null,
    identity:
      centerLoading || transcriptHydrationOutstanding
        ? null
        : agentRevealKey,
    title: selectedSession?.title || t.navigation.agent,
  }))
  const committedSessionTitle = committedSession.id === selectedSession?.id
    ? selectedSession?.title || t.navigation.agent
    : state.agentSessions[committedSession.id ?? '']?.title
      ?? committedSession.title
  const historicalTranscriptPending =
    committedSession.identity !== agentRevealKey
    && sessionRuns.some(
      (run) =>
        runHistoryRef.current.runIds.has(run.runId)
        && !control.isTranscriptHydrated(run.runId),
    )
  const surfaceTransitioning = committedSession.identity !== agentRevealKey
  // The transcript joins the SHARED scroll contract (chat/knowledge):
  // follow while a run appends, never against a user who scrolled away,
  // and remember the position per session. Without it new steps would
  // grow below the fold and a session switch would land at the very top
  // of a long transcript.
  const transcriptScrollAreaRef = useRef<HTMLDivElement | null>(null)
  const agentScrollMemoryKey = agentScrollKey(selectedSession?.id)
  const transcriptAppendSnapshotRef = useRef<
    ReturnType<typeof decideConversationAppend>['next'] | null
  >(null)
  const agentLoadPhase = centerLoading || historicalTranscriptPending
    ? 'pending'
    : transcriptHydrationOutstanding
      ? 'refreshing'
      : sessionRuns.length > 0
        ? 'ready'
        : 'empty'
  const latestRun = sessionRuns.at(-1)
  const runningRun = sessionRuns.find((run) => isActiveAgentRun(run.status))
  const transcriptReady = !centerLoading && !surfaceTransitioning
  const composerAvailability = agentComposerAvailability({
    agentAvailable,
    runningRunLocked: Boolean(runningRun && !canEditAgentRun(runningRun)),
    surfaceTransitioning,
  })
  const transcriptVersion = useMemo(
    () => agentTranscriptVersion(sessionRuns),
    [sessionRuns],
  )
  const transcriptScroll = useScrollRestoration({
    contentReady: transcriptReady,
    getViewport: () =>
      transcriptScrollAreaRef.current?.querySelector<HTMLElement>(
        '[data-scroll-area-viewport]',
      ) ?? null,
    // A live run streams answer tokens and appends step lines; the
    // shared rule then follows instantly instead of smoothing every
    // token.
    isStreaming: Boolean(runningRun),
    memoryKey: agentScrollMemoryKey,
    reduceMotion,
  })
  const previousRunsRef = useRef<readonly AgentRunRecord[] | null>(null)
  useEffect(() => {
    const decision = decideConversationAppend(
      transcriptAppendSnapshotRef.current,
      {
        contentReady: transcriptReady,
        contentVersion: transcriptVersion,
        key: agentScrollMemoryKey,
      },
    )
    transcriptAppendSnapshotRef.current = decision.next
    // The update that only SETTLES a run the user watched stream follows
    // instantly: a smooth scroll there reads as the finished answer
    // sliding away (the last visible part of the streaming-end jump).
    const settled = transcriptSettled(previousRunsRef.current, sessionRuns)
    previousRunsRef.current = sessionRuns
    if (decision.shouldAppend) {
      transcriptScroll.onContentAppended({ instant: settled })
    }
  }, [
    agentScrollMemoryKey,
    sessionRuns,
    transcriptReady,
    transcriptScroll,
    transcriptVersion,
  ])
  const statusExecution = useMemo(
    () => normalizeAgentExecutionSnapshot(latestRun?.snapshot),
    [latestRun?.snapshot],
  )
  const toolUseCounts = useMemo(
    () => statusExecution?.toolUseCounts
      ?? countAgentToolUse({
        tasks: latestRun?.plan?.tasks ?? [],
        taskStates: latestRun?.taskStates ?? {},
      }),
    [latestRun?.plan?.tasks, latestRun?.taskStates, statusExecution?.toolUseCounts],
  )

  const openCanvasView = useCallback(
    (descriptor: CanvasViewDescriptor) => {
      // Diff tabs key on runId (unlike document tabs): normalize a
      // possibly stale timeline anchor to the resolved one so the ±
      // badge and the revision select share ONE tab per diff.
      const normalized = descriptor.view === 'diff'
        ? {
          ...descriptor,
          runId: resolveAgentArtifact(
            state.agentRuns,
            state.agentSessionArtifacts,
            { artifactId: descriptor.artifactId, runId: descriptor.runId },
          ).runId,
        }
        : descriptor
      dispatch({
        descriptor: normalized,
        source: 'user',
        type: 'openAgentCanvasView',
      })
    },
    [dispatch, state.agentRuns, state.agentSessionArtifacts],
  )

  const setPlanDraft = useCallback(
    (runId: string, draft: Parameters<AgentCanvasContextValue['setPlanDraft']>[1]) => {
      if (!canEditAgentRun(state.agentRuns[runId])) return
      dispatch({ draft, runId, type: 'setAgentPlanDraft' })
    },
    [dispatch, state.agentRuns],
  )

  const requestPlanRefresh = useCallback(
    (runId: string) => {
      dispatch({ runId, type: 'markAgentRunPlanStale' })
    },
    [dispatch],
  )

  // ONE derived file name per session document, computed from
  // the anchor-independent index in created order; empty while the
  // index has not loaded (surfaces then fall back to plain titles).
  const sessionFileNames = useMemo<Record<string, string>>(() => {
    const index = selectedSession
      ? state.agentSessionArtifacts[selectedSession.id]
      : undefined
    if (!index) return {}
    return assignArtifactFileNames(
      index.order
        .map((artifactId) => index.byId[artifactId])
        .filter((meta) => meta.kind === 'memo' || meta.kind === 'deliverable')
        .map((meta) => ({ artifactId: meta.artifactId, title: meta.title })),
    )
  }, [selectedSession, state.agentSessionArtifacts])

  // The mention group's candidates — session documents with
  // their derived names and CURRENT revisions (canvas_context needs
  // revision >= 1, so only the loaded index feeds this, never a
  // revision-less fallback).
  const canvasDocumentOptions = useMemo<AgentCanvasDocumentOption[]>(() => {
    const index = selectedSession
      ? state.agentSessionArtifacts[selectedSession.id]
      : undefined
    if (!index) return []
    return index.order
      .map((artifactId) => index.byId[artifactId])
      .filter((meta) => meta.kind === 'memo' || meta.kind === 'deliverable')
      .map((meta) => ({
        artifactId: meta.artifactId,
        name: sessionFileNames[meta.artifactId] ?? meta.title,
        revision: meta.revision,
        title: meta.title,
      }))
  }, [selectedSession, sessionFileNames, state.agentSessionArtifacts])

  // A pin whose document vanished, or that belongs to another session after
  // a switch, is not reset here: its capsule stays in the sentence, marked
  // as no longer available, and the composer holds the send until the user
  // removes it — a stale id is never submitted, and no word disappears.

  // Display context for plan tasks (id -> title + backend label): the
  // "wo" of the approval transparency, shared by the timeline card and
  // the canvas plan view.
  const planSource = useMemo<PlanSourceInfo>(
    () => ({
      collections,
      vectorBackendLabel: vectorBackendDisplay(
        capabilities?.knowledge?.vector_backend,
      ),
    }),
    [capabilities, collections],
  )

  // Finished Research-Desk reports, attachable as agent input.
  const attachableReports = useMemo(
    () => mentionableReportOptions(state),
    [state],
  )
  // Every finished report: an attached one is named and found missing
  // against these, also when it is kept out of the menus.
  const knownReports = useMemo(() => completedReportOptions(state), [state])

  // Library rules the user opted into for the agent surface. Opt-in by
  // design: a rule written for chat must not start shaping reports. ONE
  // list for the composer and the plan gate, keyed by the id the server
  // resolves — the template id, never the chat's local id (a rule pushed
  // to the server keeps its local id until the next hydrate, and sending
  // that is refused). The demo has no server: its own ids are the ones
  // it resolves.
  const agentRules = useMemo(
    () => agentRuleSources(chatRuleOptions(state, 'agent'), { localIds: Boolean(demo) }),
    [demo, state],
  )
  // The gate adds only what the composer lets the user pick: no context
  // pack with linked files, no rule the server does not know yet.
  const reportRuleOptions = useMemo<ReportRuleOption[]>(
    () => gateRuleCatalog(agentRules),
    [agentRules],
  )
  // The transcript names a rule the way its pill did: by its title.
  const ruleTitles = useMemo(
    () => Object.fromEntries(agentRules.map((rule) => [rule.id, rule.title])),
    [agentRules],
  )

  const canvasContext = useMemo<AgentCanvasContextValue>(
    () => ({
      applyPatch: demo ? demo.applyPatch : control.applyPatch,
      cancelTask: control.cancelTask,
      clientOptions,
      inspectChildRun: control.inspectChildRun,
      knowledgeDataSource,
      decideApproval: demo ? demo.decideApproval : control.decideApproval,
      rejectPatch: demo ? demo.rejectPatch : control.rejectPatch,
      exportArtifact: control.exportArtifact,
      fileAssets: state.fileAssets,
      loadArtifact: control.loadArtifact,
      loadTaskResult: control.loadTaskResult,
      openCanvasView,
      saveRegistry,
      planDrafts: state.agentPlanDrafts,
      planSource,
      reportRuleOptions,
      reportGuidanceMaxChars: reportGuidanceLimit,
      reportRuleIdsMax: reportRuleLimit,
      pollingRunIds: pollingRunIds ?? [],
      prefetchTaskResult: control.prefetchTaskResult,
      requestPlanRefresh,
      queueCanvasComment,
      canvasComments: canvasCommentQueue,
      canvasCommentEdit,
      clearCanvasCommentEdit: () => setCanvasCommentEdit(null),
      updateCanvasComment: (id: string, comment: string) =>
        setCanvasCommentQueue((current) =>
          current.map((draft) =>
            draft.id === id ? { ...draft, comment } : draft,
          )),
      renameArtifact: control.renameArtifact,
      runs: state.agentRuns,
      saveArtifact: control.saveArtifact,
      sessionArtifacts: state.agentSessionArtifacts,
      sessionFileNames,
      setPlanDraft,
      workspaceId,
    }),
    [
      canvasCommentEdit,
      canvasCommentQueue,
      clientOptions,
      control,
      demo,
      knowledgeDataSource,
      openCanvasView,
      planSource,
      pollingRunIds,
      queueCanvasComment,
      // The gate reads these too: without them a rule catalog that
      // changed while no run did kept offering the previous list.
      reportGuidanceLimit,
      reportRuleLimit,
      reportRuleOptions,
      requestPlanRefresh,
      sessionFileNames,
      setPlanDraft,
      state.agentPlanDrafts,
      state.agentRuns,
      state.agentSessionArtifacts,
      state.fileAssets,
      workspaceId,
    ],
  )

  // Follow-the-agent: the LATEST run of the selected session steers.
  const followTarget = useMemo(
    () => (latestRun ? routeAgentRunToView(latestRun) : null),
    [latestRun],
  )
  useCanvasFollow({
    canvas,
    // Follow default on; mobile default off — the canvas is a
    // full occluding overlay there and must never take over on its own.
    enabled: isDesktop,
    onAutoPin: () => dispatch({ pinned: true, type: 'setAgentCanvasPinned' }),
    openView: (descriptor) =>
      dispatch({ descriptor, source: 'agent', type: 'openAgentCanvasView' }),
    target: followTarget,
  })

  // Esc leaves focus mode (tabs replaced the overlay stack — closing a
  // tab is an explicit affordance, never a global key).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      if (canvas.focus) {
        dispatch({ type: 'toggleAgentCanvasFocus' })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canvas.focus, dispatch])

  const timelineActions = useMemo<AgentTimelineActions>(
    () => ({
      answerClarification: demo
        ? demo.answerClarification
        : control.answerClarification,
      // Demo runs never delegate, so the child-gate twins have no demo
      // variant — the control path is the only producer of childGates.
      answerChildClarification: control.answerChildClarification,
      applyPatch: demo ? demo.applyPatch : control.applyPatch,
      decideApproval: demo ? demo.decideApproval : control.decideApproval,
      decideChildApproval: control.decideChildApproval,
      rejectPatch: demo ? demo.rejectPatch : control.rejectPatch,
      onCancelRun: demo
        ? (runId) => demo.cancel(runId)
        : (runId) => {
          if (canEditAgentRun(state.agentRuns[runId])) void cancelRun(runId)
        },
      onOpenCanvas: openCanvasView,
      // Inline chip diff: resolve the CURRENT anchor before the
      // revision fetch — the timeline's runId can be stale.
      loadArtifactRevision: (runId, artifactId, revision) =>
        control.loadArtifact(
          resolveAgentArtifact(
            state.agentRuns,
            state.agentSessionArtifacts,
            { artifactId, runId },
          ).runId,
          artifactId,
          revision,
        ),
      planDrafts: state.agentPlanDrafts,
      planSource,
      setPlanDraft,
    }),
    [
      cancelRun,
      control,
      demo,
      openCanvasView,
      planSource,
      setPlanDraft,
      state.agentPlanDrafts,
      state.agentRuns,
      state.agentSessionArtifacts,
    ],
  )

  const handleSubmit = useCallback(
    async ({
      autonomy,
      collectionIds,
      depth,
      tier,
      documentId,
      engineMode: submitEngineMode,
      knowledgeScope,
      knowledgeSwitch: submitKnowledgeSwitch,
      question,
      responseForm,
      skillIds,
      sourcePolicy: submitSourcePolicy,
    }: AgentComposerSubmit) => {
      if (runningRun && !canEditAgentRun(runningRun)) return false
      // Pending canvas edits flush BEFORE anything else — also when this
      // send answers a gate: the answer may reference exactly that edit,
      // and the agent reads the LATEST revision. flushSave handles
      // its own errors (restore + notice), so the submit never dies here.
      await saveRegistry.flushAll()
      // A pending clarification absorbs the send (ONE input locus):
      // free text answers the gate instead of racing a new run.
      const gate = runningRun ? pendingGate(runningRun) : null
      if (gate?.kind === 'clarification') {
        await timelineActions.answerClarification(
          runningRun!.runId,
          gate.clarification.clarificationId,
          { answer: question },
        )
        return true
      }
      // A child's clarification absorbs the send the same way — the
      // answer routes to the child run, the echo to the parent record.
      if (gate?.kind === 'child_clarification') {
        await timelineActions.answerChildClarification(
          runningRun!.runId,
          gate.childRunId,
          gate.clarification.clarificationId,
          { answer: question },
        )
        return true
      }
      let sessionId = state.selectedAgentSessionId
      const selectedSession = sessionId
        ? state.agentSessions[sessionId]
        : undefined
      if (!sessionId || !selectedSession || selectedSession.persistable === false) {
        // A shared-run view is read-only session scaffolding. A recipient's
        // new run starts in their own syncable session and never sends the
        // derived view id back through the run/session persistence surfaces.
        const now = new Date().toISOString()
        const candidate: AgentSessionRecord = {
          id: createProjectEntityId('agent-session'),
          title: question.trim().slice(0, 80),
          titleSource: 'initial', titleRevision: 0,
          groupId: null,
          createdAt: now,
          updatedAt: now,
          runIds: [],
          sourcePolicy: submitSourcePolicy,
          // The switch as the message leaves it (a trailing `/wissen`
          // included) rides the submission, so no stale draft is read here.
          knowledgeSwitch: submitKnowledgeSwitch,
        }
        sessionId = candidate.id
        if (demo) {
          dispatch({ session: candidate, type: 'createAgentSession' })
        } else if (!await persistAgentSession(candidate)) {
          // The rail receives the hook's visible sync error. Crucially, no
          // local session and no run carrying an unconfirmed id exist.
          return false
        }
      }
      // Single-document channel: comments bind it; a mention pin
      // travels only with an empty queue. The pin's revision is the
      // index's CURRENT one at submission (snapshot semantics).
      const pinnedMeta = pinnedCanvasArtifactId
        ? state.agentSessionArtifacts[sessionId]?.byId[
          pinnedCanvasArtifactId
        ] ?? null
        : null
      const canvasSubmitContext = canvasContextFromSelection(
        canvasCommentQueue,
        pinnedMeta
          ? {
            artifactId: pinnedMeta.artifactId,
            revision: pinnedMeta.revision,
          }
          : null,
      )
      const ruleIds = ruleIdsForSubmit(selectedRuleIds)
      if (demo) {
        demo.submit({
          agentTier: tier ?? '',
          autonomy,
          collectionIds,
          depth,
          documentId,
          engineMode: submitEngineMode,
          question,
          responseForm,
          ruleIds,
          sessionId,
          skillLabels: skillIds.map(
            (id) => slashSkills.find((skill) => skill.id === id)?.label ?? id,
          ),
          sourcePolicy: submitSourcePolicy,
          modelSelection: {
            model: state.ui.selectedAgentModel,
            effort: state.ui.selectedAgentEffort,
            tier: state.ui.selectedAgentModelTier,
          },
        })
        if (shouldRequestAgentTitle(selectedSession)) {
          dispatch({ type: 'requestSessionTitle', kind: 'agent', id: sessionId, question, language: locale, stack: titleStack ?? null })
        }
        // The demo twin accepts every submission it is handed.
        settleSubmittedAttachments(true)
        return true
      }
      const overrides = agentOverridesFromSelection(
        depth,
        state.ui.selectedAgentModelTier,
        state.ui.selectedAgentModel,
        state.ui.selectedAgentEffort,
        tier,
      )
      // ONE consumption policy (tested): settled after the server answered
      // — a refused send keeps every comment and every rule for the retry.
      const summary = await sendThenSettle(
        () => submitRun({
          agentOverrides: overrides,
          autonomy,
          canvasContext: canvasSubmitContext,
          documentId,
          // Chips pin; the session switch asks for every visible collection
          // (the server resolves and pins the boundary); otherwise no
          // knowledge at all.
          knowledgeFilters:
            knowledgeScope === 'pinned'
              ? { collectionIds }
              : knowledgeScope === 'all'
                ? { collectionIds: [], scope: 'all' }
                : undefined,
          mode: submitEngineMode,
          question,
          responseForm,
          sessionId,
          skillIds: skillIds.length > 0 ? skillIds : undefined,
          sourcePolicy: submitSourcePolicy,
          reportIds: reportIds.length > 0 ? reportIds : undefined,
          reportRuleIds: ruleIds.length > 0 ? ruleIds : undefined,
        }),
        settleSubmittedAttachments,
      )
      if (!summary) return false
      if (shouldRequestAgentTitle(selectedSession)) {
        dispatch({ type: 'requestSessionTitle', kind: 'agent', id: sessionId, question, language: locale, stack: titleStack ?? null })
      }
      // Sending is an explicit user action: it always lands at the
      // bottom, exactly like the chat composer. Without it the own
      // question appears below the fold and the run looks like it
      // never started.
      transcriptScroll.scrollToBottom()
      return true
    },
    [
      // canvasCommentQueue belongs in these deps: without it the submit
      // closes over a pre-add queue snapshot. The attachment reads are
      // inputs like every other value this callback reads.
      canvasCommentQueue,
      locale,
      titleStack,
      demo,
      dispatch,
      persistAgentSession,
      pinnedCanvasArtifactId,
      reportIds,
      runningRun,
      selectedRuleIds,
      settleSubmittedAttachments,
      transcriptScroll,
      slashSkills,
      state.agentSessionArtifacts,
      state.agentSessions,
      state.selectedAgentSessionId,
      state.ui.selectedAgentEffort,
      state.ui.selectedAgentModel,
      state.ui.selectedAgentModelTier,
      submitRun,
      timelineActions,
    ],
  )

  // Sending waits until the selected session is the one on screen; typing
  // never waits. The send then runs with the handler of that render.
  const [sendGate] = useState(createReadyGate)
  useEffect(() => {
    sendGate.update({
      failed: false,
      key: selectedSession?.id ?? null,
      pending: composerAvailability.sendWaits,
    })
  }, [composerAvailability.sendWaits, selectedSession?.id, sendGate])
  const handleSubmitRef = useRef(handleSubmit)
  handleSubmitRef.current = handleSubmit
  const submitWhenShown = useCallback(async (submission: AgentComposerSubmit) => {
    if (await sendGate.wait() !== 'ready') return false
    return handleSubmitRef.current(submission)
  }, [sendGate])

  // A new session appears once the server confirmed it (no phantom session
  // id can reach a run); until then the button shows that it is on its way.
  const [creatingSession, setCreatingSession] = useState(false)
  const creatingSessionRef = useRef(false)

  const createSession = useCallback(() => {
    const now = new Date().toISOString()
    const session: AgentSessionRecord = {
      id: createProjectEntityId('agent-session'),
      title: t.agent.sessions.create,
      titleSource: 'initial', titleRevision: 0,
      groupId: null,
      createdAt: now,
      updatedAt: now,
      runIds: [],
      sourcePolicy: { ...DEFAULT_AGENT_SOURCE_POLICY },
    }
    if (demo) {
      dispatch({ session, type: 'createAgentSession' })
      return
    }
    startExclusive(creatingSessionRef, () => persistAgentSession(session), setCreatingSession)
  }, [demo, dispatch, persistAgentSession, t])

  const railVisible = state.ui.isAgentSessionsVisible
  const railMotion = useAnimatedResizablePanelCollapse({
    expanded: railVisible,
    expandedSize: state.ui.panelLayout.agentSessions,
    reduceMotion,
  })
  const canvasMotion = useAnimatedResizablePanelCollapse({
    expanded: canvas.open,
    expandedSize: canvasPanelSize,
    reduceMotion,
  })

  const canvasLabelFor = useCallback(
    (descriptor: CanvasViewDescriptor): string => {
      if (descriptor.view === 'document') {
        // Anchor-independent resolution: the raw descriptor run is a
        // possibly stale anchor — resolving it gives a chip-opened,
        // re-anchored document its own name instead of the generic
        // document label.
        const { artifact } = resolveAgentArtifact(
          state.agentRuns,
          state.agentSessionArtifacts,
          descriptor,
        )
        return (
          sessionFileNames[descriptor.artifactId]
          || artifact?.title
          || t.agent.canvas.views.document
        )
      }
      if (descriptor.view === 'diff') {
        const base = sessionFileNames[descriptor.artifactId]
          || t.agent.canvas.views.diff
        return (
          `${base} · r${descriptor.fromRevision}`
          + `→r${descriptor.toRevision}`
        )
      }
      if (descriptor.view === 'evidence') {
        return `${t.agent.canvas.views.evidence} ${descriptor.label}`
      }
      return t.agent.canvas.views[descriptor.view]
    },
    [sessionFileNames, state.agentRuns, state.agentSessionArtifacts, t],
  )

  const rail = (
    <AgentSessionRail
      creatingSession={creatingSession}
      onCreateSession={createSession}
      onCreateSessionGroup={() =>
        dispatch({ title: t.agent.sessions.createGroup, type: 'createAgentSessionGroup' })}
      onDeleteSession={(sessionId) => {
        const session = state.agentSessions[sessionId]
        if (session?.persistable === false) return
        // The remembered position dies with the session — otherwise a
        // later session reusing the id would inherit a stranger's
        // scroll offset (same reason ResearchDesk clears chat and
        // knowledge keys on deletion).
        const memoryKey = agentScrollKey(sessionId)
        if (memoryKey) clearScrollMemory(memoryKey)
        void deletePersistedAgentSession(sessionId)
      }}
      onRenameSession={(sessionId, title) => {
        if (state.agentSessions[sessionId]?.persistable === false) return
        dispatch({ sessionId, title, type: 'renameAgentSession' })
      }}
      onRetrySessionDeletion={(sessionId) => {
        void retrySessionDeletion(sessionId)
      }}
      onSelectSession={(sessionId) => {
        dispatch({ sessionId, type: 'selectAgentSession' })
        if (!isDesktop) setIsMobileSessionsOpen(false)
      }}
      onTogglePinnedSession={(sessionId) =>
        dispatch({ sessionId, type: 'togglePinnedAgentSession' })}
      onAdoptVisibleOrder={(itemIds, folderIds) => dispatch({
        desk: 'agent',
        folderIds,
        itemIds,
        type: 'adoptExplorerOrder',
      })}
      onChangeSortMode={(mode) => dispatch({ desk: 'agent', mode, type: 'setExplorerSortMode' })}
      onDeleteSessionGroup={(groupId) => dispatch({ groupId, type: 'deleteAgentSessionGroup' })}
      onMoveSessionGroup={(groupId, targetIndex) =>
        dispatch({ groupId, targetIndex, type: 'moveAgentSessionGroup' })}
      onMoveSessionToGroup={(sessionId, groupId, targetIndex) => {
        if (state.agentSessions[sessionId]?.persistable === false) return
        dispatch({ groupId, sessionId, targetIndex, type: 'moveAgentSessionToGroup' })
      }}
      onRenameSessionGroup={(groupId, title) =>
        dispatch({ groupId, title, type: 'renameAgentSessionGroup' })}
      sortMode={state.ui.explorerSort.agent}
      pinnedSessionIds={state.ui.pinnedExplorer.agentSessionIds}
      syncError={(sessionObservationStopped
        ? t.common.observationStopped(sessionSyncError)
        : sessionSyncError) ?? (() => {
        const conflict = Object.values(state.agentSessions).find((session) => session.titleConflict)?.titleConflict
        return conflict ? t.sessionTitles.conflict(conflict) : null
      })()}
      runs={state.agentRuns}
      selectedSessionId={state.selectedAgentSessionId}
      sessionGroupOrder={state.agentSessionGroupOrder}
      sessionGroups={state.agentSessionGroups}
      sessionOrder={state.agentSessionOrder}
      sessions={state.agentSessions}
    />
  )

  const activeGate = runningRun ? pendingGate(runningRun) : null
  // The report pill surfaces the run's document artifact — a mission
  // `memo` OR a kernel `deliverable` (a kernel run writes the latter, so
  // a memo-only check would leave a finished kernel document unreachable
  // from the composer).
  // The session index is the anchor-independent SSOT: a document's
  // run_id moves to the newest updating run, so latestRun-only lookups
  // lose documents that older turns produced. latestRun stays the live
  // fallback while the index has not loaded yet.
  const sessionArtifactIndex = selectedSession
    ? state.agentSessionArtifacts[selectedSession.id]
    : undefined
  const sessionDocuments = (sessionArtifactIndex?.order ?? [])
    .map((artifactId) => sessionArtifactIndex!.byId[artifactId])
    .filter((meta) => meta.kind === 'memo' || meta.kind === 'deliverable')
  const reportArtifactId =
    sessionDocuments[0]?.artifactId
    ?? latestRun?.artifactOrder.find((artifactId) => {
      const kind = latestRun.artifacts[artifactId]?.kind
      return kind === 'memo' || kind === 'deliverable'
    })
    ?? null
  const reportRunId =
    sessionDocuments[0]?.runId ?? latestRun?.runId ?? null
  const earlyPhase =
    runningRun
    && ['intake', 'discovery', 'planning'].includes(runningRun.phase)
  // Context pills: one tap from the composer to the live
  // canvas surface; the pulsing dot marks the live one.
  const pills = (runningRun || reportArtifactId) && (
    <div className="mx-auto mb-1.5 flex max-w-5xl flex-wrap items-center gap-1.5">
      {runningRun && (
        <button
          className="inline-flex h-6 items-center gap-1.5 rounded-full border border-brand/20 bg-brand-subtle px-2.5 t-hint font-semibold text-brand transition-colors hover:bg-brand-subtle/80"
          onClick={() =>
            openCanvasView({ runId: runningRun.runId, view: 'run' })}
          type="button"
        >
          <span
            aria-hidden="true"
            className={cn(
              'size-1.5 rounded-full bg-brand',
              !reduceMotion && 'inqtrix-running-dot',
            )}
          />
          {earlyPhase
            ? t.agent.pills.followPlanning
            : t.agent.pills.followExecution}
        </button>
      )}
      {reportArtifactId && reportRunId && (
        <button
          className="inline-flex h-6 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 t-hint font-semibold text-muted-foreground transition-colors hover:text-foreground"
          onClick={() => {
            openCanvasView({
              artifactId: reportArtifactId,
              runId: reportRunId,
              view: 'document',
            })
          }}
          type="button"
        >
          <FileText aria-hidden="true" className="size-3" />
          {t.agent.pills.viewReport}
        </button>
      )}
    </div>
  )

  const composer = (
    <>
      {/* Pills ABOVE the gate tray: a pending gate docks directly onto
          the composer (one decision surface), the follow/report pills
          float above it. */}
      {pills}
      <ComposerGateTray actions={timelineActions} run={runningRun} />
      <AgentComposer
        answerMode={
          activeGate?.kind === 'clarification'
          || activeGate?.kind === 'child_clarification'
        }
        autonomy={selectedAutonomy}
        autonomyModes={autonomyModes}
        canvasComments={canvasCommentQueue}
        canvasDocuments={canvasDocumentOptions}
        pinnedCanvasDocumentId={pinnedCanvasArtifactId}
        onPinnedCanvasDocumentChange={setPinnedCanvasArtifactId}
        collections={collections}
        reportOptions={attachableReports}
        reportCatalog={knownReports}
        reportIds={reportIds}
        reportIdsMax={reportLimit}
        onReportIdsChange={setReportIds}
        ruleOptions={agentRules}
        selectedRuleIds={selectedRuleIds}
        onSelectedRuleIdsChange={setSelectedRuleIds}
        ruleIdsMax={reportRuleLimit}
        ruleComposedCharsMax={reportComposedLimit}
        onOpenPromptLibrary={() => dispatch({ type: 'setActiveView', view: 'prompt-library' })}
        disabled={composerAvailability.disabled}
        documents={documents}
        draftDoc={draftDoc}
        draftQuestion={draftQuestion}
        onDraftDocChange={setDraftDoc}
        depthMode={selectedDepth}
        depthSelectable={depthSelectable}
        tierMode={effectiveTier}
        tiers={tiers}
        onTierModeChange={onTierChange}
        engineMode={effectiveEngineMode}
        kernelSelectable={kernelSelectable}
        memoryEnabled={memoryEnabled}
        modelPicker={modelPicker}
        notice={
          !agentAvailable
            ? t.agent.composer.notAvailable
            : runningRun && !canEditAgentRun(runningRun)
              ? t.sharing.sharedViewOnly
              : null
        }
        onAutonomyChange={onAutonomyChange}
        onDepthModeChange={onDepthChange}
        onDraftQuestionChange={onDraftQuestionChange}
        onEngineModeChange={setEngineMode}
        onEditCanvasComment={(id) => {
          const draft = canvasCommentQueue.find((item) => item.id === id)
          if (!draft) return
          // Focus the document, then hand the draft to its view.
          const anchorRunId = selectedSession
            ? state.agentSessionArtifacts[selectedSession.id]
              ?.byId[draft.artifactId]?.runId
            : undefined
          openCanvasView({
            artifactId: draft.artifactId,
            runId: anchorRunId ?? latestRun?.runId ?? '',
            view: 'document',
          })
          setCanvasCommentEdit(draft)
        }}
        onRemoveCanvasComment={(id) =>
          setCanvasCommentQueue((current) =>
            current.filter((item) => item.id !== id))}
        onSelectedCollectionIdsChange={onSelectedCollectionIdsChange}
        onSelectedDocumentIdChange={onSelectedDocumentIdChange}
        onStop={() => {
          if (!runningRun || !canEditAgentRun(runningRun)) return
          if (demo) demo.cancel(runningRun.runId)
          else void cancelRun(runningRun.runId)
        }}
        onResponseFormChange={setResponseForm}
        onSelectedSkillIdsChange={setSelectedSkillIds}
        onSourcePolicyChange={handleSourcePolicyChange}
        knowledgeSwitch={knowledgeSwitch}
        onKnowledgeSwitchChange={handleKnowledgeSwitchChange}
        submitError={submitError}
        onSubmit={submitWhenShown}
        overview={overview}
        responseForm={responseForm}
        running={Boolean(runningRun)}
        selectedCollectionIds={selectedCollectionIds}
        selectedDocumentId={selectedDocumentId}
        selectedSkillIds={selectedSkillIds}
        statusExecution={statusExecution}
        sourcePolicy={sourcePolicy}
        sourceAvailability={sourceAvailability}
        toolUseCounts={toolUseCounts}
        maxAttachedSkills={agentBlock?.skills?.max_attached ?? 3}
        slashSkills={slashSkills}
      />
    </>
  )

  const timeline = (
    <div className="inqtrix-contained-panel flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <header
        aria-busy={surfaceTransitioning || undefined}
        className="z-10 flex inqtrix-panel-header shrink-0 items-center justify-between gap-2 border-b border-border bg-background px-3"
        data-agent-surface-transitioning={surfaceTransitioning || undefined}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <PanelToggle
            collapseLabel={t.agent.sessions.title}
            controlsId={SESSIONS_PANEL_ID}
            expandLabel={t.agent.sessions.title}
            expanded={isDesktop ? railVisible : isMobileSessionsOpen}
            onToggle={(next) => {
              if (isDesktop) {
                if (next !== railVisible) dispatch({ type: 'toggleAgentSessionsVisible' })
                return
              }
              setIsMobileSessionsOpen(next)
            }}
            side="left"
          />
          <h1 className="truncate t-section text-foreground">
            {committedSessionTitle}
          </h1>
        </div>
        <span inert={surfaceTransitioning || undefined}>
          <PanelToggle
            collapseLabel={t.agent.canvas.title}
            controlsId={CANVAS_PANEL_ID}
            expandLabel={t.agent.canvas.title}
            expanded={canvas.open}
            onToggle={(next) => {
              if (next) {
                dispatch({
                  descriptor:
                    activeCanvasView(canvas)
                    ?? fallbackCanvasDescriptor(latestRun),
                  source: 'user',
                  type: 'openAgentCanvasView',
                })
              } else {
                dispatch({ type: 'closeAgentCanvas' })
              }
            }}
            side="right"
          />
        </span>
      </header>
      <StructuralLoadBoundary
        className="min-h-0 flex-1"
        fallback={(
          <div className="mx-auto flex min-h-0 w-full max-w-5xl flex-1 flex-col px-4 py-4 md:px-8">
            <ConversationSkeleton anchor="top" fill />
          </div>
        )}
        identity={agentRevealKey}
        onVisibilityChange={({ identity, visible }) => {
          if (!visible) return
          const next = requestedSessionByIdentityRef.current.get(identity)
          if (!next) return
          setCommittedSession((current) =>
            current.identity === identity
              && current.id === next.id
              && current.title === next.title
              ? current
              : { ...next, identity })
        }}
        phase={agentLoadPhase}
      >
        {/* overflow-anchor:none like the chat transcript: the browser's
            own scroll anchoring would fight the follow rule when async
            markdown grows above the viewport. */}
        <ScrollArea
          className="min-h-0 flex-1 [overflow-anchor:none]"
          ref={transcriptScrollAreaRef}
        >
        {/* Same content width as the chat mode's transcript (max-w-5xl)
            so questions and answers read at the SAME measure across
            desks — and flush with the composer below. */}
        <div className="mx-auto w-full max-w-5xl px-4 py-4 md:px-8">
          {centerLoading ? (
            null
          ) : sessionRuns.length === 0 ? (
            <div className="flex min-h-[40vh] items-center justify-center">
              <WelcomeState
                actions={
                  <div className="flex flex-wrap items-center justify-center gap-2">
                    {[
                      t.agent.empty.exampleWeb,
                      t.agent.empty.exampleKnowledge,
                      t.agent.empty.exampleMission,
                    ].map((example) => (
                      <button
                        className="inline-flex min-h-8 max-w-full items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-center text-xs font-medium leading-snug text-muted-foreground transition-colors hover:bg-surface hover:text-foreground sm:max-w-56"
                        key={example}
                        onClick={() => onDraftQuestionChange(example)}
                        type="button"
                      >
                        <span className="whitespace-normal break-words">{example}</span>
                      </button>
                    ))}
                  </div>
                }
                subtitle={t.agent.empty.subtitle}
                title={t.agent.empty.title}
              />
            </div>
          ) : (
            <div className="space-y-6">
              {sessionRuns.map((run) => (
                <AgentRunTurn
                  actions={timelineActions}
                  artifactNames={sessionFileNames}
                  historical={runHistoryRef.current.runIds.has(run.runId)}
                  key={run.runId}
                  ruleTitles={ruleTitles}
                  run={run}
                  transportDegraded={pollingRunIds?.includes(run.runId)}
                />
              ))}
            </div>
          )}
        </div>
        </ScrollArea>
      </StructuralLoadBoundary>
      <div
        aria-busy={surfaceTransitioning || undefined}
        className="z-10 shrink-0 px-3 pb-2 pt-2 md:px-6"
      >
        {composer}
      </div>
    </div>
  )

  // The '+' menu aggregates SESSION-wide: the index carries every
  // document with its CURRENT anchor; before it loads, the latest run's
  // own artifacts are the honest fallback. The answer stays inline
  // (AgentAnswerBlock), never a tab.
  const menuDocuments: { artifactId: string; runId: string; title: string }[] =
    sessionDocuments.length > 0
      ? sessionDocuments.map((meta) => ({
        artifactId: meta.artifactId,
        runId: meta.runId,
        title: meta.title,
      }))
      : latestRun
        ? latestRun.artifactOrder
          .filter((artifactId) => {
            const kind = latestRun.artifacts[artifactId]?.kind
            return kind === 'memo' || kind === 'deliverable'
          })
          .map((artifactId) => ({
            artifactId,
            runId: latestRun.runId,
            title: latestRun.artifacts[artifactId]?.title ?? '',
          }))
        : []
  const addMenu = latestRun ? (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={t.agent.canvas.addTab}
          className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
          size="icon"
          type="button"
          variant="ghost"
        >
          <Plus className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className={optionMenuContentClassName}
        side="bottom"
        sideOffset={6}
      >
        <div className="py-1">
          <OptionMenuItem
            active={false}
            icon={ListChecks}
            label={t.agent.canvas.views.plan}
            onSelect={() =>
              openCanvasView({ runId: latestRun.runId, view: 'plan' })}
          />
          <OptionMenuItem
            active={false}
            icon={Waypoints}
            label={t.agent.canvas.views.run}
            onSelect={() =>
              openCanvasView({ runId: latestRun.runId, view: 'run' })}
          />
          {menuDocuments.map((entry) => (
            <OptionMenuItem
              active={false}
              icon={FileText}
              key={entry.artifactId}
              label={sessionFileNames[entry.artifactId]
                || entry.title
                || t.agent.canvas.views.document}
              onSelect={() =>
                openCanvasView({
                  artifactId: entry.artifactId,
                  runId: entry.runId,
                  view: 'document',
                })}
            />
          ))}
          {sessionArtifactIndex?.error && (
            <p className="px-2.5 py-1 t-hint text-destructive/90">
              {sessionArtifactIndex.error}
            </p>
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  ) : undefined

  const canvasHost = (
    <AgentCanvasReactContext.Provider value={canvasContext}>
      <CanvasHost
        addMenu={addMenu}
        canvas={canvas}
      emptyState={
        <div className="flex h-full items-center justify-center px-6">
          <div className="text-center">
            <p className="t-card text-foreground">{t.agent.canvas.empty}</p>
            <p className="mt-1 t-meta text-muted-foreground">
              {t.agent.canvas.emptyHint}
            </p>
          </div>
        </div>
      }
      iconFor={canvasIconFor}
      labelFor={canvasLabelFor}
      labels={{
        close: t.agent.canvas.close,
        closeTab: t.agent.canvas.closeTab,
        tabOverflow: t.agent.canvas.tabOverflow,
        follow: t.agent.canvas.follow,
        pinTab: t.agent.canvas.pinTab,
        pinned: t.agent.canvas.pinned,
        previewTab: t.agent.canvas.previewTab,
        unpin: t.agent.canvas.unpin,
      }}
        onActivateTab={(key) =>
          dispatch({ key, type: 'activateAgentCanvasTab' })}
        onClose={() => dispatch({ type: 'closeAgentCanvas' })}
        onCloseTab={(key) => dispatch({ key, type: 'closeAgentCanvasTab' })}
        onPinTab={(key) => dispatch({ key, type: 'pinAgentCanvasTab' })}
        onSetPinned={(pinned) =>
          dispatch({ pinned, type: 'setAgentCanvasPinned' })}
        registry={AGENT_CANVAS_REGISTRY}
        working={Boolean(runningRun)}
      />
    </AgentCanvasReactContext.Provider>
  )

  const timelineAndCanvas = (
    <ResizablePanelGroup
      className="min-h-0 min-w-0 flex-1 overflow-hidden"
      elementRef={canvasMotion.groupRef}
      onLayoutChanged={(layout: Record<string, number>) => {
        const size = layout[CANVAS_PANEL_ID]
        if (
          canvas.open
          && !canvasMotion.isProgrammaticLayoutChange()
          && typeof size === 'number'
          && size > 0
        ) {
          onCanvasPanelSizeChange(size)
        }
      }}
      orientation="horizontal"
    >
      <ResizablePanel
        id={AGENT_TIMELINE_PANEL_ID}
        // Identity anchor — must stay unconditional so the timeline DOM
        // survives the desktop/mobile flip.
        key={AGENT_TIMELINE_PANEL_ID}
        minSize="32%"
      >
        {timeline}
      </ResizablePanel>
      {isDesktop && (
        <>
          <AnimatedResizableHandle expanded={canvas.open} />
          <ResizablePanel
            collapsible
            collapsedSize="0%"
            defaultSize={`${canvas.open ? canvasPanelSize : 0}%`}
            id={CANVAS_PANEL_ID}
            maxSize="62%"
            minSize={canvas.open ? '30%' : '0%'}
            panelRef={canvasMotion.panelRef}
          >
            <AnimatedPanelBody expanded={canvas.open} side="right">
              {canvasHost}
            </AnimatedPanelBody>
          </ResizablePanel>
        </>
      )}
    </ResizablePanelGroup>
  )

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background">
      <div className="relative flex min-h-0 w-full flex-1 overflow-hidden bg-background">
        <ResizablePanelGroup
          className="min-h-0 min-w-0 flex-1 overflow-hidden"
          elementRef={railMotion.groupRef}
          onLayoutChanged={(layout: Record<string, number>) => {
            const size = layout[SESSIONS_PANEL_ID]
            if (
              railVisible
              && !railMotion.isProgrammaticLayoutChange()
              && typeof size === 'number'
              && size > 0
            ) {
              onSessionsPanelSizeChange(size)
            }
          }}
          orientation="horizontal"
        >
          {isDesktop && (
            <>
              <ResizablePanel
                collapsible
                collapsedSize="0%"
                defaultSize={`${railVisible ? state.ui.panelLayout.agentSessions : 0}%`}
                id={SESSIONS_PANEL_ID}
                maxSize="42%"
                minSize={railVisible ? '18%' : '0%'}
                panelRef={railMotion.panelRef}
              >
                <AnimatedPanelBody expanded={railVisible} side="left">
                  {rail}
                </AnimatedPanelBody>
              </ResizablePanel>
              <AnimatedResizableHandle expanded={railVisible} />
            </>
          )}
          <ResizablePanel
            id={AGENT_CENTER_PANEL_ID}
            // Identity anchor — must stay unconditional so the center content
            // survives the desktop/mobile flip.
            key={AGENT_CENTER_PANEL_ID}
            minSize="30%"
          >
            {timelineAndCanvas}
          </ResizablePanel>
        </ResizablePanelGroup>
        {!isDesktop && (
          <>
            <ResponsiveSidePanel
              closeLabel={t.agent.sessions.title}
              controlsId={SESSIONS_PANEL_ID}
              onOpenChange={setIsMobileSessionsOpen}
              open={isMobileSessionsOpen}
              showHeader={false}
              side="left"
              title={t.agent.sessions.title}
            >
              {rail}
            </ResponsiveSidePanel>
            <ResponsiveSidePanel
              closeLabel={t.agent.canvas.title}
              controlsId={CANVAS_PANEL_ID}
              onOpenChange={(open) => {
                if (open) {
                  dispatch({
                    descriptor:
                  activeCanvasView(canvas)
                  ?? fallbackCanvasDescriptor(latestRun),
                    source: 'user',
                    type: 'openAgentCanvasView',
                  })
                  return
                }
                dispatch({ type: 'closeAgentCanvas' })
              }}
              open={canvas.open}
              showHeader={false}
              side="right"
              title={t.agent.canvas.title}
            >
              {canvasHost}
            </ResponsiveSidePanel>
          </>
        )}
      </div>
    </section>
  )
}

/** Per-view tab glyphs for the canvas tab row (host stays agnostic). */
function canvasIconFor(descriptor: CanvasViewDescriptor): LucideIcon {
  switch (descriptor.view) {
    case 'plan':
      return ListChecks
    case 'run':
      return Waypoints
    case 'document':
      return FileText
    case 'evidence':
      return descriptor.label.startsWith('W') ? Globe2 : BookOpen
    case 'file':
      return Folder
    case 'diff':
      return Repeat2
    case 'patch':
      return PenLine
  }
}

function fallbackCanvasDescriptor(
  run: AgentRunRecord | undefined,
): CanvasViewDescriptor {
  if (run) {
    const documentId =
      run.artifactOrder.find(
        (artifactId) => run.artifacts[artifactId]?.kind === 'memo',
      )
      ?? [...run.artifactOrder]
        .reverse()
        .find(
          (artifactId) =>
            run.artifacts[artifactId]?.kind === 'deliverable',
        )
    if (documentId) {
      return { view: 'document', runId: run.runId, artifactId: documentId }
    }
    // A live run without a memo/deliverable: the Verlauf is what the user opens
    // the canvas for; settled runs land on the plan.
    if (run.status === 'running' || run.status === 'queued') {
      return { view: 'run', runId: run.runId }
    }
    return { view: 'plan', runId: run.runId }
  }
  return { view: 'plan', runId: '' }
}
