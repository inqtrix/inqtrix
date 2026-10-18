import { describe, expect, it } from 'vitest'
import { createEmptyProjectState } from './seedProject'
import {
  attachmentContextReadiness,
  chatRuleOptions,
  chatAttachmentChipsFromRefs,
  chatAttachmentsFromRefs,
  chatContextRefKey,
  completedReportOptions,
  mentionableReportOptions,
  dedupeChatContextRefs,
  displayRelativeAge,
  fileAssetReferenceCount,
  fileAssetReferenceCounts,
  isResearchDeskRun,
  projectAgentTargetEditorDocuments,
  projectAllKnowledgeItems,
  projectChatRules,
  projectKnowledgeItems,
  projectKnowledgeSessionSections,
  projectKnowledgeSessions,
  referenceDocsFromRefs,
  researchRunToJob,
} from './selectors'
import type {
  ChatMessageAttachmentRecord,
  ChatRuleRecord,
  ChatThreadRecord,
  EditorDocumentRecord,
  FileAssetRecord,
  FileGroupRecord,
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
  ProjectState,
  ResearchRunRecord,
  VectorIndexRecord,
} from './types'

describe('displayRelativeAge', () => {
  const now = new Date('2026-06-26T12:00:00.000Z')

  it('formats compact German age labels', () => {
    expect(displayRelativeAge('2026-06-26T11:59:20.000Z', 'de', now)).toBe('Gerade eben')
    expect(displayRelativeAge('2026-06-26T11:54:00.000Z', 'de', now)).toBe('6 Min.')
    expect(displayRelativeAge('2026-06-26T10:00:00.000Z', 'de', now)).toBe('2 Std.')
    expect(displayRelativeAge('2026-06-23T12:00:00.000Z', 'de', now)).toBe('3 Tage')
    expect(displayRelativeAge('2026-06-05T12:00:00.000Z', 'de', now)).toBe('3 W')
  })

  it('formats compact English age labels', () => {
    expect(displayRelativeAge('2026-06-26T11:59:20.000Z', 'en', now)).toBe('Just now')
    expect(displayRelativeAge('2026-06-26T11:54:00.000Z', 'en', now)).toBe('6 min')
    expect(displayRelativeAge('2026-06-26T10:00:00.000Z', 'en', now)).toBe('2 h')
    expect(displayRelativeAge('2026-06-23T12:00:00.000Z', 'en', now)).toBe('3 d')
    expect(displayRelativeAge('2026-06-05T12:00:00.000Z', 'en', now)).toBe('3 w')
  })

  it('returns an empty label for invalid dates', () => {
    expect(displayRelativeAge('not-a-date', 'de', now)).toBe('')
    expect(displayRelativeAge('2026-06-26T12:00:00.000Z', 'en', new Date('invalid'))).toBe('')
  })
})

describe('projectAgentTargetEditorDocuments', () => {
  it('keeps local recovery copies visible in project state but out of agent targeting', () => {
    const base = createEmptyProjectState()
    const persisted = {
      contentMarkdown: '# Persisted',
      createdAt: '2026-07-29T10:00:00.000Z',
      folderId: null,
      id: 'persisted-document',
      metadataRevision: 1,
      revision: 1,
      serverSynced: true,
      source: 'blank',
      title: 'Persisted.md',
      updatedAt: '2026-07-29T10:00:00.000Z',
    } satisfies EditorDocumentRecord
    const recovery = {
      ...persisted,
      id: 'editor-recovery-local',
      metadataRevision: undefined,
      recovery: {
        capturedAt: '2026-07-29T10:01:00.000Z',
        originalDocumentId: persisted.id,
        reason: 'remote_deleted',
      },
      revision: 0,
      serverSynced: undefined,
      title: 'Recovered.md',
    } satisfies EditorDocumentRecord
    const state: ProjectState = {
      ...base,
      editorDocumentOrder: [persisted.id, recovery.id],
      editorDocuments: {
        [persisted.id]: persisted,
        [recovery.id]: recovery,
      },
    }

    expect(projectAgentTargetEditorDocuments(state).map((document) => document.id))
      .toEqual([persisted.id])
    expect(state.editorDocuments[recovery.id]).toBe(recovery)
  })
})

function makeReportRun(
  runId: string,
  mode: ResearchRunRecord['mode'],
): ResearchRunRecord {
  // Only the fields completedReportOptions reads matter here; the cast keeps
  // the test focused on the mode filter, not on a full run-record fixture.
  return {
    mode,
    result: { markdown: `# ${runId}` },
    runId,
    status: 'completed',
    summary: { title: runId },
  } as unknown as ResearchRunRecord
}

function makeAsset(id: string, label: string, overrides: Partial<FileAssetRecord> = {}): FileAssetRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    extractedText: `${label} content`,
    fileName: `${label}.txt`,
    groupId: null,
    id,
    label,
    mimeType: 'text/plain',
    origin: 'library',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-temp',
    sizeBytes: 12,
    textTruncated: false,
    title: `${label}.txt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

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

function makeKnowledgeItem(
  id: string,
  sessionId: string,
  overrides: Partial<KnowledgeThreadItemRecord> = {},
): KnowledgeThreadItemRecord {
  return {
    collectionTitles: [],
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    progress: { steps: [] },
    question: id,
    requestedProfile: null,
    runId: null,
    sessionId,
    status: 'completed',
    ...overrides,
  }
}

function makeKnowledgeSession(
  id: string,
  title: string,
  overrides: Partial<KnowledgeSessionRecord> = {},
): KnowledgeSessionRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeKnowledgeSessionGroup(
  id: string,
  title: string,
  overrides: Partial<KnowledgeSessionGroupRecord> = {},
): KnowledgeSessionGroupRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function stateWith(assets: FileAssetRecord[], groups: FileGroupRecord[] = []): ProjectState {
  const base = createEmptyProjectState()
  return {
    ...base,
    fileAssetOrder: assets.map((asset) => asset.id),
    fileAssets: Object.fromEntries(assets.map((asset) => [asset.id, asset])),
    fileGroupOrder: groups.map((group) => group.id),
    fileGroups: Object.fromEntries(groups.map((group) => [group.id, group])),
  }
}

function stateWithRules(rules: ChatRuleRecord[]): ProjectState {
  const base = createEmptyProjectState()
  return {
    ...base,
    chatRuleOrder: rules.map((rule) => rule.id),
    chatRules: Object.fromEntries(rules.map((rule) => [rule.id, rule])),
  }
}

describe('chatContextRefKey', () => {
  it('produces a stable key per reference kind', () => {
    expect(chatContextRefKey({ fileId: 'f1', kind: 'file-asset' })).toBe('file-asset:f1')
    expect(chatContextRefKey({ groupId: 'g1', kind: 'file-group' })).toBe('file-group:g1')
  })
})

describe('dedupeChatContextRefs', () => {
  it('keeps the first occurrence of each reference', () => {
    const refs = dedupeChatContextRefs([
      { fileId: 'f1', kind: 'file-asset' },
      { fileId: 'f1', kind: 'file-asset' },
      { groupId: 'g1', kind: 'file-group' },
    ])
    expect(refs).toEqual([
      { fileId: 'f1', kind: 'file-asset' },
      { groupId: 'g1', kind: 'file-group' },
    ])
  })
})

describe('chatAttachmentsFromRefs', () => {
  it('resolves a file-asset reference to one attachment', () => {
    const state = stateWith([makeAsset('f1', 'alpha')])
    const attachments = chatAttachmentsFromRefs(state, [{ fileId: 'f1', kind: 'file-asset' }])
    expect(attachments).toHaveLength(1)
    expect(attachments[0]).toMatchObject({ contentMarkdown: 'alpha content', kind: 'file-asset', label: 'alpha' })
  })

  it('expands a file-group reference to one attachment per member', () => {
    const group: FileGroupRecord = {
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'g1',
      sectionId: 'file-section-temp',
      title: 'Dossier',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const state = stateWith(
      [makeAsset('f1', 'alpha', { groupId: 'g1' }), makeAsset('f2', 'beta', { groupId: 'g1' })],
      [group],
    )
    const attachments = chatAttachmentsFromRefs(state, [{ groupId: 'g1', kind: 'file-group' }])
    expect(attachments.map((attachment) => attachment.label)).toEqual(['alpha', 'beta'])
    expect(attachments.every((attachment) => attachment.kind === 'file-group')).toBe(true)
  })

  it('renders context-pack rules with linked database files as a stored snapshot', () => {
    const state = {
      ...stateWith([makeAsset('f1', 'alpha')]),
      chatRuleOrder: ['r1'],
      chatRules: {
        r1: makeRule('r1', 'cv', {
          category: 'context',
          contentMarkdown: 'Use this profile context:\n{{context}}\nThen answer.',
          linkedContextRefs: [{ fileId: 'f1', kind: 'file-asset' }],
        }),
      },
    }

    const attachments = chatAttachmentsFromRefs(state, [{ kind: 'chat-rule', ruleId: 'r1' }])

    expect(attachments).toHaveLength(1)
    expect(attachments[0]).toMatchObject({ kind: 'chat-rule', label: 'cv' })
    expect(attachments[0].contentMarkdown).toContain('Use this profile context:')
    expect(attachments[0].contentMarkdown).toContain('--- [1] @files:alpha ---')
    expect(attachments[0].contentMarkdown).toContain('alpha content')
    expect(attachments[0].contentMarkdown).toContain('Then answer.')
    expect(attachments[0].contentMarkdown).not.toContain('{{context}}')
  })
})

describe('attachmentContextReadiness', () => {
  it('blocks normal attachments until the durable upload is ready and exposes retryable failures', () => {
    const pending = stateWith([
      makeAsset('f1', 'alpha', {
        serverFileId: null,
        uploadPending: true,
        uploadStatus: 'uploading',
      }),
    ])
    expect(attachmentContextReadiness(
      pending,
      [{ fileId: 'f1', kind: 'file-asset' }],
    )).toMatchObject({ reason: 'upload_pending', status: 'pending' })

    const failed = stateWith([
      makeAsset('f1', 'alpha', {
        serverFileId: null,
        uploadError: 'storage unavailable',
        uploadStatus: 'failed',
      }),
    ])
    expect(attachmentContextReadiness(
      failed,
      [{ fileId: 'f1', kind: 'file-asset' }],
    )).toEqual({
      error: 'storage unavailable',
      reason: 'upload_failed',
      retryAssetIds: ['f1'],
      status: 'failed',
    })
  })

  it('keeps a metadata-only server attachment pending until its body load settles', () => {
    const state = stateWith([
      makeAsset('f1', 'alpha', {
        extractedText: '',
        preparedAt: '2026-01-01T00:00:00.000Z',
        preparedContentHash: 'sha256:prepared',
        preparedParserId: 'markitdown',
        preparedText: '',
        serverFileId: 'fl_1',
        uploadStatus: 'ready',
      }),
    ])
    const ref = [{ fileId: 'f1', kind: 'file-asset' }] as const

    expect(attachmentContextReadiness(state, ref)).toMatchObject({
      reason: 'upload_pending',
      status: 'pending',
    })
    expect(attachmentContextReadiness(state, ref, {
      bodyLoadStates: {
        f1: { error: 'body unavailable', status: 'failed' },
      },
    })).toMatchObject({
      error: 'body unavailable',
      reason: 'content_empty',
      status: 'failed',
    })
    expect(attachmentContextReadiness(state, ref, {
      assetBodyOverride: new Map([['f1', 'server body']]),
      requireContent: true,
    })).toMatchObject({ reason: null, status: 'ready' })
    expect(attachmentContextReadiness(state, ref, {
      assetBodyOverride: new Map([['f1', '']]),
      requireContent: true,
    })).toMatchObject({ reason: 'content_empty', status: 'failed' })
  })

  it('treats groups atomically and never drops a failed child silently', () => {
    const group: FileGroupRecord = {
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'g1',
      sectionId: 'file-section-temp',
      title: 'Dossier',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const state = stateWith([
      makeAsset('f1', 'alpha', {
        preparedAt: '2026-01-01T00:00:00.000Z',
        preparedContentHash: 'sha256:prepared',
        preparedParserId: 'markitdown',
        preparedText: 'alpha content',
        serverFileId: 'fl_1',
        uploadStatus: 'ready',
      }),
      makeAsset('f2', 'beta', {
        groupId: 'g1',
        uploadError: 'retry me',
        uploadStatus: 'failed',
      }),
    ], [group])
    state.fileAssets.f1.groupId = 'g1'

    const ref = [{ groupId: 'g1', kind: 'file-group' }] as const
    expect(attachmentContextReadiness(state, ref)).toEqual({
      error: 'retry me',
      reason: 'upload_failed',
      retryAssetIds: ['f2'],
      status: 'failed',
    })
    expect(chatAttachmentChipsFromRefs(state, ref)[0]).toMatchObject({
      readiness: 'failed',
      retryAssetIds: ['f2'],
    })

    expect(attachmentContextReadiness(stateWith([], [group]), ref)).toMatchObject({
      reason: 'group_empty',
      status: 'failed',
    })

    expect(attachmentContextReadiness(
      stateWith([], [{ ...group, lifecycleStatus: 'deleting' }]),
      ref,
    )).toMatchObject({
      reason: 'source_deleting',
      status: 'failed',
    })
  })

  it('never admits a bound client body without explicit server preparation', () => {
    const state = stateWith([
      makeAsset('f1', 'client-only', {
        extractedText: 'browser extracted body',
        parserId: 'markitdown',
        serverFileId: 'fl_1',
        uploadStatus: 'ready',
      }),
    ])
    const ref = [{ fileId: 'f1', kind: 'file-asset' }] as const

    expect(attachmentContextReadiness(state, ref)).toMatchObject({
      reason: 'server_preparation_missing',
      status: 'failed',
    })
    expect(attachmentContextReadiness(state, ref, {
      assetBodyOverride: new Map([['f1', 'browser extracted body']]),
      requireContent: true,
    })).toMatchObject({
      reason: 'server_preparation_missing',
      status: 'failed',
    })
  })

  it('allows local attachment bodies only for the explicit incognito path', () => {
    const state = stateWith([makeAsset('f1', 'alpha')])
    const ref = [{ fileId: 'f1', kind: 'file-asset' }] as const

    expect(attachmentContextReadiness(state, ref)).toMatchObject({
      reason: 'upload_not_bound',
      status: 'failed',
    })
    expect(attachmentContextReadiness(state, ref, {
      allowLocalFiles: true,
      requireContent: true,
    })).toMatchObject({ reason: null, status: 'ready' })
  })
})

describe('projectChatRules', () => {
  it('normalizes legacy rules without new prompt-library fields', () => {
    const state = stateWithRules([makeRule('r1', 'legacy')])

    expect(projectChatRules(state)[0]).toMatchObject({
      category: 'instruction',
      includeInAutocomplete: true,
      linkedContextRefs: [],
      visibility: { agent: false, chat: true, editor: true },
    })
  })
})

describe('knowledge selectors', () => {
  it('filters thread items to the selected knowledge session', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const state: ProjectState = {
      ...base,
      knowledgeItemOrder: ['ki-1', 'ki-2'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId),
        'ki-2': makeKnowledgeItem('ki-2', 'ks-2'),
      },
      knowledgeSessionOrder: [defaultSessionId, 'ks-2'],
      knowledgeSessions: {
        ...base.knowledgeSessions,
        'ks-2': {
          createdAt: '2026-01-01T00:00:00.000Z',
          id: 'ks-2',
          title: 'Second',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      selectedKnowledgeSessionId: 'ks-2',
    }

    expect(projectKnowledgeItems(state).map((item) => item.id)).toEqual(['ki-2'])
    expect(projectAllKnowledgeItems(state).map((item) => item.id)).toEqual(['ki-1', 'ki-2'])
    expect(projectKnowledgeSessions(state).map((session) => session.id)).toEqual([defaultSessionId, 'ks-2'])
  })

  it('groups knowledge sessions into folder sections and keeps invalid memberships ungrouped', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const state: ProjectState = {
      ...base,
      knowledgeSessionGroupMemberships: {
        'ks-2': 'kg-1',
        'ks-3': 'missing-group',
      },
      knowledgeSessionGroupOrder: ['kg-1'],
      knowledgeSessionGroups: {
        'kg-1': makeKnowledgeSessionGroup('kg-1', 'Client work'),
      },
      knowledgeSessionOrder: [defaultSessionId, 'ks-2', 'ks-3'],
      knowledgeSessions: {
        ...base.knowledgeSessions,
        'ks-2': makeKnowledgeSession('ks-2', 'Inside folder'),
        'ks-3': makeKnowledgeSession('ks-3', 'Loose session'),
      },
    }

    const sections = projectKnowledgeSessionSections(state)

    expect(sections).toHaveLength(2)
    expect(sections[0]).toMatchObject({ groupId: 'kg-1', kind: 'group' })
    expect(sections[0].sessions.map((session) => session.id)).toEqual(['ks-2'])
    expect(sections[1]).toMatchObject({ groupId: null, kind: 'ungrouped' })
    expect(sections[1].sessions.map((session) => session.id)).toEqual([defaultSessionId, 'ks-3'])
  })

  it('sorts folders alphabetically and rows by activity in recent mode, untouched in manual', () => {
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      knowledgeSessionGroupMemberships: {},
      knowledgeSessionGroupOrder: ['kg-z', 'kg-a'],
      knowledgeSessionGroups: {
        'kg-a': makeKnowledgeSessionGroup('kg-a', 'Alpha'),
        'kg-z': makeKnowledgeSessionGroup('kg-z', 'Zeta'),
      },
      knowledgeSessionOrder: ['ks-old', 'ks-new'],
      knowledgeSessions: {
        'ks-new': makeKnowledgeSession('ks-new', 'Newer', { updatedAt: '2026-08-29T10:00:00.000Z' }),
        'ks-old': makeKnowledgeSession('ks-old', 'Older', { updatedAt: '2026-08-01T10:00:00.000Z' }),
      },
    }

    const recent = projectKnowledgeSessionSections(state)
    expect(recent.map((section) => section.groupId)).toEqual(['kg-a', 'kg-z', null])
    expect(recent[2].sessions.map((session) => session.id)).toEqual(['ks-new', 'ks-old'])

    const manual = projectKnowledgeSessionSections({
      ...state,
      ui: { ...state.ui, explorerSort: { ...state.ui.explorerSort, knowledge: 'manual' } },
    })
    expect(manual.map((section) => section.groupId)).toEqual(['kg-z', 'kg-a', null])
    expect(manual[2].sessions.map((session) => session.id)).toEqual(['ks-old', 'ks-new'])
  })
})

describe('chatRuleOptions', () => {
  it('filters autocomplete options by surface visibility and autocomplete status', () => {
    const state = stateWithRules([
      makeRule('r1', 'chat-only', {
        category: 'instruction',
        visibility: { agent: false, chat: true, editor: false },
      }),
      makeRule('r2', 'editor-only', {
        category: 'function',
        visibility: { agent: false, chat: false, editor: true },
      }),
      makeRule('r3', 'hidden-autocomplete', {
        category: 'context',
        includeInAutocomplete: false,
        visibility: { agent: false, chat: true, editor: true },
      }),
    ])

    expect(chatRuleOptions(state, 'chat').map((rule) => rule.label)).toEqual(['chat-only'])
    expect(chatRuleOptions(state, 'editor').map((rule) => rule.label)).toEqual(['editor-only'])
    expect(chatRuleOptions(state).map((rule) => rule.category)).toEqual(['instruction', 'function', 'context'])
  })

  it('carries the server identity next to the local id', () => {
    // The chat keys attachments by the local id; the Agent Desk sends the
    // server template id. Both must be readable from one option.
    const state = stateWithRules([
      makeRule('rule-local', 'briefing', {
        serverComposedChars: 1_250,
        serverRevision: 3,
        serverTemplateId: 'pt_brief',
        visibility: { agent: true, chat: true, editor: true },
      }),
      makeRule('rule-unsynced', 'local'),
    ])
    expect(chatRuleOptions(state).map((rule) => [
      rule.ruleId, rule.serverTemplateId, rule.serverRevision, rule.composedChars,
    ])).toEqual([
      ['rule-local', 'pt_brief', 3, 1_250],
      ['rule-unsynced', undefined, undefined, undefined],
    ])
  })
})

describe('referenceDocsFromRefs', () => {
  it('maps file and research-report refs to ReferenceDoc DTOs and skips chat rules', () => {
    const base = stateWith([makeAsset('f1', 'alpha', { pageCount: 3 })])
    const report = {
      result: { markdown: 'report body' },
      runId: 'r1',
      status: 'completed',
      summary: { title: 'My Report' },
    } as unknown as ProjectState['researchRuns'][string]
    const state: ProjectState = {
      ...base,
      researchRunOrder: ['r1'],
      researchRuns: { r1: report },
    }
    const docs = referenceDocsFromRefs(state, [
      { fileId: 'f1', kind: 'file-asset' },
      { kind: 'research-report', runId: 'r1' },
      { kind: 'chat-rule', ruleId: 'does-not-exist' },
    ])
    expect(docs).toEqual([
      { content: 'alpha content', label: 'alpha', pageCount: 3, sizeBytes: 12 },
      { content: 'report body', label: 'my-report', pageCount: null, sizeBytes: undefined },
    ])
  })
})

describe('fileAssetReferenceCounts', () => {
  const TS = '2026-01-01T00:00:00.000Z'

  function makeIndex(id: string, fileIds: string[]): VectorIndexRecord {
    return {
      createdAt: TS,
      dims: 4,
      handle: id,
      id,
      members: fileIds.map((fileId) => ({ fileId, state: 'embedded' as const })),
      model: 'text-embedding-3-small',
      status: 'ready',
      title: id,
      updatedAt: TS,
    }
  }

  function fileAttachment(fileId: string): ChatMessageAttachmentRecord {
    return {
      attachedAt: TS, contentMarkdown: '', fileId, kind: 'file-asset',
      label: fileId, pageCount: null, sizeBytes: 12, title: fileId,
    }
  }

  function groupAttachment(groupId: string): ChatMessageAttachmentRecord {
    return {
      attachedAt: TS, contentMarkdown: '', fileId: `${groupId}-first`,
      groupId, groupLabel: groupId, kind: 'file-group', label: groupId,
      pageCount: null, sizeBytes: 12, title: groupId,
    }
  }

  function makeThread(id: string, attachments: ChatMessageAttachmentRecord[][]): ChatThreadRecord {
    return {
      createdAt: TS,
      id,
      messages: attachments.map((messageAttachments, n) => ({
        attachments: messageAttachments,
        contentMarkdown: `m${n}`,
        createdAt: TS,
        id: `${id}-m${n}`,
        role: 'user',
      })),
      preview: '',
      source: 'imported',
      title: id,
      updatedAt: TS,
    }
  }

  function fixtureState(): ProjectState {
    return {
      ...stateWith([
        makeAsset('a1', 'a1', { groupId: 'g1' }),
        makeAsset('a2', 'a2', { groupId: 'g1' }),
        makeAsset('a3', 'a3'),
      ]),
      chatThreads: {
        // t1: a1 directly AND its group in one thread -> a1 counts once.
        t1: makeThread('t1', [[fileAttachment('a1'), groupAttachment('g1')]]),
        t2: makeThread('t2', [[groupAttachment('g1')]]),
        // t3: the same file in two messages -> the thread counts once.
        t3: makeThread('t3', [[fileAttachment('a3')], [fileAttachment('a3')]]),
      },
      vectorIndexOrder: ['i1', 'i2'],
      vectorIndexes: {
        // Duplicate member entries never double-count an index.
        i1: makeIndex('i1', ['a1', 'a1', 'a3']),
        i2: makeIndex('i2', ['a2']),
      },
    }
  }

  it('matches the per-id reference count for every asset', () => {
    const state = fixtureState()
    const counts = fileAssetReferenceCounts(state)
    for (const id of state.fileAssetOrder) {
      expect(counts.get(id) ?? 0).toBe(fileAssetReferenceCount(state, id))
    }
  })

  it('counts indexes once per file and threads once per asset (direct, group, or both)', () => {
    const counts = fileAssetReferenceCounts(fixtureState())
    expect(counts.get('a1')).toBe(3) // i1 + t1 (direct+group merged) + t2
    expect(counts.get('a2')).toBe(3) // i2 + t1 (via group) + t2
    expect(counts.get('a3')).toBe(2) // i1 (deduped member) + t3 (two messages)
  })

  it('yields zero for unreferenced assets, matching the per-id count', () => {
    const state = fixtureState()
    const lonely: ProjectState = {
      ...state,
      fileAssetOrder: [...state.fileAssetOrder, 'a4'],
      fileAssets: { ...state.fileAssets, a4: makeAsset('a4', 'a4') },
    }
    expect(fileAssetReferenceCounts(lonely).get('a4') ?? 0).toBe(0)
    expect(fileAssetReferenceCount(lonely, 'a4')).toBe(0)
  })
})

describe('isResearchDeskRun', () => {
  it('excludes only knowledge-mode runs; research/direct_llm/legacy count as desk runs', () => {
    expect(isResearchDeskRun({ mode: 'knowledge' } as ResearchRunRecord)).toBe(false)
    expect(isResearchDeskRun({ mode: 'research' } as ResearchRunRecord)).toBe(true)
    expect(isResearchDeskRun({ mode: 'direct_llm' } as ResearchRunRecord)).toBe(true)
    // A run with no mode is a legacy record and stays visible as a report.
    expect(isResearchDeskRun({} as ResearchRunRecord)).toBe(true)
  })
})

describe('completedReportOptions', () => {
  it('offers research/direct_llm/legacy reports but never knowledge-mode runs', () => {
    const state = createEmptyProjectState()
    state.researchRuns = {
      r1: makeReportRun('r1', 'research'),
      r2: makeReportRun('r2', 'direct_llm'),
      r3: makeReportRun('r3', 'knowledge'),
      r4: makeReportRun('r4', undefined),
    }
    state.researchRunOrder = ['r1', 'r2', 'r3', 'r4']

    const runIds = completedReportOptions(state).map((option) => option.runId)

    // The editor "import report" + chat @research lists must match the desk:
    // knowledge runs (the "Wissen" thread) never leak in as importable reports.
    expect(runIds).toEqual(['r1', 'r2', 'r4'])
  })
})

describe('mentionableReportOptions', () => {
  it('hides an opted-out report from the @-source but keeps it resolvable as an attached chip', () => {
    const state = createEmptyProjectState()
    state.researchRuns = {
      r1: makeReportRun('r1', 'research'),
      r2: { ...makeReportRun('r2', 'research'), includeInAutocomplete: false },
    }
    state.researchRunOrder = ['r1', 'r2']

    // The @-mention source drops the opted-out report...
    expect(mentionableReportOptions(state).map((option) => option.runId)).toEqual(['r1'])
    // ...but the shared selector still resolves it, so a chat that already
    // attached r2 keeps rendering its chip (gate the source, not the resolver).
    expect(completedReportOptions(state).map((option) => option.runId)).toEqual(['r1', 'r2'])
  })
})

describe('researchRunToJob live-status events', () => {
  it('carries the record id through so the live rows key on a stable identity', () => {
    // Rows keyed on `${formattedTime}-${index}` break, because the formatter
    // has MINUTE resolution. Two consequences, both visible: within one minute
    // the keys stay identical as the 4-row window slides, so React reuses a row
    // for a different event; across a minute boundary every key changes at once
    // and the whole block remounts and re-animates. The stable identity is the
    // record's id, so it has to reach the view.
    const run = {
      events: [
        { createdAt: '2026-08-22T11:48:04.000Z', id: 'run-1-41', severity: 'info', title: 'Plane Suchanfragen' },
        { createdAt: '2026-08-22T11:48:57.000Z', id: 'run-1-42', severity: 'info', title: 'Durchsuche 6 Suchanfragen' },
      ],
      metrics: { claims: 0, queries: 0, rounds: '1/4', sources: 0 },
      phaseState: { activePhase: 'search', completedPhases: [] },
      runId: 'run-1',
      status: 'running',
      submittedAt: '2026-08-22T11:47:00.000Z',
      summary: { title: 'Frage' },
    } as unknown as Parameters<typeof researchRunToJob>[0]

    const job = researchRunToJob(run)

    expect(job.events.map((event) => event.id)).toEqual(['run-1-41', 'run-1-42'])
    // Same minute, so `time` alone could not have told the two rows apart.
    expect(new Set(job.events.map((event) => event.time)).size).toBe(1)
  })
})
