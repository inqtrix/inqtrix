/**
 * One lane for the server mutations of a single collection.
 *
 * The chat history has two writers: the debounced autosave, which pushes a
 * thread with an UPSERT, and the click-path deletion. Left unsynchronized,
 * an autosave that started before a deletion can land after it and re-create
 * the row it just removed — the deletion looks done, and the next reload
 * brings the conversation back. Routing both through this lane makes the
 * last request the user asked for also the last one the server sees.
 *
 * Tasks run in submission order and never overlap. A rejecting task settles
 * the lane like any other, so one failure never stalls the queue, and its
 * rejection still reaches its own caller.
 */
export type MutationLane = {
  run: <Result>(task: () => Promise<Result>) => Promise<Result>
}

export function createMutationLane(): MutationLane {
  let tail: Promise<unknown> = Promise.resolve()
  return {
    run: (task) => {
      const result = tail.then(task)
      // The lane advances on a SETTLED tail, never on the caller's promise:
      // a task that rejects would otherwise leave the tail rejected and every
      // later task would be skipped instead of run. The rejection still
      // reaches its own caller through `result`.
      tail = result.catch(() => undefined)
      return result
    },
  }
}
