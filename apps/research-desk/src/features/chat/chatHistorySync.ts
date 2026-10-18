/**
 * Pure conversion + diff helpers between the local ProjectState chat
 * records and the server wire shape (project-persistence tier).
 *
 * The boundary the API client (inqtrixClient.ts) does NOT cross: the
 * client speaks the verbatim server shape (snake_case, unix-seconds float
 * timestamps); this module maps that to/from the ISO-timestamped
 * ChatThreadRecord / ChatMessageRecord / ChatThreadGroupRecord the reducer
 * uses, exactly as fromRunSummary maps the run wire shape. Group
 * membership lives in ProjectState.chatThreadGroupMemberships (a
 * thread->group map), NOT on the thread record, so the converters carry
 * the group id alongside the record.
 *
 * The functions here are pure (no I/O, no React) so the conversion and
 * change-detection logic is unit-testable; useChatHistoryApi orchestrates
 * the requests + dispatches.
 */

import {
  appendChatMessages,
  saveChatThread,
  saveChatThreadGroup,
  type ClientOptions,
  type ServerChatMessage,
  type ServerChatThread,
  type ServerChatThreadGroup,
} from '@/api/inqtrixClient'
import type {
  ChatMessageAttachmentRecord,
  ChatChainStepRecord,
  ChatMessageModelResolutionRecord,
  ChatMessageRequestContextRecord,
  ChatMessageRecord,
  ChatRole,
  ChatThreadGroupRecord,
  ChatThreadRecord,
} from '@/features/project/types'
import {
  agentModelSelectionKey,
  normalizeAgentModelSelection,
} from '@/features/agent/executionPolicy'
import { isoFromUnixSeconds, unixSecondsFromIso } from '@/lib/time'
import { syncCollection } from '@/features/project/syncCollection'
import { titleFromWire, titleWire } from '@/features/sessionTitle/model'

const VALID_SOURCES: ReadonlySet<string> = new Set(['api', 'imported', 'mock'])

function normalizeSource(source: string): ChatThreadRecord['source'] {
  return VALID_SOURCES.has(source) ? (source as ChatThreadRecord['source']) : 'api'
}

/** One server thread -> its local record (no messages — loaded on open)
 * plus its group membership (``null`` = ungrouped). */
export function threadRecordFromServer(thread: ServerChatThread): {
  groupId: string | null
  record: ChatThreadRecord
} {
  const modelSelection = threadModelSelectionFromWire(thread.model_selection)
  return {
    groupId: thread.group_id,
    record: {
      createdAt: isoFromUnixSeconds(thread.created_at),
      id: thread.id,
      messages: [],
      preview: thread.preview,
      source: normalizeSource(thread.source),
      ...titleFromWire(thread),
      updatedAt: isoFromUnixSeconds(thread.updated_at),
      ...(modelSelection ? { modelSelection } : {}),
    },
  }
}

/** Parse the thread's stored pick defensively; '' / absent / garbage all
 * resolve to null so the account preference can seed instead. */
export function threadModelSelectionFromWire(value: string | undefined) {
  if (!value) return null
  try {
    return normalizeAgentModelSelection(JSON.parse(value) as unknown)
  } catch {
    return null
  }
}

/** One server message -> its local record, unpacking the verbatim
 * optional fields the backend stored in ``metadata``. */
export function messageRecordFromServer(message: ServerChatMessage): ChatMessageRecord {
  const metadata = message.metadata ?? {}
  const attachments = metadata.attachments as
    | ChatMessageAttachmentRecord[]
    | undefined
  const chainTrace = metadata.chainTrace as ChatChainStepRecord[] | undefined
  const modelResolution = metadata.modelResolution as
    | ChatMessageModelResolutionRecord
    | undefined
  const requestContext = messageRequestContextFromMetadata(metadata.requestContext)
  return {
    contentMarkdown: message.content_markdown,
    createdAt: isoFromUnixSeconds(message.created_at),
    id: message.id,
    role: message.role as ChatRole,
    ...(attachments ? { attachments } : {}),
    ...(chainTrace ? { chainTrace } : {}),
    ...(modelResolution ? { modelResolution } : {}),
    ...(requestContext ? { requestContext } : {}),
  }
}

function messageRequestContextFromMetadata(value: unknown): ChatMessageRequestContextRecord | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const knowledgeCollectionIds = (value as { knowledgeCollectionIds?: unknown }).knowledgeCollectionIds
  if (!Array.isArray(knowledgeCollectionIds)) return undefined
  const ids = knowledgeCollectionIds.filter((id): id is string => (
    typeof id === 'string' && id.trim().length > 0
  ))
  return ids.length > 0 ? { knowledgeCollectionIds: ids } : undefined
}

/** One server group -> its local record. */
export function groupRecordFromServer(group: ServerChatThreadGroup): ChatThreadGroupRecord {
  return {
    createdAt: isoFromUnixSeconds(group.created_at),
    id: group.id,
    title: group.title,
    updatedAt: isoFromUnixSeconds(group.updated_at),
  }
}

/** A thread record + its group membership -> the server PUT body. */
export function serverThreadPayload(
  record: ChatThreadRecord,
  groupId: string | null,
): {
  created_at: number
  group_id: string | null
  preview: string
  source: string
  title: string
  title_revision?: number
  title_source?: import('@/features/sessionTitle/model').TitleSource
  updated_at: number
  model_selection: string
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    group_id: groupId,
    preview: record.preview,
    source: record.source,
    ...titleWire(record),
    updated_at: unixSecondsFromIso(record.updatedAt),
    // Whole-row PUT: the field must ride every save or the server resets
    // the stored pick to '' on the next unrelated title/preview save.
    model_selection: record.modelSelection
      ? JSON.stringify(record.modelSelection)
      : '',
  }
}

/** Change-detection key for the thread's pick (rides updatedAt in the
 * fingerprint, but comparisons in tests use this directly). */
export function threadModelSelectionKey(record: ChatThreadRecord): string {
  return agentModelSelectionKey(record.modelSelection)
}

/** A message record -> the server append body, packing the optional
 * fields back into ``metadata`` verbatim (round-trip fidelity). */
export function serverMessagePayload(message: ChatMessageRecord): {
  content_markdown: string
  created_at: number
  id: string
  metadata: Record<string, unknown>
  role: string
} {
  const metadata: Record<string, unknown> = {}
  if (message.attachments) metadata.attachments = message.attachments
  if (message.chainTrace) metadata.chainTrace = message.chainTrace
  if (message.modelResolution) metadata.modelResolution = message.modelResolution
  if (message.requestContext) metadata.requestContext = message.requestContext
  return {
    content_markdown: message.contentMarkdown,
    created_at: unixSecondsFromIso(message.createdAt),
    id: message.id,
    metadata,
    role: message.role,
  }
}

/** Lazy message hydration is only authoritative for a thread the server has
 * already listed or accepted. A newly-created local empty thread is saved by
 * the autosave path first; reading it before that PUT would turn the expected
 * not-yet-present state into a visible 404 sync error. */
export function shouldLoadServerChatMessages(
  thread: Pick<ChatThreadRecord, 'messages'> | undefined,
  serverThreadKnown: boolean,
  alreadyLoaded: boolean,
): boolean {
  return Boolean(
    thread
    && serverThreadKnown
    && !alreadyLoaded
    && thread.messages.length === 0,
  )
}

/** A group record -> the server PUT body. */
export function serverGroupPayload(group: ChatThreadGroupRecord): {
  created_at: number
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(group.createdAt),
    title: group.title,
    updated_at: unixSecondsFromIso(group.updatedAt),
  }
}

/**
 * Per-thread sync fingerprint for the autosave diff. A thread needs a
 * server write when its fingerprint changed since the last successful
 * sync. ``updatedAt`` advances on EVERY chat mutation that touches the
 * thread (the reducer stamps it on rename/clear/message-add/edit/delete),
 * so it alone catches content changes; ``groupId`` is tracked separately
 * because moving a thread between groups changes its membership without
 * necessarily bumping ``updatedAt``. The server-pushed message hydration
 * (load-on-open) deliberately does NOT change ``updatedAt``, so filling a
 * thread's messages from the server never reads back as a local change.
 */
export type ThreadFingerprint = {
  groupId: string | null
  updatedAt: string
  titleState?: string
}

export function fingerprintThread(
  record: ChatThreadRecord,
  groupId: string | null,
): ThreadFingerprint {
  return { groupId, updatedAt: record.updatedAt, titleState: JSON.stringify(titleWire(record)) }
}

export function threadNeedsSync(
  previous: ThreadFingerprint | undefined,
  current: ThreadFingerprint,
): boolean {
  return (
    previous === undefined ||
    previous.updatedAt !== current.updatedAt ||
    previous.titleState !== current.titleState ||
    previous.groupId !== current.groupId
  )
}

/**
 * Which message ids the server still holds but the local thread no longer
 * does — the per-message counterpart to syncCollection's "synced minus
 * current" delete detection, applied INSIDE a thread. The append push only
 * upserts, so a locally-removed message survives on the server until it is
 * deleted by id; this diff names exactly those ids.
 *
 * ``knownServerIds`` is the per-thread baseline the sync hook seeds on
 * load-on-open and advances on every push. When it is ``undefined`` the
 * server-side message set is unknown (a thread whose messages were never
 * loaded — e.g. a metadata-only rename of an un-opened thread), so the
 * function returns ``[]``: deleting against an unknown baseline would risk
 * dropping messages the client simply has not fetched.
 */
export function messageIdsToDelete(
  knownServerIds: ReadonlySet<string> | undefined,
  currentMessages: readonly ChatMessageRecord[],
): string[] {
  if (knownServerIds === undefined) return []
  const currentIds = new Set(currentMessages.map((message) => message.id))
  return [...knownServerIds].filter((id) => !currentIds.has(id))
}

/**
 * Whether a message push must first learn the server's current message ids.
 *
 * Unknown baseline + local messages is the destructive-retry danger zone:
 * appending the local replacement without first fetching the server set would
 * make old server-only tail messages invisible to delete detection.
 */
export function shouldFetchMessageBaselineBeforePush(
  knownServerIds: ReadonlySet<string> | undefined,
  currentMessages: readonly ChatMessageRecord[],
): boolean {
  return knownServerIds === undefined && currentMessages.length > 0
}

/**
 * Pushes one thread's messages from an autosave snapshot: deletes the ids the
 * server held when the pass took its snapshot and the snapshot no longer has,
 * then upserts the snapshot's messages. ``knownAtSnapshot`` must come from the
 * same moment as ``messages`` (see {@link syncChatThreads}); a baseline that a
 * load-on-open seeded later describes messages this snapshot never saw.
 *
 * Returns the baseline to record after the push, or ``undefined`` when the
 * server set is still unknown (an unopened thread without local messages), so
 * a baseline a load seeds meanwhile keeps its value.
 */
export async function pushThreadMessages({
  appendMessages,
  deleteMessage,
  fetchBaseline,
  knownAtSnapshot,
  messages,
}: {
  appendMessages: (messages: readonly ChatMessageRecord[]) => Promise<void>
  deleteMessage: (id: string) => Promise<void>
  fetchBaseline: () => Promise<ReadonlySet<string>>
  knownAtSnapshot: ReadonlySet<string> | undefined
  messages: readonly ChatMessageRecord[]
}): Promise<Set<string> | undefined> {
  let known = knownAtSnapshot
  if (shouldFetchMessageBaselineBeforePush(known, messages)) {
    known = await fetchBaseline()
  }
  for (const id of messageIdsToDelete(known, messages)) {
    await deleteMessage(id)
  }
  if (messages.length > 0) await appendMessages(messages)
  if (known || messages.length > 0) {
    return new Set(messages.map((message) => message.id))
  }
  return undefined
}

/**
 * One autosave pass over the chat threads. The per-thread message baselines
 * are copied together with the thread snapshot, the same rule
 * {@link syncCollection} applies to the thread baseline: each thread's push
 * compares its snapshot messages with what the server held at that moment,
 * never with a baseline a load-on-open seeded while the pass waited on a
 * request.
 */
export async function syncChatThreads({
  deleteThread,
  exclude,
  fingerprintOf,
  inScope,
  pushThread,
  syncedMessages,
  syncedThreads,
  threads,
}: {
  deleteThread: (id: string) => Promise<void>
  exclude?: ReadonlySet<string>
  fingerprintOf: (thread: ChatThreadRecord) => ThreadFingerprint
  inScope?: () => boolean
  pushThread: (
    thread: ChatThreadRecord,
    knownMessageIds: ReadonlySet<string> | undefined,
  ) => Promise<void>
  syncedMessages: ReadonlyMap<string, ReadonlySet<string>>
  syncedThreads: Map<string, ThreadFingerprint>
  threads: Record<string, ChatThreadRecord>
}): Promise<void> {
  const knownMessages = new Map(syncedMessages)
  await syncCollection<ChatThreadRecord, ThreadFingerprint>({
    changed: threadNeedsSync,
    current: threads,
    deleteOne: deleteThread,
    exclude,
    fingerprintOf,
    inScope,
    pushOne: (thread) => pushThread(thread, knownMessages.get(thread.id)),
    synced: syncedThreads,
  })
}

/** Push ALL of a local project's chat entities to the server (the one-time
 * import). Groups first, then threads (each with its messages). Idempotent
 * server upserts make a re-run safe; the per-entity sync hooks then hydrate
 * the pushed data and quiesce. */
export async function pushAllChatEntities(
  args: {
    threads: Record<string, ChatThreadRecord>
    groups: Record<string, ChatThreadGroupRecord>
    memberships: Record<string, string | null>
  },
  options: ClientOptions,
): Promise<void> {
  for (const group of Object.values(args.groups)) {
    await saveChatThreadGroup(group.id, serverGroupPayload(group), options)
  }
  for (const thread of Object.values(args.threads)) {
    await saveChatThread(
      thread.id,
      serverThreadPayload(thread, args.memberships[thread.id] ?? null),
      options,
    )
    if (thread.messages.length > 0) {
      await appendChatMessages(
        thread.id,
        thread.messages.map(serverMessagePayload),
        options,
      )
    }
  }
}
