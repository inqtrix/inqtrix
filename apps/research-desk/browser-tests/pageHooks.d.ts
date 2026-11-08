/** Hooks the specs read from the page under test; each spec installs its own. */
export {}

type AxeResult = {
  violations: {
    help: string
    helpUrl: string
    id: string
    impact: string | null
    nodes: { failureSummary: string; target: string[] }[]
    tags: string[]
  }[]
}

declare global {
  interface Window {
    axe: { run(context?: unknown, options?: unknown): Promise<AxeResult> }
    __answerCanvasSaves?: () => void
    __bodyChanges?: string[]
    __bodyEditor?: {
      caretAtFirstBlockEnd(): boolean
      lastBlock(): string | null
      pasteText(text: string): boolean
      removeTrailingParagraph(): string
      storedBody: string
      tellEditability(): string
    }
    __canvasSaves?: { expectedRevision: number; markdown: string }[]
    __canvasStoredBody?: string
    __canvasViews?: unknown[]
    __collaborationRenderSnapshots?: Record<string, unknown>[]
    __commentScaleFirstInteractiveMs?: number
    __commentScaleSelectionMs?: number
    __requestPaths?: string[]
    __setCollaborationCanEdit?: (canEdit: boolean) => void
    __slashCollaborationServerUpdate?: number[]
    __slashCollaborationUpdate?: number[]
    __slashCollaborationUpdates?: number[][]
    __slashStructureUpdateDetections?: unknown[]
    __slashSuggestionBoundaryDetections?: unknown[]
  }
}
