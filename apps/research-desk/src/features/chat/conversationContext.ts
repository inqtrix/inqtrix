import type { ChatCompletionMessage } from '@/api/inqtrixClient'
import { estimateTokensFromText } from '@/features/files/contextTokens'
import { contentWithAttachmentContext } from '@/features/project/attachmentContext'
import type {
  ChatMessageAttachmentRecord,
  ChatMessageRecord,
} from '@/features/project/types'

/** The earlier messages a chat request carries: the last 20 with text. */
const HISTORY_WINDOW = 20

/**
 * What a chat request knows of the conversation before its own message.
 *
 * Sending and retrying build their requests from here, so both see the same
 * history and the same material.
 */
export function historyWindow(history: readonly ChatMessageRecord[]): ChatMessageRecord[] {
  return history
    .filter((message) => message.contentMarkdown.trim().length > 0)
    .slice(-HISTORY_WINDOW)
}

export function historyRequestMessages(
  history: readonly ChatMessageRecord[],
): ChatCompletionMessage[] {
  return historyWindow(history).map((message) => ({
    content: message.contentMarkdown,
    role: message.role,
  }))
}

/**
 * The identity of a piece of material; a rule chip is none. A file is one
 * piece whether it came on its own or as a member of its group.
 */
function materialKey(attachment: ChatMessageAttachmentRecord): string | null {
  switch (attachment.kind) {
    case 'research-report':
      return `research-report:${attachment.runId}`
    case 'file-asset':
    case 'file-group':
      return `file:${attachment.fileId}`
    case 'chat-rule':
      return null
  }
}

function groupIds(attachments: readonly ChatMessageAttachmentRecord[]): Set<string> {
  return new Set(attachments.flatMap((attachment) => (
    attachment.kind === 'file-group' ? [attachment.groupId] : []
  )))
}

/**
 * The material of earlier user messages that the current message carries.
 *
 * Material that an earlier user message attached (a report, a file, a file
 * group) stays part of the conversation, as it does in ChatGPT and Claude:
 * a follow-up on an attached report needs the report. A rule chip is an
 * instruction for its own message and stays there. Each piece goes once, in
 * its newest copy, and not at all when the current message attaches it
 * again. A group attached again stands for its members of that moment: a
 * member removed from the group since then is no longer carried.
 */
export function earlierMaterial(
  history: readonly ChatMessageRecord[],
  current: readonly ChatMessageAttachmentRecord[],
): ChatMessageAttachmentRecord[] {
  const earlier = new Map<string, ChatMessageAttachmentRecord>()
  const forgetGroups = (ids: Set<string>) => {
    for (const [key, attachment] of earlier) {
      if (attachment.kind === 'file-group' && ids.has(attachment.groupId)) earlier.delete(key)
    }
  }
  for (const message of historyWindow(history)) {
    if (message.role !== 'user') continue
    const attachments = message.attachments ?? []
    forgetGroups(groupIds(attachments))
    for (const attachment of attachments) {
      const key = materialKey(attachment)
      if (key === null) continue
      earlier.delete(key)
      earlier.set(key, attachment)
    }
  }
  forgetGroups(groupIds(current))
  for (const attachment of current) {
    const key = materialKey(attachment)
    if (key !== null) earlier.delete(key)
  }
  return [...earlier.values()]
}

/**
 * The current message's content with the conversation's material in front:
 * its own attachments numbered as the composer numbered their pills, the
 * earlier material after them.
 */
export function currentMessageContent(
  contentMarkdown: string,
  history: readonly ChatMessageRecord[],
  current: readonly ChatMessageAttachmentRecord[],
): string {
  const earlier = earlierMaterial(history, current)
  return current.length > 0 || earlier.length > 0
    ? contentWithAttachmentContext(contentMarkdown, [...current], earlier)
    : contentMarkdown
}

/**
 * Token estimate per category of what the next chat request carries, for the
 * composer meter and its overflow question: the current pills, the material
 * earlier messages attached, and the history window. The draft itself is
 * added by the composer.
 */
export function chatContextEstimate(
  history: readonly ChatMessageRecord[],
  current: readonly ChatMessageAttachmentRecord[],
): { conversation: number; documents: number; reports: number; rules: number } {
  let documents = 0
  let reports = 0
  let rules = 0
  for (const attachment of [...current, ...earlierMaterial(history, current)]) {
    const tokens = estimateTokensFromText(attachment.contentMarkdown ?? '')
    if (attachment.kind === 'research-report') reports += tokens
    else if (attachment.kind === 'chat-rule') rules += tokens
    else documents += tokens
  }
  const conversation = historyWindow(history).reduce(
    (sum, message) => sum + estimateTokensFromText(message.contentMarkdown ?? ''),
    0,
  )
  return { conversation, documents, reports, rules }
}
