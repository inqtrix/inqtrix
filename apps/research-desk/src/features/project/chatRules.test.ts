import { describe, expect, it } from 'vitest'
import {
  chatFunctionChainTemplatesFromRefs,
  compareChatRulesByCategory,
  normalizeChatRule,
} from './chatRules'
import type { ChatRuleRecord } from './types'

function makeRule(id: string, label: string, overrides: Partial<ChatRuleRecord> = {}): ChatRuleRecord {
  return {
    contentMarkdown: `${label} prompt`,
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    label,
    title: `${label} prompt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

describe('normalizeChatRule', () => {
  it('applies prompt-library defaults to legacy rules', () => {
    expect(normalizeChatRule(makeRule('r1', 'legacy'))).toMatchObject({
      category: 'instruction',
      includeInAutocomplete: true,
      linkedContextRefs: [],
      visibility: { agent: false, chat: true, editor: true },
    })
  })
})

describe('chatFunctionChainTemplatesFromRefs', () => {
  it('uses only function prompts as chain templates', () => {
    const rules = {
      context: makeRule('context', 'profile', { category: 'context' }),
      function: makeRule('function', 'translate', {
        category: 'function',
        contentMarkdown: 'Translate the input.',
      }),
      instruction: makeRule('instruction', 'style', { category: 'instruction' }),
    }

    expect(chatFunctionChainTemplatesFromRefs(rules, [
      { kind: 'chat-rule', ruleId: 'instruction' },
      { kind: 'chat-rule', ruleId: 'function' },
      { kind: 'chat-rule', ruleId: 'context' },
      { fileId: 'f1', kind: 'file-asset' },
    ])).toEqual([
      { instruction: 'Translate the input.', label: 'translate' },
    ])
  })
})

describe('compareChatRulesByCategory', () => {
  it('sorts prompts by category and then by title', () => {
    const sorted = [
      makeRule('context', 'z-context', { category: 'context', title: 'Z context' }),
      makeRule('function-b', 'b-function', { category: 'function', title: 'B function' }),
      makeRule('instruction', 'instruction', { category: 'instruction', title: 'Instruction' }),
      makeRule('function-a', 'a-function', { category: 'function', title: 'A function' }),
    ].toSorted(compareChatRulesByCategory)

    expect(sorted.map((rule) => rule.id)).toEqual([
      'instruction',
      'function-a',
      'function-b',
      'context',
    ])
  })
})
