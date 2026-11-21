/**
 * Follow-the-agent routing, the PURE part: derive the view the
 * canvas should show from the run RECORD state — not from raw events, so a
 * reconnect/replay lands on the same target as a live stream. Only
 * lifecycle-level state routes; child progress never does. The timing
 * discipline (dwell, hysteresis, interaction deferral) lives in
 * `useCanvasFollow`; the pin/auto-open discipline lives in the reducer.
 */

import type { CanvasViewDescriptor } from '@/features/canvas/types'
import type { AgentRunRecord } from './model'

export type FollowTarget = {
  descriptor: CanvasViewDescriptor
  /**
   * `synthesis` wins against dwell/hysteresis (the memo streaming in is
   * the moment the user is waiting for); `open-only` targets never open
   * a closed canvas (anti auto-open); `auto-open` may open it once.
   */
  urgency: 'auto-open' | 'open-only' | 'synthesis'
}

/** The most recent artifact of a kind (one lookup idiom for every
 * document-shaped target: kernel `write_canvas` deliverables and the memos
 * of stored mission runs both open as the document view). */
function latestArtifactOfKind(
  run: AgentRunRecord,
  kind: 'memo' | 'deliverable',
) {
  for (let index = run.artifactOrder.length - 1; index >= 0; index -= 1) {
    const artifact = run.artifacts[run.artifactOrder[index]]
    if (artifact?.kind === kind) return artifact
  }
  return undefined
}

export function routeAgentRunToView(
  run: AgentRunRecord,
): FollowTarget | null {
  // A document-shaped deliverable (kernel canvas, or a stored mission's
  // memo) being written (or finished) is the strongest target: the ONE
  // allowed auto-open. Kernel runs have no memo, so memo precedence only
  // decides for a stored mission run.
  const document =
    latestArtifactOfKind(run, 'memo')
    ?? latestArtifactOfKind(run, 'deliverable')
  if (
    document
    && (document.status === 'writing' || run.status === 'completed')
  ) {
    return {
      descriptor: {
        view: 'document',
        runId: run.runId,
        artifactId: document.artifactId,
      },
      urgency: document.status === 'writing' ? 'synthesis' : 'auto-open',
    }
  }

  // Execution follows the control-room overview, also while a gate of
  // the run waits; it never opens a closed canvas.
  if (run.phase === 'execution') {
    return {
      descriptor: { view: 'run', runId: run.runId },
      urgency: 'open-only',
    }
  }

  return null
}

/** The view the canvas opens on when no tab is left: the run's document
 * (a stored mission's memo first, else the latest kernel deliverable), else
 * the run view; `null` for a session without a run. */
export function fallbackCanvasDescriptor(
  run: AgentRunRecord | undefined,
): CanvasViewDescriptor | null {
  if (!run) return null
  const documentId =
    run.artifactOrder.find(
      (artifactId) => run.artifacts[artifactId]?.kind === 'memo',
    )
    ?? [...run.artifactOrder]
      .reverse()
      .find(
        (artifactId) =>
          run.artifacts[artifactId]?.kind === 'deliverable',
      )
  if (documentId) {
    return { view: 'document', runId: run.runId, artifactId: documentId }
  }
  return { view: 'run', runId: run.runId }
}
