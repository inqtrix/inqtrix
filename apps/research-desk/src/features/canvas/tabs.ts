/**
 * Pure canvas-tab transitions, extracted from the
 * reducer so the invariants are unit-testable without ProjectState:
 *
 * - user opens create/focus PINNED tabs (manual navigation also pins
 *   follow-mode) and de-duplicate by tab key;
 * - agent opens write into the ONE unpinned preview tab (VS-Code
 *   preview pattern) and never fire while follow is pinned;
 * - a closed canvas only ever auto-opens once, for the first document
 *   (the anti auto-open rule) — everything else needs a user action;
 * - closing the last tab closes the panel; closing the active tab
 *   activates its right neighbor (else the new last tab).
 */

import {
  canvasTabKey,
  sameCanvasView,
  type CanvasOpenSource,
  type CanvasState,
  type CanvasTab,
  type CanvasViewDescriptor,
} from './types'

/**
 * Replace the single unpinned preview tab in place, else append one. A
 * tab that already holds the key (a pinned one the user opened before
 * the panel was closed) is refreshed instead: the key is the tab's
 * identity, and two tabs with one key would both render active and
 * close together.
 */
function upsertPreview(
  tabs: CanvasTab[],
  key: string,
  descriptor: CanvasViewDescriptor,
): CanvasTab[] {
  const held = tabs.findIndex((tab) => tab.key === key)
  if (held !== -1) {
    return tabs.map((tab, tabIndex) =>
      tabIndex === held ? { ...tab, descriptor } : tab,
    )
  }
  const preview: CanvasTab = { descriptor, key, pinned: false }
  const index = tabs.findIndex((tab) => !tab.pinned)
  if (index === -1) return [...tabs, preview]
  return tabs.map((tab, tabIndex) => (tabIndex === index ? preview : tab))
}

export function openCanvasTab(
  state: CanvasState,
  descriptor: CanvasViewDescriptor,
  source: CanvasOpenSource,
): CanvasState {
  const key = canvasTabKey(descriptor)
  if (source === 'agent') {
    // Agent-sourced targets obey the follow rules: ignored while
    // pinned; a CLOSED canvas opens exactly once, for the first
    // document view.
    if (state.pinned) return state
    if (!state.open) {
      if (descriptor.view !== 'document' || state.autoOpened) return state
      return {
        ...state,
        activeTabId: key,
        autoOpened: true,
        open: true,
        tabs: upsertPreview(state.tabs, key, descriptor),
      }
    }
    const existing = state.tabs.find((tab) => tab.key === key)
    if (existing) {
      if (
        state.activeTabId === key
        && sameCanvasView(existing.descriptor, descriptor)
      ) {
        return state
      }
      return {
        ...state,
        activeTabId: key,
        tabs: state.tabs.map((tab) =>
          tab.key === key ? { ...tab, descriptor } : tab,
        ),
      }
    }
    return {
      ...state,
      activeTabId: key,
      tabs: upsertPreview(state.tabs, key, descriptor),
    }
  }
  // Manual navigation always opens and PINS follow; an
  // existing tab is focused (and claimed as pinned), never duplicated.
  const existing = state.tabs.find((tab) => tab.key === key)
  if (existing) {
    const unchanged =
      existing.pinned && sameCanvasView(existing.descriptor, descriptor)
    const tabs = unchanged
      ? state.tabs
      : state.tabs.map((tab) =>
        tab.key === key ? { ...tab, descriptor, pinned: true } : tab,
      )
    if (
      unchanged
      && state.open
      && state.pinned
      && state.activeTabId === key
    ) {
      return state
    }
    return { ...state, activeTabId: key, open: true, pinned: true, tabs }
  }
  return {
    ...state,
    activeTabId: key,
    open: true,
    pinned: true,
    tabs: [...state.tabs, { descriptor, key, pinned: true }],
  }
}

export function activateCanvasTab(
  state: CanvasState,
  key: string,
): CanvasState {
  if (!state.tabs.some((tab) => tab.key === key)) return state
  if (state.activeTabId === key && state.pinned) return state
  // Clicking a tab is manual navigation — it pins follow-mode but
  // leaves the tab's own preview/pinned nature untouched (an explicit
  // pin affordance claims a preview tab).
  return { ...state, activeTabId: key, pinned: true }
}

export function closeCanvasTab(
  state: CanvasState,
  key: string,
): CanvasState {
  const index = state.tabs.findIndex((tab) => tab.key === key)
  if (index === -1) return state
  const tabs = state.tabs.filter((tab) => tab.key !== key)
  if (tabs.length === 0) {
    return {
      ...state,
      activeTabId: null,
      focus: false,
      open: false,
      pinned: false,
      tabs,
    }
  }
  const activeTabId =
    state.activeTabId === key
      ? tabs[Math.min(index, tabs.length - 1)].key
      : state.activeTabId
  return { ...state, activeTabId, tabs }
}

export function pinCanvasTab(state: CanvasState, key: string): CanvasState {
  const tab = state.tabs.find((item) => item.key === key)
  if (!tab || tab.pinned) return state
  return {
    ...state,
    tabs: state.tabs.map((item) =>
      item.key === key ? { ...item, pinned: true } : item,
    ),
  }
}
