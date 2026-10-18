import type { KnowledgeThreadItemRecord } from '@/features/project/types'
import type { ResearchRunMessage } from '@/features/researchRuns/types'

export const KNOWLEDGE_CONTEXT_TURN_LIMIT = 6

const CITATION_LABEL_PATTERN = /\[K\d+\]/gi
const WHITESPACE_PATTERN = /\s+/g

export function buildKnowledgeAskMessages(
  items: KnowledgeThreadItemRecord[],
  currentQuestion: string,
  options: { replaceItemId?: string } = {},
): ResearchRunMessage[] | undefined {
  const question = currentQuestion.trim()
  if (!question) return undefined

  const historyItems = itemsBeforeReplacement(items, options.replaceItemId)
    .filter((item) => item.status === 'completed' && item.answer?.answerMarkdown)
    .slice(-KNOWLEDGE_CONTEXT_TURN_LIMIT)

  if (historyItems.length === 0) return undefined

  const messages: ResearchRunMessage[] = []
  for (const item of historyItems) {
    const answer = normalizeHistoryAnswer(item.answer?.answerMarkdown ?? '')
    if (!item.question.trim() || !answer) continue
    messages.push({ content: item.question.trim(), role: 'user' })
    messages.push({ content: answer, role: 'assistant' })
  }
  if (messages.length === 0) return undefined
  messages.push({ content: question, role: 'user' })
  return messages
}

function itemsBeforeReplacement(
  items: KnowledgeThreadItemRecord[],
  replaceItemId?: string,
) {
  if (!replaceItemId) return items
  const replaceIndex = items.findIndex((item) => item.id === replaceItemId)
  if (replaceIndex >= 0) return items.slice(0, replaceIndex)
  return items.filter((item) => item.id !== replaceItemId)
}

function normalizeHistoryAnswer(markdown: string): string {
  return markdown
    .replace(CITATION_LABEL_PATTERN, '')
    .replace(WHITESPACE_PATTERN, ' ')
    .trim()
}
