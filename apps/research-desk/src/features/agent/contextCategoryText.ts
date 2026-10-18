import type { Locale, TranslationDictionary } from '@/i18n/translations'

import {
  pickableCount,
  type AgentContextCategory,
  type AgentContextCategoryId,
  type AgentContextItem,
} from './contextCategories'
import type { RuleRequirementBudget } from './reportRequirement'

/** The caps the server published; null = not published, so the count is
 * not pre-checked and the menus say so. */
export type AgentContextCaps = { report: number | null; rule: number | null }

/** The categories that can reach a cap; a collection selection has none. */
type CappedCategoryId = Exclude<AgentContextCategoryId, 'collection'>

export type AgentContextText = {
  /** What blocks a whole category, in words; null = nothing does. */
  categoryBlockText: (category: AgentContextCategory) => string | null
  /** The "@" root row's note: the block, or the pickable count with the
   * category's limits. */
  categoryNote: (category: AgentContextCategory) => string
  /** One entry's secondary line: why it cannot be picked, or its handle. */
  itemSecondary: (id: AgentContextCategoryId, item: AgentContextItem) => string
  /** The limits the menu can and cannot pre-check, in one line — the same
   * on the "@" root row, in the scoped header and in "+". */
  limitsLabel: (category: AgentContextCategory) => string | undefined
}

/**
 * What the context menus say about a category and its entries — ONE
 * vocabulary for the "@" root row, the scoped "@" header, the "+" sub-menu
 * and every blocked row.
 */
export function agentContextText(
  t: TranslationDictionary,
  locale: Locale,
  caps: AgentContextCaps,
): AgentContextText {
  const formatChars = (value: number) => new Intl.NumberFormat(locale).format(value)
  const budgetLabel = (budget: RuleRequirementBudget): string =>
    'unchecked' in budget
      ? t.agent.composer.ruleBudgetUnchecked
      : t.agent.composer.ruleBudget
        .replace('{used}', formatChars(budget.used))
        .replace('{max}', formatChars(budget.max))
  // Every cap in its own words — a report row never reads as a rule cap, a
  // taken single slot (the pinned canvas document, the editing target) never
  // as a report cap — and at its own size: the cap the server published for
  // rules and reports (null = not published, never pre-checked), one for a
  // single slot.
  const capText: Record<CappedCategoryId, (max: number) => string> = {
    canvas: () => t.agent.composer.canvasSlotTaken,
    document: () => t.agent.composer.editTargetSlotTaken,
    report: (max) => t.agent.composer.reportCap.replace('{max}', String(max)),
    rule: (max) => t.agent.plan.reportRulesCap.replace('{max}', String(max)),
  }
  const capOf: Record<CappedCategoryId, number | null> = {
    canvas: 1,
    document: 1,
    report: caps.report,
    rule: caps.rule,
  }
  const capUncheckedText = (id: AgentContextCategoryId): string =>
    id === 'rule' ? t.agent.plan.reportRulesCapUnchecked : t.agent.composer.reportCapUnchecked
  const limitsLabel = (category: AgentContextCategory): string | undefined => {
    const parts = [
      ...(category.budget ? [budgetLabel(category.budget)] : []),
      ...(category.capUnchecked ? [capUncheckedText(category.id)] : []),
    ]
    return parts.length > 0 ? parts.join(' · ') : undefined
  }
  const categoryBlockText = (category: AgentContextCategory): string | null => {
    if (category.blocked === 'answerMode') return t.agent.composer.ruleAnswerMode
    if (category.blocked === 'allAttached') return t.agent.composer.ruleAllAttached
    if (category.blocked !== 'cap' || category.id === 'collection') return null
    const max = capOf[category.id]
    return max === null ? null : capText[category.id](max)
  }
  const categoryNote = (category: AgentContextCategory): string => {
    const limits = limitsLabel(category)
    return categoryBlockText(category)
      ?? (limits ? `${pickableCount(category)} · ${limits}` : String(pickableCount(category)))
  }
  const itemSecondary = (id: AgentContextCategoryId, item: AgentContextItem): string => {
    const blocked = item.blocked
    if (blocked?.kind === 'notSynced') return t.agent.composer.ruleNotSynced
    if (blocked?.kind === 'cap' && id !== 'collection') return capText[id](blocked.max)
    if (blocked?.kind === 'answerMode') return t.agent.composer.ruleAnswerMode
    if (blocked?.kind === 'linkedFiles') return t.agent.composer.ruleLinkedFiles
    if (blocked?.kind === 'tooLong') {
      return t.agent.composer.ruleTooLong
        .replace('{chars}', formatChars(blocked.chars))
        .replace('{max}', formatChars(blocked.max))
    }
    if (blocked?.kind === 'overBudget') {
      return t.agent.composer.ruleOverBudget
        .replace('{total}', formatChars(blocked.total))
        .replace('{max}', formatChars(blocked.max))
    }
    return id === 'rule'
      ? `@rules:${item.secondary}`
      : id === 'collection'
        ? t.knowledge.collectionMenuHandle
        : id === 'document'
          ? t.agent.patch.title
          : id === 'canvas'
            ? t.agent.composer.canvasDocChip
            : item.secondary || t.agent.composer.reportChip
  }
  return { categoryBlockText, categoryNote, itemSecondary, limitsLabel }
}
