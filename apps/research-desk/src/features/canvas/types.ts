/**
 * Agent-agnostic canvas view host types (tab model).
 *
 * The canvas is ONE polymorphic right-hand panel holding a row of TABS:
 * user-opened tabs are pinned (stable), and at most ONE unpinned
 * "preview" tab exists — the slot agent-driven follow writes into (the
 * VS-Code preview-tab pattern), so a working agent never sprawls tabs.
 * Descriptors are plain serializable data — the registry maps `view` to
 * a renderer, the host knows no feature types. v1 registers only Agent
 * Desk views, but nothing in this module may import from
 * `features/agent`.
 */

export type CanvasViewDescriptor =
  | { view: 'document'; runId: string; artifactId: string }
  | { view: 'plan'; runId: string; version?: number }
  | {
      view: 'run'
      runId: string
      taskId?: string
      /** Task button that regains focus after the nested detail closes. */
      focusTaskId?: string
    }
  | {
      view: 'evidence'
      runId: string
      /** Evidence tabs bind to the immutable artifact ledger entry, not a
       * copied URL/chunk payload, so enriched provenance survives navigation. */
      artifactId: string
      label: string
    }
  | { view: 'file'; assetId?: string; documentId?: string }
  | {
      view: 'diff'
      runId: string
      artifactId: string
      fromRevision: number
      toRevision: number
    }
  | { view: 'patch'; runId: string; patchId: string }

export type CanvasViewKind = CanvasViewDescriptor['view']

/** Who asked for a view — agent-sourced opens obey follow/pin rules. */
export type CanvasOpenSource = 'agent' | 'user'

/**
 * One open tab. `pinned` tabs are user-owned (agent follow never
 * replaces their content); the single unpinned tab is the preview slot.
 */
export type CanvasTab = {
  /** Stable identity (see :func:`canvasTabKey`) — reopening the same
   * key focuses the existing tab instead of duplicating it. */
  key: string
  descriptor: CanvasViewDescriptor
  pinned: boolean
}

/**
 * The canvas slice: a tab row plus the active tab. `pinned` freezes
 * follow-mode (manual navigation pins; un-pin jumps back to the agent's
 * target). Ephemeral state — never serialized, dies with the tab.
 */
export type CanvasState = {
  open: boolean
  focus: boolean
  pinned: boolean
  tabs: CanvasTab[]
  /** `key` of the visible tab; `null` only while `tabs` is empty. */
  activeTabId: string | null
  /** Set once the first artifact auto-open fired (anti auto-open rule). */
  autoOpened: boolean
}

export const EMPTY_CANVAS_STATE: CanvasState = {
  open: false,
  focus: false,
  pinned: false,
  tabs: [],
  activeTabId: null,
  autoOpened: false,
}

/**
 * Stable tab identity per descriptor. Deliberately coarser than value
 * equality where one tab should absorb variants: a plan's versions
 * share one tab (the view owns version navigation). A run and its task
 * drill-down also share one tab; the task is a nested view with Back, not a
 * sibling document. Documents and patches remain distinct per entity.
 */
export function canvasTabKey(descriptor: CanvasViewDescriptor): string {
  switch (descriptor.view) {
    case 'plan':
      return `plan:${descriptor.runId}`
    case 'run':
      return `run:${descriptor.runId}`
    case 'document':
      // The artifact IS the document's identity: its run anchor
      // moves to the newest updating run, so a runId in the key would
      // split one document across tabs and dead-end the old one. The
      // descriptor's runId stays as a resolution HINT only.
      return `document:${descriptor.artifactId}`
    case 'patch':
      return `patch:${descriptor.runId}:${descriptor.patchId}`
    case 'evidence':
      return `evidence:${descriptor.runId}:${descriptor.artifactId}:${descriptor.label}`
    case 'file':
      return `file:${descriptor.assetId ?? ''}:${descriptor.documentId ?? ''}`
    case 'diff':
      return (
        `diff:${descriptor.runId}:${descriptor.artifactId}:`
        + `${descriptor.fromRevision}-${descriptor.toRevision}`
      )
  }
}

/** The view currently visible (the active tab's descriptor). */
export function activeCanvasView(
  state: CanvasState,
): CanvasViewDescriptor | null {
  if (state.activeTabId === null) return null
  return (
    state.tabs.find((tab) => tab.key === state.activeTabId)?.descriptor
    ?? null
  )
}

/** Value equality for descriptors (avoids re-push of the identical view). */
export function sameCanvasView(
  a: CanvasViewDescriptor | null,
  b: CanvasViewDescriptor | null,
): boolean {
  if (a === b) return true
  if (!a || !b) return false
  return JSON.stringify(a) === JSON.stringify(b)
}
