import { describe, expect, it } from 'vitest'

import {
  DEFAULT_PANEL_LAYOUT,
  PANEL_LAYOUT_LIMITS,
  clampPanelLayoutSize,
  normalizePanelLayout,
} from './panelLayout'

describe('panel layout state', () => {
  it('provides stable defaults for legacy projects without persisted panel sizes', () => {
    expect(normalizePanelLayout(undefined)).toEqual(DEFAULT_PANEL_LAYOUT)
    expect(normalizePanelLayout({})).toEqual(DEFAULT_PANEL_LAYOUT)
  })

  it('clamps persisted sizes to the supported per-panel ranges', () => {
    expect(clampPanelLayoutSize('chatHistory', 8)).toBe(18)
    expect(clampPanelLayoutSize('chatHistory', 50)).toBe(42)
    expect(clampPanelLayoutSize('knowledgeSource', 20)).toBe(28)
    expect(clampPanelLayoutSize('knowledgeSource', 52)).toBe(48)
    expect(clampPanelLayoutSize('researchReport', Number.NaN)).toBe(42)
    expect(clampPanelLayoutSize('editorTree', 8)).toBe(16)
    expect(clampPanelLayoutSize('editorTree', 50)).toBe(34)
    expect(clampPanelLayoutSize('editorComments', 12)).toBe(20)
    expect(clampPanelLayoutSize('editorComments', 60)).toBe(38)
  })

  it('keeps valid imported sizes and repairs missing or invalid keys', () => {
    expect(normalizePanelLayout({
      chatHistory: 31,
      knowledgeHistory: 'wide',
      knowledgeSource: 99,
      researchReport: 38,
    })).toEqual({
      chatHistory: 31,
      knowledgeHistory: DEFAULT_PANEL_LAYOUT.knowledgeHistory,
      knowledgeSource: 48,
      researchReport: 38,
      agentSessions: DEFAULT_PANEL_LAYOUT.agentSessions,
      agentCanvas: DEFAULT_PANEL_LAYOUT.agentCanvas,
      editorTree: DEFAULT_PANEL_LAYOUT.editorTree,
      editorComments: DEFAULT_PANEL_LAYOUT.editorComments,
    })
  })
})

describe('editor panel default widths', () => {
  it('gives the comments panel pixel parity with the document tree', () => {
    // The two defaults are percentages of NESTED bases: the tree takes its
    // share of the FULL row, the comments panel only of what remains next to
    // it. A naive "26 > 22" therefore rendered the right panel ~30px
    // NARROWER than the left one. This contract pins the effective full-row
    // share of the comments panel to the tree's share, so nobody re-tunes
    // one number against the wrong base again.
    const tree = PANEL_LAYOUT_LIMITS.editorTree.defaultSize
    const comments = PANEL_LAYOUT_LIMITS.editorComments.defaultSize
    const effectiveCommentsShare = (comments * (100 - tree)) / 100
    expect(Math.abs(effectiveCommentsShare - tree)).toBeLessThanOrEqual(1)
  })

  it('keeps every remembered size untouched by the default change', () => {
    // The width memory writes user values through the clamp; the default
    // only seeds layouts that were never touched. Values across the band
    // must pass through unchanged.
    for (const value of [20, 26, 31.5, 38]) {
      expect(clampPanelLayoutSize('editorComments', value)).toBe(value)
    }
  })
})
