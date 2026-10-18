import { describe, expect, it } from 'vitest'

import type {
  ServerAsset,
  ServerAssetGroup,
  ServerAssetSection,
} from '@/api/inqtrixClient'
import type { FileAssetRecord, FileLibrarySectionRecord } from '@/features/project/types'
import { syncCollection } from '@/features/project/syncCollection'
import {
  createDefaultFileLibrarySections,
  defaultFileSectionIdReplacements,
} from '@/features/files/sections'
import {
  assetAutosaveFingerprint,
  assetRecordFromServer,
  groupRecordFromServer,
  isAssetSettledForSync,
  sectionRecordFromServer,
  serverAssetPayload,
  serverGroupPayload,
  serverSectionPayload,
  serverUploadBinding,
  visibleServerAssetSections,
} from './assetSync'

describe('assetSync converters', () => {
  it('round-trips a section and group', () => {
    const section: ServerAssetSection = {
      id: 'file-section-1', kind: 'custom', title: 'My collection',
      semantic_role: 'custom',
      created_at: 100, updated_at: 200,
    }
    const sectionRecord = sectionRecordFromServer(section)
    expect(sectionRecord.kind).toBe('custom')
    expect(sectionRecord.semanticRole).toBe('custom')
    const sectionPayload = serverSectionPayload(sectionRecord)
    expect(sectionPayload).toMatchObject({ kind: 'custom', title: 'My collection', created_at: 100, updated_at: 200 })

    const group: ServerAssetGroup = {
      id: 'file-group-1', section_id: 'file-section-1', title: 'G',
      created_at: 5, updated_at: 6,
    }
    const groupRecord = groupRecordFromServer(group)
    expect(groupRecord.sectionId).toBe('file-section-1')
    expect(serverGroupPayload(groupRecord)).toMatchObject({ section_id: 'file-section-1', created_at: 5, updated_at: 6 })
  })

  it('round-trips an asset WITH its extracted text', () => {
    const server: ServerAsset = {
      id: 'file-asset-1', section_id: 'file-section-1', group_id: 'file-group-1',
      title: 'Doc', label: 'Doc.pdf', file_name: 'Doc.pdf', mime_type: 'application/pdf',
      origin: 'library', page_count: 12, parse_status: 'parsed', parse_warning: null,
      text_truncated: true, size_bytes: 4096, server_file_id: 'fl_9',
      parser_id: 'markitdown',
      prepared_parser_id: 'markitdown',
      prepared_content_hash: 'sha256:canonical',
      prepared_at: 1_700_000_000,
      extracted_text: 'the heavy body', created_at: 1_699_000_000, updated_at: 1_700_000_000,
      prepared_text: 'canonical prepared body',
    }
    const record = assetRecordFromServer(server)
    expect(record.extractedText).toBe('the heavy body')
    expect(record.groupId).toBe('file-group-1')
    expect(record.serverFileId).toBe('fl_9')
    expect(record.parserId).toBe('markitdown')
    expect(record.preparedParserId).toBe('markitdown')
    expect(record.preparedContentHash).toBe('sha256:canonical')
    expect(record.preparedText).toBe('canonical prepared body')
    expect(record.textTruncated).toBe(true)

    const payload = serverAssetPayload(record)
    expect(payload.extracted_text).toBe('the heavy body')
    expect(payload.section_id).toBe('file-section-1')
    expect(payload.group_id).toBe('file-group-1')
    expect(payload.server_file_id).toBe('fl_9')
    expect(payload.parser_id).toBe('markitdown')
    expect(payload.created_at).toBe(server.created_at)
    expect(payload.updated_at).toBe(server.updated_at)
  })

  it('treats a list row (no extracted_text) as an empty body', () => {
    const record = assetRecordFromServer({
      id: 'file-asset-2', section_id: 'file-section-1', group_id: null,
      title: 'Listed', label: 'l', file_name: 'l.pdf', mime_type: 'application/pdf',
      origin: 'library', page_count: null, parse_status: 'parsed', parse_warning: null,
      text_truncated: false, size_bytes: 0, server_file_id: null,
      created_at: 1, updated_at: 1,
    })
    expect(record.extractedText).toBe('')
    expect(record.groupId).toBe(null)
    expect(record.serverFileId).toBe(null)
    // A list-row record carries a null body -> the payload must not send "" as
    // a real body if pushed without loading (the hook guards this; here we
    // only assert the converter is faithful).
    expect(serverAssetPayload(record).extracted_text).toBe('')
  })

  it('hydrates the durable upload lifecycle from metadata rows', () => {
    const base: ServerAsset = {
      id: 'file-asset-upload', section_id: 'file-section-1', group_id: null,
      title: 'Listed', label: 'l', file_name: 'l.pdf', mime_type: 'application/pdf',
      origin: 'library', page_count: null, parse_status: 'parsed', parse_warning: null,
      text_truncated: false, size_bytes: 12, server_file_id: null,
      created_at: 1, updated_at: 2,
    }
    expect(assetRecordFromServer({
      ...base,
      upload_error: 'Objektspeicher antwortet nicht; Wiederholung 2/4.',
      upload_operation_id: 'up_1',
      upload_status: 'retrying',
    })).toMatchObject({
      uploadError: 'Objektspeicher antwortet nicht; Wiederholung 2/4.',
      uploadOperationId: 'up_1',
      uploadPending: true,
      uploadStatus: 'retrying',
    })
    expect(assetRecordFromServer({ ...base, upload_status: 'awaiting_upload' })).toMatchObject({
      uploadOperationId: null,
      uploadPending: true,
      uploadStatus: 'awaiting_upload',
    })
    expect(assetRecordFromServer({
      ...base,
      upload_error: 'Speicher nicht erreichbar.',
      upload_status: 'failed',
    })).toMatchObject({
      uploadError: 'Speicher nicht erreichbar.',
      uploadPending: false,
    })
    expect(assetRecordFromServer({
      ...base,
      server_file_id: 'fl_ready',
      upload_status: 'ready',
    })).toMatchObject({ uploadError: null, uploadPending: false })
  })

  it('normalizes unknown enum values to safe defaults', () => {
    const record = assetRecordFromServer({
      id: 'file-asset-3', section_id: 's', group_id: null, title: 'x', label: 'x',
      file_name: 'x', mime_type: 'x', origin: 'bogus', page_count: null,
      parse_status: 'weird', parse_warning: null, text_truncated: false,
      size_bytes: 0, server_file_id: null, created_at: 1, updated_at: 1,
    })
    expect(record.origin).toBe('library')
    expect(record.parseStatus).toBe('parsed')
    const section = sectionRecordFromServer({
      id: 's', kind: 'nope', title: 't', semantic_role: null,
      created_at: 1, updated_at: 1,
    })
    expect(section.kind).toBe('custom')
  })
})

describe('transient upload state and the sync boundary', () => {
  const baseRecord: FileAssetRecord = {
    createdAt: '2026-01-01T00:00:00.000Z',
    extractedText: 'body',
    fileName: 'a.pdf',
    groupId: null,
    id: 'file-a',
    label: 'a',
    mimeType: 'application/pdf',
    origin: 'library',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-1',
    sizeBytes: 1,
    textTruncated: false,
    title: 'a',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }

  it('holds back mid-upload/mid-parse rows and releases settled ones', () => {
    expect(isAssetSettledForSync({ ...baseRecord, uploadPending: true })).toBe(false)
    expect(isAssetSettledForSync({ ...baseRecord, parsePending: true })).toBe(false)
    expect(isAssetSettledForSync({ ...baseRecord, uploadError: 'kaputt' })).toBe(true)
    expect(isAssetSettledForSync(baseRecord)).toBe(true)
  })

  it('keeps a tracked upload on its server fingerprint until parsing settles', () => {
    const serverFingerprint = '2026-01-01T00:00:00.000Z'
    const locallyAdvanced = {
      ...baseRecord,
      extractedText: '',
      parsePending: true,
      serverSynced: true,
      updatedAt: '2026-01-01T00:00:01.000Z',
      uploadPending: false,
      uploadStatus: 'ready' as const,
    }

    expect(assetAutosaveFingerprint(
      locallyAdvanced,
      serverFingerprint,
    )).toBe(serverFingerprint)
    expect(assetAutosaveFingerprint(
      { ...locallyAdvanced, parsePending: false },
      serverFingerprint,
    )).toBe(locallyAdvanced.updatedAt)
  })

  it('does not issue a full-record push from an incomplete tracked upload', async () => {
    const serverFingerprint = baseRecord.updatedAt
    const incomplete = {
      ...baseRecord,
      extractedText: '',
      parsePending: true,
      serverSynced: true,
      updatedAt: '2026-01-01T00:00:01.000Z',
      uploadPending: false,
      uploadStatus: 'ready' as const,
    }
    const pushedBodies: string[] = []
    const synced = new Map([[incomplete.id, serverFingerprint]])

    await syncCollection({
      changed: (previous, current) => previous !== current,
      current: { [incomplete.id]: incomplete },
      deleteOne: async () => undefined,
      fingerprintOf: (asset) => assetAutosaveFingerprint(
        asset,
        synced.get(asset.id),
      ),
      pushOne: async (asset) => {
        pushedBodies.push(asset.extractedText)
      },
      synced,
    })

    expect(pushedBodies).toEqual([])

    const settled = {
      ...incomplete,
      extractedText: 'server-confirmed body',
      parsePending: false,
    }
    await syncCollection({
      changed: (previous, current) => previous !== current,
      current: { [settled.id]: settled },
      deleteOne: async () => undefined,
      fingerprintOf: (asset) => assetAutosaveFingerprint(
        asset,
        synced.get(asset.id),
      ),
      pushOne: async (asset) => {
        pushedBodies.push(asset.extractedText)
      },
      synced,
    })
    await syncCollection({
      changed: (previous, current) => previous !== current,
      current: { [settled.id]: settled },
      deleteOne: async () => undefined,
      fingerprintOf: (asset) => assetAutosaveFingerprint(
        asset,
        synced.get(asset.id),
      ),
      pushOne: async (asset) => {
        pushedBodies.push(asset.extractedText)
      },
      synced,
    })

    expect(pushedBodies).toEqual(['server-confirmed body'])
    expect(synced.get(settled.id)).toBe(settled.updatedAt)
  })

  it('never leaks transient flags into the asset wire payload', () => {
    const payload = serverAssetPayload({
      ...baseRecord,
      parsePending: true,
      uploadError: 'kaputt',
      uploadPending: true,
    })
    expect('uploadPending' in payload).toBe(false)
    expect('uploadError' in payload).toBe(false)
    expect('parsePending' in payload).toBe(false)
  })

  it('maps an upload binding to the wire form fields (unix seconds, snake_case)', () => {
    expect(serverUploadBinding({
      assetId: 'file-a',
      createdAt: '2026-01-01T00:00:10.000Z',
      groupId: 'file-group-1',
      label: 'a',
      origin: 'library',
      sectionId: 'file-section-1',
      title: 'a.pdf',
      updatedAt: '2026-01-01T00:00:20.000Z',
    })).toEqual({
      asset_id: 'file-a',
      created_at: new Date('2026-01-01T00:00:10.000Z').getTime() / 1000,
      group_id: 'file-group-1',
      label: 'a',
      origin: 'library',
      section_id: 'file-section-1',
      title: 'a.pdf',
      updated_at: new Date('2026-01-01T00:00:20.000Z').getTime() / 1000,
    })
  })
})

describe('visibleServerAssetSections', () => {
  const section = (
    id: string,
    title: string,
    kind: FileLibrarySectionRecord['kind'] = 'custom',
    updatedAt = '2026-01-01T00:00:00.000Z',
  ): FileLibrarySectionRecord => ({
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    kind,
    title,
    updatedAt,
  })

  it('projects ambiguous empty legacy signatures without deleting them', () => {
    const sections = [
      ...Array.from({ length: 260 }, (_, index) =>
        section(`library-${index}`, 'Bibliothek')),
      ...Array.from({ length: 257 }, (_, index) =>
        section(`sources-${index}`, 'Projekt-Quellen')),
      ...Array.from({ length: 20 }, (_, index) =>
        section(`temp-${index}`, 'Temporäre Dateien', 'temporary')),
    ]

    const visible = visibleServerAssetSections(sections, [], [])

    expect(visible.filter((item) => item.title === 'Bibliothek')).toHaveLength(1)
    expect(
      visible.filter((item) => item.title === 'Projekt-Quellen'),
    ).toHaveLength(1)
    expect(
      visible.filter((item) => item.title === 'Temporäre Dateien'),
    ).toHaveLength(1)
  })

  it('preserves referenced, renamed, and deliberately identical user sections', () => {
    const pristineUsed = section('library-used', 'Bibliothek')
    const pristineUnused = section('library-unused', 'Bibliothek')
    const renamed = section(
      'renamed',
      'Bibliothek',
      'custom',
      '2026-01-02T00:00:00.000Z',
    )
    const sameTitleUserSection = section(
      'same-title-user',
      'Bibliothek',
      'custom',
      '2026-01-03T00:00:00.000Z',
    )

    const visible = visibleServerAssetSections(
      [pristineUnused, pristineUsed, renamed, sameTitleUserSection],
      [{
        createdAt: pristineUsed.createdAt,
        id: 'group-1',
        sectionId: pristineUsed.id,
        title: 'Real group',
        updatedAt: pristineUsed.updatedAt,
      }],
      [],
    )

    expect(visible.map((item) => item.id)).toEqual([
      pristineUsed.id,
      renamed.id,
      sameTitleUserSection.id,
    ])
  })

  it('never coalesces explicit custom sections that share a prepared title', () => {
    const customA = { ...section('custom-a', 'Bibliothek'), semanticRole: 'custom' as const }
    const customB = { ...section('custom-b', 'Bibliothek'), semanticRole: 'custom' as const }
    const canonical = { ...section('canonical', 'Bibliothek'), semanticRole: 'library' as const }

    expect(
      visibleServerAssetSections([canonical, customA, customB], [], [])
        .map((item) => item.id),
    ).toEqual(['canonical', 'custom-a', 'custom-b'])
  })
})

describe('defaultFileSectionIdReplacements', () => {
  it('rekeys only explicit bootstrap roles and never matching custom titles', () => {
    const [temporary, library, sources] = createDefaultFileLibrarySections(
      '2026-01-01T00:00:00.000Z',
    )
    const deliberateCustom: FileLibrarySectionRecord = {
      createdAt: library.createdAt,
      id: 'deliberate-custom',
      kind: 'custom',
      semanticRole: 'custom',
      title: 'Bibliothek',
      updatedAt: library.updatedAt,
    }
    const canonical = [
      { ...temporary, id: 'server-temp', isBootstrapPlaceholder: false },
      { ...library, id: 'server-library', isBootstrapPlaceholder: false },
      { ...sources, id: 'server-sources', isBootstrapPlaceholder: false },
    ]

    expect(defaultFileSectionIdReplacements(
      Object.fromEntries(
        [temporary, library, sources, deliberateCustom].map((item) => [item.id, item]),
      ),
      canonical,
    )).toEqual({
      [temporary.id]: 'server-temp',
      [library.id]: 'server-library',
      [sources.id]: 'server-sources',
    })
  })
})
