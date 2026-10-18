import type { ChatCompletionMessage } from '@/api/inqtrixClient'
import { contentWithAttachmentContext } from '@/features/project/attachmentContext'
import type { ChatMessageRecord } from '@/features/project/types'
import type { ChatModelTier } from '@/features/researchRuns/types'

export type ChatRetryMode = 'details' | 'plain' | 'shorter'

export type ChatRetryOptions = {
  effort?: string | null
  model?: string | null
  modelTier?: ChatModelTier | null
}

export type ChatRetryTarget = {
  assistantMessage: ChatMessageRecord
  history: ChatMessageRecord[]
  userMessage: ChatMessageRecord
}

export function findAssistantRetryTarget(
  messages: readonly ChatMessageRecord[],
  assistantMessageId: string,
): ChatRetryTarget | null {
  const assistantIndex = messages.findIndex((message) => message.id === assistantMessageId)
  const assistantMessage = assistantIndex >= 0 ? messages[assistantIndex] : undefined
  const userMessage = assistantIndex > 0 ? messages[assistantIndex - 1] : undefined
  if (
    !assistantMessage
    || assistantMessage.role !== 'assistant'
    || !assistantMessage.contentMarkdown.trim()
    || !userMessage
    || userMessage.role !== 'user'
    || !userMessage.contentMarkdown.trim()
  ) {
    return null
  }

  return {
    assistantMessage,
    history: messages.slice(0, assistantIndex - 1),
    userMessage,
  }
}

export function buildChatRetryMessages(
  target: ChatRetryTarget,
  mode: ChatRetryMode,
): ChatCompletionMessage[] {
  const messages = target.history
    .filter((message) => message.contentMarkdown.trim().length > 0)
    .slice(-20)
    .map((message) => ({
      content: message.contentMarkdown,
      role: message.role,
    }))

  messages.push({
    content: retryUserContent(target.userMessage, target.assistantMessage.contentMarkdown, mode),
    role: 'user',
  })
  return messages
}

function retryUserContent(
  userMessage: ChatMessageRecord,
  previousAnswer: string,
  mode: ChatRetryMode,
): string {
  const originalRequest = userMessage.attachments && userMessage.attachments.length > 0
    ? contentWithAttachmentContext(userMessage.contentMarkdown, userMessage.attachments)
    : userMessage.contentMarkdown

  if (mode === 'plain') return originalRequest

  const directive = mode === 'details'
    ? 'Answer the original user request again with more useful detail, clearer reasoning, and concrete examples where helpful.'
    : 'Answer the original user request again in a shorter, more concise form while preserving the essential substance.'

  return [
    directive,
    'Use the previous answer only as context for the revision. Do not mention that this is a retry unless the user explicitly asked for that.',
    '',
    'Original user request:',
    originalRequest,
    '',
    'Previous answer:',
    previousAnswer.trim(),
  ].join('\n')
}
