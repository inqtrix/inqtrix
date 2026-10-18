import { useMemo, useRef, useState } from 'react'

import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import { Folder, FolderOpen, FolderPlus, LoaderCircle, MoreHorizontal, PenLine, Pin, PinOff, RotateCcw, SquarePen, Trash2 } from '@/components/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ExplorerSortMenu } from '@/components/ui/explorer-sort-menu'
import { orderPinnedExplorerItems, sortExplorerFolders, sortExplorerItems } from '@/features/project/explorerSort'
import type { ExplorerSortMode } from '@/features/project/explorerSort'
import { Button } from '@/components/ui/button'
import {
  ExplorerFolderRow,
  ExplorerFolderToggle,
  ExplorerHistoryRow,
  ExplorerHistoryTitleInput,
  ExplorerSearchField,
  ExplorerSectionLabel,
  ExplorerRunningIndicator,
  isExplorerActionTarget,
  isPastExplorerDragThreshold,
} from '@/components/ui/explorer-list'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { displayRelativeAge } from '@/features/project/selectors'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import {
  agentSessionHistoryTimeIso,
  isActiveAgentRun,
  isGateAgentRun,
  type AgentRunRecord,
  type AgentSessionGroupRecord,
  type AgentSessionRecord,
} from './model'

const UNGROUPED_AGENT_SECTION_ID = '__ungrouped_agent__'

type AgentSessionDropTarget = { groupId: string | null; targetIndex: number }

/**
 * Session rail of the Agent Desk (KnowledgeHistoryPanel structure, shared
 * explorer primitives). Rows show a micro live indicator while a session's
 * latest run is active; a parked run (waiting) shows the warning dot —
 * ambient signal for "needs you". Sessions and folders move by the same
 * pointer drag the chat and knowledge rails use (unification round —
 * the interim move-to-folder menu is gone).
 */
import { sessionTitleHint } from '@/features/sessionTitle/model'

export function AgentSessionRail({
  creatingSession = false,
  onAdoptVisibleOrder,
  onChangeSortMode,
  onCreateSession,
  onCreateSessionGroup,
  onDeleteSession,
  onDeleteSessionGroup,
  onMoveSessionGroup,
  onMoveSessionToGroup,
  onRenameSession,
  onRenameSessionGroup,
  onRetrySessionDeletion,
  onSelectSession,
  onTogglePinnedSession,
  pinnedSessionIds,
  sortMode,
  runs,
  selectedSessionId,
  sessionGroupOrder,
  sessionGroups,
  sessionOrder,
  sessions,
  syncError = null,
}: {
  /** Sorting: a drag in an automatic mode first adopts the visible
   * order (items, folders) so the switch to manual never jumps. */
  onAdoptVisibleOrder: (itemIds: string[], folderIds: string[]) => void
  onChangeSortMode: (mode: ExplorerSortMode) => void
  /** A new session is on its way to the server: the button shows it and
   * takes no second click. */
  creatingSession?: boolean
  onCreateSession: () => void
  onCreateSessionGroup: () => void
  onDeleteSession: (sessionId: string) => void
  onDeleteSessionGroup: (groupId: string) => void
  onMoveSessionGroup: (groupId: string, targetIndex: number) => void
  onMoveSessionToGroup: (sessionId: string, groupId: string | null, targetIndex: number) => void
  onRenameSession: (sessionId: string, title: string) => void
  onRenameSessionGroup: (groupId: string, title: string) => void
  onRetrySessionDeletion: (sessionId: string) => void
  onSelectSession: (sessionId: string) => void
  onTogglePinnedSession: (sessionId: string) => void
  pinnedSessionIds: readonly string[]
  sortMode: ExplorerSortMode
  runs: Record<string, AgentRunRecord>
  selectedSessionId: string | null
  sessionGroupOrder: string[]
  sessionGroups: Record<string, AgentSessionGroupRecord>
  sessionOrder: string[]
  sessions: Record<string, AgentSessionRecord>
  /** Session-sync failure — shown loudly under the header, never dropped. */
  syncError?: string | null
}) {
  const { locale, t } = useLocale()
  const [searchQuery, setSearchQuery] = useState('')
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<ReadonlySet<string>>(() => new Set())
  const [editingGroupId, setEditingGroupId] = useState<string | null>(null)
  const [groupTitleDraft, setGroupTitleDraft] = useState('')
  const reduceMotion = useReducedMotion()
  const [titleDraft, setTitleDraft] = useState('')
  const listRef = useRef<HTMLDivElement | null>(null)
  const [draggedSessionId, setDraggedSessionId] = useState<string | null>(null)
  const [draggedGroupId, setDraggedGroupId] = useState<string | null>(null)
  const [sessionDropTarget, setSessionDropTarget] = useState<AgentSessionDropTarget | null>(null)
  const [groupDropTargetIndex, setGroupDropTargetIndex] = useState<number | null>(null)
  const suppressSessionSelectClickRef = useRef(false)
  const suppressGroupToggleClickRef = useRef(false)

  const orderedSessions = useMemo(
    () =>
      sessionOrder
        .map((id) => sessions[id])
        .filter((session): session is AgentSessionRecord => Boolean(session)),
    [sessionOrder, sessions],
  )
  const query = searchQuery.trim().toLowerCase()
  // Sorting: automatic modes order sessions by their latest turn
  // (the rail's own time label) or by name; manual keeps insertion order.
  const sortedSessions = sortExplorerItems(
    orderedSessions,
    sortMode,
    (session) => agentSessionHistoryTimeIso(session, runs),
    (session) => session.title,
  )
  const visibleSessions = query
    ? sortedSessions.filter((session) =>
      session.title.toLowerCase().includes(query))
    : sortedSessions
  const pinned = orderPinnedExplorerItems(
    visibleSessions.filter((session) => pinnedSessionIds.includes(session.id)),
    pinnedSessionIds,
    sortMode,
    (session) => session.id,
  )
  const grouped = new Map<string | null, AgentSessionRecord[]>()
  for (const session of visibleSessions) {
    if (pinnedSessionIds.includes(session.id)) continue
    const key = session.groupId && sessionGroups[session.groupId]
      ? session.groupId
      : null
    const bucket = grouped.get(key) ?? []
    bucket.push(session)
    grouped.set(key, bucket)
  }
  const orderedGroups = sortExplorerFolders(
    sessionGroupOrder
      .map((groupId) => sessionGroups[groupId])
      .filter((group): group is AgentSessionGroupRecord => Boolean(group)),
    sortMode,
    (group) => group.title,
  )
  const ungroupedSessions = grouped.get(null) ?? []
  const dropIndicatorGroupIds = draggedGroupId
    ? orderedGroups.filter((group) => group.id !== draggedGroupId).map((group) => group.id)
    : []

  // The drag handlers are document-level listeners frozen at pointer-
  // down; the ref always points at the LATEST render's adopt closure so
  // the adopted order matches the DOM the drop index was read from.
  const adoptVisibleOrderIfAutomatic = () => {
    if (sortMode === 'manual') return
    onAdoptVisibleOrder(
      [
        ...pinned.map((session) => session.id),
        ...orderedGroups.flatMap((group) =>
          (grouped.get(group.id) ?? []).map((session) => session.id)),
        ...ungroupedSessions.map((session) => session.id),
      ],
      orderedGroups.map((group) => group.id),
    )
  }
  const adoptVisibleOrderRef = useRef(adoptVisibleOrderIfAutomatic)
  adoptVisibleOrderRef.current = adoptVisibleOrderIfAutomatic

  function readSessionDropTarget(
    clientY: number,
    excludedSessionId = draggedSessionId,
  ): AgentSessionDropTarget | null {
    const container = listRef.current
    if (!container) return null
    const sectionElements = Array.from(
      container.querySelectorAll<HTMLElement>('[data-agent-session-section]'),
    ).filter((sectionElement) => sectionElement.getBoundingClientRect().height > 0)
    if (sectionElements.length === 0) return null

    let pickedSection: HTMLElement | null = null
    for (const sectionElement of sectionElements) {
      const rect = sectionElement.getBoundingClientRect()
      if (clientY >= rect.top - 8 && clientY <= rect.bottom + 8) {
        pickedSection = sectionElement
        break
      }
    }
    if (!pickedSection) {
      let bestDistance = Number.POSITIVE_INFINITY
      for (const sectionElement of sectionElements) {
        const rect = sectionElement.getBoundingClientRect()
        const distance = Math.min(Math.abs(clientY - rect.top), Math.abs(clientY - rect.bottom))
        if (distance < bestDistance) {
          bestDistance = distance
          pickedSection = sectionElement
        }
      }
    }
    if (!pickedSection) return null

    const sectionGroupKey = pickedSection.dataset.agentSessionGroupId ?? UNGROUPED_AGENT_SECTION_ID
    const groupId = sectionGroupKey === UNGROUPED_AGENT_SECTION_ID ? null : sectionGroupKey
    const rowElements = Array.from(
      pickedSection.querySelectorAll<HTMLElement>('[data-agent-session-id]'),
    ).filter((rowElement) => (
      rowElement.dataset.agentSessionId !== excludedSessionId
      && rowElement.getBoundingClientRect().height > 0
    ))
    for (const [index, rowElement] of rowElements.entries()) {
      const rect = rowElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return { groupId, targetIndex: index }
    }
    return { groupId, targetIndex: rowElements.length }
  }

  function readGroupDropTarget(clientY: number, excludedGroupId = draggedGroupId) {
    const container = listRef.current
    if (!container) return null
    const groupElements = Array.from(
      container.querySelectorAll<HTMLElement>('[data-agent-session-draggable-group-id]'),
    ).filter((groupElement) => (
      groupElement.dataset.agentSessionDraggableGroupId !== excludedGroupId
      && groupElement.getBoundingClientRect().height > 0
    ))
    for (const [index, groupElement] of groupElements.entries()) {
      const rect = groupElement.getBoundingClientRect()
      if (clientY < rect.top + rect.height / 2) return index
    }
    return groupElements.length
  }

  function beginSessionDrag(event: React.PointerEvent<HTMLDivElement>, sessionId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        didStartDrag = true
        suppressSessionSelectClickRef.current = true
        setDraggedSessionId(sessionId)
      }
      moveEvent.preventDefault()
      setSessionDropTarget(readSessionDropTarget(moveEvent.clientY, sessionId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const target = didStartDrag ? readSessionDropTarget(upEvent.clientY, sessionId) : null
      cleanupPointerDrag()
      if (!target) return
      adoptVisibleOrderRef.current()
      onMoveSessionToGroup(sessionId, target.groupId, target.targetIndex)
    }

    function cancelPointerDrag() {
      cleanupPointerDrag()
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cancelPointerDrag)
      setDraggedSessionId(null)
      setSessionDropTarget(null)
      window.setTimeout(() => {
        suppressSessionSelectClickRef.current = false
      }, 0)
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cancelPointerDrag)
  }

  function beginGroupDrag(event: React.PointerEvent<HTMLDivElement>, groupId: string) {
    if (event.button !== 0 || isExplorerActionTarget(event.target)) return
    const startX = event.clientX
    const startY = event.clientY
    let didStartDrag = false

    function handlePointerMove(moveEvent: PointerEvent) {
      if (!didStartDrag) {
        if (!isPastExplorerDragThreshold(startX, startY, moveEvent)) return
        didStartDrag = true
        suppressGroupToggleClickRef.current = true
        setDraggedGroupId(groupId)
      }
      moveEvent.preventDefault()
      setGroupDropTargetIndex(readGroupDropTarget(moveEvent.clientY, groupId))
    }

    function finishPointerDrag(upEvent: PointerEvent) {
      const nextDropTarget = didStartDrag ? readGroupDropTarget(upEvent.clientY, groupId) : null
      cleanupPointerDrag()
      if (nextDropTarget === null) return
      adoptVisibleOrderRef.current()
      onMoveSessionGroup(groupId, nextDropTarget)
    }

    function cancelPointerDrag() {
      cleanupPointerDrag()
    }

    function cleanupPointerDrag() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerDrag)
      document.removeEventListener('pointercancel', cancelPointerDrag)
      setDraggedGroupId(null)
      setGroupDropTargetIndex(null)
      window.setTimeout(() => {
        suppressGroupToggleClickRef.current = false
      }, 0)
    }

    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerDrag)
    document.addEventListener('pointercancel', cancelPointerDrag)
  }

  const selectSession = (sessionId: string) => {
    if (suppressSessionSelectClickRef.current) {
      suppressSessionSelectClickRef.current = false
      return
    }
    onSelectSession(sessionId)
  }

  const toggleGroupCollapse = (groupId: string) => {
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

  const commitGroupEdit = () => {
    if (editingGroupId && groupTitleDraft.trim()) {
      onRenameSessionGroup(editingGroupId, groupTitleDraft.trim())
    }
    setEditingGroupId(null)
  }

  const commitEdit = () => {
    const editingSession = editingSessionId
      ? sessions[editingSessionId]
      : undefined
    if (
      editingSessionId
      && editingSession?.persistable !== false
      && titleDraft.trim()
    ) {
      onRenameSession(editingSessionId, titleDraft.trim())
    }
    setEditingSessionId(null)
  }

  const renderSession = (
    session: AgentSessionRecord,
    nested: boolean,
    sectionGroupId: string | null | undefined,
    index: number,
    sectionLength: number,
  ) => {
    const latestRun = session.runIds
      .map((runId) => runs[runId])
      .filter(Boolean)
      .at(-1)
    const gate = latestRun !== undefined && isGateAgentRun(latestRun.status)
    // Working = active minus gates: a children-wait keeps the live dot
    // (the children ARE working), a human gate shows the warning dot.
    const working =
      latestRun !== undefined
      && isActiveAgentRun(latestRun.status)
      && !gate
    const isPinned = pinnedSessionIds.includes(session.id)
    const editing = editingSessionId === session.id
    const mutable = session.persistable !== false
    const deleting = session.deletion?.status === 'deleting'
    const deleteFailed = session.deletion?.status === 'delete_failed'
    // Drags start only on mutable, quiet rows outside a search (the
    // filtered view must never become the adopted order — chat parity:
    // its flat search list is drag-inert too).
    const draggable = mutable && !session.deletion && !query
    const inDropSection = sectionGroupId !== undefined
      && sessionDropTarget !== null
      && sessionDropTarget.groupId === sectionGroupId
    const showBeforeIndicator = inDropSection && sessionDropTarget.targetIndex === index
    const showAfterIndicator = inDropSection
      && index === sectionLength - 1
      && sessionDropTarget.targetIndex === sectionLength
    const timeLabel = displayRelativeAge(
      agentSessionHistoryTimeIso(session, runs),
      locale,
    )
    return (
      <div
        className="relative"
        data-agent-session-id={session.id}
        key={session.id}
      >
        {showBeforeIndicator && (
          <span className="pointer-events-none absolute -top-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
        )}
        <ExplorerHistoryRow
          actions={session.deletion ? (deleteFailed ? [{
            icon: <RotateCcw className="icon-sm" />,
            label: t.agent.sessions.retryDelete,
            onSelect: () => onRetrySessionDeletion(session.id),
          }] : []) : [
            {
              icon: isPinned ? <PinOff className="icon-sm" /> : <Pin className="icon-sm" />,
              label: isPinned ? t.agent.sessions.unpin : t.agent.sessions.pin,
              onSelect: () => onTogglePinnedSession(session.id),
            },
            ...(mutable ? [{
              destructive: true,
              icon: <Trash2 className="icon-sm" />,
              label: t.agent.sessions.delete,
              onSelect: () => onDeleteSession(session.id),
            }] : []),
          ]}
          active={selectedSessionId === session.id}
          disabled={Boolean(session.deletion)}
          dragging={draggedSessionId === session.id}
          indicator={
            deleting ? (
              <ExplorerRunningIndicator label={t.agent.sessions.deleting} />
            ) : gate ? (
              <span
                aria-hidden="true"
                className="size-1.5 shrink-0 rounded-full bg-warning inqtrix-running-dot"
              />
            ) : working ? (
              <ExplorerRunningIndicator label={t.status.running} />
            ) : undefined
          }
          nested={nested}
          onPointerDown={draggable
            ? (event) => beginSessionDrag(event, session.id)
            : undefined}
          onSelect={() => selectSession(session.id)}
          onStartRename={mutable && !session.deletion ? () => {
            setEditingSessionId(session.id)
            setTitleDraft(session.titleConflict ?? session.title)
          } : undefined}
          renameEditor={mutable && editing ? (
            <ExplorerHistoryTitleInput
              autoFocus
              label={t.agent.sessions.rename}
              onCancel={() => setEditingSessionId(null)}
              onChange={setTitleDraft}
              onCommit={commitEdit}
              value={titleDraft}
            />
          ) : undefined}
          renameLabel={mutable && !session.deletion ? sessionTitleHint(session, t.sessionTitles, t.agent.sessions.rename) : undefined}
          timeLabel={deleting
            ? t.agent.sessions.deleting
            : deleteFailed
              ? t.agent.sessions.deleteFailed
              : timeLabel}
          title={session.title}
        />
        {showAfterIndicator && (
          <span className="pointer-events-none absolute -bottom-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
        )}
      </div>
    )
  }

  return (
    <aside className="flex h-full min-h-0 w-full flex-col border-r border-border bg-surface/60">
      <div className="flex inqtrix-panel-header items-center justify-between gap-2 border-b border-border px-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="truncate t-section text-foreground">
            {t.agent.sessions.title}
          </h2>
        </div>
        <div className="flex items-center gap-1.5">
          <ExplorerSortMenu mode={sortMode} onChangeMode={onChangeSortMode} />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.agent.sessions.createGroup}
                className="size-7 shrink-0"
                onClick={onCreateSessionGroup}
                size="icon"
                type="button"
                variant="ghost"
              >
                <FolderPlus className="size-4 text-foreground/85" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t.agent.sessions.createGroup}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-busy={creatingSession || undefined}
                aria-label={creatingSession ? t.agent.sessions.creating : t.agent.sessions.create}
                className="size-7 shrink-0"
                disabled={creatingSession}
                onClick={onCreateSession}
                size="icon"
                type="button"
                variant="ghost"
              >
                {creatingSession
                  ? <LoaderCircle className="size-4 animate-spin" />
                  : <SquarePen className="size-4" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {creatingSession ? t.agent.sessions.creating : t.agent.sessions.create}
            </TooltipContent>
          </Tooltip>
        </div>
      </div>
      {syncError && (
        <p className="border-b border-border px-3 py-1.5 t-meta-sm text-destructive">
          {syncError}
        </p>
      )}
      <ExplorerSearchField
        clearLabel={t.knowledge.searchClear}
        label={t.agent.sessions.searchPlaceholder}
        onChange={setSearchQuery}
        onClear={() => setSearchQuery('')}
        placeholder={t.agent.sessions.searchPlaceholder}
        value={searchQuery}
      />
      <ScrollArea className="min-h-0 flex-1">
        <div className="inqtrix-explorer-list space-y-1 p-2" ref={listRef}>
          {visibleSessions.length === 0 && (
            <p className="px-1.5 py-2 t-meta text-muted-foreground">
              {t.agent.sessions.empty}
            </p>
          )}
          {pinned.length > 0 && (
            <div className="space-y-0.5">
              <ExplorerSectionLabel>{t.agent.canvas.pinned}</ExplorerSectionLabel>
              {pinned.map((session) =>
                renderSession(session, false, undefined, 0, 0))}
            </div>
          )}
          {orderedGroups.map((group) => {
            const members = grouped.get(group.id) ?? []
            // An empty folder stays VISIBLE outside a search (the "new
            // folder" button creates one — hiding it made the feature
            // look broken); a search only shows folders with matches.
            if (members.length === 0 && query) return null
            // A live search overrides collapse — a collapsed folder must
            // never hide its matches behind a bare count (review find).
            const isCollapsed = !query && collapsedGroupIds.has(group.id)
            const GroupIcon = isCollapsed ? Folder : FolderOpen
            const showDropFrame = sessionDropTarget?.groupId === group.id
            const shouldRenderContent = !isCollapsed || showDropFrame
            // Indicator math runs in the reader's coordinates (dragged
            // group EXCLUDED) - comparing against the raw index drew the
            // line one slot too high past the dragged folder (review find).
            const filteredGroupIndex = dropIndicatorGroupIds.indexOf(group.id)
            const showGroupBeforeIndicator = draggedGroupId !== null
              && filteredGroupIndex !== -1
              && groupDropTargetIndex === filteredGroupIndex
            const showGroupAfterIndicator = draggedGroupId !== null
              && filteredGroupIndex !== -1
              && filteredGroupIndex === dropIndicatorGroupIds.length - 1
              && groupDropTargetIndex === dropIndicatorGroupIds.length
            return (
              <div
                className={cn(
                  'relative space-y-0.5 rounded-md transition-colors',
                  showDropFrame && 'bg-brand-subtle/45',
                  draggedGroupId === group.id
                    && 'scale-[0.995] opacity-80 shadow-[0_8px_20px_var(--shadow-soft)] ring-1 ring-ring/40',
                )}
                data-agent-session-draggable-group-id={group.id}
                data-agent-session-group-id={group.id}
                data-agent-session-section
                key={group.id}
              >
                {showGroupBeforeIndicator && (
                  <span className="pointer-events-none absolute -top-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
                )}
                {showGroupAfterIndicator && (
                  <span className="pointer-events-none absolute -bottom-1 left-1 right-1 h-0.5 rounded-full bg-brand shadow-[0_0_0_1px_var(--background)]" />
                )}
                <ExplorerFolderRow
                  actions={
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
                        <DropdownMenuItem
                          onSelect={() => {
                            setEditingGroupId(group.id)
                            setGroupTitleDraft(group.title)
                          }}
                        >
                          <PenLine className="icon-sm" />
                          {t.agent.sessions.renameFolder}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          className="text-destructive focus:text-destructive"
                          onSelect={() => onDeleteSessionGroup(group.id)}
                        >
                          <Trash2 className="icon-sm" />
                          {t.agent.sessions.deleteFolder}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  }
                  onPointerDown={query
                    ? undefined
                    : (event) => beginGroupDrag(event, group.id)}
                >
                  {editingGroupId === group.id ? (
                    <span className="flex min-h-8 min-w-0 items-center gap-1.5" data-explorer-action>
                      <FolderOpen className="icon-sm shrink-0 text-muted-foreground" />
                      <ExplorerHistoryTitleInput
                        autoFocus
                        label={t.agent.sessions.renameFolder}
                        onCancel={() => setEditingGroupId(null)}
                        onChange={setGroupTitleDraft}
                        onCommit={commitGroupEdit}
                        value={groupTitleDraft}
                      />
                    </span>
                  ) : (
                    <ExplorerFolderToggle
                      count={members.length}
                      expanded={!isCollapsed}
                      icon={<GroupIcon className="icon-sm shrink-0" />}
                      label={`${isCollapsed ? t.agent.sessions.expandGroup : t.agent.sessions.collapseGroup}: ${group.title}`}
                      onToggle={() => toggleGroupCollapse(group.id)}
                      title={group.title}
                    />
                  )}
                </ExplorerFolderRow>
                <AnimatePresence initial={false}>
                  {shouldRenderContent && (
                    <motion.div
                      animate={{ height: 'auto', opacity: 1 }}
                      className="overflow-hidden"
                      exit={reduceMotion ? undefined : { height: 0, opacity: 0 }}
                      initial={reduceMotion ? false : { height: 0, opacity: 0 }}
                      transition={{ duration: 0.16, ease: [0.2, 0, 0, 1] }}
                    >
                      <div className="space-y-0.5">
                        {(isCollapsed || members.length === 0) && showDropFrame ? (
                          <div className="rounded-md border border-dashed border-brand/30 px-2 py-1.5 text-center t-meta-sm font-semibold text-brand">
                            {t.agent.sessions.dropIntoFolder}
                          </div>
                        ) : members.length === 0 ? (
                          <p className="px-1.5 py-1 t-meta text-muted-foreground">
                            {t.agent.sessions.emptyGroup}
                          </p>
                        ) : (
                          members.map((session, index) =>
                            renderSession(session, true, group.id, index, members.length))
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )
          })}
          <div
            className={cn(
              'space-y-0.5 rounded-md transition-colors',
              sessionDropTarget?.groupId === null && 'bg-brand-subtle/45',
            )}
            data-agent-session-section
          >
            {/* The header lives INSIDE the section (chat parity) and
                renders whenever folders exist: it keeps an EMPTY
                ungrouped zone measurable as a drop target - without it a
                fully-foldered rail could never un-group a session again
                (review find). */}
            {orderedGroups.length > 0 && (
              <div className="flex min-h-7 items-center gap-2 rounded-md px-1.5 t-caption text-foreground/65">
                <Folder className="icon-sm shrink-0" />
                <span className="truncate">{t.agent.sessions.ungrouped}</span>
              </div>
            )}
            {ungroupedSessions.length === 0 && sessionDropTarget?.groupId === null && (
              <div className="rounded-md border border-dashed border-brand/30 px-2 py-1.5 text-center t-meta-sm font-semibold text-brand">
                {t.agent.sessions.dropIntoFolder}
              </div>
            )}
            {ungroupedSessions.map((session, index) =>
              renderSession(session, false, null, index, ungroupedSessions.length))}
          </div>
        </div>
      </ScrollArea>
    </aside>
  )
}
