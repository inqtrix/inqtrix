import type { HocuspocusProvider } from '@hocuspocus/provider'
import {
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  SUGGESTION_MARK_NAMES,
  isStructureSuggestionData,
  suggestionDescriptors,
  type CollaborationChangeKind,
} from '@inqtrix/editor-schema'
import type { Editor } from '@tiptap/react'
import type { Transaction } from '@tiptap/pm/state'
import { useEffect, useMemo, useReducer, useRef, useState, useSyncExternalStore } from 'react'

import {
  listEditorCollaborationActivity,
  type EditorCollaborationActivity,
  type EditorCollaborationUser,
} from '@/api/inqtrixClient'
import { readerTexts } from '@/api/requestErrorText'
import {
  beginCollaborationAuthorityGuard,
  CollaborationAuthorityError,
  type CollaborationAuthorityGuard,
  type CollaborationAuthoritySource,
} from '../collaborationAuthority'
import type { CollaborationDocumentHandle } from '../useCollaborationDocument'
import {
  buildInspectorChanges,
  type InspectorChange,
  type InspectorHistoryEntry,
  type InspectorParticipant,
  type InspectorSuggestionExcerpt,
} from './model'
import {
  COLLABORATION_EDIT_REFUSAL_REASONS,
  collaborationSuggestionCollisionEvent,
  collaborationSuggestionErrorEvent,
  type CollaborationEditRefusal,
  type CollaborationSuggestionCollision,
} from '../tiptap'
import { messageFromError } from '@/api/requestErrorText'

type AwarenessAdapter = {
  getStates?: () => Map<number, unknown>
  off?: (event: 'change' | 'update', listener: () => void) => void
  on?: (event: 'change' | 'update', listener: () => void) => void
  states?: Map<number, unknown>
}

export function bindInspectorSuggestionEvents(
  editor: Editor,
  handlers: {
    onCollision: (event: Event) => void
    onError: (event: Event) => void
  },
): (() => void) | null {
  if (editor.isDestroyed) return null
  // Tiptap destroys the legacy editor while React is still cleaning up the
  // inspector effect during the one-way Markdown -> collaboration remount.
  // Capture the mounted DOM node once; resolving `editor.view` again in the
  // cleanup throws after the editor has been destroyed.
  const viewDom = editor.view.dom
  viewDom.addEventListener(collaborationSuggestionErrorEvent, handlers.onError)
  viewDom.addEventListener(collaborationSuggestionCollisionEvent, handlers.onCollision)
  return () => {
    viewDom.removeEventListener(collaborationSuggestionErrorEvent, handlers.onError)
    viewDom.removeEventListener(collaborationSuggestionCollisionEvent, handlers.onCollision)
  }
}

export type InspectorCollaborationSnapshot = {
  changes: InspectorChange[]
  /** The pending suggestions could not be read from the document: one of
   * them lacks complete metadata. */
  changesUnreadable: boolean
  collision: CollaborationSuggestionCollision | null
  /** The editor's last refusal of a local change, until a change goes
   * through. */
  error: CollaborationEditRefusal | null
  participants: InspectorParticipant[]
}

const REFUSAL_REASONS: ReadonlySet<string> = new Set(COLLABORATION_EDIT_REFUSAL_REASONS)

/** The refusal an editor event carries, or null for a cleared refusal. */
export function editRefusalFromEvent(event: Event): CollaborationEditRefusal | null {
  const detail = (event as CustomEvent<unknown>).detail
  if (typeof detail !== 'object' || detail === null) return null
  const { message, reason } = detail as { message?: unknown; reason?: unknown }
  if (typeof reason !== 'string' || !REFUSAL_REASONS.has(reason)) {
    console.warn('An editor refusal event carried an unknown reason.', detail)
    return {
      message: typeof message === 'string' ? message : 'This edit cannot be applied.',
      reason: 'unknown',
    }
  }
  return {
    message: typeof message === 'string' ? message : '',
    reason: reason as CollaborationEditRefusal['reason'],
  }
}

export function useInspectorCollaborationSnapshot(
  editor: Editor | null,
  collaboration: CollaborationDocumentHandle,
  durableSuggestionAuthors: ReadonlyMap<string, InspectorParticipant> = new Map(),
): InspectorCollaborationSnapshot {
  const [revision, refresh] = useReducer((value: number) => value + 1, 0)
  const [suggestionError, setSuggestionError] = useState<CollaborationEditRefusal | null>(null)
  const [collision, setCollision] = useState<CollaborationSuggestionCollision | null>(null)
  const provider = collaboration.provider

  useEffect(() => {
    if (!editor) return
    const handleTransaction = ({ transaction }: { transaction: Transaction }) => {
      if (transaction.docChanged) refresh()
    }
    editor.on('transaction', handleTransaction)
    return () => {
      editor.off('transaction', handleTransaction)
    }
  }, [editor])

  useEffect(() => {
    // A refusal belongs to the editor that refused; another editor starts
    // without one.
    setSuggestionError(null)
    setCollision(null)
    if (!editor) return
    const handleSuggestionError = (event: Event) => {
      setSuggestionError(editRefusalFromEvent(event))
    }
    const handleSuggestionCollision = (event: Event) => {
      const detail = (event as CustomEvent<CollaborationSuggestionCollision | null>).detail
      setCollision(detail?.patchId && detail.suggestionId ? detail : null)
    }
    return bindInspectorSuggestionEvents(editor, {
      onCollision: handleSuggestionCollision,
      onError: handleSuggestionError,
    }) ?? undefined
  }, [editor])

  useEffect(() => {
    const awareness = providerAwareness(provider)
    if (!awareness?.on || !awareness.off) return
    const handleAwareness = () => refresh()
    awareness.on('change', handleAwareness)
    awareness.on('update', handleAwareness)
    return () => {
      awareness.off?.('change', handleAwareness)
      awareness.off?.('update', handleAwareness)
    }
  }, [provider])

  const userSignature = collaboration.user
    ? `${collaboration.user.id}:${collaboration.user.name}:${collaboration.user.color}`
    : ''
  return useMemo(() => {
    const participants = readInspectorParticipants(provider, collaboration.user)
    if (!editor) {
      return {
        changes: [],
        changesUnreadable: false,
        collision,
        error: suggestionError,
        participants,
      }
    }
    try {
      const descriptors = suggestionDescriptors(editor.state.doc)
      const excerpts = suggestionExcerpts(editor)
      return {
        changes: buildInspectorChanges(
          descriptors,
          excerpts,
          participants,
          durableSuggestionAuthors,
        ),
        changesUnreadable: false,
        collision,
        error: suggestionError,
        participants,
      }
    } catch (error) {
      console.warn('The pending suggestions of the document could not be read.', error)
      return {
        changes: [],
        changesUnreadable: true,
        collision,
        error: suggestionError,
        participants,
      }
    }
  }, [
    collision,
    durableSuggestionAuthors,
    editor,
    provider,
    revision,
    suggestionError,
    userSignature,
  ])
}

export function useInspectorOpenSuggestionIds(editor: Editor | null): readonly string[] {
  const [revision, refresh] = useReducer((value: number) => value + 1, 0)

  useEffect(() => {
    if (!editor) return
    const handleTransaction = ({ transaction }: { transaction: Transaction }) => {
      if (transaction.docChanged) refresh()
    }
    editor.on('transaction', handleTransaction)
    return () => {
      editor.off('transaction', handleTransaction)
    }
  }, [editor])

  return useMemo(() => {
    if (!editor) return []
    try {
      return [...new Set(
        suggestionDescriptors(editor.state.doc).map((descriptor) => descriptor.suggestionId),
      )].sort()
    } catch {
      return []
    }
  }, [editor, revision])
}

export function readInspectorParticipants(
  provider: HocuspocusProvider | null,
  localUser: EditorCollaborationUser | null,
): InspectorParticipant[] {
  const participants = new Map<string, InspectorParticipant>()
  if (localUser) participants.set(localUser.id, normalizedParticipant(localUser))
  const awareness = providerAwareness(provider)
  const states = awareness?.getStates?.() ?? awareness?.states
  for (const state of states?.values() ?? []) {
    const participant = participantFromAwarenessState(state)
    if (participant) participants.set(participant.id, participant)
  }
  return [...participants.values()].sort((left, right) => {
    if (left.id === localUser?.id) return -1
    if (right.id === localUser?.id) return 1
    return left.name.localeCompare(right.name)
  })
}

function providerAwareness(provider: HocuspocusProvider | null): AwarenessAdapter | null {
  if (!provider) return null
  const candidate = (provider as unknown as { awareness?: AwarenessAdapter }).awareness
  return candidate ?? null
}

function participantFromAwarenessState(state: unknown): InspectorParticipant | null {
  if (!state || typeof state !== 'object') return null
  const user = (state as { user?: unknown }).user
  if (!user || typeof user !== 'object') return null
  const candidate = user as Partial<EditorCollaborationUser>
  if (
    typeof candidate.id !== 'string'
    || typeof candidate.name !== 'string'
    || typeof candidate.color !== 'string'
  ) return null
  return normalizedParticipant({
    color: candidate.color,
    id: candidate.id,
    name: candidate.name,
  })
}

function normalizedParticipant(user: EditorCollaborationUser): InspectorParticipant {
  return {
    color: /^#[0-9a-f]{6}$/i.test(user.color) ? user.color : '#6b7280',
    id: user.id,
    name: user.name.trim() || 'Collaborator',
  }
}

export function suggestionExcerpts(editor: Editor): Map<string, InspectorSuggestionExcerpt> {
  const excerpts = new Map<string, InspectorSuggestionExcerpt>()
  editor.state.doc.descendants((node, position) => {
    for (const mark of node.marks) {
      if (!SUGGESTION_MARK_NAMES.has(mark.type.name)) continue
      const suggestionId = mark.attrs.suggestionId
      if (typeof suggestionId !== 'string' || !suggestionId) continue
      const existing = excerpts.get(suggestionId) ?? {
        deletionText: '',
        insertionText: '',
        modificationText: '',
        position,
      }
      const text = node.textContent
      const key = `${mark.type.name}Text` as
        | 'deletionText'
        | 'insertionText'
        | 'modificationText'
      existing[key] += text
      existing.position = Math.min(existing.position, position)
      excerpts.set(suggestionId, existing)
    }
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (isStructureSuggestionData(structure)) {
      excerpts.set(structure.suggestionId, {
        deletionText: structureSourceLabel(node.type.name, node.attrs.level),
        insertionText: structureActionLabel(structure.action),
        modificationText: node.textContent,
        position,
      })
    }
    return true
  })
  return excerpts
}

function structureSourceLabel(type: string, level: unknown): string {
  if (type === 'heading') return `H${String(level ?? '')}`
  if (type === 'codeBlock') return 'Code'
  return '¶'
}

function structureActionLabel(action: string): string {
  const labels: Record<string, string> = {
    blockquote: '❝ Quote',
    bulletList: '• List',
    codeBlock: 'Code',
    heading1: 'H1',
    heading2: 'H2',
    heading3: 'H3',
    orderedList: '1. List',
    paragraph: '¶',
    taskList: '☐ List',
  }
  return labels[action] ?? action
}

type UseInspectorActivityOptions = {
  /** The collaboration session is live; the loaded activity belongs to it. */
  active: boolean
  activityRevision: number
  apiKey?: string
  authorityRevision: number
  documentId: string | null
  generation: number | null
  lifecycleKey: string
  locale: 'de' | 'en'
  openSuggestionIds: readonly string[]
  readAuthority: CollaborationAuthoritySource['readAuthority']
  /** The inspector shows the activity. Only this decides whether it is read:
   * while hidden, the last loaded activity stays as it is. */
  visible: boolean
  workspaceId: string
}

type InspectorActivityState = {
  activity: EditorCollaborationActivity[]
  attributionActivity: EditorCollaborationActivity[]
  attributionWarning: string | null
  authorityRevision: number | null
  documentId: string | null
  error: string | null
  isLoading: boolean
  lifecycleKey: string | null
}

/** The shortest time between the starts of two activity reads. */
export const INSPECTOR_ACTIVITY_MIN_SPACING_MS = 3_000

export type ActivityRefreshScheduler = {
  dispose(): void
  request(): void
}

/** Plans activity reads the way a stale-while-revalidate cache does: one
 * read at a time, at most one follow-up for any number of changes while it
 * runs, and two starts at least `minSpacingMs` apart. A change never aborts
 * a running read; it only marks its result as one to follow up. */
export function createActivityRefreshScheduler<Handle>({
  cancel,
  minSpacingMs,
  now,
  run,
  schedule,
}: {
  cancel: (handle: Handle) => void
  minSpacingMs: number
  now: () => number
  run: () => Promise<void>
  schedule: (callback: () => void, delayMs: number) => Handle
}): ActivityRefreshScheduler {
  let disposed = false
  let inFlight = false
  let followUp = false
  let lastStartedAt: number | null = null
  let timer: Handle | null = null

  const start = (): void => {
    timer = null
    if (disposed) return
    inFlight = true
    followUp = false
    lastStartedAt = now()
    const settle = (): void => {
      inFlight = false
      if (!followUp) return
      followUp = false
      request()
    }
    void run().then(settle, settle)
  }

  const request = (): void => {
    if (disposed) return
    if (inFlight) {
      followUp = true
      return
    }
    // A planned start reads the state current when it starts.
    if (timer !== null) return
    const wait = lastStartedAt === null ? 0 : lastStartedAt + minSpacingMs - now()
    if (wait <= 0) start()
    else timer = schedule(start, wait)
  }

  return {
    dispose: () => {
      disposed = true
      if (timer !== null) cancel(timer)
      timer = null
    },
    request,
  }
}

const NO_ACTIVITY: readonly EditorCollaborationActivity[] = Object.freeze([])

/** What the inspector shows of the loaded activity: the loaded lists while
 * they belong to the current session, otherwise the same empty lists on
 * every render, so the views derived from them stay memoized. */
export function inspectorActivityView(
  state: InspectorActivityState,
  keys: {
    active: boolean
    authorityRevision: number
    documentId: string | null
    lifecycleKey: string
  },
): {
  activity: readonly EditorCollaborationActivity[]
  attributionActivity: readonly EditorCollaborationActivity[]
  attributionWarning: string | null
  error: string | null
  isLoading: boolean
} {
  const current = keys.active
    && state.authorityRevision === keys.authorityRevision
    && state.documentId === keys.documentId
    && state.lifecycleKey === keys.lifecycleKey
  return {
    activity: current ? state.activity : NO_ACTIVITY,
    attributionActivity: current ? state.attributionActivity : NO_ACTIVITY,
    attributionWarning: current ? state.attributionWarning : null,
    error: current ? state.error : null,
    isLoading: current && state.isLoading,
  }
}

function subscribePageVisibility(onChange: () => void): () => void {
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}

function pageIsVisible(): boolean {
  return document.visibilityState === 'visible'
}

export function useInspectorCollaborationActivity({
  active,
  activityRevision,
  apiKey,
  authorityRevision,
  documentId,
  generation,
  lifecycleKey,
  locale,
  openSuggestionIds,
  readAuthority,
  visible,
  workspaceId,
}: UseInspectorActivityOptions): {
  attributionWarning: string | null
  entries: InspectorHistoryEntry[]
  error: string | null
  isLoading: boolean
  suggestionAuthors: ReadonlyMap<string, InspectorParticipant>
} {
  const [requestState, setRequestState] = useState<InspectorActivityState>({
    activity: [],
    attributionActivity: [],
    attributionWarning: null,
    authorityRevision: null,
    documentId: null,
    error: null,
    isLoading: false,
    lifecycleKey: null,
  })
  const pageVisible = useSyncExternalStore(subscribePageVisibility, pageIsVisible, () => true)
  const openSuggestionIdsSignature = [...openSuggestionIds].sort().join('\u0000')
  const changeKey = `${activityRevision}\u0001${openSuggestionIdsSignature}`
  const latest = useRef({ changeKey, openSuggestionIds })
  const refresh = useRef<{ changeKey: string; scheduler: ActivityRefreshScheduler } | null>(null)
  const reading = active && visible && pageVisible

  useEffect(() => {
    latest.current = { changeKey, openSuggestionIds }
  })

  useEffect(() => {
    if (!reading || !documentId || generation === null) return
    const controller = new AbortController()
    let authorityGuard: CollaborationAuthorityGuard
    try {
      authorityGuard = beginCollaborationAuthorityGuard(
        { readAuthority },
        { documentId, generation },
        'read',
        locale,
      )
    } catch {
      return () => controller.abort()
    }
    const load = async (): Promise<void> => {
      setRequestState((current) => ({
        activity: current.documentId === documentId && current.lifecycleKey === lifecycleKey
          ? current.activity
          : [],
        attributionActivity: current.documentId === documentId && current.lifecycleKey === lifecycleKey
          ? current.attributionActivity
          : [],
        attributionWarning: current.documentId === documentId && current.lifecycleKey === lifecycleKey
          ? current.attributionWarning
          : null,
        authorityRevision,
        documentId,
        error: null,
        isLoading: true,
        lifecycleKey,
      }))
      try {
        const response = await loadInspectorCollaborationActivity({
          apiKey,
          authorityGuard,
          documentId,
          openSuggestionIds: latest.current.openSuggestionIds,
          signal: controller.signal,
          workspaceId,
        })
        authorityGuard.assertCurrent()
        if (!controller.signal.aborted) {
          setRequestState({
            activity: response.data,
            attributionActivity: response.attributionData,
            attributionWarning: inspectorAttributionWarning(response, locale),
            authorityRevision,
            documentId,
            error: null,
            isLoading: false,
            lifecycleKey,
          })
        }
      } catch (requestError: unknown) {
        if (!controller.signal.aborted && !(requestError instanceof CollaborationAuthorityError)) {
          setRequestState((current) => ({
            activity: current.documentId === documentId && current.lifecycleKey === lifecycleKey
              ? current.activity
              : [],
            attributionActivity: current.documentId === documentId && current.lifecycleKey === lifecycleKey
              ? current.attributionActivity
              : [],
            attributionWarning: current.documentId === documentId && current.lifecycleKey === lifecycleKey
              ? current.attributionWarning
              : null,
            authorityRevision,
            documentId,
            error: messageFromError(requestError),
            isLoading: false,
            lifecycleKey,
          }))
        }
      }
    }
    const scheduler = createActivityRefreshScheduler({
      cancel: (handle: number) => window.clearTimeout(handle),
      minSpacingMs: INSPECTOR_ACTIVITY_MIN_SPACING_MS,
      now: () => Date.now(),
      run: load,
      schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
    })
    refresh.current = { changeKey: latest.current.changeKey, scheduler }
    scheduler.request()
    return () => {
      refresh.current = null
      scheduler.dispose()
      controller.abort()
    }
  }, [
    apiKey,
    authorityRevision,
    documentId,
    generation,
    lifecycleKey,
    locale,
    readAuthority,
    reading,
    workspaceId,
  ])

  useEffect(() => {
    // A new durable sequence or another set of open suggestions asks for a
    // read; the scheduler decides when it starts.
    const current = refresh.current
    if (!current || current.changeKey === changeKey) return
    current.changeKey = changeKey
    current.scheduler.request()
  }, [changeKey])

  const view = inspectorActivityView(requestState, {
    active,
    authorityRevision,
    documentId,
    lifecycleKey,
  })
  const { activity, attributionActivity } = view
  const entries = useMemo(
    () => normalizeActivity(activity),
    [activity],
  )
  const suggestionAuthors = useMemo(
    () => durableSuggestionAuthorsFromActivity([...activity, ...attributionActivity]),
    [activity, attributionActivity],
  )
  return {
    attributionWarning: view.attributionWarning,
    entries,
    error: view.error,
    isLoading: view.isLoading,
    suggestionAuthors,
  }
}

type InspectorActivityPage = Awaited<ReturnType<typeof listEditorCollaborationActivity>>

const INSPECTOR_ACTIVITY_PAGE_SIZE = 100
// Five pages cap each refresh at 500 canonical rows. The bound is tied to the
// endpoint's established page size, so unique cursors can never cause an
// unbounded attribution walk.
export const INSPECTOR_ATTRIBUTION_MAX_PAGES = 5

type LoadInspectorActivityOptions = {
  apiKey?: string
  authorityGuard?: CollaborationAuthorityGuard | null
  documentId: string
  fetchActivity?: typeof listEditorCollaborationActivity
  openSuggestionIds: readonly string[]
  signal?: AbortSignal
  workspaceId: string
}

export async function loadInspectorCollaborationActivity({
  apiKey,
  authorityGuard = null,
  documentId,
  fetchActivity = listEditorCollaborationActivity,
  openSuggestionIds,
  signal,
  workspaceId,
}: LoadInspectorActivityOptions): Promise<{
  attributionComplete: boolean
  attributionData: EditorCollaborationActivity[]
  data: EditorCollaborationActivity[]
  lookupLimited: boolean
  unresolvedSuggestionCount: number
}> {
  authorityGuard?.assertCurrent()
  const firstPage = await fetchActivity(documentId, {
    apiKey,
    limit: INSPECTOR_ACTIVITY_PAGE_SIZE,
    signal,
    workspaceId,
  })
  authorityGuard?.assertCurrent()
  const missingSuggestionIds = new Set(openSuggestionIds)
  removeAttributedSuggestionIds(missingSuggestionIds, firstPage.data)
  const attributionData: EditorCollaborationActivity[] = []
  const visitedCursors = new Set<string>()
  let cursor = firstPage.next_cursor
  let pagesRead = 1

  while (
    cursor
    && missingSuggestionIds.size > 0
    && pagesRead < INSPECTOR_ATTRIBUTION_MAX_PAGES
    && !visitedCursors.has(cursor)
  ) {
    visitedCursors.add(cursor)
    authorityGuard?.assertCurrent()
    const page: InspectorActivityPage = await fetchActivity(documentId, {
      apiKey,
      cursor,
      limit: INSPECTOR_ACTIVITY_PAGE_SIZE,
      signal,
      workspaceId,
    })
    authorityGuard?.assertCurrent()
    pagesRead += 1
    for (const item of page.data) {
      if (
        item.type === 'suggestion'
        && item.suggestion_ids.some((suggestionId) => missingSuggestionIds.has(suggestionId))
      ) {
        attributionData.push(item)
      }
    }
    removeAttributedSuggestionIds(missingSuggestionIds, page.data)
    cursor = page.next_cursor
  }

  const attributionComplete = missingSuggestionIds.size === 0
  return {
    attributionComplete,
    attributionData,
    data: firstPage.data,
    lookupLimited: !attributionComplete
      && cursor !== null
      && pagesRead >= INSPECTOR_ATTRIBUTION_MAX_PAGES,
    unresolvedSuggestionCount: missingSuggestionIds.size,
  }
}

export function inspectorAttributionWarning(
  result: Pick<
    Awaited<ReturnType<typeof loadInspectorCollaborationActivity>>,
    'attributionComplete' | 'lookupLimited' | 'unresolvedSuggestionCount'
  >,
  locale: 'de' | 'en',
): string | null {
  if (result.attributionComplete || result.unresolvedSuggestionCount === 0) return null
  const count = result.unresolvedSuggestionCount
  const rowLimit = INSPECTOR_ACTIVITY_PAGE_SIZE * INSPECTOR_ATTRIBUTION_MAX_PAGES
  if (locale === 'de') {
    const changes = count === 1 ? 'offene Änderung' : 'offene Änderungen'
    return result.lookupLimited
      ? `Die Autorenzuordnung ist für ${count} ${changes} unvollständig, weil die begrenzte Suche nach ${rowLimit} Aktivitätseinträgen beendet wurde.`
      : `Für ${count} ${changes} ist keine kanonische Autorenzuordnung verfügbar.`
  }
  const changes = count === 1 ? 'open change' : 'open changes'
  return result.lookupLimited
    ? `Author attribution is incomplete for ${count} ${changes} because the bounded ${rowLimit}-row activity lookup was exhausted.`
    : `Canonical author attribution is unavailable for ${count} ${changes}.`
}

function removeAttributedSuggestionIds(
  missingSuggestionIds: Set<string>,
  activity: readonly EditorCollaborationActivity[],
): void {
  for (const item of activity) {
    if (item.type !== 'suggestion') continue
    for (const suggestionId of item.suggestion_ids) missingSuggestionIds.delete(suggestionId)
  }
}

export function normalizeActivity(
  activity: readonly EditorCollaborationActivity[],
): InspectorHistoryEntry[] {
  return activity.map((item) => {
    return {
      actor: activityActor(item),
      actorKind: item.actor_kind,
      commandId: item.command_id,
      commentAction: item.comment_action,
      createdAt: item.created_at * 1_000,
      fromSequence: item.from_sequence,
      id: `${item.from_sequence}:${item.to_sequence}:${item.command_id ?? item.type}`,
      omittedEditCount: item.summary?.omitted_edit_count ?? 0,
      outcome: item.outcome ?? null,
      removedSuggestionCount: item.rejected_suggestion_count ?? 0,
      removedSuggestions: (item.rejected_suggestions ?? []).map((removed) => ({
        author: participantOf(
          removed.author,
          removed.author.kind === 'guest' ? 'guest' : 'human',
        ),
        patchId: removed.patch_id,
        suggestionId: removed.suggestion_id,
      })),
      summary: item.summary?.edits ?? [],
      suggestionIds: item.suggestion_ids,
      toSequence: item.to_sequence,
      type: item.type,
      updateCount: item.update_count ?? 1,
    }
  })
}

/** Derive attribution from canonical suggestion events. Decision events may
 * mention the same suggestion IDs but describe the reviewer, not the author. */
export function durableSuggestionAuthorsFromActivity(
  activity: readonly EditorCollaborationActivity[],
): ReadonlyMap<string, InspectorParticipant> {
  const authors = new Map<string, InspectorParticipant>()
  for (const item of activity) {
    if (item.type !== 'suggestion') continue
    const actor = activityActor(item)
    for (const suggestionId of item.suggestion_ids) {
      if (!authors.has(suggestionId)) authors.set(suggestionId, actor)
    }
  }
  return authors
}

function activityActor(item: EditorCollaborationActivity): InspectorParticipant {
  return participantOf(item.actor, item.actor_kind)
}

function participantOf(
  actor: EditorCollaborationActivity['actor'],
  kind: EditorCollaborationActivity['actor_kind'],
): InspectorParticipant {
  // An erased account's work stays under its placeholder, named in the
  // reader's language rather than by the stored name.
  const name =
    actor.kind === 'deleted' ? readerTexts().common.deletedAccount : actor.name.trim()
  const id = actor.id ?? `${kind}:${name || 'unknown'}`
  return {
    color: actorColor(kind),
    id,
    name: name || activityActorFallbackName(kind),
  }
}

function activityActorFallbackName(kind: EditorCollaborationActivity['actor_kind']): string {
  const names: Record<EditorCollaborationActivity['actor_kind'], string> = {
    assistant: 'Assistant',
    agent: 'Agent',
    guest: 'Guest',
    human: 'Collaborator',
    system: 'System',
  }
  return names[kind]
}

function actorColor(kind: EditorCollaborationActivity['actor_kind']): string {
  const colors: Record<EditorCollaborationActivity['actor_kind'], string> = {
    assistant: '#7c3aed',
    agent: '#0f766e',
    guest: '#6b7280',
    human: '#6b7280',
    system: '#64748b',
  }
  return colors[kind]
}

export function collaborationChangeKinds(
  entries: readonly InspectorHistoryEntry[],
): Array<CollaborationChangeKind | 'comment'> {
  return [...new Set(entries.map((entry) => entry.type))]
}
