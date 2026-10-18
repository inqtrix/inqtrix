import { describe, expect, it } from 'vitest'

import type { ChatMessageRecord } from '@/features/project/types'
import {
  buildChatRetryMessages,
  findAssistantRetryTarget,
} from './retry'

function message(
  id: string,
  role: ChatMessageRecord['role'],
  contentMarkdown: string,
  overrides: Partial<ChatMessageRecord> = {},
): ChatMessageRecord {
  return {
    contentMarkdown,
    createdAt: `2026-06-26T12:0${id.at(-1) ?? '0'}:00.000Z`,
    id,
    role,
    ...overrides,
  }
}

describe('chat retry request construction', () => {
  it('plain retry uses the original user request and excludes the previous assistant answer', () => {
    const target = findAssistantRetryTarget([
      message('cm_u0', 'user', 'Earlier prompt'),
      message('cm_a0', 'assistant', 'Earlier answer'),
      message('cm_u1', 'user', 'Original prompt'),
      message('cm_a1', 'assistant', 'Answer to replace'),
    ], 'cm_a1')

    expect(target).not.toBeNull()
    const request = buildChatRetryMessages(target!, 'plain')

    expect(request).toEqual([
      { content: 'Earlier prompt', role: 'user' },
      { content: 'Earlier answer', role: 'assistant' },
      { content: 'Original prompt', role: 'user' },
    ])
    expect(request.map((item) => item.content).join('\n')).not.toContain('Answer to replace')
  })

  it('details and shorter retries include the original prompt plus the previous answer as revision context', () => {
    const target = findAssistantRetryTarget([
      message('cm_u1', 'user', 'Explain the filing'),
      message('cm_a1', 'assistant', 'The filing says revenue grew.'),
    ], 'cm_a1')

    expect(target).not.toBeNull()
    const detailed = buildChatRetryMessages(target!, 'details')
    const shorter = buildChatRetryMessages(target!, 'shorter')

    expect(detailed).toHaveLength(1)
    expect(detailed[0].content).toContain('Original user request:\nExplain the filing')
    expect(detailed[0].content).toContain('Previous answer:\nThe filing says revenue grew.')
    expect(detailed[0].content).toContain('more useful detail')
    expect(shorter[0].content).toContain('Original user request:\nExplain the filing')
    expect(shorter[0].content).toContain('Previous answer:\nThe filing says revenue grew.')
    expect(shorter[0].content).toContain('shorter, more concise')
  })

  it('preserves user attachments when reconstructing the retried request', () => {
    const target = findAssistantRetryTarget([
      message('cm_u1', 'user', 'Summarise [1]', {
        attachments: [
          {
            attachedAt: '2026-06-26T12:00:00.000Z',
            contentMarkdown: 'Attachment body',
            fileId: 'file_1',
            kind: 'file-asset',
            label: 'memo',
            pageCount: 1,
            sizeBytes: 42,
            title: 'Attached memo',
          },
        ],
      }),
      message('cm_a1', 'assistant', 'Initial summary'),
    ], 'cm_a1')

    expect(target).not.toBeNull()
    const request = buildChatRetryMessages(target!, 'plain')

    expect(request[0].content).toContain('Use the attached Inqtrix chat context blocks')
    expect(request[0].content).toContain('--- [1] @files:memo ---')
    expect(request[0].content).toContain('Title: Attached memo')
    expect(request[0].content).toContain('Attachment body')
    expect(request[0].content).toContain('User message:\nSummarise [1]')
  })
})
