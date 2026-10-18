/**
 * The Agent Desk composer around a session that is still staging.
 *
 * While a newly selected session stages, the header and transcript keep
 * showing the previous session (one identity on screen). The composer stays
 * usable so nothing typed is lost; only sending waits until the selected
 * session is the one shown, so a question never lands in a session the user
 * is not looking at.
 */
export function agentComposerAvailability({
  agentAvailable,
  runningRunLocked,
  surfaceTransitioning,
}: {
  agentAvailable: boolean
  runningRunLocked: boolean
  surfaceTransitioning: boolean
}): { disabled: boolean; sendWaits: boolean } {
  return {
    disabled: !agentAvailable || runningRunLocked,
    sendWaits: surfaceTransitioning,
  }
}

/** Start `start` unless an earlier start is still on its way; `onBusy` reports
 * the busy state so the trigger can show it and refuse a second click. */
export function startExclusive(
  flag: { current: boolean },
  start: () => Promise<unknown>,
  onBusy: (busy: boolean) => void,
): boolean {
  if (flag.current) return false
  flag.current = true
  onBusy(true)
  void start().finally(() => {
    flag.current = false
    onBusy(false)
  })
  return true
}

/** Key of the chips attached before any session exists (the first question
 * creates the session). */
const UNSAVED_SESSION = ''

const sessionKey = (sessionId: string | null) => sessionId ?? UNSAVED_SESSION

/** The collection chips attached in this session's composer. */
export function collectionIdsForSession(
  bySession: Readonly<Record<string, readonly string[]>>,
  sessionId: string | null,
): string[] {
  return [...(bySession[sessionKey(sessionId)] ?? [])]
}

export function withSessionCollectionIds(
  bySession: Readonly<Record<string, readonly string[]>>,
  sessionId: string | null,
  collectionIds: readonly string[],
): Record<string, string[]> {
  const next: Record<string, string[]> = {}
  for (const [key, ids] of Object.entries(bySession)) next[key] = [...ids]
  const key = sessionKey(sessionId)
  if (collectionIds.length > 0) next[key] = [...collectionIds]
  else delete next[key]
  return next
}

/** When the first question creates a session, its chips move with it. A
 * session that already has chips keeps its own. */
export function carryCollectionsToNewSession(
  bySession: Readonly<Record<string, readonly string[]>>,
  previousSessionId: string | null,
  nextSessionId: string | null,
): Record<string, string[]> {
  const unsaved = bySession[UNSAVED_SESSION]
  if (
    previousSessionId !== null
    || nextSessionId === null
    || !unsaved?.length
    || bySession[nextSessionId] !== undefined
  ) {
    return withSessionCollectionIds(bySession, null, bySession[UNSAVED_SESSION] ?? [])
  }
  const moved = withSessionCollectionIds(bySession, nextSessionId, unsaved)
  return withSessionCollectionIds(moved, null, [])
}
