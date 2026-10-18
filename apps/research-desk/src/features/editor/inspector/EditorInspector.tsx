import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { SuggestionKind } from '@inqtrix/editor-schema'

import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  ListChecks,
  LoaderCircle,
  LockKeyhole,
  MessageSquarePlus,
  MessageSquareText,
  PanelRightClose,
  PenLine,
  RefreshCw,
  Users,
  X,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import type { CollaborationReviewDisplay } from '../tiptap'
import {
  adjacentChangeId,
  compactChangeText,
  filterInspectorChanges,
  filterInspectorHistory,
  type EditorCollaborationStatusKind,
  type EditorCollaborationStatusModel,
  type EditorChangesView,
  type EditorInspectorTab,
  type EditorWriteMode,
  type InspectorChange,
  type InspectorHistoryEntry,
  type InspectorHistoryFilters,
  type InspectorHistoryKind,
  type InspectorOpenFilters,
  type InspectorParticipant,
  COLLABORATION_STARTUP_GRACE_MS,
  CalmCollaborationStatus,
  startupPresentation,
} from './model'

type InspectorDecision = 'accept' | 'reject'

/** The status kind a person sees, see CalmCollaborationStatus. */
function useCalmCollaborationStatusKind(
  kind: EditorCollaborationStatusKind,
  unconfirmedSince: number | null,
): EditorCollaborationStatusKind {
  const [calm] = useState(() => new CalmCollaborationStatus(kind, unconfirmedSince))
  const [visibleKind, setVisibleKind] = useState(calm.kind)
  useEffect(() => calm.listen(setVisibleKind), [calm])
  useEffect(() => {
    calm.update(kind, unconfirmedSince)
  }, [calm, kind, unconfirmedSince])
  return visibleKind
}

export type EditorInspectorProps = {
  activeTab: EditorInspectorTab
  assistant: ReactNode
  canDecide: boolean
  changes: readonly InspectorChange[]
  changesError: string | null
  changesView: EditorChangesView
  collaborationActive: boolean
  collaborationStatus: EditorCollaborationStatusModel
  commentCount: number
  comments: ReactNode
  commentUnreadCount: number
  decisionError: string | null
  display: CollaborationReviewDisplay
  history: readonly InspectorHistoryEntry[]
  historyError: string | null
  historyFilters: InspectorHistoryFilters
  historyLoading: boolean
  isDecisionPending: boolean
  onActiveTabChange: (tab: EditorInspectorTab) => void
  onChangesViewChange: (view: EditorChangesView) => void
  onClose: () => void
  onDecision: (decision: InspectorDecision, patchIds: string[]) => void
  onDisplayChange: (display: CollaborationReviewDisplay) => void
  onHistoryFiltersChange: (filters: InspectorHistoryFilters) => void
  onLogin?: () => void
  onOpenFiltersChange: (filters: InspectorOpenFilters) => void
  onReconnect?: () => Promise<void>
  onReload?: () => void
  onSelectedChangeIdChange: (changeId: string | null) => void
  openFilters: InspectorOpenFilters
  selectedChangeId: string | null
}

const copy = {
  de: {
    accept: 'Annehmen',
    acceptAll: 'Alle annehmen',
    all: 'Alle',
    allAuthors: 'Alle Personen',
    allTypes: 'Alle Arten',
    assistant: 'KI',
    author: 'Person',
    batchAcceptBody: 'Alle gefilterten offenen Änderungen annehmen?',
    batchAcceptTitle: 'Änderungen annehmen',
    batchRejectBody: 'Alle gefilterten offenen Änderungen ablehnen?',
    batchRejectTitle: 'Änderungen ablehnen',
    cancel: 'Abbrechen',
    changes: 'Änderungen',
    comments: 'Kommentare',
    commentMode: 'Kommentieren',
    close: 'Inspector schließen',
    status: {
      access_revoked: 'Zugriff entzogen',
      error: 'Synchronisierungsfehler',
      inactive: 'Lokal',
      read_only: 'Schreibgeschützt',
      reconnecting: 'Verbindung wird wiederhergestellt',
      saved: 'Gespeichert',
      saving: 'Wird gespeichert',
      saving_delayed: 'Speichern verzögert',
      syncing: 'Wird synchronisiert',
      origin_rejected: 'Serveradresse stimmt nicht',
      reload_required: 'Neu laden erforderlich',
    },
    projection: 'Bestätigter Stand',
    diagnostics: 'Verbindungsdiagnose',
    reconnectAttempt: 'Verbindungsversuch',
    nextReconnect: 'Nächster Versuch',
    retryNow: 'Jetzt erneut verbinden',
    signInAgain: 'Erneut anmelden',
    reloadPage: 'Seite neu laden',
    unconfirmed: 'Nicht bestätigte Änderungen',
    none: 'Keine',
    participants: 'Teilnehmende',
    deletion: 'Löschung',
    display: 'Anzeige',
    edit: 'Bearbeiten',
    final: 'Final',
    format: 'Formatierung',
    history: 'Verlauf',
    insertion: 'Einfügung',
    loading: 'Änderungen werden geladen',
    modification: 'Änderung',
    replacement: 'Ersetzung',
    next: 'Nächste Änderung',
    noChanges: 'Keine offenen Änderungen.',
    noHistory: 'Kein Verlauf für diese Filter.',
    open: 'Offen',
    original: 'Original',
    previous: 'Vorherige Änderung',
    proposed: 'Vorgeschlagen',
    reject: 'Ablehnen',
    rejectAll: 'Alle ablehnen',
    simple: 'Einfach',
    sourceReadOnly: 'Quelltext ist in der Zusammenarbeit schreibgeschützt.',
    suggest: 'Vorschlagen',
    suggestLocked: 'Diese Freigabe erlaubt nur Vorschläge.',
    structure: 'Struktur',
    type: 'Art',
    technicalDetails: 'Technische Details',
    moreEdits: 'weitere Änderungen',
    removedSuggestionBy: '{name} hat einen Vorschlag von {author} entfernt',
    withdrewSuggestion: '{name} hat einen Vorschlag zurückgezogen',
    removedSuggestionsOne: '{name} hat einen Vorschlag entfernt',
    removedSuggestions: '{name} hat {count} Vorschläge entfernt',
    suggestionRemovedBy: 'Vorschlag von {author} entfernt',
    suggestionWithdrawnBy: 'Vorschlag von {author} zurückgezogen',
    moreRemovedSuggestionsOne: '+{count} weiterer entfernter Vorschlag',
    moreRemovedSuggestions: '+{count} weitere entfernte Vorschläge',
    viewLocked: 'Diese Freigabe ist schreibgeschützt.',
  },
  en: {
    accept: 'Accept',
    acceptAll: 'Accept all',
    all: 'All',
    allAuthors: 'All people',
    allTypes: 'All types',
    assistant: 'AI',
    author: 'Person',
    batchAcceptBody: 'Accept all filtered open changes?',
    batchAcceptTitle: 'Accept changes',
    batchRejectBody: 'Reject all filtered open changes?',
    batchRejectTitle: 'Reject changes',
    cancel: 'Cancel',
    changes: 'Changes',
    comments: 'Comments',
    commentMode: 'Comment',
    close: 'Close inspector',
    status: {
      access_revoked: 'Access revoked',
      error: 'Sync error',
      inactive: 'Local',
      read_only: 'Read-only',
      reconnecting: 'Reconnecting',
      saved: 'Saved',
      saving: 'Saving',
      saving_delayed: 'Saving delayed',
      syncing: 'Syncing',
      origin_rejected: 'Server address mismatch',
      reload_required: 'Reload required',
    },
    projection: 'Confirmed version',
    diagnostics: 'Connection diagnostics',
    reconnectAttempt: 'Connection attempt',
    nextReconnect: 'Next attempt',
    retryNow: 'Reconnect now',
    signInAgain: 'Sign in again',
    reloadPage: 'Reload page',
    unconfirmed: 'Unconfirmed changes',
    none: 'None',
    participants: 'Participants',
    deletion: 'Deletion',
    display: 'Display',
    edit: 'Edit',
    final: 'Final',
    format: 'Formatting',
    history: 'History',
    insertion: 'Insertion',
    loading: 'Loading changes',
    modification: 'Change',
    replacement: 'Replacement',
    next: 'Next change',
    noChanges: 'No open changes.',
    noHistory: 'No history for these filters.',
    open: 'Open',
    original: 'Original',
    previous: 'Previous change',
    proposed: 'Proposed',
    reject: 'Reject',
    rejectAll: 'Reject all',
    simple: 'Simple',
    sourceReadOnly: 'Source is read-only during collaboration.',
    suggest: 'Suggest',
    suggestLocked: 'This share permits suggestions only.',
    structure: 'Structure',
    type: 'Type',
    technicalDetails: 'Technical details',
    moreEdits: 'more edits',
    removedSuggestionBy: '{name} removed a suggestion by {author}',
    withdrewSuggestion: '{name} withdrew a suggestion',
    removedSuggestionsOne: '{name} removed a suggestion',
    removedSuggestions: '{name} removed {count} suggestions',
    suggestionRemovedBy: 'Suggestion by {author} removed',
    suggestionWithdrawnBy: 'Suggestion by {author} withdrawn',
    moreRemovedSuggestionsOne: '+{count} more removed suggestion',
    moreRemovedSuggestions: '+{count} more removed suggestions',
    viewLocked: 'This share is read-only.',
  },
} as const

function InspectorTabTrigger({
  count,
  countClassName,
  label,
  value,
}: {
  count?: number
  countClassName?: string
  label: string
  value: EditorInspectorTab
}) {
  const accessibleLabel = count && count > 0 ? `${label} ${count}` : label

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="block h-7 min-w-0">
          <TabsTrigger
            aria-label={accessibleLabel}
            className="h-7 w-full min-w-0 gap-1 overflow-hidden px-2 text-xs"
            value={value}
          >
            <span className="min-w-0 truncate">{label}</span>
            {count && count > 0 ? (
              <span className={cn('t-hint shrink-0 tabular-nums', countClassName)}>
                {count}
              </span>
            ) : null}
          </TabsTrigger>
        </span>
      </TooltipTrigger>
      <TooltipContent>{accessibleLabel}</TooltipContent>
    </Tooltip>
  )
}

export function EditorInspector({
  activeTab,
  assistant,
  canDecide,
  changes,
  changesError,
  changesView,
  collaborationActive,
  collaborationStatus,
  commentCount,
  comments,
  commentUnreadCount,
  decisionError,
  display,
  history,
  historyError,
  historyFilters,
  historyLoading,
  isDecisionPending,
  onActiveTabChange,
  onChangesViewChange,
  onClose,
  onDecision,
  onDisplayChange,
  onHistoryFiltersChange,
  onLogin,
  onOpenFiltersChange,
  onReconnect,
  onReload,
  onSelectedChangeIdChange,
  openFilters,
  selectedChangeId,
}: EditorInspectorProps) {
  const { locale } = useLocale()
  const labels = copy[locale]
  const filteredChanges = useMemo(
    () => filterInspectorChanges(changes, openFilters),
    [changes, openFilters],
  )
  const filteredHistory = useMemo(
    () => filterInspectorHistory(history, historyFilters),
    [history, historyFilters],
  )
  const openAuthors = useMemo(
    () => uniqueParticipants(changes.map((change) => change.author)),
    [changes],
  )
  const historyActors = useMemo(
    () => uniqueParticipants(history.map((entry) => entry.actor)),
    [history],
  )
  const [expandedChangeId, setExpandedChangeId] = useState<string | null>(null)
  const [expandedHistoryId, setExpandedHistoryId] = useState<string | null>(null)
  const [batchDecision, setBatchDecision] = useState<InspectorDecision | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!selectedChangeId || activeTab !== 'changes' || changesView !== 'open') return
    listRef.current
      ?.querySelector<HTMLElement>(`[data-inspector-change-id="${CSS.escape(selectedChangeId)}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeTab, changesView, selectedChangeId])

  useEffect(() => {
    if (canDecide || batchDecision === null) return
    setBatchDecision(null)
  }, [batchDecision, canDecide])

  const navigate = (direction: -1 | 1) => {
    const changeId = adjacentChangeId(filteredChanges, selectedChangeId, direction)
    onSelectedChangeIdChange(changeId)
    if (changeId) setExpandedChangeId(changeId)
  }

  const confirmBatch = () => {
    if (!batchDecision || !canDecide || filteredChanges.length === 0) return
    onDecision(batchDecision, filteredChanges.map((change) => change.id))
    setBatchDecision(null)
  }

  return (
    <Tabs
      className="inqtrix-contained-panel flex h-full w-full min-w-0 flex-col bg-background"
      onValueChange={(value) => onActiveTabChange(value as EditorInspectorTab)}
      value={activeTab}
    >
      <div className="flex inqtrix-panel-header items-center gap-2 border-b border-border px-3">
        <TabsList className={cn(
          'grid h-8 min-w-0 flex-1 rounded-md p-0.5',
          collaborationActive ? 'grid-cols-3' : 'grid-cols-1',
        )}>
          {collaborationActive ? (
            <InspectorTabTrigger
              count={commentCount > 0 ? (commentUnreadCount > 0 ? commentUnreadCount : commentCount) : 0}
              countClassName={commentUnreadCount > 0 ? 'text-brand' : 'text-muted-foreground'}
              label={labels.comments}
              value="comments"
            />
          ) : null}
          {collaborationActive ? (
            <InspectorTabTrigger
              count={changes.length}
              countClassName="text-muted-foreground"
              label={labels.changes}
              value="changes"
            />
          ) : null}
          <InspectorTabTrigger label={labels.assistant} value="assistant" />
        </TabsList>
        <IconButton label={labels.close} onClick={onClose}>
          <PanelRightClose className="icon-sm" />
        </IconButton>
      </div>

      <EditorCollaborationStatus
        collaborationExpected={collaborationActive}
        model={collaborationStatus}
        onLogin={onLogin}
        onReconnect={onReconnect}
        onReload={onReload}
        variant="inspector"
      />

      <TabsContent
        className="m-0 min-h-0 flex-1 data-[state=inactive]:hidden"
        forceMount
        value="comments"
      >
        {comments}
      </TabsContent>
      <TabsContent
        className="m-0 min-h-0 flex-1 data-[state=inactive]:hidden"
        forceMount
        value="assistant"
      >
        {assistant}
      </TabsContent>
      <TabsContent
        className="m-0 flex min-h-0 flex-1 flex-col data-[state=inactive]:hidden"
        forceMount
        value="changes"
      >
        <div className="flex flex-col gap-2 border-b border-border px-3 py-2">
          <div className="grid grid-cols-2 gap-0.5 rounded-md bg-muted/60 p-0.5">
            <SegmentButton
              active={changesView === 'open'}
              count={changes.length}
              label={labels.open}
              onClick={() => onChangesViewChange('open')}
            />
            <SegmentButton
              active={changesView === 'history'}
              label={labels.history}
              onClick={() => onChangesViewChange('history')}
            />
          </div>

          {changesView === 'open' ? (
            <>
              <div aria-label={labels.display} className="grid grid-cols-4 gap-0.5 rounded-md bg-muted/60 p-0.5">
                {(['simple', 'all', 'final', 'original'] as const).map((mode) => (
                  <SegmentButton
                    active={display === mode}
                    key={mode}
                    label={labels[mode]}
                    onClick={() => onDisplayChange(mode)}
                  />
                ))}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <InspectorSelect
                  label={labels.author}
                  onValueChange={(value) => onOpenFiltersChange({
                    ...openFilters,
                    authorId: value === 'all' ? null : value,
                  })}
                  options={openAuthors.map((participant) => ({
                    label: participant.name,
                    value: participant.id,
                  }))}
                  placeholder={labels.allAuthors}
                  value={openFilters.authorId ?? 'all'}
                />
                <InspectorSelect
                  label={labels.type}
                  onValueChange={(value) => onOpenFiltersChange({
                    ...openFilters,
                    type: value === 'all' ? null : value as SuggestionKind,
                  })}
                  options={suggestionTypeOptions(labels)}
                  placeholder={labels.allTypes}
                  value={openFilters.type ?? 'all'}
                />
              </div>
              <div className="flex h-7 items-center justify-between gap-2">
                <span className="t-meta-sm truncate text-muted-foreground">
                  {filteredChanges.length} {labels.open.toLocaleLowerCase(locale)}
                </span>
                <div className="flex shrink-0 items-center gap-0.5">
                  <IconButton
                    disabled={filteredChanges.length === 0}
                    label={labels.previous}
                    onClick={() => navigate(-1)}
                  >
                    <ChevronLeft className="icon-sm" />
                  </IconButton>
                  <IconButton
                    disabled={filteredChanges.length === 0}
                    label={labels.next}
                    onClick={() => navigate(1)}
                  >
                    <ChevronRight className="icon-sm" />
                  </IconButton>
                </div>
              </div>
            </>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <InspectorSelect
                label={labels.author}
                onValueChange={(value) => onHistoryFiltersChange({
                  ...historyFilters,
                  actorId: value === 'all' ? null : value,
                })}
                options={historyActors.map((participant) => ({
                  label: participant.name,
                  value: participant.id,
                }))}
                placeholder={labels.allAuthors}
                value={historyFilters.actorId ?? 'all'}
              />
              <InspectorSelect
                label={labels.type}
                onValueChange={(value) => onHistoryFiltersChange({
                  ...historyFilters,
                  type: value === 'all' ? null : value as InspectorHistoryKind,
                })}
                options={historyTypeOptions(labels)}
                placeholder={labels.allTypes}
                value={historyFilters.type ?? 'all'}
              />
            </div>
          )}
        </div>

        <ScrollArea className="min-h-0 flex-1">
          <div ref={listRef}>
            {changesView === 'open' ? (
              <OpenChanges
                changes={filteredChanges}
                canDecide={canDecide}
                error={changesError ?? decisionError}
                expandedId={expandedChangeId}
                isDecisionPending={isDecisionPending}
                labels={labels}
                locale={locale}
                onDecision={onDecision}
                onExpandedChange={setExpandedChangeId}
                onSelectedChange={onSelectedChangeIdChange}
                selectedId={selectedChangeId}
              />
            ) : (
              <HistoryChanges
                entries={filteredHistory}
                error={historyError}
                expandedId={expandedHistoryId}
                isLoading={historyLoading}
                labels={labels}
                locale={locale}
                onExpandedChange={setExpandedHistoryId}
              />
            )}
          </div>
        </ScrollArea>

        {changesView === 'open' && filteredChanges.length > 0 ? (
          <div className="grid grid-cols-2 gap-2 border-t border-border p-3">
            <Button
              disabled={isDecisionPending || !canDecide}
              onClick={() => setBatchDecision('reject')}
              size="sm"
              type="button"
              variant="outline"
            >
              {canDecide ? <X /> : <LockKeyhole />}
              {labels.rejectAll}
            </Button>
            <Button
              disabled={isDecisionPending || !canDecide}
              onClick={() => setBatchDecision('accept')}
              size="sm"
              type="button"
            >
              {canDecide ? <Check /> : <LockKeyhole />}
              {labels.acceptAll}
            </Button>
          </div>
        ) : null}

        <Dialog
          closeLabel={labels.cancel}
          description={batchDecision === 'accept' ? labels.batchAcceptBody : labels.batchRejectBody}
          dismissable={!isDecisionPending}
          footer={(
            <>
              <Button onClick={() => setBatchDecision(null)} size="sm" type="button" variant="outline">
                {labels.cancel}
              </Button>
              <Button
                disabled={isDecisionPending || !canDecide}
                onClick={confirmBatch}
                size="sm"
                type="button"
                variant={batchDecision === 'reject' ? 'destructive' : 'default'}
              >
                {isDecisionPending ? <LoaderCircle className="animate-spin" /> : null}
                {batchDecision === 'accept' ? labels.acceptAll : labels.rejectAll}
              </Button>
            </>
          )}
          onClose={() => setBatchDecision(null)}
          open={batchDecision !== null}
          title={batchDecision === 'accept' ? labels.batchAcceptTitle : labels.batchRejectTitle}
        >
          <p className="t-meta text-muted-foreground">
            {filteredChanges.length} {labels.changes.toLocaleLowerCase(locale)}
          </p>
        </Dialog>
      </TabsContent>
    </Tabs>
  )
}

export function EditorWriteModeControl({
  access,
  canEdit,
  collaborationActive,
  mode,
  onModeChange,
  sourceReadOnly,
}: {
  access: 'comment' | 'edit' | 'suggest' | 'view' | null
  canEdit: boolean
  collaborationActive: boolean
  mode: EditorWriteMode
  onModeChange: (mode: Exclude<EditorWriteMode, 'view'>) => void
  sourceReadOnly: boolean
}) {
  const { locale } = useLocale()
  const labels = copy[locale]
  if (!collaborationActive) return null
  const editLocked = sourceReadOnly || access !== 'edit' || !canEdit
  const suggestLocked = sourceReadOnly
    || access === 'comment'
    || access === 'view'
    || access === null
    || !canEdit
  const commentLocked = sourceReadOnly
    || access === 'view'
    || access === null
    || !canEdit
  const lockLabel = sourceReadOnly
    ? labels.sourceReadOnly
    : access === 'view' || access === null
      ? labels.viewLocked
      : labels.suggestLocked

  return (
    <div className="flex h-7 shrink-0 items-center rounded-md bg-muted/60 p-0.5" role="group">
      <ModeButton
        active={mode === 'edit'}
        disabled={editLocked}
        icon={<PenLine className="icon-sm" />}
        label={labels.edit}
        lockLabel={editLocked ? lockLabel : null}
        onClick={() => onModeChange('edit')}
      />
      <ModeButton
        active={mode === 'suggest'}
        disabled={suggestLocked}
        icon={<MessageSquareText className="icon-sm" />}
        label={labels.suggest}
        lockLabel={suggestLocked ? lockLabel : null}
        onClick={() => onModeChange('suggest')}
      />
      <ModeButton
        active={mode === 'comment'}
        disabled={commentLocked}
        icon={<MessageSquarePlus className="icon-sm" />}
        label={labels.commentMode}
        lockLabel={commentLocked ? lockLabel : null}
        onClick={() => onModeChange('comment')}
      />
      {/* The read-only badge owns its slot permanently — `invisible` keeps
          the 24px reserved, so leaving/entering view mode never re-lays the
          group out. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              'flex size-6 items-center justify-center text-muted-foreground',
              mode !== 'view' && 'invisible',
            )}
            tabIndex={mode === 'view' ? 0 : -1}
          >
            <LockKeyhole className="icon-xs" />
          </span>
        </TooltipTrigger>
        {mode === 'view' ? <TooltipContent>{lockLabel}</TooltipContent> : null}
      </Tooltip>
    </div>
  )
}

export function EditorCollaborationStatus({
  collaborationExpected,
  model,
  onLogin,
  onReconnect,
  onReload,
  variant,
}: {
  /** Whether this document is a collaboration document at all. A local
   * markdown document is FINAL `inactive` ("Lokal") — the startup grace
   * must never dress it up as syncing. */
  collaborationExpected: boolean
  model: EditorCollaborationStatusModel
  onLogin?: () => void
  onReconnect?: () => Promise<void>
  onReload?: () => void
  variant: 'inspector' | 'topbar'
}) {
  const { locale } = useLocale()
  const labels = copy[locale]
  const [actionPending, setActionPending] = useState(false)
  const calmedKind = useCalmCollaborationStatusKind(model.kind, model.unconfirmedSince)
  // Startup grace: within the first COLLABORATION_STARTUP_GRACE_MS the two
  // expected startup transients present as ONE quiet syncing state with a
  // muted dot — a session still not up after the window shows its real
  // state, and exceptional kinds bypass the calm entirely (model contract).
  const mountedAtRef = useRef(Date.now())
  const [startupGraceElapsed, setStartupGraceElapsed] = useState(false)
  useEffect(() => {
    const remaining = COLLABORATION_STARTUP_GRACE_MS
      - (Date.now() - mountedAtRef.current)
    if (remaining <= 0) {
      setStartupGraceElapsed(true)
      return undefined
    }
    const timer = window.setTimeout(
      () => setStartupGraceElapsed(true),
      remaining,
    )
    return () => window.clearTimeout(timer)
  }, [])
  const presented = startupPresentation(
    calmedKind,
    startupGraceElapsed
      ? COLLABORATION_STARTUP_GRACE_MS
      : Date.now() - mountedAtRef.current,
    collaborationExpected,
  )
  const visibleKind = presented.kind
  const statusLabel = labels.status[visibleKind]
  const projectionLabel = model.projectionConfirmedAt
    ? `${labels.projection}: ${new Date(model.projectionConfirmedAt).toLocaleString(locale)}`
    : null
  const participantLabel = model.participants.length === 0
    ? `0 ${labels.participants}`
    : `${model.participants.length} ${labels.participants}: ${model.participants
      .map((participant) => participant.name)
      .join(', ')}`
  const fullLabel = [
    statusLabel,
    model.notice,
    projectionLabel,
    participantLabel,
  ].filter((part): part is string => Boolean(part)).reduce(
    (label, part) => {
      if (!label) return part
      return `${label}${/[.!?]$/u.test(label) ? ' ' : '. '}${part}`
    },
    '',
  )
  const avatarSize = variant === 'topbar' ? 'size-5' : 'size-6'
  const action = model.recoverability === 'retry' && onReconnect
    ? {
        label: labels.retryNow,
        run: async () => onReconnect(),
      }
    : model.recoverability === 'login' && onLogin
      ? {
          label: labels.signInAgain,
          run: async () => onLogin(),
        }
      : model.recoverability === 'reload' && onReload
        ? {
            label: labels.reloadPage,
            run: async () => onReload(),
          }
        : null
  const runAction = () => {
    if (!action || actionPending) return
    setActionPending(true)
    void action.run().finally(() => setActionPending(false))
  }
  return (
    <div
      aria-label={fullLabel}
      aria-live="polite"
      className={cn(
        'shrink-0',
        variant === 'inspector'
          ? 'border-b border-border px-3 py-1'
          : 'max-w-60',
      )}
      data-editor-status-kind={visibleKind}
      role="status"
      title={fullLabel}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            aria-label={fullLabel}
            className={cn(
              'flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-1.5 text-left outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring',
              variant === 'inspector' ? 'h-7' : 'h-7 bg-muted/60',
            )}
            type="button"
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <span
                aria-hidden
                className={cn(
                  'size-2 shrink-0 rounded-full',
                  presented.calm ? 'bg-muted-foreground/50'
                    : visibleKind === 'saved' ? 'bg-success'
                      : visibleKind === 'saving'
                        || visibleKind === 'saving_delayed'
                        || visibleKind === 'syncing'
                        || visibleKind === 'reconnecting'
                        ? 'bg-warning'
                        : visibleKind === 'read_only' || visibleKind === 'inactive'
                          ? 'bg-muted-foreground/50'
                          : 'bg-destructive',
                )}
              />
              <span
                className={cn(
                  // A FIXED track in both variants: the label swaps between
                  // very different lengths ("Wird synchronisiert" ->
                  // "Gespeichert") and a content-sized span re-laid the
                  // whole chip out on every state change. It is wide
                  // enough for "Synchronisierungsfehler" uncut; longer
                  // labels are truncated and read in full from the
                  // accessible name and the tooltip.
                  't-meta-sm w-[8.25rem] truncate text-muted-foreground',
                )}
                data-editor-status-label
              >
                {statusLabel}
              </span>
            </span>
            <span
              aria-hidden
              className={cn(
                // Reserved presence slot (3 avatars + overflow badge at the
                // variant's stack width): peers joining fill the box from
                // the right instead of widening the chip and shifting the
                // toolbar row.
                'flex shrink-0 items-center justify-end',
                variant === 'topbar' ? 'w-16' : 'w-20',
              )}
              data-participant-count={model.participants.length}
            >
              {model.participants.length === 0 ? <Users className="icon-sm text-muted-foreground" /> : null}
              {model.visibleParticipants.map((participant, index) => (
                <span
                  className={cn(
                    't-hint flex items-center justify-center rounded-full border-2 border-background font-medium',
                    avatarSize,
                    index > 0 && '-ml-1.5',
                  )}
                  key={participant.id}
                  data-participant-id={participant.id}
                  style={{ backgroundColor: participant.color, color: avatarTextColor(participant.color) }}
                  title={participant.name}
                >
                  {initials(participant.name)}
                </span>
              ))}
              {model.participantOverflow > 0 ? (
                <span className={cn(
                  't-hint -ml-1.5 flex items-center justify-center rounded-full border-2 border-background bg-muted text-muted-foreground',
                  avatarSize,
                )}>
                  +{model.participantOverflow}
                </span>
              ) : null}
            </span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          <div className="space-y-1 px-2 py-1.5">
            <p className="t-list">{labels.diagnostics}</p>
            <p className="t-meta-sm text-muted-foreground">{model.notice ?? statusLabel}</p>
          </div>
          <DropdownMenuSeparator />
          <div className="space-y-1 px-2 py-1.5">
            <DiagnosticRow
              label={labels.projection}
              value={model.projectionConfirmedAt
                ? new Date(model.projectionConfirmedAt).toLocaleString(locale)
                : labels.none}
            />
            <DiagnosticRow
              label={labels.unconfirmed}
              value={model.hasUnconfirmedLocalChanges ? '1+' : labels.none}
            />
            {model.reconnectAttempt > 0 ? (
              <DiagnosticRow
                label={labels.reconnectAttempt}
                value={String(model.reconnectAttempt)}
              />
            ) : null}
            {model.nextReconnectAt ? (
              <DiagnosticRow
                label={labels.nextReconnect}
                value={new Date(model.nextReconnectAt).toLocaleTimeString(locale)}
              />
            ) : null}
          </div>
          {action ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                disabled={actionPending}
                onSelect={(event) => {
                  event.preventDefault()
                  runAction()
                }}
              >
                <RefreshCw className={actionPending ? 'animate-spin' : undefined} />
                {action.label}
              </DropdownMenuItem>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

function DiagnosticRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="t-meta-sm text-muted-foreground">{label}</span>
      <span className="t-meta-sm tabular-nums">{value}</span>
    </div>
  )
}

function OpenChanges({
  changes,
  canDecide,
  error,
  expandedId,
  isDecisionPending,
  labels,
  locale,
  onDecision,
  onExpandedChange,
  onSelectedChange,
  selectedId,
}: {
  changes: readonly InspectorChange[]
  canDecide: boolean
  error: string | null
  expandedId: string | null
  isDecisionPending: boolean
  labels: typeof copy.de | typeof copy.en
  locale: 'de' | 'en'
  onDecision: (decision: InspectorDecision, patchIds: string[]) => void
  onExpandedChange: (id: string | null) => void
  onSelectedChange: (id: string | null) => void
  selectedId: string | null
}) {
  if (changes.length === 0) {
    return error
      ? <InspectorState icon={<AlertTriangle className="icon-md" />} message={error} tone="error" />
      : <InspectorState icon={<ListChecks className="icon-md" />} message={labels.noChanges} />
  }
  return (
    <>
      {error ? (
        <div className="flex items-start gap-2 border-b border-destructive/30 bg-destructive-subtle px-3 py-2 text-destructive">
          <AlertTriangle className="icon-sm mt-0.5 shrink-0" />
          <p className="t-meta">{error}</p>
        </div>
      ) : null}
      {changes.map((change) => {
    const expanded = expandedId === change.id
    const selected = selectedId === change.id
    return (
      <div
        className={cn(
          'border-b border-border transition-colors last:border-b-0',
          selected && 'bg-brand-subtle/45',
        )}
        data-inspector-change-id={change.id}
        key={change.id}
      >
        <button
          aria-expanded={expanded}
          className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-2 px-3 py-2 text-left outline-none hover:bg-surface focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
          onClick={() => {
            onSelectedChange(change.id)
            onExpandedChange(expanded ? null : change.id)
          }}
          type="button"
        >
          <span className="min-w-0">
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="t-list truncate">{compactChangeText(change) || suggestionTypeLabel(change.type, labels)}</span>
              <span className="t-hint shrink-0 rounded-sm bg-muted px-1 py-0.5 text-muted-foreground">
                {suggestionTypeLabel(change.type, labels)}
              </span>
            </span>
            <span className="t-meta-sm mt-0.5 flex min-w-0 items-center gap-1.5 text-muted-foreground">
              <span className="truncate">{change.author.name}</span>
              <span aria-hidden>·</span>
              <span className="shrink-0">{formatInspectorTime(change.createdAt, locale)}</span>
            </span>
          </span>
          <ChevronDown className={cn('icon-sm mt-0.5 text-muted-foreground transition-transform', expanded && 'rotate-180')} />
        </button>
        {expanded ? (
          <div className="space-y-2 border-t border-border/70 px-3 py-2">
            {change.originalText ? (
              <ChangeText label={labels.original} text={change.originalText} tone="original" />
            ) : null}
            {change.proposedText ? (
              <ChangeText label={labels.proposed} text={change.proposedText} tone="proposed" />
            ) : null}
            <div className="flex justify-end gap-1">
              <DecisionButton
                decision="reject"
                disabled={isDecisionPending || !canDecide}
                label={labels.reject}
                onClick={() => onDecision('reject', [change.id])}
              />
              <DecisionButton
                decision="accept"
                disabled={isDecisionPending || !canDecide}
                label={labels.accept}
                onClick={() => onDecision('accept', [change.id])}
              />
            </div>
          </div>
        ) : null}
      </div>
    )
      })}
    </>
  )
}

function HistoryChanges({
  entries,
  error,
  expandedId,
  isLoading,
  labels,
  locale,
  onExpandedChange,
}: {
  entries: readonly InspectorHistoryEntry[]
  error: string | null
  expandedId: string | null
  isLoading: boolean
  labels: typeof copy.de | typeof copy.en
  locale: 'de' | 'en'
  onExpandedChange: (id: string | null) => void
}) {
  if (isLoading) return <InspectorState icon={<LoaderCircle className="icon-md animate-spin" />} message={labels.loading} />
  if (error) return <InspectorState icon={<AlertTriangle className="icon-md" />} message={error} tone="error" />
  if (entries.length === 0) return <InspectorState icon={<Clock3 className="icon-md" />} message={labels.noHistory} />
  return entries.map((entry) => {
    const expanded = entry.id === expandedId
    const removed = entry.removedSuggestions ?? []
    const removedCount = entry.removedSuggestionCount ?? 0
    return (
      <div className="border-b border-border last:border-b-0" key={entry.id}>
        <button
          aria-expanded={expanded}
          className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-2 px-3 py-2 text-left outline-none hover:bg-surface focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
          onClick={() => onExpandedChange(expanded ? null : entry.id)}
          type="button"
        >
          <span className="min-w-0">
            <span className="t-list block text-pretty">
              {activityHeadline(entry, locale)}
            </span>
            <span className="t-meta-sm mt-0.5 flex min-w-0 items-center gap-1.5 text-muted-foreground">
              <span className="shrink-0">{formatInspectorTime(entry.createdAt, locale)}</span>
              {(entry.updateCount ?? 1) > 1 ? (
                <>
                  <span aria-hidden>·</span>
                  <span>{entry.updateCount} Updates</span>
                </>
              ) : null}
            </span>
          </span>
          <ChevronDown className={cn('icon-sm mt-0.5 text-muted-foreground transition-transform', expanded && 'rotate-180')} />
        </button>
        {expanded ? (
          <div className="space-y-2 border-t border-border/70 px-3 py-2">
            {(entry.summary ?? []).map((edit, index) => (
              <div
                className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-1.5"
                key={`${entry.id}:${edit.position}:${index}`}
              >
                <span className="t-meta-sm break-words rounded-sm bg-destructive-subtle px-1.5 py-1 text-destructive">
                  {edit.before || '∅'}
                </span>
                <span aria-hidden className="t-meta-sm pt-1 text-muted-foreground">→</span>
                <span className="t-meta-sm break-words rounded-sm bg-success-subtle px-1.5 py-1 text-success">
                  {edit.after || '∅'}
                </span>
              </div>
            ))}
            {(entry.omittedEditCount ?? 0) > 0 ? (
              <p className="t-meta-sm text-muted-foreground">
                +{entry.omittedEditCount} {labels.moreEdits}
              </p>
            ) : null}
            {removedCount > 1 && removed.length > 0 ? (
              <ul className="space-y-0.5" data-testid="inspector-removed-suggestions">
                {removed.map((suggestion) => (
                  <li className="t-meta-sm text-pretty text-muted-foreground" key={suggestion.suggestionId}>
                    {fillNames(
                      suggestion.author.id === entry.actor.id
                        ? labels.suggestionWithdrawnBy
                        : labels.suggestionRemovedBy,
                      { author: suggestion.author.name },
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
            {removedCount > removed.length ? (
              <p className="t-meta-sm text-muted-foreground">
                {countLabel(
                  removedCount - removed.length,
                  labels.moreRemovedSuggestionsOne,
                  labels.moreRemovedSuggestions,
                )}
              </p>
            ) : null}
            <details className="group">
              <summary className="t-meta-sm cursor-pointer select-none text-muted-foreground">
                {labels.technicalDetails}
              </summary>
              <div className="t-hint mt-1 space-y-0.5 break-all text-muted-foreground">
                <p className="tabular-nums">Sequence {entry.fromSequence}–{entry.toSequence}</p>
                {entry.commandId ? <p>Command {entry.commandId}</p> : null}
                {entry.suggestionIds.length > 0 ? (
                  <p>{entry.suggestionIds.length} Suggestion IDs</p>
                ) : null}
              </div>
            </details>
          </div>
        ) : null}
      </div>
    )
  })
}

function InspectorSelect({
  label,
  onValueChange,
  options,
  placeholder,
  value,
}: {
  label: string
  onValueChange: (value: string) => void
  options: readonly { label: string; value: string }[]
  placeholder: string
  value: string
}) {
  return (
    <Select onValueChange={onValueChange} value={value}>
      <SelectTrigger aria-label={label} density="toolbar">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">{placeholder}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function SegmentButton({ active, count, label, onClick }: {
  active: boolean
  count?: number
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-pressed={active}
      className={cn(
        'flex h-7 min-w-0 items-center justify-center gap-1 rounded-md px-1 text-xs font-medium transition-colors',
        active ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={onClick}
      type="button"
    >
      <span className="truncate">{label}</span>
      {count !== undefined ? <span className="t-hint tabular-nums">{count}</span> : null}
    </button>
  )
}

function ModeButton({ active, disabled, icon, label, lockLabel, onClick }: {
  active: boolean
  disabled: boolean
  icon: ReactNode
  label: string
  lockLabel: string | null
  onClick: () => void
}) {
  const button = (
    <button
      aria-label={label}
      aria-pressed={active}
      className={cn(
        'flex h-6 items-center gap-1 rounded-sm px-2 text-xs font-medium transition-colors',
        active ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
        // Locked state changes COLOR only, never geometry: an extra lock
        // glyph here once made the whole group ~72px wider while the
        // session came up, shifting the toolbar, the title truncation
        // point and the actions in one visible jolt (the 4-column grid
        // redistributes every Δ across both 1fr tracks). The tooltip and
        // aria-disabled carry the why.
        disabled && 'cursor-not-allowed opacity-50',
      )}
      disabled={disabled}
      onClick={onClick}
      type="button"
    >
      <span aria-hidden data-editor-mode-icon>{icon}</span>
      <span data-editor-mode-label>{label}</span>
    </button>
  )
  if (!lockLabel) return button
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="inline-flex" tabIndex={0}>{button}</span></TooltipTrigger>
      <TooltipContent>{lockLabel}</TooltipContent>
    </Tooltip>
  )
}

function IconButton({ children, disabled, label, onClick }: {
  children: ReactNode
  disabled?: boolean
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
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

function DecisionButton({ decision, disabled, label, onClick }: {
  decision: InspectorDecision
  disabled: boolean
  label: string
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className={cn('size-7', decision === 'accept' && 'text-success hover:text-success')}
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="ghost"
        >
          {decision === 'accept' ? <Check className="icon-sm" /> : <X className="icon-sm" />}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function ChangeText({ label, text, tone }: {
  label: string
  text: string
  tone: 'original' | 'proposed'
}) {
  return (
    <div>
      <span className="t-label text-muted-foreground">{label}</span>
      <p className={cn(
        't-meta mt-1 whitespace-pre-wrap break-words border-l-2 pl-2 text-foreground',
        tone === 'proposed' ? 'border-success' : 'border-destructive',
      )}>
        {text}
      </p>
    </div>
  )
}

function InspectorState({ icon, message, tone = 'muted' }: {
  icon: ReactNode
  message: string
  tone?: 'error' | 'muted'
}) {
  return (
    <div className={cn(
      'flex min-h-32 flex-col items-center justify-center gap-2 px-6 py-8 text-center',
      tone === 'error' ? 'text-destructive' : 'text-muted-foreground',
    )}>
      <span>{icon}</span>
      <p className="t-meta">{message}</p>
    </div>
  )
}

function uniqueParticipants(participants: readonly InspectorParticipant[]): InspectorParticipant[] {
  return [...new Map(participants.map((participant) => [participant.id, participant])).values()]
    .sort((left, right) => left.name.localeCompare(right.name))
}

function suggestionTypeOptions(labels: typeof copy.de | typeof copy.en) {
  return (['insertion', 'deletion', 'replacement', 'format', 'structure'] as const).map((type) => ({
    label: suggestionTypeLabel(type, labels),
    value: type,
  }))
}

function historyTypeOptions(labels: typeof copy.de | typeof copy.en) {
  return (['direct', 'suggestion', 'decision', 'comment', 'system'] as const).map((type) => ({
    label: historyTypeLabel(type, labels),
    value: type,
  }))
}

function suggestionTypeLabel(
  type: SuggestionKind,
  labels: typeof copy.de | typeof copy.en,
): string {
  return labels[type]
}

function historyTypeLabel(
  type: InspectorHistoryKind,
  labels: typeof copy.de | typeof copy.en,
): string {
  const historyLabels = labels === copy.de
    ? { comment: 'Kommentar', decision: 'Entscheidung', direct: 'Direkte Änderung', suggestion: 'Vorschlag', system: 'System' }
    : { comment: 'Comment', decision: 'Decision', direct: 'Direct edit', suggestion: 'Suggestion', system: 'System' }
  return historyLabels[type]
}

/**
 * The headline of an edit that removed pending suggestions by deleting the
 * text they proposed. One suggestion names its author, or reads as
 * withdrawn when the writer removed their own; several are counted, and the
 * entry lists them.
 */
function removalHeadline(
  entry: InspectorHistoryEntry,
  count: number,
  labels: typeof copy.de | typeof copy.en,
): string {
  const [only] = entry.removedSuggestions ?? []
  if (count === 1 && only) {
    return fillNames(
      only.author.id === entry.actor.id ? labels.withdrewSuggestion : labels.removedSuggestionBy,
      { author: only.author.name, name: entry.actor.name },
    )
  }
  return fillNames(
    countLabel(count, labels.removedSuggestionsOne, labels.removedSuggestions),
    { name: entry.actor.name },
  )
}

/** Puts names into a label's `{name}` and `{author}` slots, taking every
 * name literally. */
function fillNames(label: string, names: { author?: string; name?: string }): string {
  return label.replace(/\{(author|name)\}/g, (slot, key: 'author' | 'name') => names[key] ?? slot)
}

function activityHeadline(
  entry: InspectorHistoryEntry,
  locale: 'de' | 'en',
): string {
  const count = Math.max(1, entry.suggestionIds.length)
  const firstEdit = entry.summary?.[0]
  const removedCount = entry.removedSuggestionCount ?? 0
  if (removedCount > 0) return removalHeadline(entry, removedCount, copy[locale])
  if (locale === 'de') {
    if (entry.type === 'comment') {
      const actions = {
        created: 'erstellte einen Kommentar',
        message_deleted: 'löschte einen Kommentar',
        message_edited: 'bearbeitete einen Kommentar',
        reopened: 'öffnete einen Kommentar erneut',
        replied: 'antwortete auf einen Kommentar',
        resolved: 'löste einen Kommentar auf',
      } as const
      return `${entry.actor.name} ${
        actions[entry.commentAction ?? 'created']
      }`
    }
    if (entry.type === 'decision') {
      const noun = count === 1 ? 'Vorschlag' : 'Vorschläge'
      return entry.outcome === 'rejected'
        ? `${entry.actor.name} lehnte ${count} ${noun} ab`
        : `${entry.actor.name} nahm ${count} ${noun} an`
    }
    if (entry.type === 'suggestion') {
      // German nouns keep their capital letter: "eine Einfügung".
      const kind = firstEdit && firstEdit.kind !== 'direct'
        ? suggestionTypeLabel(firstEdit.kind, copy.de)
        : 'Änderung'
      return `${entry.actor.name} schlug eine ${kind} vor`
    }
    if (entry.type === 'direct') {
      return firstEdit?.kind === 'structure' && firstEdit.before && firstEdit.after
        ? `${entry.actor.name} änderte ${firstEdit.before} in ${firstEdit.after}`
        : `${entry.actor.name} bearbeitete das Dokument`
    }
    return `${entry.actor.name} aktualisierte den Dokumentstatus`
  }
  if (entry.type === 'comment') {
    const actions = {
      created: 'created a comment',
      message_deleted: 'deleted a comment',
      message_edited: 'edited a comment',
      reopened: 'reopened a comment',
      replied: 'replied to a comment',
      resolved: 'resolved a comment',
    } as const
    return `${entry.actor.name} ${actions[entry.commentAction ?? 'created']}`
  }
  if (entry.type === 'decision') {
    const noun = count === 1 ? 'suggestion' : 'suggestions'
    return entry.outcome === 'rejected'
      ? `${entry.actor.name} rejected ${count} ${noun}`
      : `${entry.actor.name} accepted ${count} ${noun}`
  }
  if (entry.type === 'suggestion') {
    const kind = firstEdit && firstEdit.kind !== 'direct'
      ? suggestionTypeLabel(firstEdit.kind, copy.en).toLocaleLowerCase('en')
      : 'change'
    return `${entry.actor.name} suggested a ${kind}`
  }
  if (entry.type === 'direct') {
    return firstEdit?.kind === 'structure' && firstEdit.before && firstEdit.after
      ? `${entry.actor.name} changed ${firstEdit.before} to ${firstEdit.after}`
      : `${entry.actor.name} edited the document`
  }
  return `${entry.actor.name} updated the document status`
}

function formatInspectorTime(timestamp: number, locale: 'de' | 'en'): string {
  return new Intl.DateTimeFormat(locale === 'de' ? 'de-DE' : 'en-US', {
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    month: 'short',
  }).format(timestamp)
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toLocaleUpperCase())
    .join('') || '?'
}

function avatarTextColor(color: string): '#111827' | '#ffffff' {
  const red = Number.parseInt(color.slice(1, 3), 16)
  const green = Number.parseInt(color.slice(3, 5), 16)
  const blue = Number.parseInt(color.slice(5, 7), 16)
  const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
  return luminance > 0.58 ? '#111827' : '#ffffff'
}
