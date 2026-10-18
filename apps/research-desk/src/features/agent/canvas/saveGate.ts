/** How long the canvas waits after the last change before it saves. */
export const CANVAS_SAVE_DEBOUNCE_MS = 900

/**
 * The body the server holds for one canvas once every submitted save has
 * landed: the loaded body until a save is submitted, then the last
 * submitted one. A change back to it is no change.
 *
 * A server body that arrives while a save is pending or in flight says
 * nothing about that save (the reload may predate it), so it is ignored; a
 * save that did not land leaves the held body unknown, and every change
 * counts again until the next server body arrives.
 */
export type CanvasSaveGate = {
  /** Whether *markdown* is the body the server holds or is about to hold. */
  holds: (markdown: string) => boolean
  /** A body the server delivered; `savePending` when an edit waits unsent. */
  observeServerBody: (markdown: string, savePending: boolean) => void
  /** The save of *markdown* ended; `landed` when the server stored it. */
  settle: (markdown: string, landed: boolean) => void
  /** A save of *markdown* is submitted. */
  submit: (markdown: string) => void
}

export function createCanvasSaveGate(): CanvasSaveGate {
  let held: string | null = null
  let inFlight = 0
  return {
    holds: (markdown) => held !== null && markdown === held,
    observeServerBody(markdown, savePending) {
      if (savePending || inFlight > 0) return
      held = markdown
    },
    settle(markdown, landed) {
      inFlight -= 1
      if (!landed && held === markdown) held = null
    },
    submit(markdown) {
      held = markdown
      inFlight += 1
    },
  }
}
