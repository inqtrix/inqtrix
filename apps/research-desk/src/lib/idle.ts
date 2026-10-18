/**
 * Schedule non-urgent work in a browser idle slot, with a timeout so it
 * still runs on a busy main thread and a `setTimeout` fallback for
 * engines without `requestIdleCallback` (Safari). Returns the cleanup
 * for effect teardown — the ONE idle shim; callers must not
 * re-implement the `requestIdleCallback` feature detection inline.
 */
export function scheduleIdle(
  callback: () => void,
  options: { timeout: number },
): () => void {
  const idleWindow = window as Window & {
    cancelIdleCallback?: (handle: number) => void
    requestIdleCallback?: (
      run: () => void,
      opts?: { timeout?: number },
    ) => number
  }
  if (idleWindow.requestIdleCallback) {
    const handle = idleWindow.requestIdleCallback(callback, options)
    return () => idleWindow.cancelIdleCallback?.(handle)
  }
  const handle = window.setTimeout(callback, 0)
  return () => window.clearTimeout(handle)
}
