import { describe, expect, it } from 'vitest'

import {
  activateCanvasTab,
  closeCanvasTab,
  openCanvasTab,
  pinCanvasTab,
} from './tabs'
import {
  EMPTY_CANVAS_STATE,
  canvasTabKey,
  type CanvasState,
  type CanvasViewDescriptor,
} from './types'

const RUN: CanvasViewDescriptor = { runId: 'r1', view: 'run' }
const NEXT_RUN: CanvasViewDescriptor = { runId: 'r2', view: 'run' }
const DOC: CanvasViewDescriptor = {
  artifactId: 'a1',
  runId: 'r1',
  view: 'document',
}
const EVIDENCE_W1: CanvasViewDescriptor = {
  artifactId: 'a1',
  label: 'W1',
  runId: 'r1',
  view: 'evidence',
}

const OPEN: CanvasState = { ...EMPTY_CANVAS_STATE, open: true }

describe('openCanvasTab (user)', () => {
  it('keys evidence by artifact and label', () => {
    expect(canvasTabKey(EVIDENCE_W1)).toBe('evidence:r1:a1:W1')
    expect(canvasTabKey({ ...EVIDENCE_W1, label: 'W2' })).not.toBe(
      canvasTabKey(EVIDENCE_W1),
    )
  })

  it('keys a document by its artifact alone (anchors move)', () => {
    // The same session document under a NEW run anchor must land in the
    // SAME tab: an artifact update re-parents its run_id server-side,
    // and a runId in the key split one document across dead tabs.
    expect(canvasTabKey(DOC)).toBe('document:a1')
    expect(canvasTabKey({ ...DOC, runId: 'r2' })).toBe(canvasTabKey(DOC))
    const first = openCanvasTab(EMPTY_CANVAS_STATE, DOC, 'user')
    const rerouted = openCanvasTab(first, { ...DOC, runId: 'r2' }, 'user')
    expect(rerouted.tabs).toHaveLength(1)
  })

  it('opens a new pinned tab, focuses it and pins follow-mode', () => {
    const state = openCanvasTab(EMPTY_CANVAS_STATE, NEXT_RUN, 'user')
    expect(state.open).toBe(true)
    expect(state.pinned).toBe(true)
    expect(state.tabs).toEqual([
      { descriptor: NEXT_RUN, key: canvasTabKey(NEXT_RUN), pinned: true },
    ])
    expect(state.activeTabId).toBe(canvasTabKey(NEXT_RUN))
  })

  it('focuses an existing key instead of duplicating the tab', () => {
    const first = openCanvasTab(EMPTY_CANVAS_STATE, NEXT_RUN, 'user')
    const second = openCanvasTab(first, RUN, 'user')
    const refocused = openCanvasTab(second, NEXT_RUN, 'user')
    expect(refocused.tabs).toHaveLength(2)
    expect(refocused.activeTabId).toBe(canvasTabKey(NEXT_RUN))
  })

  it('claims the preview tab as pinned when opening its key', () => {
    const preview = openCanvasTab(OPEN, RUN, 'agent')
    expect(preview.tabs[0].pinned).toBe(false)
    const claimed = openCanvasTab(preview, RUN, 'user')
    expect(claimed.tabs).toHaveLength(1)
    expect(claimed.tabs[0].pinned).toBe(true)
  })

  it('keys a run by its id alone: one tab per run', () => {
    expect(canvasTabKey(RUN)).toBe('run:r1')
    expect(canvasTabKey(NEXT_RUN)).toBe('run:r2')
  })
})

describe('openCanvasTab (agent, follow rules)', () => {
  it('is ignored while follow is pinned', () => {
    const pinned = { ...OPEN, pinned: true }
    expect(openCanvasTab(pinned, RUN, 'agent')).toBe(pinned)
  })

  it('never opens a closed canvas except the one document auto-open', () => {
    expect(openCanvasTab(EMPTY_CANVAS_STATE, RUN, 'agent')).toBe(
      EMPTY_CANVAS_STATE,
    )
    const opened = openCanvasTab(EMPTY_CANVAS_STATE, DOC, 'agent')
    expect(opened.open).toBe(true)
    expect(opened.autoOpened).toBe(true)
    // The auto-open is once-only.
    const closedAgain = { ...opened, open: false }
    expect(openCanvasTab(closedAgain, DOC, 'agent')).toBe(closedAgain)
  })

  it('drives ONE preview slot: replaces it instead of sprawling tabs', () => {
    const one = openCanvasTab(OPEN, RUN, 'agent')
    const two = openCanvasTab(one, NEXT_RUN, 'agent')
    expect(two.tabs).toHaveLength(1)
    expect(two.tabs[0]).toEqual({
      descriptor: NEXT_RUN,
      key: canvasTabKey(NEXT_RUN),
      pinned: false,
    })
    expect(two.activeTabId).toBe(canvasTabKey(NEXT_RUN))
  })

  it('keeps pinned tabs intact and appends the preview beside them', () => {
    // Manual navigation pins follow; the user re-enables it (unpin chip)
    // before the agent may drive the preview slot again.
    const user = openCanvasTab(OPEN, DOC, 'user')
    const following = { ...user, pinned: false }
    const withPreview = openCanvasTab(following, RUN, 'agent')
    expect(withPreview.tabs.map((tab) => tab.pinned)).toEqual([true, false])
    const swapped = openCanvasTab(withPreview, NEXT_RUN, 'agent')
    expect(swapped.tabs.map((tab) => tab.key)).toEqual([
      canvasTabKey(DOC),
      canvasTabKey(NEXT_RUN),
    ])
  })

  it('re-opens a closed canvas onto the pinned tab that already holds the document (no duplicate key)', () => {
    // The user opens the memo (pinned tab) and closes the panel; the
    // agent's next write must not re-open the canvas with a SECOND tab of
    // the same key — both would render active and close together.
    const held = openCanvasTab(EMPTY_CANVAS_STATE, DOC, 'user')
    const closed = { ...held, open: false, pinned: false }
    const next = openCanvasTab(closed, DOC, 'agent')
    expect(next.open).toBe(true)
    expect(next.tabs.map((tab) => tab.key)).toEqual(['document:a1'])
    expect(next.tabs[0].pinned).toBe(true)
    expect(next.activeTabId).toBe('document:a1')
  })

  it('focuses an existing pinned tab with the same key (no duplicate)', () => {
    const user = openCanvasTab(OPEN, NEXT_RUN, 'user')
    const unpinnedFollow = { ...user, pinned: false }
    const followed = openCanvasTab(unpinnedFollow, NEXT_RUN, 'agent')
    expect(followed.tabs).toHaveLength(1)
    expect(followed.tabs[0].pinned).toBe(true)
  })
})

describe('activate/close/pin', () => {
  it('activating a tab pins follow-mode but not the tab itself', () => {
    const preview = openCanvasTab(OPEN, RUN, 'agent')
    const activated = activateCanvasTab(preview, canvasTabKey(RUN))
    expect(activated.pinned).toBe(true)
    expect(activated.tabs[0].pinned).toBe(false)
    expect(activateCanvasTab(preview, 'missing')).toBe(preview)
  })

  it('closing the active tab activates the right neighbor, else the last', () => {
    let state = openCanvasTab(OPEN, NEXT_RUN, 'user')
    state = openCanvasTab(state, RUN, 'user')
    state = openCanvasTab(state, DOC, 'user')
    state = activateCanvasTab(state, canvasTabKey(RUN))
    const closed = closeCanvasTab(state, canvasTabKey(RUN))
    expect(closed.tabs.map((tab) => tab.key)).toEqual([
      canvasTabKey(NEXT_RUN),
      canvasTabKey(DOC),
    ])
    expect(closed.activeTabId).toBe(canvasTabKey(DOC))
    const closedLast = closeCanvasTab(closed, canvasTabKey(DOC))
    expect(closedLast.activeTabId).toBe(canvasTabKey(NEXT_RUN))
  })

  it('closing the last tab closes the panel', () => {
    const one = openCanvasTab(OPEN, NEXT_RUN, 'user')
    const closed = closeCanvasTab(one, canvasTabKey(NEXT_RUN))
    expect(closed.open).toBe(false)
    expect(closed.tabs).toEqual([])
    expect(closed.activeTabId).toBeNull()
    expect(closed.pinned).toBe(false)
  })

  it('pinCanvasTab claims the preview slot', () => {
    const preview = openCanvasTab(OPEN, RUN, 'agent')
    const pinned = pinCanvasTab(preview, canvasTabKey(RUN))
    expect(pinned.tabs[0].pinned).toBe(true)
    expect(pinCanvasTab(pinned, canvasTabKey(RUN))).toBe(pinned)
  })
})
