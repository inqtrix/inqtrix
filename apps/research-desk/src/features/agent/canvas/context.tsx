import { createContext, useContext } from 'react'

import type { ClientOptions } from '@/api/inqtrixClient'
import type { KnowledgeDataSource } from '@/features/knowledge/types'
import type { CanvasViewDescriptor } from '@/features/canvas/types'
import type { FileAssetRecord } from '@/features/project/types'
import type { AgentRunRecord } from '../model'
import type { AgentSessionArtifactIndex } from '../model'
import type { CanvasSaveRegistry } from './saveRegistry'
import type {
  ArtifactRenameResult,
  ArtifactSaveResult,
} from '../useAgentControlApi'

/**
 * Data plane of the agent canvas views. A React context (not per-render
 * closures) so the view registry stays a MODULE CONSTANT — otherwise every
 * workspace render would recreate the renderer component types and
 * remount the active view (killing the editor cursor).
 */
export type AgentCanvasContextValue = {
  clientOptions: ClientOptions | null
  /** The caller-visible knowledge collections (id -> display title): the
   * evidence view names a citation's collection by its title. */
  collections: { id: string; title: string }[]
  /** Knowledge document reader for the evidence view's knowledge
   * citations: the same reader the Knowledge Desk uses, so "verify the
   * source" is one experience across desks. */
  knowledgeDataSource: KnowledgeDataSource
  exportArtifact: (
    runId: string,
    artifactId: string,
    title?: string,
  ) => Promise<unknown>
  fileAssets: Record<string, FileAssetRecord>
  loadArtifact: (
    runId: string,
    artifactId: string,
    revision?: number,
  ) => Promise<{ content_markdown: string; revision: number }>
  openCanvasView: (descriptor: CanvasViewDescriptor) => void
  /** Pending canvas saves keyed by artifact; the composer flushes ALL
   * of them before any submit — including one that answers a gate. */
  saveRegistry: CanvasSaveRegistry
  /** Parent runs currently using the visible polling fallback. */
  pollingRunIds: readonly string[]
  runs: Record<string, AgentRunRecord>
  /** Anchor-independent artifact index per session. */
  sessionArtifacts: Record<string, AgentSessionArtifactIndex>
  /** Queue one selection comment for the next submission. Returns
   * false when the queue is at the server's comment bound — the caller
   * shows the visible limit hint instead of dropping silently. */
  queueCanvasComment: (
    draft: import('./commentQueue').AgentCanvasCommentDraft,
  ) => boolean
  /** The queued drafts: the document view highlights their anchors and
   * the edit round-trip resolves against them. */
  canvasComments: import('./commentQueue').AgentCanvasCommentDraft[]
  /** Replace one queued comment's text in place. */
  updateCanvasComment: (id: string, comment: string) => void
  /** Pending edit request from the composer's stacked rows: the
   * matching document view scrolls to the anchor, opens the popover
   * prefilled, and clears the request. */
  canvasCommentEdit:
    | import('./commentQueue').AgentCanvasCommentDraft
    | null
  clearCanvasCommentEdit: () => void
  saveArtifact: (
    runId: string,
    artifactId: string,
    contentMarkdown: string,
    expectedRevision: number,
  ) => Promise<ArtifactSaveResult>
  /** Metadata-only title change: revision stays, chips/tabs/registry
   * follow via the refreshed row + session index. */
  renameArtifact: (
    runId: string,
    artifactId: string,
    title: string,
    sessionId: string | null,
  ) => Promise<ArtifactRenameResult>
  /** Derived session file names (artifactId -> `name.md`); empty while
   * the session index has not loaded. */
  sessionFileNames: Record<string, string>
  workspaceId: string
}

export const AgentCanvasReactContext =
  createContext<AgentCanvasContextValue | null>(null)

export function useAgentCanvas(): AgentCanvasContextValue {
  const value = useContext(AgentCanvasReactContext)
  if (!value) {
    throw new Error('Agent canvas views require the AgentCanvasReactContext')
  }
  return value
}
