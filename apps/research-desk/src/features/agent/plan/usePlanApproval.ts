import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  clearPlanDraftsForRun,
  planDraftStorageKey,
  readPlanDraft,
  writePlanDraft,
} from './planDraftStorage'
import type {
  AgentPlanRecord,
  AgentPlanTaskRecord,
  AgentRunRecord,
} from '../model'
import type { AgentApprovalDecisionRequest } from '../types'
import { gateRequirementInForce, type GateRequirementInForce } from './reportGuidance'

/** Editable projection of one plan task (v1 scope: title, query strings,
 * delete, reorder, profile + model tier params). */
export type AgentPlanTaskDraft = {
  taskId: string
  title: string
  toolKind: AgentPlanTaskRecord['toolKind']
  objective: string
  queries: string[]
  gapIds: string[]
  dependsOn: string[]
  budget: Record<string, unknown>
  params: Record<string, unknown>
  expectedOutput: string
}

export type AgentPlanDraft = {
  version: number
  summaryMarkdown: string
  assumptions: string[]
  successCriteria: string[]
  tasks: AgentPlanTaskDraft[]
  /** Decision-scoped user guidance for the REPORT (structure, focus,
   * audience). Travels with approve/edit as `report_guidance`; never
   * part of the wire plan, so guidance alone never turns an approve
   * into an edit. */
  reportGuidance: string
  /** Prompt-library rules attached as the reusable half of the same
   * requirement. Ids only: the server resolves label, revision and text
   * from the deciding caller's own catalog, so a client cannot put
   * unchecked text into the writing prompts. Like the free text, never
   * part of the wire plan. */
  reportRuleIds: string[]
  /** True while the user is writing the note for a rejection. Lives in
   * the SHARED draft, not in one surface's local state: the gate is
   * rendered twice (canvas and composer tray) and both must show the
   * same decision in progress — otherwise the tray still offers a plain
   * approve and silently discards the note being typed next to it.
   * Never part of the wire plan. */
  rejectPending: boolean
  rejectNote: string
  /** Whether the user touched the requirement AT THIS GATE.
   *
   * The gate draft always starts empty, so an approve that sent the
   * field unconditionally sent `report_guidance: ''` — which the server
   * reads, correctly, as "clear it". A requirement set in the composer
   * before the run was therefore deleted by a plain click on Freigeben,
   * silently, without ever having been shown at the gate.
   *
   * Presence, not truthiness, needs this flag to be honest: untouched
   * means the key is OMITTED and whatever is in force stays; touched
   * means the key travels — including the empty string, which is how a
   * user deliberately clears a requirement here. */
  reportRequirementTouched: boolean
}

/**
 * A fresh gate draft. It opens with the requirement IN FORCE (the parts
 * the gate's payload carries), still untouched: a plain approve says
 * nothing about it, and an edit of the typed text resends the rules in
 * force instead of an empty list the server would read as "remove them".
 */
export function draftFromPlan(
  plan: AgentPlanRecord,
  depth: string | undefined = undefined,
  inForce: GateRequirementInForce | null = null,
): AgentPlanDraft {
  const draft = withEffectiveResearchProfile(rawDraftFromPlan(plan), depth)
  return inForce
    ? {
      ...draft,
      reportGuidance: inForce.freeText,
      reportRuleIds: inForce.rules.map((rule) => rule.templateId),
    }
    : draft
}

/**
 * The draft a gate shows AND the base every edit starts from — one choice,
 * so the two cannot drift apart: a stored draft of the same plan version,
 * otherwise a fresh one prefilled with the requirement in force. Were the
 * edit base the bare plan, the first edit of the typed text would send an
 * empty rule list, and the server would remove the rules in force.
 */
export function planDraftBase({
  depth,
  draft,
  inForce,
  plan,
}: {
  depth: string | undefined
  draft: AgentPlanDraft | null
  inForce: GateRequirementInForce | null
  plan: AgentPlanRecord
}): AgentPlanDraft {
  const base = draft && draft.version === plan.version
    ? draft
    : draftFromPlan(plan, depth, inForce)
  return withEffectiveResearchProfile(base, depth)
}

function rawDraftFromPlan(plan: AgentPlanRecord): AgentPlanDraft {
  return {
    version: plan.version,
    summaryMarkdown: plan.summaryMarkdown,
    reportRuleIds: [],
    rejectPending: false,
    rejectNote: '',
    reportRequirementTouched: false,
    assumptions: [...plan.assumptions],
    successCriteria: [...plan.successCriteria],
    reportGuidance: '',
    tasks: plan.tasks.map((task) => ({
      taskId: task.taskId,
      title: task.title,
      toolKind: task.toolKind,
      objective: task.objective,
      queries: [...task.queries],
      gapIds: [...task.gapIds],
      dependsOn: [...task.dependsOn],
      // Budgets are server-managed. Legacy rows may still carry one, but the
      // UI has no budget editor and must never round-trip an uneditable value.
      budget: {},
      params: { ...task.params },
      expectedOutput: task.expectedOutput,
    })),
  }
}

/** The wire plan for `decision: 'edit'` — the SAME ExecutionPlanModel shape
 * the agent's planner emits, so both pass the one backend validator. */
export function draftToWirePlan(draft: AgentPlanDraft): Record<string, unknown> {
  return {
    summary_markdown: draft.summaryMarkdown,
    assumptions: draft.assumptions,
    success_criteria: draft.successCriteria,
    tasks: draft.tasks.map((task) => ({
      id: task.taskId,
      title: task.title,
      tool_kind: task.toolKind,
      objective: task.objective,
      queries: task.queries,
      gap_ids: task.gapIds,
      depends_on: task.dependsOn,
      budget: {},
      params: task.params,
      expected_output: task.expectedOutput,
    })),
  }
}

function withEffectiveResearchProfile(
  draft: AgentPlanDraft,
  depth: string | undefined,
): AgentPlanDraft {
  // Fill only a MISSING profile: the server already stamped the
  // tier-correct default (e.g. `schnell` under the Gruendlich Stufe) —
  // overwriting it here would silently deepen the search AND turn every
  // plain approve into an edit.
  const fallback = depth === 'deep' ? 'deep' : 'compact'
  let changed = false
  const tasks = draft.tasks.map((task) => {
    if (task.toolKind !== 'web_research' || task.params.profile) return task
    changed = true
    return { ...task, params: { ...task.params, profile: fallback } }
  })
  return changed ? { ...draft, tasks } : draft
}

/**
 * The decision body for one plan gate.
 *
 * Pure so the two rules it carries can be pinned without a DOM:
 *
 * - PRESENCE, not truthiness, for the requirement: an approve sends the
 *   field even when it is empty, because clearing a requirement IS a
 *   decision. Sending it only when non-empty made a once-set
 *   requirement unremovable — the emptied field never left the client.
 * - The rejection note travels. The gate's own hint asks the user to
 *   reject with a note and the server has always accepted one, but no
 *   caller ever passed it, so the note the agent would have read was
 *   dropped between the button and the request.
 */
export function approvalDecisionRequest({
  decision,
  draft,
  edited,
  note,
}: {
  decision: 'approve' | 'reject'
  draft: AgentPlanDraft | null
  edited: boolean
  note?: string
}): AgentApprovalDecisionRequest {
  // Only a requirement the user actually touched here travels. An
  // untouched gate says NOTHING about the result form, so the server
  // keeps whatever the run was submitted with.
  const guidancePayload =
    decision === 'approve' && draft?.reportRequirementTouched
      ? {
        report_guidance: (draft.reportGuidance ?? '').trim(),
        report_rule_ids: [...(draft.reportRuleIds ?? [])],
      }
      : {}
  const notePayload = note?.trim() ? { note: note.trim() } : {}
  if (edited && draft) {
    return {
      decision: 'edit',
      plan: draftToWirePlan(draft),
      ...notePayload,
      ...guidancePayload,
    }
  }
  return { decision, ...notePayload, ...guidancePayload }
}


export function planDraftDiffers(
  draft: AgentPlanDraft,
  plan: AgentPlanRecord,
): boolean {
  return JSON.stringify(draftToWirePlan(draft))
    !== JSON.stringify(draftToWirePlan(rawDraftFromPlan(plan)))
}

/**
 * The ONE plan-approval state machine shared by the compact timeline card
 * and the spacious canvas plan view — one implementation, two
 * densities. The edit draft lives in the REDUCER (`agentPlanDrafts`,
 * threaded via `draft`/`onDraftChange`) so edits made on one surface are
 * visible — and submitted — from the other. Idempotent decide guard: a
 * double-click cannot fire two decisions.
 */
export function usePlanApproval({
  decideApproval,
  draft,
  onDraftChange,
  run,
}: {
  decideApproval: (
    runId: string,
    approvalId: string,
    decision: AgentApprovalDecisionRequest,
  ) => Promise<unknown>
  draft: AgentPlanDraft | null
  onDraftChange: (draft: AgentPlanDraft | null) => void
  run: AgentRunRecord
}) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const decidedRef = useRef(new Set<string>())

  const pendingApproval = useMemo(
    () =>
      run.approvals.find(
        (approval) =>
          approval.status === 'pending'
          && (approval.kind === 'plan' || approval.kind === 'replan'),
      ) ?? null,
    [run.approvals],
  )

  const plan = run.plan ?? null
  const inForce = useMemo(
    () => gateRequirementInForce(pendingApproval),
    [pendingApproval],
  )
  // A reload empties the reducer slot; the stored draft refills it once
  // per plan version, so an unsent decision survives the page going
  // away. Restoring is a WRITE-BACK into the shared slot, not a second
  // source of truth — both gate surfaces keep reading the one draft, and
  // the ref keeps the two of them from restoring in a loop.
  //
  // Only while the gate is actually OPEN: a draft is an unsent decision,
  // and a decided plan is read from the receipt, never from a draft.
  const restoredForRef = useRef<string | null>(null)
  useEffect(() => {
    if (!plan || draft || !pendingApproval) return
    const key = planDraftStorageKey(run.runId, plan.version)
    if (restoredForRef.current === key) return
    restoredForRef.current = key
    const stored = readPlanDraft(run.runId, plan.version)
    if (stored) onDraftChange(stored)
  }, [draft, onDraftChange, pendingApproval, plan, run.runId])

  const effectiveDraft = useMemo(
    () => (plan ? planDraftBase({ depth: run.depth, draft, inForce, plan }) : null),
    [draft, inForce, plan, run.depth],
  )

  const updateDraft = useCallback(
    (update: (draft: AgentPlanDraft) => AgentPlanDraft) => {
      if (!plan) return
      const next = update(planDraftBase({ depth: run.depth, draft, inForce, plan }))
      writePlanDraft(run.runId, next)
      onDraftChange(next)
    },
    [draft, inForce, onDraftChange, plan, run.depth, run.runId],
  )

  const decide = useCallback(
    async (decision: 'approve' | 'reject', note?: string) => {
      if (!pendingApproval || !plan) return
      const key = `${pendingApproval.approvalId}:${decision}`
      if (decidedRef.current.has(key) || submitting) return
      decidedRef.current.add(key)
      setSubmitting(true)
      setError(null)
      try {
        const edited =
          decision === 'approve'
          && effectiveDraft !== null
          && planDraftDiffers(effectiveDraft, plan)
        const request = approvalDecisionRequest({
          decision,
          draft: effectiveDraft,
          edited,
          note,
        })
        await decideApproval(run.runId, pendingApproval.approvalId, request)
        clearPlanDraftsForRun(run.runId)
        onDraftChange(null)
      } catch (caught) {
        decidedRef.current.delete(key)
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setSubmitting(false)
      }
    },
    [
      decideApproval,
      effectiveDraft,
      onDraftChange,
      pendingApproval,
      plan,
      run.runId,
      submitting,
    ],
  )

  return {
    decide,
    draft: effectiveDraft,
    error,
    /** The requirement in force when the pending gate opened. */
    inForce,
    pendingApproval,
    submitting,
    updateDraft,
  }
}
