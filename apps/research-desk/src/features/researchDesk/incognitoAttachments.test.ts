import { describe, expect, it } from 'vitest'
import type { FileParser, ParsedFile } from '@/features/files/parsing'
import { createEmptyProjectState } from '@/features/project/seedProject'
import { chatAttachmentsFromRefs } from '@/features/project/selectors'
import type { FileAssetRecord } from '@/features/project/types'
import { chatStateForIncognito, ingestIncognitoFiles } from './incognitoAttachments'

const stubParsed: ParsedFile = {
  extractedText: 'incognito body',
  pageCount: null,
  parseStatus: 'parsed',
  parseWarning: null,
  textTruncated: false,
}

const stubParser: FileParser = {
  parse: async () => stubParsed,
  supports: () => true,
}

function textFile(name: string): File {
  return new File(['incognito body'], name, { type: 'text/plain' })
}

function makeAsset(id: string, label: string): FileAssetRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    extractedText: `${label} content`,
    fileName: `${label}.txt`,
    groupId: null,
    id,
    label,
    mimeType: 'text/plain',
    origin: 'chat',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-temp',
    sizeBytes: 12,
    textTruncated: false,
    title: `${label}.txt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
    serverFileId: null,
    parserId: 'client',
  }
}

describe('ingestIncognitoFiles', () => {
  it('parses client-side without ever uploading the bytes', async () => {
    const assets = await ingestIncognitoFiles([textFile('Secret.txt')], [], stubParser)
    expect(assets).toHaveLength(1)
    // The privacy invariant: no server file id means the bytes never left the
    // device, yet the client parse still carries the text the LLM needs.
    expect(assets[0].serverFileId).toBeNull()
    expect(assets[0].parserId).toBe('client')
    expect(assets[0].extractedText).toBe('incognito body')
    expect(assets[0].origin).toBe('chat')
  })
})

describe('chatStateForIncognito', () => {
  it('resolves incognito refs through the merged view without touching the synced store', () => {
    const base = createEmptyProjectState()
    const incognitoAssets = { i1: makeAsset('i1', 'incog') }
    const merged = chatStateForIncognito(base, incognitoAssets)

    const ref = { fileId: 'i1', kind: 'file-asset' } as const
    const resolved = chatAttachmentsFromRefs(merged, [ref])
    expect(resolved).toHaveLength(1)
    expect(resolved[0]).toMatchObject({ contentMarkdown: 'incog content', kind: 'file-asset' })

    // The synced store is untouched: the incognito asset is invisible to the
    // base state (so it can never sync or appear in the library).
    expect(base.fileAssets).toEqual({})
    expect(chatAttachmentsFromRefs(base, [ref])).toHaveLength(0)
  })
})
