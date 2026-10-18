import { createContext, useContext } from 'react'

import type { ClientOptions } from '@/api/inqtrixClient'
import type { KnowledgeDataSource } from '@/features/knowledge/types'
import type { CanvasViewDescriptor } from '@/features/canvas/types'
import type { FileAssetRecord } from '@/features/project/types'
import type { AgentRunRecord } from '../model'
import type { AgentSessionArtifactIndex } from '../model'
import type { CanvasSaveRegistry } from './saveRegistry'
import type { ReportRuleOption } from '../plan/PlanReviewBody'
import type { PlanSourceInfo } from '../plan/sourceLabel'
import type { AgentPlanDraft } from '../plan/usePlanApproval'
import type {
  AgentApprovalDecisionRequest,
  AgentTaskCancelWire,
  AgentTaskResultWire,
} from '../types'
import type {
  ArtifactRenameResult,
  ArtifactSaveResult,
} from '../useAgentControlApi'

/**
 * Data plane of the agent canvas views. A React context (not per-render
 * closures) so the view registry stays a MODULE CONSTANT — otherwise every
 * workspace render would recreate the renderer component types and
 * remount the active view (killing the editor cursor and the task view's
 * SSE subscription).
 */
export type AgentCanvasContextValue = {
  applyPatch: (
    runId: string,
    patchId: string,
    expectedRevision: number,
  ) => Promise<
    | { kind: 'applied'; revision: number; appliedEditIds: string[] }
    | { kind: 'conflict'; currentRevision: number | null }
  >
  clientOptions: ClientOptions | null
  inspectChildRun: (runId: string, signal: AbortSignal, onTransport: (value: 'sse' | 'polling') => void) => Promise<void>
  /** Knowledge document reader for the evidence view's knowledge
   * citations: the same reader the Knowledge Desk uses, so "verify the
   * source" is one experience across desks. */
  knowledgeDataSource: KnowledgeDataSource
  cancelTask: (
    runId: string,
    taskId: string,
  ) => Promise<AgentTaskCancelWire>
  decideApproval: (
    runId: string,
    approvalId: string,
    decision: AgentApprovalDecisionRequest,
  ) => Promise<unknown>
  rejectPatch: (
    runId: string,
    patchId: string,
    note: string,
  ) => Promise<unknown>
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
  loadTaskResult: (
    runId: string,
    taskId: string,
  ) => Promise<AgentTaskResultWire>
  /** Fire-and-forget cache warm for a settled task's result (hover /
   * idle) — errors stay silent, the on-open fetch is the loud path. */
  prefetchTaskResult: (runId: string, taskId: string) => void
  openCanvasView: (descriptor: CanvasViewDescriptor) => void
  /** Re-flags the run's plan as stale so the control hook refetches it —
   * the plan view's on-open trigger (rows stay the truth). */
  requestPlanRefresh: (runId: string) => void
  /** Pending canvas saves keyed by artifact; the composer flushes ALL
   * of them before any submit — including one that answers a gate. */
  saveRegistry: CanvasSaveRegistry
  planDrafts: Record<string, AgentPlanDraft>
  /** Collection titles + vector-backend label for the plan view's
   * per-task source line (same data plane as the timeline card). */
  planSource: PlanSourceInfo
  /** Library rules the user marked visible for the agent — attachable
   * as a saved result requirement at the plan gate. */
  reportRuleOptions: ReportRuleOption[]
  /** The server's OWN limits for that requirement (published ==
   * enforced): the gate must not accept a text or a rule count the
   * decision would then be refused for. */
  reportGuidanceMaxChars: number
  /** null = not published: the gate does not pre-check the count. */
  reportRuleIdsMax: number | null
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
  setPlanDraft: (runId: string, draft: AgentPlanDraft | null) => void
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
