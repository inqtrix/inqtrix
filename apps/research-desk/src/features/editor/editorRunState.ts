/**
 * One run-state map per scope (comment runs, suggestion runs) tracks which ids
 * are running and which carry an error. A given id is in exactly one state at a
 * time, and the map makes that property explicit. The hook derives the
 * `runningIds`/`errors` shapes its consumers read from these maps.
 */
export type EditorRunStatus = 'running' | 'error'

export type EditorRunStateMap = Record<string, { error?: string; status: EditorRunStatus }>

/** Mark an id as running, clearing any prior error for that id. */
export function markRunning(map: EditorRunStateMap, id: string): EditorRunStateMap {
  return { ...map, [id]: { status: 'running' } }
}

/** Mark several ids as running at once (used by a global run over comments). */
export function markManyRunning(map: EditorRunStateMap, ids: Iterable<string>): EditorRunStateMap {
  const next = { ...map }
  for (const id of ids) next[id] = { status: 'running' }
  return next
}

/** Replace an id's running state with a visible error. */
export function markError(map: EditorRunStateMap, id: string, error: string): EditorRunStateMap {
  return { ...map, [id]: { error, status: 'error' } }
}

/** Merge a batch of id -> error pairs (used when a global run settles). */
export function markErrors(map: EditorRunStateMap, errors: Record<string, string>): EditorRunStateMap {
  const next = { ...map }
  for (const [id, error] of Object.entries(errors)) next[id] = { error, status: 'error' }
  return next
}

/** Drop the given ids from the map entirely (no longer running, no error). */
export function clearRuns(map: EditorRunStateMap, ids: Iterable<string>): EditorRunStateMap {
  let changed = false
  const next = { ...map }
  for (const id of ids) {
    if (id in next) {
      delete next[id]
      changed = true
    }
  }
  return changed ? next : map
}

/**
 * Drop only the ids that are currently running, keeping any errored ids. This is
 * the "a run finished" update: success leaves nothing, a failure that already
 * called {@link markError} keeps its error visible.
 */
export function clearRunning(map: EditorRunStateMap, ids: Iterable<string>): EditorRunStateMap {
  let changed = false
  const next = { ...map }
  for (const id of ids) {
    if (next[id]?.status === 'running') {
      delete next[id]
      changed = true
    }
  }
  return changed ? next : map
}

/** The ids currently running, in insertion order. */
export function runningIds(map: EditorRunStateMap): string[] {
  return Object.keys(map).filter((id) => map[id].status === 'running')
}

/** The id -> message record for ids that hold an error. */
export function runErrors(map: EditorRunStateMap): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const [id, value] of Object.entries(map)) {
    if (value.status === 'error' && value.error) errors[id] = value.error
  }
  return errors
}
