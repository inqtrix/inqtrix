import type { ProjectPanelLayoutState } from './types'

export type ProjectPanelLayoutKey = keyof ProjectPanelLayoutState

type PanelLayoutLimit = {
  defaultSize: number
  max: number
  min: number
}

export const PANEL_LAYOUT_LIMITS: Record<ProjectPanelLayoutKey, PanelLayoutLimit> = {
  chatHistory: { defaultSize: 26, max: 42, min: 18 },
  knowledgeHistory: { defaultSize: 26, max: 42, min: 18 },
  knowledgeSource: { defaultSize: 36, max: 48, min: 28 },
  researchReport: { defaultSize: 42, max: 58, min: 26 },
  agentSessions: { defaultSize: 24, max: 42, min: 18 },
  agentCanvas: { defaultSize: 46, max: 62, min: 30 },
  editorTree: { defaultSize: 22, max: 34, min: 16 },
  // 28% of the REMAINING row (the tree already took its 22% of the full
  // width), which lands at pixel parity with the tree: 0.28 * 78 = 21.8% of
  // the full row. The bases are nested — never compare these two numbers
  // directly. Pinned by panelLayout.test.ts.
  editorComments: { defaultSize: 28, max: 38, min: 20 },
}

export const DEFAULT_PANEL_LAYOUT: ProjectPanelLayoutState = {
  chatHistory: PANEL_LAYOUT_LIMITS.chatHistory.defaultSize,
  knowledgeHistory: PANEL_LAYOUT_LIMITS.knowledgeHistory.defaultSize,
  knowledgeSource: PANEL_LAYOUT_LIMITS.knowledgeSource.defaultSize,
  researchReport: PANEL_LAYOUT_LIMITS.researchReport.defaultSize,
  agentSessions: PANEL_LAYOUT_LIMITS.agentSessions.defaultSize,
  agentCanvas: PANEL_LAYOUT_LIMITS.agentCanvas.defaultSize,
  editorTree: PANEL_LAYOUT_LIMITS.editorTree.defaultSize,
  editorComments: PANEL_LAYOUT_LIMITS.editorComments.defaultSize,
}

export function clampPanelLayoutSize(
  key: ProjectPanelLayoutKey,
  value: number,
): number {
  const limit = PANEL_LAYOUT_LIMITS[key]
  if (!Number.isFinite(value)) return limit.defaultSize
  return Math.min(limit.max, Math.max(limit.min, value))
}

export function normalizePanelLayout(value: unknown): ProjectPanelLayoutState {
  const record = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    chatHistory: clampPanelLayoutSize('chatHistory', numberOrDefault(record.chatHistory, DEFAULT_PANEL_LAYOUT.chatHistory)),
    knowledgeHistory: clampPanelLayoutSize('knowledgeHistory', numberOrDefault(record.knowledgeHistory, DEFAULT_PANEL_LAYOUT.knowledgeHistory)),
    knowledgeSource: clampPanelLayoutSize('knowledgeSource', numberOrDefault(record.knowledgeSource, DEFAULT_PANEL_LAYOUT.knowledgeSource)),
    researchReport: clampPanelLayoutSize('researchReport', numberOrDefault(record.researchReport, DEFAULT_PANEL_LAYOUT.researchReport)),
    agentSessions: clampPanelLayoutSize('agentSessions', numberOrDefault(record.agentSessions, DEFAULT_PANEL_LAYOUT.agentSessions)),
    agentCanvas: clampPanelLayoutSize('agentCanvas', numberOrDefault(record.agentCanvas, DEFAULT_PANEL_LAYOUT.agentCanvas)),
    editorTree: clampPanelLayoutSize('editorTree', numberOrDefault(record.editorTree, DEFAULT_PANEL_LAYOUT.editorTree)),
    editorComments: clampPanelLayoutSize('editorComments', numberOrDefault(record.editorComments, DEFAULT_PANEL_LAYOUT.editorComments)),
  }
}

function numberOrDefault(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}
