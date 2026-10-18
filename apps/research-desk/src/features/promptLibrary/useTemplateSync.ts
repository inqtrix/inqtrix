import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'
import {
  createPromptTemplate,
  deletePromptTemplate,
  hasHttpStatus,
  listPromptTemplates,
  updatePromptTemplate,
  type ClientOptions,
  type InqtrixRequestError,
} from '@/api/inqtrixClient'
import type { ChatRuleRecord } from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import {
  TemplateConflictError,
  isSameSyncedRule,
  ruleFromTemplate,
  staleSyncedRuleIds,
  templatePayloadFromRule,
  withSavedTemplate,
} from './templateSync'

export type TemplateSyncHandle = {
  /** Server-first delete; local dispatch happens only after success. */
  deleteRule: (rule: ChatRuleRecord) => Promise<void>
  /** Last sync failure, surfaced inline — never a silent fallback. */
  error: string | null
  /**
   * Server-first save. Local rules without a serverTemplateId upload
   * on their first save (the legacy-adoption rule); synced rules PUT
   * with an optimistic-concurrency precondition (the loaded integer
   * `serverRevision`). A server-side conflict re-hydrates the current
   * version into local state and throws {@link TemplateConflictError}
   * so the caller can keep the draft and prompt a re-save. Returns the
   * rule enriched with the fresh server id, access, revision, and timestamp.
   */
  saveRule: (rule: ChatRuleRecord) => Promise<ChatRuleRecord>
}

/**
 * Hydrate-on-mount + write-through sync of chat rules against
 * `/v1/prompt-templates`. The server record is the one truth for
 * synced rules: hydrate upserts the server state (including shared-in
 * templates with their access annotation) and drops local rules whose
 * server record vanished (deleted or share revoked).
 */
export function useTemplateSync({
  clientOptions,
  dispatch,
  enabled,
  localRules,
  refreshToken = 0,
}: {
  clientOptions: ClientOptions
  dispatch: Dispatch<ResearchDeskAction>
  enabled: boolean
  localRules: readonly ChatRuleRecord[]
  refreshToken?: number
}): TemplateSyncHandle | null {
  const [error, setError] = useState<string | null>(null)
  // Hydration must compare against the rules AT FETCH TIME without
  // re-running on every local edit.
  const localRulesRef = useRef(localRules)
  localRulesRef.current = localRules
  // Rule ids whose create-on-server is in flight or done, so the auto-push
  // effect below never double-creates the same local-only rule.
  const pushedLocalRuleIdsRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const controller = new AbortController()
    listPromptTemplates({ ...clientOptions, signal: controller.signal })
      .then((templates) => {
        if (cancelled) return
        const localRules = localRulesRef.current
        const byServerId = new Map(
          localRules
            .filter((rule) => rule.serverTemplateId !== undefined)
            .map((rule) => [rule.serverTemplateId as string, rule]),
        )
        for (const template of templates) {
          const existing = byServerId.get(template.id)
          const incoming = ruleFromTemplate(template, existing)
          // Skip byte-equal hydrates: a no-op upsert would mark a
          // freshly loaded project dirty for nothing.
          if (existing && isSameSyncedRule(existing, incoming)) continue
          dispatch({ rule: incoming, type: 'upsertChatRule' })
          // Heal records still keyed under a pre-adoption local id:
          // the hydrated copy lives under the server id, the twin
          // would otherwise linger as a stale duplicate.
          if (existing && existing.id !== incoming.id) {
            dispatch({ ruleId: existing.id, type: 'deleteChatRule' })
          }
        }
        // A successful empty list is authoritative too: remove only records
        // carrying a server id. Browser-local rules remain untouched.
        const serverIds = new Set(templates.map((template) => template.id))
        for (const ruleId of staleSyncedRuleIds(localRules, serverIds)) {
          dispatch({ ruleId, type: 'deleteChatRule' })
        }
        setError(null)
      })
      .catch((cause) => {
        if (cancelled) return
        setError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [clientOptions, dispatch, enabled, refreshToken])

  // Auto-push local-only rules (no serverTemplateId) to the server. A rule
  // arrives local-only when it was loaded from a project file (or seeded)
  // rather than created through the save flow; without this it would never
  // reach the server and would vanish on the next reload (the server-first
  // boot hydrates from the server, which never held it). This is the prompt
  // analogue of the chat/editor autosave "import-up": hydrate only adopts
  // rules that already carry a serverTemplateId, so a local-only rule is
  // genuinely absent server-side and a plain create is safe (no conflict
  // path). ``pushedLocalRuleIdsRef`` guards against double-create across the
  // re-runs this effect makes whenever ``localRules`` changes.
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const pending = localRules.filter(
      (rule) =>
        rule.serverTemplateId === undefined
        && !pushedLocalRuleIdsRef.current.has(rule.id),
    )
    for (const rule of pending) {
      pushedLocalRuleIdsRef.current.add(rule.id)
      createPromptTemplate(templatePayloadFromRule(rule), clientOptions)
        .then((saved) => {
          if (cancelled) return
          dispatch({ rule: withSavedTemplate(rule, saved), type: 'upsertChatRule' })
          setError(null)
        })
        .catch((cause) => {
          if (cancelled) return
          // Let a transient failure retry on the next run, and surface it.
          pushedLocalRuleIdsRef.current.delete(rule.id)
          setError(cause instanceof Error ? cause.message : String(cause))
        })
    }
    return () => {
      cancelled = true
    }
  }, [clientOptions, dispatch, enabled, localRules])

  // Refresh ONE template from the server into local state (the
  // conflict path). Re-uses the list endpoint — templates are few —
  // and preserves the existing rule's browser-local context refs. A
  // vanished template (deleted meanwhile) is dropped locally. Returns
  // whether the refresh actually happened: false means the fetch
  // failed and nothing was reloaded, so the caller must NOT claim the
  // latest version is in hand.
  const reloadOne = useCallback(
    async (
      serverTemplateId: string,
      existing: ChatRuleRecord,
    ): Promise<boolean> => {
      try {
        const templates = await listPromptTemplates(clientOptions)
        const current = templates.find((t) => t.id === serverTemplateId)
        if (current) {
          dispatch({
            rule: ruleFromTemplate(current, existing),
            type: 'upsertChatRule',
          })
        } else {
          dispatch({ ruleId: existing.id, type: 'deleteChatRule' })
        }
        return true
      } catch {
        // A failed refresh must not mask the conflict itself — but the
        // caller is told so it can soften its message.
        return false
      }
    },
    [clientOptions, dispatch],
  )

  const saveRule = useCallback(async (rule: ChatRuleRecord) => {
    // Create-claim shared with the auto-push effect: a local-only rule has
    // exactly ONE create path. If the auto-push already claimed this rule, do
    // not issue a second create (which would mint a duplicate template) —
    // return it unchanged; the auto-push's enrichment dispatch lands shortly.
    // Otherwise claim it here so the auto-push skips it. (JS is single-threaded
    // and both claim synchronously before awaiting, so the two never overlap.)
    if (!rule.serverTemplateId) {
      if (pushedLocalRuleIdsRef.current.has(rule.id)) return rule
      pushedLocalRuleIdsRef.current.add(rule.id)
    }
    const payload = templatePayloadFromRule(rule)
    try {
      const saved = rule.serverTemplateId
        ? await updatePromptTemplate(rule.serverTemplateId, {
            ...payload,
            expected_revision: requiredServerRevision(rule),
          }, clientOptions)
        : await createPromptTemplate(payload, clientOptions)
      setError(null)
      return withSavedTemplate(rule, saved)
    } catch (cause) {
      // Release the create-claim on failure so the auto-push can retry the
      // first upload (the claim only guards against a concurrent duplicate).
      if (!rule.serverTemplateId) {
        pushedLocalRuleIdsRef.current.delete(rule.id)
      }
      if (rule.serverTemplateId && hasHttpStatus(cause, 409)) {
        // Someone saved a newer version. Re-hydrate it into the authoritative
        // list and signal the conflict. The caller keeps the old draft
        // revision so a normal retry cannot overwrite the remote winner.
        const currentRevision = conflictRevision(cause)
        if (currentRevision === null) {
          const contractError = new Error(
            'Prompt-template conflict response omitted current_revision.',
          )
          console.warn(contractError.message, cause)
          setError(contractError.message)
          throw contractError
        }
        const refreshed = await reloadOne(rule.serverTemplateId, rule)
        const conflict = new TemplateConflictError(refreshed, currentRevision)
        setError(conflict.message)
        throw conflict
      }
      const message = cause instanceof Error ? cause.message : String(cause)
      setError(message)
      throw cause
    }
  }, [clientOptions, reloadOne])

  const deleteRule = useCallback(async (rule: ChatRuleRecord) => {
    if (!rule.serverTemplateId) return
    try {
      await deletePromptTemplate(rule.serverTemplateId, clientOptions)
      setError(null)
    } catch (cause) {
      // An already-deleted server record must not block the local
      // removal — everything else surfaces.
      if (hasHttpStatus(cause, 404)) {
        setError(null)
        return
      }
      const message = cause instanceof Error ? cause.message : String(cause)
      setError(message)
      throw cause
    }
  }, [clientOptions])

  if (!enabled) return null
  return { deleteRule, error, saveRule }
}

function requiredServerRevision(rule: ChatRuleRecord): number {
  if (Number.isInteger(rule.serverRevision) && Number(rule.serverRevision) > 0) {
    return Number(rule.serverRevision)
  }
  throw new Error(
    'A synced prompt template cannot be saved without its server revision.',
  )
}

function conflictRevision(error: unknown): number | null {
  const value = (error as InqtrixRequestError | undefined)?.detail
    ?.current_revision
  return Number.isInteger(value) && Number(value) > 0 ? Number(value) : null
}
