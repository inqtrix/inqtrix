import {
  BookOpenCheck,
  Folder,
  FolderOpen,
  FolderPlus,
  MoreHorizontal,
  PencilLine,
  Pin,
  PinOff,
  RotateCcw,
  SquarePen,
  Trash2,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
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
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  displayRelativeAge,
  type KnowledgeSessionHistorySection,
} from '@/features/project/selectors'
import type {
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import { QuotaUsageFooter } from '@/features/quota/QuotaUsageFooter'
import { useLocale } from '@/i18n/LocaleProvider'
import { ExplorerSortMenu } from '@/components/ui/explorer-sort-menu'
import { orderPinnedExplorerItems } from '@/features/project/explorerSort'
import type { ExplorerSortMode } from '@/features/project/explorerSort'
import { cn } from '@/lib/utils'
import { AnimatePresence, motion } from 'motion/react'
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from 'react'

type SessionDropTarget = {
  groupId: string | null
  targetIndex: number
}

type KnowledgeHistoryPanelProps = {
  items: KnowledgeThreadItemRecord[]
  onCreateSession: (groupId?: string | null) => void
  onCreateSessionGroup: () => void
  onDeleteSession: (sessionId: string) => void
  onRetrySessionDeletion: (sessionId: string) => void
  onDeleteSessionGroup: (groupId: string) => void
  onMoveSessionGroup: (groupId: string, targetIndex: number) => void
  onMoveSessionToGroup: (sessionId: string, groupId: string | null, targetIndex: number) => void
  /** Sorting: a drag in an automatic mode first adopts the visible
   * order (items, folders) so the switch to manual never jumps. */
  onAdoptVisibleOrder: (itemIds: string[], folderIds: string[]) => void
  onChangeSortMode: (mode: ExplorerSortMode) => void
  sortMode: ExplorerSortMode
  onPrefetchSession?: (sessionId: string) => void
  onRenameSession: (sessionId: string, title: string) => void
  onRenameSessionGroup: (groupId: string, title: string) => void
  onSelectSession: (sessionId: string) => void
  onTogglePinnedSession: (sessionId: string) => void
  pinnedSessionIds: readonly string[]
  sections: KnowledgeSessionHistorySection[]
  selectedSessionId: string | null
  reduceMotion: boolean
  sessions: KnowledgeSessionRecord[]
}

const UNGROUPED_KNOWLEDGE_SECTION_ID = '__ungrouped_knowledge__'

export function KnowledgeHistoryPanel({
  items,
  onCreateSession,
  onCreateSessionGroup,
  onDeleteSession,
  onRetrySessionDeletion,
  onDeleteSessionGroup,
  onMoveSessionGroup,
  onMoveSessionToGroup,
  onAdoptVisibleOrder,
  onChangeSortMode,
  sortMode,
  onPrefetchSession,
  onRenameSession,
  onRenameSessionGroup,
  onSelectSession,
  onTogglePinnedSession,
  pinnedSessionIds,
  sections,
  selectedSessionId,
  reduceMotion,
  sessions,
}: KnowledgeHistoryPanelProps) {
  const { t } = useLocale()
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<ReadonlySet<string>>(() => new Set())
  const [draggedGroupId, setDraggedGroupId] = useState<string | null>(null)
  const [draggedSessionId, setDraggedSessionId] = useState<string | null>(null)
  const [editingGroupId, setEditingGroupId] = useState<string | null>(null)
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)
  const [groupDropTargetIndex, setGroupDropTargetIndex] = useState<number | null>(null)
  const [groupTitleDraft, setGroupTitleDraft] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [sessionDropTarget, setSessionDropTarget] = useState<SessionDropTarget | null>(null)
  const [sessionTitleDraft, setSessionTitleDraft] = useState('')
  const groupTitleInputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const sessionTitleInputRef = useRef<HTMLInputElement | null>(null)
  const suppressGroupToggleClickRef = useRef(false)
  const suppressSessionSelectClickRef = useRef(false)

  const runningSessionIds = new Set<string>()
  for (const item of items) {
    if (item.status === 'running') runningSessionIds.add(item.sessionId)
  }

  useLayoutEffect(() => {
    if (!editingGroupId) return
    groupTitleInputRef.current?.focus()
    groupTitleInputRef.current?.select()
  }, [editingGroupId])

  useLayoutEffect(() => {
    if (!editingSessionId) return
    sessionTitleInputRef.current?.focus()
    sessionTitleInputRef.current?.select()
  }, [editingSessionId])

  useEffect(() => {
    if (!editingSessionId) return
    if (sessions.some((session) => session.id === editingSessionId)) return
    setEditingSessionId(null)
    setSessionTitleDraft('')
  }, [editingSessionId, sessions])

  function toggleGroup(groupId: string) {
    if (suppressGroupToggleClickRef.current) {
      suppressGroupToggleClickRef.current = false
      return
    }
    setCollapsedGroupIds((current) => {
      const next = new Set(current)
      if (next.has(groupId)) {
        next.delete(groupId)
      } else {
        next.add(groupId)
      }
      return next
    })
  }

  function selectSessionFromHistory(sessionId: string) {
    if (suppressSessionSelectClickRef.current) {
      suppressSessionSelectClickRef.current = false
      return
    }
    onSelectSession(sessionId)
  }

  function startGroupEdit(groupId: string, title: string) {
    setEditingGroupId(groupId)
    setGroupTitleDraft(title)
  }

  function commitGroupEdit() {
    if (!editingGroupId) return
    const title = groupTitleDraft.trim()
    if (title) onRenameSessionGroup(editingGroupId, title)
    setEditingGroupId(null)
    setGroupTitleDraft('')
  }

  function cancelGroupEdit() {
    setEditingGroupId(null)
    setGroupTitleDraft('')
  }

  function startSessionEdit(session: KnowledgeSessionRecord) {
    setEditingSessionId(session.id)
    setSessionTitleDraft(session.title)
  }

  function commitSessionEdit() {
    if (!editingSessionId) return
    const title = sessionTitleDraft.trim()
    if (title) onRenameSession(editingSessionId, title)
    setEditingSessionId(null)
    setSessionTitleDraft('')
  }

  function cancelSessionEdit() {
    setEditingSessionId(null)
    setSessionTitleDraft('')
  }

  function readGroupDropTarget(clientY: number, excludedGroupId = draggedGroupId) {
    const container = listRef.current
    if (!container) return null
    const groupElements = Array.from(container.querySelectorAll<HTMLElement>('[data-knowledge-history-group-id]'))
      .filter((groupElement) => (
        groupElement.dataset.knowledgeHistoryGroupId !== excludedGroupId
        && groupElement.getBoundingClientRect().height > 0
      ))

    for (const [index, groupElement] of groupElements.entries()) {
      const rect = groupElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return index
    }

    return groupElements.length
  }

  function beginGroupDrag(event: ReactPointerEvent<HTMLElement>, groupId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function startDrag(moveEvent: PointerEvent) {
      didStartDrag = true
      suppressGroupToggleClickRef.current = true
      setDraggedGroupId(groupId)
      setGroupDropTargetIndex(readGroupDropTarget(moveEvent.clientY, groupId))
    }

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        startDrag(moveEvent)
      }
      moveEvent.preventDefault()
      setGroupDropTargetIndex(readGroupDropTarget(moveEvent.clientY, groupId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const targetIndex = didStartDrag ? readGroupDropTarget(upEvent.clientY, groupId) : null
      cleanupPointerDrag()
      if (targetIndex !== null) {
        adoptVisibleOrderRef.current()
        onMoveSessionGroup(groupId, targetIndex)
      }
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cleanupPointerDrag)
      setDraggedGroupId(null)
      setGroupDropTargetIndex(null)
      if (didStartDrag) {
        window.setTimeout(() => {
          suppressGroupToggleClickRef.current = false
        }, 0)
      }
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cleanupPointerDrag)
  }

  function readSessionDropTarget(clientY: number, excludedSessionId = draggedSessionId): SessionDropTarget | null {
    const container = listRef.current
    if (!container) return null
    const sectionElements = Array.from(container.querySelectorAll<HTMLElement>('[data-knowledge-history-section]'))
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
    const groupKey = sectionElement.dataset.knowledgeHistorySectionGroupId ?? UNGROUPED_KNOWLEDGE_SECTION_ID
    const groupId = groupKey === UNGROUPED_KNOWLEDGE_SECTION_ID ? null : groupKey
    const sessionElements = Array.from(sectionElement.querySelectorAll<HTMLElement>('[data-knowledge-history-session-id]'))
      .filter((sessionElement) => (
        sessionElement.dataset.knowledgeHistorySessionId !== excludedSessionId
        && sessionElement.getBoundingClientRect().height > 0
      ))

    for (const [index, sessionElement] of sessionElements.entries()) {
      const rect = sessionElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return { groupId, targetIndex: index }
    }

    return { groupId, targetIndex: sessionElements.length }
  }

  function beginSessionDrag(event: ReactPointerEvent<HTMLElement>, sessionId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function startDrag(moveEvent: PointerEvent) {
      didStartDrag = true
      suppressSessionSelectClickRef.current = true
      setDraggedSessionId(sessionId)
      setSessionDropTarget(readSessionDropTarget(moveEvent.clientY, sessionId))
    }

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        startDrag(moveEvent)
      }
      moveEvent.preventDefault()
      setSessionDropTarget(readSessionDropTarget(moveEvent.clientY, sessionId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const target = didStartDrag ? readSessionDropTarget(upEvent.clientY, sessionId) : null
      cleanupPointerDrag()
      if (target) {
        adoptVisibleOrderRef.current()
        onMoveSessionToGroup(sessionId, target.groupId, target.targetIndex)
      }
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cleanupPointerDrag)
      setDraggedSessionId(null)
      setSessionDropTarget(null)
      if (didStartDrag) {
        window.setTimeout(() => {
          suppressSessionSelectClickRef.current = false
        }, 0)
      }
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cleanupPointerDrag)
  }

  const pinnedSessionIdSet = new Set(pinnedSessionIds)
  const trimmedSearchQuery = searchQuery.trim().toLowerCase()
  const isSearching = trimmedSearchQuery.length > 0
  const searchResults = useMemo(
    () => (isSearching
      ? sessions.filter((session) => session.title.toLowerCase().includes(trimmedSearchQuery))
      : []),
    [isSearching, sessions, trimmedSearchQuery],
  )
  const pinnedSessions = orderPinnedExplorerItems(
    sessions.filter((session) => pinnedSessionIdSet.has(session.id)),
    pinnedSessionIds,
    sortMode,
    (session) => session.id,
  )
  const explorerSections = sections.map((section) => ({
    ...section,
    sessions: section.sessions.filter((session) => !pinnedSessionIdSet.has(session.id)),
  })) as KnowledgeSessionHistorySection[]
  const adoptVisibleOrderIfAutomatic = () => {
    if (sortMode === 'manual') return
    onAdoptVisibleOrder(
      [
        ...pinnedSessions.map((session) => session.id),
        ...explorerSections.flatMap((section) => section.sessions.map((session) => session.id)),
      ],
      explorerSections.flatMap((section) => (section.kind === 'group' ? [section.groupId] : [])),
    )
  }
  const adoptVisibleOrderRef = useRef(adoptVisibleOrderIfAutomatic)
  adoptVisibleOrderRef.current = adoptVisibleOrderIfAutomatic
  const hasStructure = pinnedSessions.length > 0
    || explorerSections.some((section) => section.sessions.length > 0 || section.kind === 'group')
  const showUngroupedHeader = explorerSections.some((section) => section.kind === 'group')
  const groupIds = explorerSections.flatMap((section) => (section.kind === 'group' ? [section.groupId] : []))
  const dropGroupIds = draggedGroupId ? groupIds.filter((groupId) => groupId !== draggedGroupId) : groupIds

  const prefetchSessionFromTarget = (target: EventTarget | null) => {
    if (!onPrefetchSession || !(target instanceof Element)) return
    const row = target.closest<HTMLElement>('[data-knowledge-history-session-id]')
    const sessionId = row?.dataset.knowledgeHistorySessionId
    if (sessionId) onPrefetchSession(sessionId)
  }

  return (
    <aside
      className="inqtrix-contained-panel flex h-full min-h-0 w-full flex-col border-r border-border bg-surface/60"
      onFocusCapture={(event) => prefetchSessionFromTarget(event.target)}
      onPointerOver={(event) => prefetchSessionFromTarget(event.target)}
    >
      <div className="flex inqtrix-panel-header items-center justify-between gap-2 border-b border-border px-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="truncate t-section text-foreground">{t.knowledge.sessions}</h2>
        </div>
        <div className="flex items-center gap-1.5">
          <ExplorerSortMenu mode={sortMode} onChangeMode={onChangeSortMode} />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.knowledge.newFolder}
                className="size-7 shrink-0"
                onClick={onCreateSessionGroup}
                size="icon"
                type="button"
                variant="ghost"
              >
                <FolderPlus className="size-4 text-foreground/85" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t.knowledge.newFolder}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.knowledge.newSession}
                className="size-7 shrink-0"
                onClick={() => onCreateSession()}
                size="icon"
                type="button"
                variant="ghost"
              >
                <SquarePen className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t.knowledge.newSession}</TooltipContent>
          </Tooltip>
        </div>
      </div>
      <ExplorerSearchField
        clearLabel={t.knowledge.searchClear}
        label={t.knowledge.searchSessions}
        onChange={setSearchQuery}
        onClear={() => setSearchQuery('')}
        placeholder={t.knowledge.searchSessions}
        value={searchQuery}
      />

      <ScrollArea className="min-h-0 flex-1">
        <div className="inqtrix-explorer-list space-y-1 p-2" ref={listRef}>
          {isSearching ? (
            searchResults.length > 0 ? (
              <div className="space-y-0.5">
                {searchResults.map((session) => (
                  <KnowledgeSessionHistoryItem
                    beginSessionDrag={beginSessionDrag}
                    commitSessionEdit={commitSessionEdit}
                    cancelSessionEdit={cancelSessionEdit}
                    dragged={draggedSessionId === session.id}
                    editing={editingSessionId === session.id}
                    key={session.id}
                    nested={false}
                    onDeleteSession={onDeleteSession}
                    onRetrySessionDeletion={onRetrySessionDeletion}
                    onSelectSession={selectSessionFromHistory}
                    onSessionTitleDraftChange={setSessionTitleDraft}
                    onTogglePinnedSession={onTogglePinnedSession}
                    pinned={pinnedSessionIdSet.has(session.id)}
                    running={runningSessionIds.has(session.id)}
                    selected={selectedSessionId === session.id}
                    sectionGroupId={null}
                    session={session}
                    sessionDropTarget={null}
                    sessionIndex={0}
                    sessionTitleDraft={sessionTitleDraft}
                    sessionTitleInputRef={sessionTitleInputRef}
                    startSessionEdit={startSessionEdit}
                  />
                ))}
              </div>
            ) : (
              <p className="px-2 py-6 text-center t-meta-sm text-muted-foreground">{t.knowledge.searchEmpty}</p>
            )
          ) : hasStructure ? (
            <>
              {pinnedSessions.length > 0 && (
                <div className="space-y-0.5">
                  <ExplorerSectionLabel className="pt-0">{t.knowledge.pinned}</ExplorerSectionLabel>
                  {pinnedSessions.map((session) => (
                    <KnowledgeSessionHistoryItem
                      beginSessionDrag={beginSessionDrag}
                      commitSessionEdit={commitSessionEdit}
                      cancelSessionEdit={cancelSessionEdit}
                      dragged={draggedSessionId === session.id}
                      editing={editingSessionId === session.id}
                      key={session.id}
                      nested={false}
                      onDeleteSession={onDeleteSession}
                      onRetrySessionDeletion={onRetrySessionDeletion}
                      onSelectSession={selectSessionFromHistory}
                      onSessionTitleDraftChange={setSessionTitleDraft}
                      onTogglePinnedSession={onTogglePinnedSession}
                      pinned
                      running={runningSessionIds.has(session.id)}
                      selected={selectedSessionId === session.id}
                      sectionGroupId={null}
                      session={session}
                      sessionDropTarget={null}
                      sessionIndex={0}
                      sessionTitleDraft={sessionTitleDraft}
                      sessionTitleInputRef={sessionTitleInputRef}
                      startSessionEdit={startSessionEdit}
                    />
                  ))}
                </div>
              )}
              {explorerSections.map((section) => (
                <KnowledgeHistorySectionView
                  beginGroupDrag={beginGroupDrag}
                  beginSessionDrag={beginSessionDrag}
                  cancelGroupEdit={cancelGroupEdit}
                  cancelSessionEdit={cancelSessionEdit}
                  collapsedGroupIds={collapsedGroupIds}
                  commitGroupEdit={commitGroupEdit}
                  commitSessionEdit={commitSessionEdit}
                  draggedGroupId={draggedGroupId}
                  draggedSessionId={draggedSessionId}
                  editingGroupId={editingGroupId}
                  editingSessionId={editingSessionId}
                  groupDropTargetIndex={groupDropTargetIndex}
                  groupIndex={section.kind === 'group' ? dropGroupIds.indexOf(section.groupId) : -1}
                  groupTitleDraft={groupTitleDraft}
                  groupTitleInputRef={groupTitleInputRef}
                  groupCount={dropGroupIds.length}
                  key={section.kind === 'group' ? section.groupId : UNGROUPED_KNOWLEDGE_SECTION_ID}
                  onCreateSession={onCreateSession}
                  onDeleteSession={onDeleteSession}
                  onRetrySessionDeletion={onRetrySessionDeletion}
                  onDeleteSessionGroup={onDeleteSessionGroup}
                  onGroupTitleDraftChange={setGroupTitleDraft}
                  onSelectSession={selectSessionFromHistory}
                  onSessionTitleDraftChange={setSessionTitleDraft}
                  onTogglePinnedSession={onTogglePinnedSession}
                  reduceMotion={reduceMotion}
                  runningSessionIds={runningSessionIds}
                  section={section}
                  selectedSessionId={selectedSessionId}
                  sessionDropTarget={sessionDropTarget}
                  sessionTitleDraft={sessionTitleDraft}
                  sessionTitleInputRef={sessionTitleInputRef}
                  showUngroupedHeader={showUngroupedHeader}
                  startGroupEdit={startGroupEdit}
                  startSessionEdit={startSessionEdit}
                  toggleGroup={toggleGroup}
                />
              ))}
            </>
          ) : (
            <div className="rounded-md border border-dashed border-border p-4 text-center t-meta-sm text-muted-foreground">
              {t.knowledge.noSessions}
            </div>
          )}
        </div>
      </ScrollArea>
      <QuotaUsageFooter dimensions={['embedding_tokens', 'llm_tokens']} />
    </aside>
  )
}

function KnowledgeHistorySectionView({
  beginGroupDrag,
  beginSessionDrag,
  cancelGroupEdit,
  cancelSessionEdit,
  collapsedGroupIds,
  commitGroupEdit,
  commitSessionEdit,
  draggedGroupId,
  draggedSessionId,
  editingGroupId,
  editingSessionId,
  groupDropTargetIndex,
  groupIndex,
  groupTitleDraft,
  groupTitleInputRef,
  groupCount,
  onCreateSession,
  onDeleteSession,
  onRetrySessionDeletion,
  onDeleteSessionGroup,
  onGroupTitleDraftChange,
  onSelectSession,
  onSessionTitleDraftChange,
  onTogglePinnedSession,
  reduceMotion,
  runningSessionIds,
  section,
  selectedSessionId,
  sessionDropTarget,
  sessionTitleDraft,
  sessionTitleInputRef,
  showUngroupedHeader,
  startGroupEdit,
  startSessionEdit,
  toggleGroup,
}: {
  beginGroupDrag: (event: ReactPointerEvent<HTMLElement>, groupId: string) => void
  beginSessionDrag: (event: ReactPointerEvent<HTMLElement>, sessionId: string) => void
  cancelGroupEdit: () => void
  cancelSessionEdit: () => void
  collapsedGroupIds: ReadonlySet<string>
  commitGroupEdit: () => void
  commitSessionEdit: () => void
  draggedGroupId: string | null
  draggedSessionId: string | null
  editingGroupId: string | null
  editingSessionId: string | null
  groupDropTargetIndex: number | null
  groupIndex: number
  groupTitleDraft: string
  groupTitleInputRef: RefObject<HTMLInputElement | null>
  groupCount: number
  onCreateSession: (groupId?: string | null) => void
  onDeleteSession: (sessionId: string) => void
  onRetrySessionDeletion: (sessionId: string) => void
  onDeleteSessionGroup: (groupId: string) => void
  onGroupTitleDraftChange: (value: string) => void
  onSelectSession: (sessionId: string) => void
  onSessionTitleDraftChange: (value: string) => void
  onTogglePinnedSession: (sessionId: string) => void
  reduceMotion: boolean
  runningSessionIds: ReadonlySet<string>
  section: KnowledgeSessionHistorySection
  selectedSessionId: string | null
  sessionDropTarget: SessionDropTarget | null
  sessionTitleDraft: string
  sessionTitleInputRef: RefObject<HTMLInputElement | null>
  showUngroupedHeader: boolean
  startGroupEdit: (groupId: string, title: string) => void
  startSessionEdit: (session: KnowledgeSessionRecord) => void
  toggleGroup: (groupId: string) => void
}) {
  const { t } = useLocale()
  const groupId = section.groupId
  const groupKey = groupId ?? UNGROUPED_KNOWLEDGE_SECTION_ID
  const isCollapsed = groupId ? collapsedGroupIds.has(groupId) : false
  const isDraggingGroup = groupId !== null && draggedGroupId === groupId
  const showDropFrame = sessionDropTarget?.groupId === groupId
  const showGroupBeforeIndicator = section.kind === 'group' && groupIndex >= 0 && groupDropTargetIndex === groupIndex
  const showGroupAfterIndicator = (
    section.kind === 'group'
    && groupIndex >= 0
    && groupDropTargetIndex === groupCount
    && groupIndex === groupCount - 1
  )
  const sessions = isCollapsed ? [] : section.sessions
  const SectionIcon = section.kind === 'group'
    ? isCollapsed ? Folder : FolderOpen
    : BookOpenCheck
  const isGroupSection = section.kind === 'group'
  const [visibleSessionCount, setVisibleSessionCount] = useState(EXPLORER_SECTION_REVEAL_STEP)

  return (
    <motion.div
      className={cn(
        'relative transition-colors',
        showDropFrame && 'rounded-md bg-brand-subtle/45',
        isDraggingGroup && 'scale-[0.995] opacity-80 shadow-[0_8px_20px_var(--shadow-soft)] ring-1 ring-ring/40',
      )}
      data-knowledge-history-group-id={section.kind === 'group' ? section.groupId : undefined}
      data-knowledge-history-section
      data-knowledge-history-section-group-id={groupKey}
    >
      {showGroupBeforeIndicator && (
        <span className="pointer-events-none absolute -top-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
      )}
      {showGroupAfterIndicator && (
        <span className="pointer-events-none absolute -bottom-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
      )}

      {section.kind === 'group' && (
        <ExplorerFolderRow
          onPointerDown={(event) => beginGroupDrag(event, section.groupId)}
          actions={(
            <>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    aria-label={t.common.menu}
                    className="size-6 shrink-0 text-foreground/55 hover:text-foreground"
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <MoreHorizontal className="icon-sm" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-56">
                  <DropdownMenuItem onSelect={() => startGroupEdit(section.groupId, section.group.title)}>
                    <PencilLine className="icon-sm" />
                    {t.knowledge.renameFolder}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    className="text-destructive focus:text-destructive"
                    onSelect={() => onDeleteSessionGroup(section.groupId)}
                  >
                    <Trash2 className="icon-sm" />
                    {t.knowledge.deleteFolder}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <HistoryIconButton
                label={`${t.knowledge.newInFolder}: ${section.group.title}`}
                onClick={() => onCreateSession(section.groupId)}
              >
                <SquarePen className="icon-sm" />
              </HistoryIconButton>
            </>
          )}
        >
          {editingGroupId === section.groupId ? (
            <span className="flex min-h-8 min-w-0 items-center gap-1.5" data-explorer-action>
              <FolderOpen className="icon-sm shrink-0 text-muted-foreground" />
              <input
                aria-label={t.knowledge.renameFolder}
                className="min-w-0 flex-1 rounded-sm border-0 bg-background/85 px-1.5 py-0.5 t-list text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
                onBlur={commitGroupEdit}
                onChange={(event) => onGroupTitleDraftChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    commitGroupEdit()
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    cancelGroupEdit()
                  }
                }}
                ref={groupTitleInputRef}
                value={groupTitleDraft}
              />
            </span>
          ) : (
            <ExplorerFolderToggle
              count={section.sessions.length}
              expanded={!isCollapsed}
              icon={<SectionIcon className="icon-sm shrink-0" />}
              label={`${isCollapsed ? t.chat.expandGroup : t.chat.collapseGroup}: ${section.group.title}`}
              onDoubleClick={(event) => {
                event.preventDefault()
                startGroupEdit(section.groupId, section.group.title)
              }}
              onToggle={() => toggleGroup(section.groupId)}
              title={section.group.title}
            />
          )}
        </ExplorerFolderRow>
      )}

      {section.kind === 'ungrouped' && showUngroupedHeader && (
        <div className="flex min-h-7 items-center gap-2 rounded-md px-1.5 t-caption text-foreground/65">
          <SectionIcon className="icon-sm shrink-0" />
          <span className="truncate">{t.knowledge.ungrouped}</span>
        </div>
      )}

      <AnimatePresence initial={false}>
        {(!isCollapsed || showDropFrame) && (
          <motion.div
            animate={{ height: 'auto', opacity: 1 }}
            className="overflow-hidden"
            exit={reduceMotion ? undefined : { height: 0, opacity: 0 }}
            initial={reduceMotion ? false : { height: 0, opacity: 0 }}
            transition={{ duration: 0.16, ease: [0.2, 0, 0, 1] }}
          >
            <div className="space-y-0.5">
              {(isGroupSection ? sessions.slice(0, visibleSessionCount) : sessions).map((session, index) => (
                <KnowledgeSessionHistoryItem
                  beginSessionDrag={beginSessionDrag}
                  commitSessionEdit={commitSessionEdit}
                  cancelSessionEdit={cancelSessionEdit}
                  dragged={draggedSessionId === session.id}
                  editing={editingSessionId === session.id}
                  key={session.id}
                  nested={section.kind === 'group'}
                  onDeleteSession={onDeleteSession}
                  onRetrySessionDeletion={onRetrySessionDeletion}
                  onSelectSession={onSelectSession}
                  onSessionTitleDraftChange={onSessionTitleDraftChange}
                  onTogglePinnedSession={onTogglePinnedSession}
                  running={runningSessionIds.has(session.id)}
                  selected={selectedSessionId === session.id}
                  sectionGroupId={groupId}
                  session={session}
                  sessionDropTarget={sessionDropTarget}
                  sessionIndex={index}
                  sessionTitleDraft={sessionTitleDraft}
                  sessionTitleInputRef={sessionTitleInputRef}
                  startSessionEdit={startSessionEdit}
                />
              ))}
              {/* Folders only — see EXPLORER_SECTION_REVEAL_STEP. */}
              {isGroupSection && (
                <ExplorerRevealControls
                  onShowLess={() => setVisibleSessionCount(EXPLORER_SECTION_REVEAL_STEP)}
                  onShowMore={() => setVisibleSessionCount((count) => Math.min(count + EXPLORER_SECTION_REVEAL_STEP, sessions.length))}
                  showLessLabel={t.knowledge.showLess}
                  showMoreLabel={t.knowledge.showMore}
                  total={sessions.length}
                  visibleCount={visibleSessionCount}
                />
              )}
              {section.kind === 'group' && sessions.length === 0 && !isCollapsed && (
                <div className="rounded-md px-2 py-1.5 t-meta-sm font-medium text-muted-foreground">
                  {t.knowledge.emptyFolder}
                </div>
              )}
              {showDropFrame && sessions.length === 0 && (
                <div className="rounded-md border border-dashed border-brand/30 px-2 py-1.5 text-center t-meta-sm font-semibold text-brand">
                  {t.knowledge.dropIntoFolder}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function KnowledgeSessionHistoryItem({
  beginSessionDrag,
  cancelSessionEdit,
  commitSessionEdit,
  dragged,
  editing,
  nested,
  onDeleteSession,
  onRetrySessionDeletion,
  onSelectSession,
  onSessionTitleDraftChange,
  onTogglePinnedSession,
  pinned,
  running,
  selected,
  sectionGroupId,
  session,
  sessionDropTarget,
  sessionIndex,
  sessionTitleDraft,
  sessionTitleInputRef,
  startSessionEdit,
}: {
  beginSessionDrag: (event: ReactPointerEvent<HTMLElement>, sessionId: string) => void
  cancelSessionEdit: () => void
  commitSessionEdit: () => void
  dragged: boolean
  editing: boolean
  nested: boolean
  onDeleteSession: (sessionId: string) => void
  onRetrySessionDeletion: (sessionId: string) => void
  onSelectSession: (sessionId: string) => void
  onSessionTitleDraftChange: (value: string) => void
  onTogglePinnedSession: (sessionId: string) => void
  pinned?: boolean
  running: boolean
  selected: boolean
  sectionGroupId: string | null
  session: KnowledgeSessionRecord
  sessionDropTarget: SessionDropTarget | null
  sessionIndex: number
  sessionTitleDraft: string
  sessionTitleInputRef: RefObject<HTMLInputElement | null>
  startSessionEdit: (session: KnowledgeSessionRecord) => void
}) {
  const { locale, t } = useLocale()
  const sessionDropApplies = sessionDropTarget?.groupId === sectionGroupId
  const showBeforeIndicator = sessionDropApplies && sessionDropTarget?.targetIndex === sessionIndex
  const showAfterIndicator = sessionDropApplies && sessionDropTarget?.targetIndex === sessionIndex + 1
  const sessionTime = displayRelativeAge(session.updatedAt, locale)
  const deleting = session.deletion?.status === 'deleting'
  const deleteFailed = session.deletion?.status === 'delete_failed'

  return (
    <motion.div
      className="relative"
      data-knowledge-history-session-id={session.id}
    >
      {showBeforeIndicator && (
        <span className="pointer-events-none absolute -top-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
      )}
      {showAfterIndicator && (
        <span className="pointer-events-none absolute -bottom-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
      )}
      <ExplorerHistoryRow
        actions={session.deletion ? (deleteFailed ? [{
          ariaLabel: `${t.knowledge.retrySessionDelete}: ${session.title}`,
          icon: <RotateCcw className="icon-sm" />,
          label: t.knowledge.retrySessionDelete,
          onSelect: () => onRetrySessionDeletion(session.id),
        }] : []) : [
          {
            ariaLabel: `${pinned ? t.knowledge.unpinSession : t.knowledge.pinSession}: ${session.title}`,
            icon: pinned ? <PinOff className="icon-sm" /> : <Pin className="icon-sm" />,
            label: pinned ? t.knowledge.unpinSession : t.knowledge.pinSession,
            onSelect: () => onTogglePinnedSession(session.id),
          },
          {
            ariaLabel: `${t.knowledge.deleteSession}: ${session.title}`,
            destructive: true,
            icon: <Trash2 className="icon-sm" />,
            label: t.knowledge.deleteSession,
            onSelect: () => onDeleteSession(session.id),
          },
        ]}
        active={selected}
        disabled={Boolean(session.deletion)}
        dragging={dragged}
        indicator={deleting
          ? <ExplorerRunningIndicator label={t.knowledge.sessionDeleting} />
          : running ? <ExplorerRunningIndicator label={t.common.running} /> : undefined}
        nested={nested}
        onPointerDown={session.deletion
          ? undefined
          : (event) => beginSessionDrag(event, session.id)}
        onSelect={() => onSelectSession(session.id)}
        onStartRename={session.deletion ? undefined : () => startSessionEdit(session)}
        renameEditor={editing ? (
          <ExplorerHistoryTitleInput
            inputRef={sessionTitleInputRef}
            label={t.knowledge.renameSession}
            onCancel={cancelSessionEdit}
            onChange={onSessionTitleDraftChange}
            onCommit={commitSessionEdit}
            value={sessionTitleDraft}
          />
        ) : undefined}
        renameLabel={session.deletion ? undefined : t.knowledge.renameSession}
        timeLabel={deleting
          ? t.knowledge.sessionDeleting
          : deleteFailed
            ? t.knowledge.sessionDeleteFailed
            : sessionTime}
        title={session.title}
      />
    </motion.div>
  )
}

function HistoryIconButton({
  children,
  destructive,
  label,
  onClick,
}: {
  children: ReactNode
  destructive?: boolean
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className={cn(
            'size-6 shrink-0 text-foreground/50 transition',
            destructive ? 'hover:text-destructive' : 'hover:text-foreground',
          )}
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
