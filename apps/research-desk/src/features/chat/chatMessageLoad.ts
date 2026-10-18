export type SharedChatMessageLoad = {
  promise: Promise<void>
  surfaceErrors: boolean
}

type SelectedChatMessageLoadStateInput = {
  errorByThreadId: ReadonlyMap<string, string>
  expectsServerMessages: boolean
  resolvedThreadIds: ReadonlySet<string>
  selectedThreadId: string | null
}

export function updateChatMessageLoadError(
  current: ReadonlyMap<string, string>,
  threadId: string,
  error: string | null,
): ReadonlyMap<string, string> {
  if (error === null && !current.has(threadId)) return current
  if (error !== null && current.get(threadId) === error) return current

  const next = new Map(current)
  if (error === null) next.delete(threadId)
  else next.set(threadId, error)
  return next
}

/** Whether continuing a thread has to wait for its server history: the server
 * listed it, nothing of it is loaded locally, and its load has neither
 * finished nor failed. A message sent before that would reach the model
 * without the conversation and enter the autosave as if it were the whole
 * thread. */
export function chatThreadHistoryPending({
  failed,
  localMessageCount,
  resolved,
  serverThreadKnown,
}: {
  failed: boolean
  localMessageCount: number
  resolved: boolean
  serverThreadKnown: boolean
}): boolean {
  return serverThreadKnown && localMessageCount === 0 && !resolved && !failed
}

/** A failed selected load is terminal but distinct from an authoritative empty
 * payload. Removing that error for retry re-arms loading until success. */
export function selectedChatMessageLoadState({
  errorByThreadId,
  expectsServerMessages,
  resolvedThreadIds,
  selectedThreadId,
}: SelectedChatMessageLoadStateInput): {
  error: string | null
  loading: boolean
} {
  const error = selectedThreadId
    ? errorByThreadId.get(selectedThreadId) ?? null
    : null
  return {
    error,
    loading: Boolean(
      selectedThreadId
      && expectsServerMessages
      && !resolvedThreadIds.has(selectedThreadId)
      && error === null,
    ),
  }
}

/**
 * Share one message-hydration request across background and selected-thread
 * callers. A selected load can promote an already-running silent prefetch so
 * the eventual result is handled with visible error semantics.
 */
export function shareChatMessageLoad(
  loads: Map<string, SharedChatMessageLoad>,
  threadId: string,
  surfaceErrors: boolean,
  start: (load: SharedChatMessageLoad) => Promise<void>,
): Promise<void> {
  const existing = loads.get(threadId)
  if (existing) {
    if (surfaceErrors) existing.surfaceErrors = true
    return existing.promise
  }

  const load: SharedChatMessageLoad = {
    promise: Promise.resolve(),
    surfaceErrors,
  }
  load.promise = Promise.resolve()
    .then(() => start(load))
    .finally(() => {
      if (loads.get(threadId) === load) loads.delete(threadId)
    })
  loads.set(threadId, load)
  return load.promise
}
