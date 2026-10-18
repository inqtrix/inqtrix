/** Server-template <-> chat-rule mapping (pure; the hook stays thin).
 *
 * Sync model: the SERVER record is the one truth
 * for synced rules — hydrate always takes the server state. Updates use
 * OPTIMISTIC concurrency: a save carries the integer `revision` it loaded as
 * a precondition, and a server-side conflict (someone else saved in
 * between) surfaces instead of silently overwriting. Local-only rules
 * (no serverTemplateId) stay untouched until their first save uploads
 * them.
 */

import type { ChatRuleCategory, ChatRuleRecord } from '@/features/project/types'
import type { ResourceAccess } from '@/features/sharing/types'

/** A save was rejected because the template changed server-side since
 * it was loaded — the optimistic-concurrency conflict (HTTP 409).
 *
 * `refreshed` says whether the conflict recovery managed to reload the
 * current version into local state. A refreshed conflict still does not
 * lend that newer revision to the dirty draft: the user must explicitly
 * keep the draft as a new copy or discard it for the server version. */
export class TemplateConflictError extends Error {
  readonly currentRevision: number
  readonly refreshed: boolean
  constructor(refreshed: boolean, currentRevision: number) {
    super('prompt template was modified by someone else')
    this.name = 'TemplateConflictError'
    this.refreshed = refreshed
    this.currentRevision = currentRevision
  }
}

export type PromptTemplateAccess = ResourceAccess

/** Wire shape of one `/v1/prompt-templates` record. */
export type PromptTemplateInfo = {
  access: PromptTemplateAccess
  category: ChatRuleCategory | null
  /** Size of the block this template composes into as an attached rule,
   * measured by the server's own composition (code points). Absent on
   * servers that do not report it. */
  composed_chars?: number
  content_markdown: string
  created_at: number
  id: string
  include_in_autocomplete: boolean
  label: string
  revision: number
  title: string
  updated_at: number
  visibility: { agent?: boolean; chat?: boolean; editor?: boolean }
}

export type PromptTemplatePayload = {
  category: ChatRuleCategory | null
  content_markdown: string
  include_in_autocomplete: boolean
  label: string
  title: string
  visibility: { agent: boolean; chat: boolean; editor: boolean }
}

function isoFromUnix(seconds: number): string {
  return new Date(seconds * 1000).toISOString()
}

/** The composed size off the wire, or nothing — never an invented one. */
function composedCharsFromWire(value: unknown): number | undefined {
  return Number.isInteger(value) && Number(value) >= 0 ? Number(value) : undefined
}

/**
 * A local rule after the server accepted its save: the server fields
 * replaced, the composed size included. The size belongs to the saved
 * revision, so a save whose response carries none DROPS the old one
 * instead of keeping the size of the previous body.
 */
export function withSavedTemplate(
  rule: ChatRuleRecord,
  saved: PromptTemplateInfo,
): ChatRuleRecord {
  const next: ChatRuleRecord = {
    ...rule,
    access: saved.access,
    serverRevision: saved.revision,
    serverTemplateId: saved.id,
    updatedAt: isoFromUnix(saved.updated_at),
  }
  const composedChars = composedCharsFromWire(saved.composed_chars)
  if (composedChars === undefined) delete next.serverComposedChars
  else next.serverComposedChars = composedChars
  return next
}

/** Map one server template into the local rule shape.
 *
 * The rule id IS the server id (`pt_...`) so hydrate upserts stay
 * idempotent. `linkedContextRefs` remain browser-local (they point at
 * local file assets) — the reducer's upsert REPLACES records
 * wholesale, so the refs of the matching *existing* rule are carried
 * over here explicitly.
 */
export function ruleFromTemplate(
  info: PromptTemplateInfo,
  existing?: ChatRuleRecord,
): ChatRuleRecord {
  const composedChars = composedCharsFromWire(info.composed_chars)
  return {
    access: info.access,
    category: info.category ?? undefined,
    contentMarkdown: info.content_markdown,
    createdAt: isoFromUnix(info.created_at),
    id: info.id,
    includeInAutocomplete: info.include_in_autocomplete,
    label: info.label,
    linkedContextRefs: existing?.linkedContextRefs,
    serverRevision: info.revision,
    ...(composedChars !== undefined ? { serverComposedChars: composedChars } : {}),
    serverTemplateId: info.id,
    title: info.title,
    updatedAt: isoFromUnix(info.updated_at),
    visibility: {
      // The agent surface is opt-IN (the two older ones default on):
      // rules that existed before it must not start shaping reports.
      agent: info.visibility.agent === true,
      chat: info.visibility.chat !== false,
      editor: info.visibility.editor !== false,
    },
  }
}

/** Whether a hydrated rule matches the local record (skip the no-op
 * upsert — it would mark a freshly loaded project dirty). */
export function isSameSyncedRule(
  existing: ChatRuleRecord,
  incoming: ChatRuleRecord,
): boolean {
  return (
    existing.id === incoming.id
    && existing.serverRevision === incoming.serverRevision
    && existing.serverComposedChars === incoming.serverComposedChars
    && existing.updatedAt === incoming.updatedAt
    && existing.contentMarkdown === incoming.contentMarkdown
    && existing.title === incoming.title
    && existing.label === incoming.label
    && (existing.category ?? null) === (incoming.category ?? null)
    && (existing.includeInAutocomplete !== false)
      === (incoming.includeInAutocomplete !== false)
    && JSON.stringify(existing.access ?? null)
      === JSON.stringify(incoming.access ?? null)
    && JSON.stringify(existing.visibility ?? null)
      === JSON.stringify(incoming.visibility ?? null)
  )
}

/** The writable server fields of one local rule. */
export function templatePayloadFromRule(rule: ChatRuleRecord): PromptTemplatePayload {
  return {
    category: rule.category ?? null,
    content_markdown: rule.contentMarkdown,
    include_in_autocomplete: rule.includeInAutocomplete !== false,
    label: rule.label,
    title: rule.title,
    visibility: {
      agent: rule.visibility?.agent === true,
      chat: rule.visibility?.chat !== false,
      editor: rule.visibility?.editor !== false,
    },
  }
}

/** Local synced rules whose server record vanished (deleted/revoked). */
export function staleSyncedRuleIds(
  localRules: readonly ChatRuleRecord[],
  serverIds: ReadonlySet<string>,
): string[] {
  return localRules
    .filter(
      (rule) =>
        rule.serverTemplateId !== undefined
        && !serverIds.has(rule.serverTemplateId),
    )
    .map((rule) => rule.id)
}

/** Whether the caller may edit/save this rule (view shares cannot). */
export function canEditRule(rule: ChatRuleRecord | null): boolean {
  return rule?.access?.mode !== 'shared' || rule.access.permission !== 'view'
}

/** Whether the current draft still has a writable destination.
 *
 * A draft loaded from a synced template retains that template id even when
 * an authoritative refresh removes the local rule after deletion or share
 * revocation. Such a draft must never fall through to the local-rule create
 * path: only a genuinely new/local draft has no source template id.
 */
export function canSavePromptDraft(
  sourceTemplateId: string | null,
  rule: ChatRuleRecord | null,
  baseRevision: number | null = null,
): boolean {
  if (sourceTemplateId !== null && rule === null) return false
  if (!canEditRule(rule)) return false
  return !hasPromptDraftConflict(sourceTemplateId, rule, baseRevision)
}

/** Whether a dirty draft is based on an older authoritative revision. */
export function hasPromptDraftConflict(
  sourceTemplateId: string | null,
  rule: ChatRuleRecord | null,
  baseRevision: number | null,
): boolean {
  return sourceTemplateId !== null
    && rule?.serverRevision !== undefined
    && baseRevision !== null
    && rule.serverRevision !== baseRevision
}

/** Whether the caller may delete this rule (shared-in never deletes). */
export function canDeleteRule(rule: ChatRuleRecord | null): boolean {
  return rule !== null && rule.access?.mode !== 'shared'
}
