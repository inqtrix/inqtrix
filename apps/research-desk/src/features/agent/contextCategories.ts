import type { CommandPillKind } from '@/features/composer/CommandPill'
import type { ChatRuleOption } from '@/features/project/selectors'
import type { InqtrixCapabilities } from '@/features/researchRuns/types'

import type { AgentEngineMode } from './agentStatusOverview'
import {
  ruleRequirementBudget,
  ruleRequirementTotalWith,
  type RuleRequirementBudget,
} from './reportRequirement'

/**
 * The Agent Desk's context categories — ONE model behind the "+" menu and
 * the "@" autocomplete, so both open on the same categories with the same
 * counts and the same items: prompt-library rules, finished research
 * reports, canvas documents, an editing target and collections. Category-first,
 * like the chat's `@research: › …`: the root lists categories with counts,
 * one step in lists that category's items; a typed query searches across
 * all of them.
 */
export type AgentContextCategoryId = 'rule' | 'collection' | 'document' | 'canvas' | 'report'

/** Why one entry cannot be picked. It stays listed with this reason —
 * never a click that quietly does nothing. */
export type AgentContextItemBlock =
  /** A context pack whose linked files the Agent Desk does not pass on. */
  | { kind: 'linkedFiles' }
  /** A rule the server does not know yet (not pushed, or its sync failed
   * or is off): sending its local id would be refused. */
  | { kind: 'notSynced' }
  /** A rule whose composed block alone exceeds the server's ceiling. */
  | { kind: 'tooLong'; chars: number; max: number }
  /** A rule that would take the composed requirement above the ceiling. */
  | { kind: 'overBudget'; total: number; max: number }
  /** Its category is at its cap (a single slot is a cap of one): listed, so
   * a search finds it and says why. */
  | { kind: 'cap'; max: number }
  /** The send answers a pending clarification, which no rule or document
   * applies to. */
  | { kind: 'answerMode' }

export type AgentContextItem = {
  id: string
  label: string
  /** Secondary line (a handle, a kind, a name). */
  secondary: string
  blocked?: AgentContextItemBlock
}

/** Why a whole category takes no further pick: its cap is reached, or
 * (rules) the send answers a pending clarification instead of starting a
 * task the rule could apply to, or every rule enabled for this surface is
 * attached already — which is not the same as none being enabled. */
export type AgentContextCategoryBlock = 'cap' | 'answerMode' | 'allAttached'

export type AgentContextCategory = {
  id: AgentContextCategoryId
  items: AgentContextItem[]
  /** The category stays visible with its message instead of a click that
   * does nothing. */
  blocked: AgentContextCategoryBlock | null
  /** Rules only: the composed size of what is attached. */
  budget?: RuleRequirementBudget
  /** Rules and reports: the server published no cap, so the count is not
   * pre-checked — the server decides on sending, and the menu says so. */
  capUnchecked?: true
}

/** A prompt-library rule the agent surface may attach. */
export type AgentRuleSource = {
  /** The id the server resolves — its template id. */
  id: string
  /** The rule's handle (`@rules:<label>` in the chat). */
  label: string
  title: string
  revision: number | null
  /** Size of its composed block as the server measured it; null = unknown. */
  composedChars: number | null
  /** A context pack with linked files: those files would not travel. */
  linkedFiles: boolean
  /** The server does not know this rule yet: listed with that reason,
   * never pickable — `id` is then its local id and only keys the row. */
  notSynced: boolean
}

export type AgentContextSources = {
  rules: readonly AgentRuleSource[]
  /** Attached rule ids (pills), in attachment order. */
  ruleIds: readonly string[]
  /** The server's cap on distinct rules; null = not published, so the
   * count is not pre-checked and the surface says so. */
  ruleLimit: number | null
  /** The server's composed ceiling; null = not published, so the size is
   * not pre-checked and the surface says so. */
  ruleCharLimit: number | null
  /** False offers no rule category (no handler for it). */
  rulesAttachable: boolean
  /** A clarification is pending: the send answers it. */
  answerMode: boolean
  /** Reports and canvas documents feed the kernel only — the server
   * refuses both on a mission run. */
  engine: AgentEngineMode
  collections: readonly { id: string; title: string }[]
  selectedCollectionIds: readonly string[]
  documents: readonly { id: string; title: string }[]
  selectedDocumentId: string | null
  canvasDocuments: readonly { artifactId: string; name: string; title: string }[]
  /** The canvas document pinned to the draft: the one slot is taken. */
  pinnedCanvasDocumentId: string | null
  reports: readonly { runId: string; title: string; label: string }[]
  reportIds: readonly string[]
  /** The server's cap on attached reports; null = not published, so the
   * count is not pre-checked and the surface says so. */
  reportLimit: number | null
  /** Only "true" offers a report category. */
  reportsAttachable: boolean
}

/** Root order, by how often the agent case reaches for each: a rule, then
 * what the task reads (reports, canvas), where it writes (the editing
 * target), and the knowledge it searches (collections). */
const AGENT_CONTEXT_CATEGORY_ORDER: readonly AgentContextCategoryId[] = [
  'rule',
  'report',
  'canvas',
  'document',
  'collection',
]

/**
 * The rules the agent surface offers, keyed by the id the server resolves.
 *
 * The wire id is the TEMPLATE id: a rule pushed to the server keeps its
 * local id until the next hydrate, and sending that id is refused. A rule
 * the server does not know yet stays listed as `notSynced` — said, never
 * silently missing, and never pickable. `localIds` is for the demo alone —
 * it has no server catalog, so its own rule ids are the only ids it
 * resolves.
 */
export function agentRuleSources(
  options: readonly ChatRuleOption[],
  { localIds }: { localIds: boolean },
): AgentRuleSource[] {
  return options.map((option) => {
    const wireId = option.serverTemplateId ?? (localIds ? option.ruleId : undefined)
    return {
      composedChars: option.composedChars ?? null,
      id: wireId ?? option.ruleId,
      label: option.label,
      linkedFiles: option.category === 'context' && option.linkedContextRefs.length > 0,
      notSynced: wireId === undefined,
      revision: option.serverRevision ?? null,
      title: option.title,
    }
  })
}

/** Each attached rule once, in attachment order. */
export function ruleIdsForSubmit(ruleIds: readonly string[]): string[] {
  return [...new Set(ruleIds)]
}

/** Rules hold for ONE task, like skills: an accepted submission clears
 * them, a refused one keeps them for the retry. */
export function settleRuleIdsAfterSubmit(
  ruleIds: readonly string[],
  accepted: boolean,
): string[] {
  return accepted ? [] : [...ruleIds]
}

/**
 * What an answer to a clarification holds back: rules and documents hold for
 * a TASK, and an answer starts none. A rule or a document capsule attached
 * before the question arrived therefore neither spells its title into the
 * answer nor leaves the draft with it — it stays attached for the next task.
 */
export function pillKindsKeptByAnswer(answerMode: boolean): readonly CommandPillKind[] {
  return answerMode ? ['rule', 'report', 'canvas', 'editTarget'] : []
}

/**
 * What a send leaves in the draft: what an answer holds back, and after a
 * task the editing target — it is where the user works, so it stays for the
 * follow-up task, as a capsule at the start of the next draft.
 */
export function pillKindsKeptAfterSend(answerMode: boolean): readonly CommandPillKind[] {
  return answerMode ? pillKindsKeptByAnswer(true) : ['editTarget']
}

/** What one rule's own facts block, before any category-wide reason. */
function ruleItemBlock(
  rule: AgentRuleSource,
  budget: RuleRequirementBudget,
  ceiling: number | null,
): AgentContextItemBlock | null {
  if (rule.notSynced) return { kind: 'notSynced' }
  if (rule.linkedFiles) return { kind: 'linkedFiles' }
  const size = rule.composedChars
  if (ceiling === null || size === null) return null
  // Its own size against the ceiling needs no other size.
  if (size > ceiling) return { chars: size, kind: 'tooLong', max: ceiling }
  // The sum needs every attached size; while one is unknown the server
  // checks it on sending, and the budget says so.
  if ('unchecked' in budget) return null
  // Never displace an attached rule to make room: refuse the addition.
  const total = ruleRequirementTotalWith(budget, size)
  return total > budget.max ? { kind: 'overBudget', max: budget.max, total } : null
}

function ruleCategory(sources: AgentContextSources): AgentContextCategory | null {
  if (!sources.rulesAttachable || sources.rules.length === 0) return null
  const attached = ruleIdsForSubmit(sources.ruleIds)
  const byId = new Map(sources.rules.map((rule) => [rule.id, rule]))
  const offered = sources.rules.filter((rule) => !attached.includes(rule.id))
  // An attached rule that vanished from the catalog (or one the server did
  // not measure) makes the composed size unknowable — said so, never
  // guessed as zero.
  const budget = ruleRequirementBudget(
    attached.map((id) => byId.get(id)?.composedChars ?? null),
    sources.ruleCharLimit,
  )
  const category = (
    blocked: AgentContextCategoryBlock | null,
    items: AgentContextItem[],
  ): AgentContextCategory => ({
    blocked,
    budget,
    ...(sources.ruleLimit === null ? { capUnchecked: true as const } : {}),
    id: 'rule',
    items,
  })
  const item = (rule: AgentRuleSource): AgentContextItem => (
    { id: rule.id, label: rule.title, secondary: rule.label }
  )
  // A blocked category keeps its entries, each with the category's reason:
  // a search must find an existing rule and say why it cannot be picked.
  if (sources.answerMode) {
    return category('answerMode', offered.map((rule) => ({ ...item(rule), blocked: { kind: 'answerMode' } })))
  }
  if (offered.length === 0) return category('allAttached', [])
  if (sources.ruleLimit !== null && attached.length >= sources.ruleLimit) {
    const max = sources.ruleLimit
    return category('cap', offered.map((rule) => ({ ...item(rule), blocked: { kind: 'cap', max } })))
  }
  return category(null, offered.map((rule) => {
    const blocked = ruleItemBlock(rule, budget, sources.ruleCharLimit)
    return blocked ? { ...item(rule), blocked } : item(rule)
  }))
}

/** The published `capabilities.agent.attached_reports`. */
export type AttachedReportsCapability = NonNullable<
  NonNullable<InqtrixCapabilities['agent']>['attached_reports']
>

/**
 * The server's cap on attached research reports, or `null`.
 *
 * Deliberately without a fallback number, like the rule cap: a server that
 * publishes no cap is not pre-checked, and the report category says the
 * server checks the count on sending.
 */
export function attachedReportsMax(
  capability: AttachedReportsCapability | null | undefined,
): number | null {
  const published = capability?.max_reports
  return typeof published === 'number' && published > 0 ? published : null
}

function reportCategory(sources: AgentContextSources): AgentContextCategory {
  const category = (
    blocked: AgentContextCategoryBlock | null,
    items: AgentContextItem[],
  ): AgentContextCategory => ({
    blocked,
    ...(sources.reportLimit === null ? { capUnchecked: true as const } : {}),
    id: 'report',
    items,
  })
  // Reports feed the kernel only: the server refuses them on a mission run.
  if (sources.engine !== 'agent_kernel' || !sources.reportsAttachable) return category(null, [])
  const offered: AgentContextItem[] = sources.reports
    .filter((report) => !sources.reportIds.includes(report.runId))
    .map((report) => ({ id: report.runId, label: report.title, secondary: report.label }))
  if (sources.answerMode) {
    return category('answerMode', offered.map((item) => ({ ...item, blocked: { kind: 'answerMode' } })))
  }
  // At the cap the category keeps its entries, each with the reason, like
  // the rules: a search must find an existing report and say why it cannot
  // be attached. Only a published cap blocks.
  if (sources.reportLimit !== null && sources.reportIds.length >= sources.reportLimit) {
    const max = sources.reportLimit
    return category('cap', offered.map((item) => ({ ...item, blocked: { kind: 'cap', max } })))
  }
  return category(null, offered)
}

/**
 * A category with ONE slot (the editing target, the pinned canvas document):
 * while the slot is taken, every other entry stays listed with the cap
 * reason — replacing would delete a word from the user's sentence. An
 * answer to a clarification blocks it like the rules.
 */
function singleSlotCategory(
  id: AgentContextCategoryId,
  items: AgentContextItem[],
  taken: string | null,
  answerMode: boolean,
): AgentContextCategory {
  const offered = items.filter((item) => item.id !== taken)
  if (answerMode) {
    return {
      blocked: 'answerMode',
      id,
      items: offered.map((item) => ({ ...item, blocked: { kind: 'answerMode' } })),
    }
  }
  if (taken !== null) {
    return {
      blocked: 'cap',
      id,
      items: offered.map((item) => ({ ...item, blocked: { kind: 'cap', max: 1 } })),
    }
  }
  return { blocked: null, id, items: offered }
}

export function agentContextCategories(sources: AgentContextSources): AgentContextCategory[] {
  const kernel = sources.engine === 'agent_kernel'
  const byId: Record<AgentContextCategoryId, AgentContextCategory | null> = {
    // Canvas documents feed the kernel only, like reports.
    canvas: kernel
      ? singleSlotCategory(
        'canvas',
        sources.canvasDocuments.map((document) => ({
          id: document.artifactId,
          label: document.name,
          secondary: document.title,
        })),
        sources.pinnedCanvasDocumentId,
        sources.answerMode,
      )
      : { blocked: null, id: 'canvas', items: [] },
    collection: {
      blocked: null,
      id: 'collection',
      items: sources.collections
        .filter((collection) => !sources.selectedCollectionIds.includes(collection.id))
        .map((collection) => ({ id: collection.id, label: collection.title, secondary: '' })),
    },
    document: singleSlotCategory(
      'document',
      sources.documents.map((document) => ({ id: document.id, label: document.title, secondary: '' })),
      sources.selectedDocumentId,
      sources.answerMode,
    ),
    report: reportCategory(sources),
    rule: ruleCategory(sources),
  }
  return AGENT_CONTEXT_CATEGORY_ORDER.flatMap((id) => {
    const category = byId[id]
    return category && (category.items.length > 0 || category.blocked !== null) ? [category] : []
  })
}

/** What can be picked — the ONE count the "@" root, the "+" menu, its
 * sub-triggers and their headers show. */
export function pickableCount(category: AgentContextCategory): number {
  return category.items.filter((item) => !item.blocked).length
}

/**
 * Whether the selected engine refuses what is attached: research reports
 * and canvas context feed the kernel only, and the server refuses both on
 * a mission run with 400. Flagged before the send, never found after it.
 */
export function engineRefusesAttachments({
  canvasAttached,
  engine,
  reportCount,
}: {
  canvasAttached: boolean
  engine: AgentEngineMode
  reportCount: number
}): boolean {
  return engine !== 'agent_kernel' && (reportCount > 0 || canvasAttached)
}

export type AgentMentionRow =
  | { kind: 'category'; category: AgentContextCategory }
  | { kind: 'item'; category: AgentContextCategoryId; item: AgentContextItem }

/**
 * What the "@" menu lists: at the root with no query the categories; with
 * a query the matching items of every category (grouped by category in
 * display order); inside one category its items filtered by the query.
 * A blocked entry is listed with its reason, never left out.
 */
export function agentMentionRows(
  categories: readonly AgentContextCategory[],
  scope: AgentContextCategoryId | null,
  query: string,
): AgentMentionRow[] {
  const needle = query.trim().toLowerCase()
  const matches = (item: AgentContextItem) =>
    !needle
    || item.label.toLowerCase().includes(needle)
    || item.secondary.toLowerCase().includes(needle)
  if (scope) {
    const category = categories.find((entry) => entry.id === scope)
    return category
      ? category.items.filter(matches).map((item) => ({ kind: 'item' as const, category: scope, item }))
      : []
  }
  if (!needle) {
    return categories.map((category) => ({ kind: 'category' as const, category }))
  }
  return categories.flatMap((category) =>
    category.items.filter(matches).map((item) => ({ kind: 'item' as const, category: category.id, item })))
}

/** The chat's category tokens, read as a scope by THIS composer only —
 * the shared `@` detector stays a plain query for the knowledge composer
 * and the plan-gate editor. */
const AGENT_MENTION_ALIASES: Readonly<Record<string, AgentContextCategoryId>> = {
  research: 'report',
  rules: 'rule',
}

/**
 * Where an `@` query searches: the category the user stepped into, the
 * category an alias names (`@rules:exec` = rules, filter "exec"), or every
 * category. `alias` says the scope lives in the typed text, so leaving it
 * means deleting that text.
 */
export function agentMentionTarget(
  rawQuery: string,
  drilled: AgentContextCategoryId | null,
): { scope: AgentContextCategoryId | null; filter: string; alias: boolean } {
  if (drilled) return { alias: false, filter: rawQuery, scope: drilled }
  const match = /^([a-z]+):(.*)$/i.exec(rawQuery)
  const aliased = match ? AGENT_MENTION_ALIASES[match[1].toLowerCase()] : undefined
  return aliased
    ? { alias: true, filter: match![2], scope: aliased }
    : { alias: false, filter: rawQuery, scope: null }
}

/** Why the "@" menu has nothing to list — each case says something true
 * about THIS situation instead of one collection message for all.
 * `noRules`: no rule is enabled for this surface at all; every enabled
 * rule attached already is the `allAttached` block instead. */
export type AgentMentionEmptyState =
  | { kind: 'noMatch'; query: string; scope: AgentContextCategoryId | null }
  | { kind: 'blocked'; reason: AgentContextCategoryBlock; scope: AgentContextCategoryId }
  | { kind: 'noRules' }
  | { kind: 'kernelOnly'; scope: 'report' | 'canvas' }
  | { kind: 'emptyCategory'; scope: AgentContextCategoryId }
  | { kind: 'nothingAttachable' }

export function agentMentionEmptyState({
  categories,
  engine,
  filter,
  scope,
}: {
  categories: readonly AgentContextCategory[]
  engine: AgentEngineMode
  filter: string
  scope: AgentContextCategoryId | null
}): AgentMentionEmptyState {
  const query = filter.trim()
  if (query) return { kind: 'noMatch', query, scope }
  if (!scope) return { kind: 'nothingAttachable' }
  const blocked = categories.find((category) => category.id === scope)?.blocked
  if (blocked) return { kind: 'blocked', reason: blocked, scope }
  if (scope === 'rule') return { kind: 'noRules' }
  if ((scope === 'report' || scope === 'canvas') && engine !== 'agent_kernel') {
    return { kind: 'kernelOnly', scope }
  }
  return { kind: 'emptyCategory', scope }
}

/** Whether the prompt library is where the user resolves this state —
 * there the empty row opens it. */
export function mentionEmptyOpensLibrary(empty: AgentMentionEmptyState): boolean {
  return empty.kind === 'noRules'
    || empty.kind === 'nothingAttachable'
    || (empty.kind === 'blocked' && empty.reason === 'allAttached')
}

/** What picking one row of the "@" menu does. */
export type AgentMentionPick =
  | { kind: 'enter'; scope: AgentContextCategoryId }
  | { kind: 'attach'; category: AgentContextCategoryId; item: AgentContextItem }
  | { kind: 'openLibrary' }
  | { kind: 'none' }

/**
 * What picking a row does — the ONE answer for the click, the Enter key
 * and whether the row is marked as not pickable. `row` undefined is the
 * state row the menu shows while it has nothing to list (`empty`).
 *
 * A category always opens, a blocked one included: inside, its entries
 * carry the reason, or its state row says it (with the library link where
 * that resolves it) — never a chevron that does nothing.
 */
export function agentMentionPick(
  row: AgentMentionRow | undefined,
  empty: AgentMentionEmptyState | null,
  { libraryAvailable }: { libraryAvailable: boolean },
): AgentMentionPick {
  if (!row) {
    return empty && libraryAvailable && mentionEmptyOpensLibrary(empty)
      ? { kind: 'openLibrary' }
      : { kind: 'none' }
  }
  if (row.kind === 'category') return { kind: 'enter', scope: row.category.id }
  // A blocked entry says why on its own row; picking it changes nothing.
  return row.item.blocked ? { kind: 'none' } : { category: row.category, item: row.item, kind: 'attach' }
}
