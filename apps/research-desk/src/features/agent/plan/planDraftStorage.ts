import type { AgentPlanDraft } from './usePlanApproval'

/**
 * The unsent plan decision, kept across a page reload.
 *
 * Everything a user puts into a pending plan gate — edited questions, the
 * result requirement, attached rules, a rejection note — lived only in
 * the reducer, so a reload threw it away and the gate snapped back to the
 * agent's proposal with no sign that anything had been lost. The draft is
 * per-person and per-browser and it dies with the decision, so
 * `localStorage` is its right home; nothing here belongs in the project
 * file or on the server. Same shape as the editor's unsent comment
 * drafts (`useCollaborationComments`).
 *
 * The key carries the plan VERSION: a replan proposes different tasks,
 * and a draft written against the old ones must not reappear on top of
 * them.
 */
const PREFIX = 'inqtrix.agent.plan-draft'

export function planDraftStorageKey(runId: string, version: number): string {
  return `${PREFIX}:${runId}:v${version}`
}

/**
 * A stored value only counts as a draft if it still has the shape the
 * gate renders. Anything else — a truncated write, a hand-edited entry,
 * a draft from an older build — is treated as absent, so the gate falls
 * back to the plan instead of rendering half a draft.
 */
function isPlanDraft(value: unknown, version: number): value is AgentPlanDraft {
  if (!value || typeof value !== 'object') return false
  const draft = value as Partial<AgentPlanDraft>
  return (
    draft.version === version
    && typeof draft.summaryMarkdown === 'string'
    && typeof draft.reportGuidance === 'string'
    && typeof draft.rejectNote === 'string'
    && typeof draft.rejectPending === 'boolean'
    && Array.isArray(draft.tasks)
    && Array.isArray(draft.assumptions)
    && Array.isArray(draft.successCriteria)
    && Array.isArray(draft.reportRuleIds)
  )
}

/** Reading may throw (private mode, blocked site data) — an unreadable
 * store simply means "no draft", never a broken gate. */
export function readPlanDraft(
  runId: string,
  version: number,
): AgentPlanDraft | null {
  try {
    const raw = globalThis.localStorage?.getItem(
      planDraftStorageKey(runId, version),
    )
    if (!raw) return null
    const parsed = JSON.parse(raw) as unknown
    return isPlanDraft(parsed, version) ? parsed : null
  } catch {
    return null
  }
}

export function writePlanDraft(runId: string, draft: AgentPlanDraft): void {
  try {
    globalThis.localStorage?.setItem(
      planDraftStorageKey(runId, draft.version),
      JSON.stringify(draft),
    )
  } catch {
    // A full or blocked store must not break the gate: the draft then
    // behaves exactly as it did before — session-only.
  }
}

/**
 * Called once a decision landed: an answered gate has no draft.
 *
 * Clears EVERY version of this run, not just the decided one. A reject
 * makes the agent replan, so one run can walk through several plan
 * versions; without the sweep each abandoned version would leave its
 * draft behind forever.
 */
export function clearPlanDraftsForRun(runId: string): void {
  try {
    const store = globalThis.localStorage
    if (!store) return
    const prefix = `${PREFIX}:${runId}:v`
    const stale: string[] = []
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index)
      if (key?.startsWith(prefix)) stale.push(key)
    }
    for (const key of stale) store.removeItem(key)
  } catch {
    // Nothing to do — the entries expire with the browser profile.
  }
}

/**
 * Drop every plan draft in this browser profile.
 *
 * A draft is deliberately per-person and per-browser, and the auth layer
 * reloads the document on an identity transition so "no prior account's
 * reducer or hook state can survive" it. `localStorage` survives the
 * reload, though — so an unsent rejection note or edited plan written by
 * one account could reappear in the gate of the next one on a shared
 * profile, wherever both can reach the same run.
 *
 * Swept at the identity boundary rather than encoded into every key:
 * one place to get right, and a draft still survives an ordinary reload
 * by the SAME person, which is what it exists for.
 */
export function clearAllPlanDrafts(): void {
  try {
    const store = globalThis.localStorage
    if (!store) return
    const stale: string[] = []
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index)
      if (key?.startsWith(`${PREFIX}:`)) stale.push(key)
    }
    for (const key of stale) store.removeItem(key)
  } catch {
    // Nothing to do — the entries expire with the browser profile.
  }
}

/**
 * Whether the identity behind the surface CHANGED, which is when the
 * drafts must go. A first observation is not a change: the same person
 * reloading their own page must keep what they typed.
 */
export function identityChanged(
  previous: string | null | undefined,
  next: string | null,
): boolean {
  if (previous === undefined) return false
  return previous !== next
}
