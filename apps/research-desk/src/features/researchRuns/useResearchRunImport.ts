import { useEffect, useRef, useState } from 'react'
import {
  importResearchRun,
  type ImportResearchRunPayload,
} from '@/api/inqtrixClient'
import type { ResearchRunRecord } from '@/features/project/types'
import type { ResearchRunSummary } from './types'
import { unixSecondsFromIso } from '@/lib/time'

type UseResearchRunImportOptions = {
  apiKey?: string
  /** Push only on a real server-synced session (capability + cookie auth),
   * never in demo / local-first — same gate as the project-data hooks. */
  enabled: boolean
  researchRuns: Record<string, ResearchRunRecord>
  researchRunOrder: readonly string[]
  /** Adopt the server-allocated run id without losing the imported report. */
  onImported: (sourceRunId: string, summary: ResearchRunSummary) => void
  /** The per-user namespace the report is stored under (effectiveWorkspaceId). */
  workspaceId: string
}

/**
 * One-way import of reports loaded from a project file into the durable run
 * tier. A loaded report has ``source: 'imported'`` and lives only in local
 * state; without this it would vanish on the next reload (the server-first boot
 * lists runs from the server, which never held it). Mirroring the chat/editor/
 * prompt import-up, this pushes each fully-formed completed report to
 * ``POST /v1/runs/import`` so it survives reload and follows the user; the
 * server import is idempotent on the report's ``source_run_id`` while that
 * imported run exists; the returned server-generated ``run_id`` becomes the
 * canonical client identity.
 *
 * Push-only (no delete loop): runs are server-owned execution artifacts, not a
 * diffed project collection. ``pushedRef`` avoids re-pushing within a session;
 * the server is idempotent across sessions. Only reports with a complete result
 * (markdown + metrics) are pushed, so the round-trip back through
 * ``attachRunResult`` on reload is always well-formed; a partial/legacy report
 * stays local view-only (unchanged from before).
 */
export function useResearchRunImport({
  apiKey,
  enabled,
  researchRuns,
  researchRunOrder,
  onImported,
  workspaceId,
}: UseResearchRunImportOptions): { error: string | null } {
  const [error, setError] = useState<string | null>(null)
  const pushedRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const pending = researchRunOrder
      .map((id) => researchRuns[id])
      .filter(
        (run): run is ResearchRunRecord =>
          isImportableReport(run)
          && !pushedRef.current.has(`${workspaceId}:${run.runId}`),
      )
    for (const run of pending) {
      const pushKey = `${workspaceId}:${run.runId}`
      pushedRef.current.add(pushKey)
      importResearchRun(importPayloadFromRun(run), { apiKey, workspaceId })
        .then((summary) => {
          if (cancelled) return
          onImported(run.runId, summary)
          setError(null)
        })
        .catch((cause) => {
          if (cancelled) return
          // Let it retry on the next run, and surface it (No Silent Fallbacks).
          pushedRef.current.delete(pushKey)
          setError(cause instanceof Error ? cause.message : String(cause))
        })
    }
    return () => {
      cancelled = true
    }
  }, [apiKey, enabled, onImported, researchRuns, researchRunOrder, workspaceId])

  return { error }
}

function isImportableReport(
  run: ResearchRunRecord | undefined,
): run is ResearchRunRecord {
  const metrics = run?.result?.metrics
  return (
    !!run
    && run.source === 'imported'
    && run.status === 'completed'
    && !!run.result?.markdown
    && !!metrics?.claims?.status_counts
    && typeof metrics.confidence === 'number'
  )
}

export function importPayloadFromRun(
  run: ResearchRunRecord,
): ImportResearchRunPayload {
  return {
    source_run_id: run.runId,
    question: run.summary.title,
    stack: run.stack,
    mode: run.mode,
    status: run.status,
    created_at: unixSecondsFromIso(run.createdAt),
    agent_overrides: run.agentOverrides,
    snapshot: run.snapshot,
    // The server result payload (the result endpoint adds run_id/status on
    // read); inverse of attachRunResult so the round-trip is lossless.
    result: run.result
      ? {
          answer: run.result.markdown,
          metrics: run.result.metrics,
          references: run.result.references,
          top_claims: run.result.topClaims,
          top_sources: run.result.topSources,
          usage: run.result.usage,
        }
      : {},
  }
}
