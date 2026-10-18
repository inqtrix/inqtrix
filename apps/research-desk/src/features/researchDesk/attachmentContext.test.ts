import { describe, expect, it } from 'vitest'
import { MAX_DOC_CHARS_SOFT } from '@/features/files/budget'
import type {
  ChatMessageAttachmentRecord,
  FileAssetAttachmentRecord,
  FileGroupAttachmentRecord,
} from '@/features/project/types'
import { attachmentMentionLabel, contentWithAttachmentContext } from './attachmentContext'

function fileAttachment(
  label: string,
  overrides: Partial<FileAssetAttachmentRecord> = {},
): FileAssetAttachmentRecord {
  return {
    attachedAt: '2026-01-01T00:00:00.000Z',
    contentMarkdown: `${label} body`,
    fileId: `file-${label}`,
    kind: 'file-asset',
    label,
    pageCount: null,
    sizeBytes: 10,
    title: `${label} title`,
    ...overrides,
  }
}

function groupMember(
  groupId: string,
  groupLabel: string,
  label: string,
  overrides: Partial<FileGroupAttachmentRecord> = {},
): FileGroupAttachmentRecord {
  return {
    attachedAt: '2026-01-01T00:00:00.000Z',
    contentMarkdown: `${label} body`,
    fileId: `file-${label}`,
    groupId,
    groupLabel,
    kind: 'file-group',
    label,
    pageCount: null,
    sizeBytes: 10,
    title: `${label} title`,
    ...overrides,
  }
}

describe('attachmentMentionLabel', () => {
  it('renders the @mention token per kind', () => {
    expect(attachmentMentionLabel(fileAttachment('alpha'))).toBe('@files:alpha')
    expect(attachmentMentionLabel(groupMember('g1', 'dossier', 'alpha'))).toBe('@filegroups:dossier')
  })
})

describe('contentWithAttachmentContext', () => {
  it('numbers two standalone files sequentially', () => {
    const result = contentWithAttachmentContext('Compare them.', [
      fileAttachment('alpha'),
      fileAttachment('beta'),
    ])
    expect(result).toContain('--- [1] @files:alpha ---')
    expect(result).toContain('--- [2] @files:beta ---')
    expect(result).not.toContain('[3]')
    expect(result).toContain('User message:\nCompare them.')
  })

  it('merges a group into one numbered block and keeps later pills aligned', () => {
    const attachments: ChatMessageAttachmentRecord[] = [
      fileAttachment('intro'),
      groupMember('g1', 'dossier', 'doc-a'),
      groupMember('g1', 'dossier', 'doc-b'),
      fileAttachment('outro'),
    ]
    const result = contentWithAttachmentContext('Use [1], [2] and [3].', attachments)

    // The group is ONE block at [2], not two blocks that would shift [outro].
    expect(result).toContain('--- [1] @files:intro ---')
    expect(result).toContain('--- [2] @filegroups:dossier (2 documents) ---')
    expect(result).toContain('--- [3] @files:outro ---')
    expect(result).not.toContain('[4]')

    // Both group members appear under the single [2] block.
    const groupBlock = result.slice(
      result.indexOf('--- [2]'),
      result.indexOf('--- End context 2 ---'),
    )
    expect(groupBlock).toContain('Title: doc-a title')
    expect(groupBlock).toContain('Title: doc-b title')
    expect(groupBlock).toContain('doc-a body')
    expect(groupBlock).toContain('doc-b body')
  })

  it('sends each member in full without silent truncation', () => {
    const long = 'x'.repeat(MAX_DOC_CHARS_SOFT + 50)
    const result = contentWithAttachmentContext('Summarize.', [
      groupMember('g1', 'dossier', 'big', { contentMarkdown: long }),
      groupMember('g1', 'dossier', 'small', { contentMarkdown: 'short body' }),
    ])
    expect(result).not.toContain('[Context truncated.]')
    expect(result).toContain('short body')
    expect(result).toContain(long)
  })
})
