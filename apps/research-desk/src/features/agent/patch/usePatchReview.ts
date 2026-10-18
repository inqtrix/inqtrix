import { useCallback, useMemo, useRef, useState } from 'react'

import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { AgentRunRecord } from '../model'
import type { AgentApprovalDecisionRequest } from '../types'

export type PatchReviewActions = {
  applyPatch: (
    runId: string,
    patchId: string,
    expectedRevision: number,
  ) => Promise<
    | { kind: 'applied'; revision: number; appliedEditIds: string[] }
    | { kind: 'conflict'; currentRevision: number | null }
  >
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
}

/**
 * The ONE patch-review state machine shared by the timeline gate card
 * and the canvas patch view: "Genehmigen & Übernehmen" applies through
 * the ONE apply route FIRST (against the freshly known document
 * revision), then resumes the run via the approval; a 409 reloads the
 * patch and surfaces the conflict instead of guessing. Reject mirrors it
 * (reject the patch row, then the approval).
 */
export function usePatchReview({
  actions,
  run,
}: {
  actions: PatchReviewActions
  run: AgentRunRecord
}) {
  const { t } = useLocale()
  const [submitting, setSubmitting] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const decidedRef = useRef(false)

  const pendingApproval = useMemo(
    () =>
      run.approvals.find(
        (approval) =>
          approval.status === 'pending' && approval.kind === 'patch',
      ) ?? null,
    [run.approvals],
  )

  const approveAndApply = useCallback(async () => {
    const patch = run.patch
    if (!patch || !pendingApproval || decidedRef.current || submitting) return
    setSubmitting(true)
    setNotice(null)
    try {
      const result = await actions.applyPatch(
        run.runId,
        patch.patchId,
        patch.documentRevision,
      )
      if (result.kind === 'conflict') {
        // The patch was reloaded with the fresh document revision — the
        // user reviews again and retries deliberately, never blind.
        setNotice(t.agent.patch.conflict)
        return
      }
      if (result.appliedEditIds.length < patch.edits.length) {
        setNotice(
          countLabel(
            patch.edits.length,
            t.agent.patch.appliedPartialOne,
            t.agent.patch.appliedPartial,
          ).replace('{applied}', String(result.appliedEditIds.length)),
        )
      }
      await actions.decideApproval(run.runId, pendingApproval.approvalId, {
        decision: 'approve',
      })
      // Only a SUCCESSFUL decide latches the guard — a failed resume must
      // stay retryable (the apply route itself replays idempotently).
      decidedRef.current = true
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }, [actions, pendingApproval, run.patch, run.runId, submitting, t])

  const reject = useCallback(async () => {
    const patch = run.patch
    if (!patch || !pendingApproval || decidedRef.current || submitting) return
    setSubmitting(true)
    setNotice(null)
    try {
      await actions.rejectPatch(run.runId, patch.patchId, '')
      await actions.decideApproval(run.runId, pendingApproval.approvalId, {
        decision: 'reject',
      })
      decidedRef.current = true
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }, [actions, pendingApproval, run.patch, run.runId, submitting])

  return { approveAndApply, notice, pendingApproval, reject, submitting }
}
