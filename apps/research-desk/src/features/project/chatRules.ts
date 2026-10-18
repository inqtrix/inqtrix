import type {
  ChatContextReferenceRecord,
  ChatRuleCategory,
  ChatRuleRecord,
  ChatRuleVisibility,
} from './types'

export type DatabaseContextReferenceRecord =
  | { fileId: string; kind: 'file-asset' }
  | { groupId: string; kind: 'file-group' }

export type ChatRuleChainTemplate = {
  instruction: string
  label: string
}

export const chatRuleCategories = ['instruction', 'function', 'context'] as const

const chatRuleCategoryRank: Record<ChatRuleCategory, number> = {
  instruction: 0,
  function: 1,
  context: 2,
}

export const defaultChatRuleCategory: ChatRuleCategory = 'instruction'

export const defaultChatRuleVisibility: ChatRuleVisibility = {
  // Opt-IN for the agent surface: an existing rule was written for chat
  // or the editor, and turning it into a report requirement without its
  // owner saying so would change what missions produce.
  agent: false,
  chat: true,
  editor: true,
}

export function chatRuleCategoryOrDefault(value: unknown): ChatRuleCategory {
  return value === 'function' || value === 'context' || value === 'instruction'
    ? value
    : defaultChatRuleCategory
}

export function chatRuleVisibilityOrDefault(value: unknown): ChatRuleVisibility {
  const record = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    agent: typeof record.agent === 'boolean' ? record.agent : defaultChatRuleVisibility.agent,
    chat: typeof record.chat === 'boolean' ? record.chat : defaultChatRuleVisibility.chat,
    editor: typeof record.editor === 'boolean' ? record.editor : defaultChatRuleVisibility.editor,
  }
}

export function chatRuleAutocompleteOrDefault(value: unknown): boolean {
  return typeof value === 'boolean' ? value : true
}

export function isDatabaseContextRef(
  ref: ChatContextReferenceRecord,
): ref is DatabaseContextReferenceRecord {
  return ref.kind === 'file-asset' || ref.kind === 'file-group'
}

export function normalizeLinkedContextRefs(
  refs: readonly ChatContextReferenceRecord[] | undefined,
): DatabaseContextReferenceRecord[] {
  if (!refs) return []
  const seen = new Set<string>()
  return refs.flatMap<DatabaseContextReferenceRecord>((ref) => {
    if (!isDatabaseContextRef(ref)) return []
    const key = ref.kind === 'file-asset'
      ? `file-asset:${ref.fileId}`
      : `file-group:${ref.groupId}`
    if (seen.has(key)) return []
    seen.add(key)
    return [ref]
  })
}

export function normalizeChatRule(rule: ChatRuleRecord): ChatRuleRecord {
  const category = chatRuleCategoryOrDefault(rule.category)
  return {
    ...rule,
    category,
    includeInAutocomplete: chatRuleAutocompleteOrDefault(rule.includeInAutocomplete),
    linkedContextRefs: category === 'context'
      ? normalizeLinkedContextRefs(rule.linkedContextRefs)
      : [],
    visibility: chatRuleVisibilityOrDefault(rule.visibility),
  }
}

export function compareChatRulesByCategory(a: ChatRuleRecord, b: ChatRuleRecord): number {
  const first = normalizeChatRule(a)
  const second = normalizeChatRule(b)
  const categoryDiff = chatRuleCategoryRank[first.category ?? 'instruction']
    - chatRuleCategoryRank[second.category ?? 'instruction']
  if (categoryDiff !== 0) return categoryDiff
  return first.title.localeCompare(second.title) || first.label.localeCompare(second.label)
}

export function chatFunctionChainTemplatesFromRefs(
  rules: Readonly<Record<string, ChatRuleRecord>>,
  refs: readonly ChatContextReferenceRecord[],
): ChatRuleChainTemplate[] {
  return refs.flatMap((ref) => {
    if (ref.kind !== 'chat-rule') return []
    const rule = rules[ref.ruleId]
    const normalized = rule ? normalizeChatRule(rule) : null
    return normalized?.category === 'function'
      ? [{ instruction: normalized.contentMarkdown, label: normalized.label }]
      : []
  })
}
