import { describe, expect, it } from 'vitest'
import { createDefaultFileLibrarySections } from '@/features/files/sections'
import { createEmptyProjectState } from './seedProject'
import {
  buildProjectFiles,
  parseChatThread,
  parseChatRule,
  parseEditorDocument,
  parseFileAsset,
  parseProjectManifest,
  parseResearchRun,
  serializeChatThread,
  serializeChatRule,
  serializeEditorDocument,
  serializeFileAsset,
  serializeProjectManifest,
  serializeResearchRun,
} from './markdown'
import type {
  ChatRuleRecord,
  ChatThreadRecord,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  FileAssetRecord,
  FileGroupRecord,
  ProjectState,
  ResearchRunRecord,
  VectorIndexRecord,
} from './types'

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

function makeRule(overrides: Partial<ChatRuleRecord> = {}): ChatRuleRecord {
  return {
    contentMarkdown: 'Follow the profile context.',
    createdAt: '2026-01-01T00:00:00.000Z',
    id: 'rule-1',
    label: 'profile',
    title: 'Profile',
    updatedAt: '2026-01-02T00:00:00.000Z',
    ...overrides,
  }
}

function makeEditorDocument(
  id: string,
  overrides: Partial<EditorDocumentRecord> = {},
): EditorDocumentRecord {
  return {
    contentMarkdown: `# ${id}`,
    createdAt: '2026-01-01T00:00:00.000Z',
    folderId: null,
    id,
    revision: 1,
    source: 'blank',
    title: `${id}.md`,
    updatedAt: '2026-01-02T00:00:00.000Z',
    ...overrides,
  }
}

function makeResearchRun(id: string, overrides: Partial<ResearchRunRecord> = {}): ResearchRunRecord {
  return {
    agentOverrides: {},
    createdAt: '2026-01-01T00:00:00.000Z',
    events: [],
    finishedAt: '2026-01-01T00:01:00.000Z',
    metrics: { claims: 0, queries: 0, rounds: '0', sources: 0 },
    mode: 'research',
    phaseState: { activePhase: 'answer', completedPhases: ['analysis', 'planning', 'search', 'evaluation', 'answer'] },
    result: {
      markdown: `# ${id}`,
      references: [],
      topClaims: [],
      topSources: [],
    },
    runId: id,
    source: 'api',
    stack: 'test-stack',
    startedAt: '2026-01-01T00:00:01.000Z',
    status: 'completed',
    submittedAt: '2026-01-01T00:00:00.000Z',
    summary: { title: id },
    ...overrides,
  }
}

describe('serializeResearchRun / parseResearchRun', () => {
  it('round-trips an opted-out @-mention availability flag; default-on stays default', () => {
    const hidden = makeResearchRun('run-hidden', { includeInAutocomplete: false })
    const parsedHidden = parseResearchRun(serializeResearchRun(hidden).contents)
    expect(parsedHidden.includeInAutocomplete).toBe(false)

    // A default-on run carries no explicit flag; the round-trip must not invent
    // a `false`, so availability stays the default (treated as available).
    const shown = makeResearchRun('run-shown')
    const parsedShown = parseResearchRun(serializeResearchRun(shown).contents)
    expect(parsedShown.includeInAutocomplete).not.toBe(false)
  })
})

describe('serializeChatRule / parseChatRule', () => {
  it('parses legacy rule files with prompt-library defaults', () => {
    const parsed = parseChatRule([
      '---',
      'created_at: 2026-01-01T00:00:00.000Z',
      'kind: inqtrix.chat_rule',
      'label: legacy',
      'rule_id: rule-legacy',
      'schema_version: 1',
      'title: Legacy',
      'updated_at: 2026-01-02T00:00:00.000Z',
      '---',
      'Legacy instruction.',
    ].join('\n'))

    expect(parsed).toEqual({
      category: 'instruction',
      contentMarkdown: 'Legacy instruction.',
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'rule-legacy',
      includeInAutocomplete: true,
      label: 'legacy',
      linkedContextRefs: [],
      title: 'Legacy',
      updatedAt: '2026-01-02T00:00:00.000Z',
      visibility: { agent: false, chat: true, editor: true },
    })
  })

  it('round-trips prompt-library frontmatter additively', () => {
    const rule = makeRule({
      category: 'context',
      includeInAutocomplete: false,
      linkedContextRefs: [
        { fileId: 'file-1', kind: 'file-asset' },
        { groupId: 'group-1', kind: 'file-group' },
        { kind: 'chat-rule', ruleId: 'ignored-linked-rule' },
      ],
      visibility: { agent: false, chat: true, editor: false },
    })

    const file = serializeChatRule(rule)
    const parsed = parseChatRule(file.contents)

    expect(file.path).toBe('rules/profile.md')
    expect(parsed).toEqual({
      ...rule,
      linkedContextRefs: [
        { fileId: 'file-1', kind: 'file-asset' },
        { groupId: 'group-1', kind: 'file-group' },
      ],
    })
  })

  it('keeps the composed size the server measured with the sync link', () => {
    // Lost on save, every reload would hydrate it back and mark a
    // freshly loaded project dirty.
    const rule = makeRule({
      serverComposedChars: 1_250,
      serverRevision: 3,
      serverTemplateId: 'pt_profile',
    })
    expect(parseChatRule(serializeChatRule(rule).contents)).toMatchObject({
      serverComposedChars: 1_250,
      serverRevision: 3,
      serverTemplateId: 'pt_profile',
    })
    expect(parseChatRule(serializeChatRule(makeRule({})).contents))
      .not.toHaveProperty('serverComposedChars')
  })
})

describe('parseFrontmatter line-ending tolerance (Windows/CRLF)', () => {
  const ruleLines = [
    '---',
    'created_at: 2026-01-01T00:00:00.000Z',
    'kind: inqtrix.chat_rule',
    'label: legacy',
    'rule_id: rule-legacy',
    'schema_version: 1',
    'title: Legacy',
    'updated_at: 2026-01-02T00:00:00.000Z',
    '---',
    'Legacy instruction.',
  ]
  const expected: ChatRuleRecord & Record<string, unknown> = {
    category: 'instruction',
    contentMarkdown: 'Legacy instruction.',
    createdAt: '2026-01-01T00:00:00.000Z',
    id: 'rule-legacy',
    includeInAutocomplete: true,
    label: 'legacy',
    linkedContextRefs: [],
    title: 'Legacy',
    updatedAt: '2026-01-02T00:00:00.000Z',
    visibility: { agent: false, chat: true, editor: true },
  }

  it('accepts CRLF frontmatter from a Windows autocrlf checkout', () => {
    expect(parseChatRule(ruleLines.join('\r\n'))).toEqual(expected)
  })

  it('accepts lone-CR frontmatter from a legacy-Mac checkout', () => {
    expect(parseChatRule(ruleLines.join('\r'))).toEqual(expected)
  })
})

describe('serializeChatThread / parseChatThread', () => {
  it('round-trips assistant request context metadata additively', () => {
    const thread: ChatThreadRecord = {
      createdAt: '2026-06-26T12:00:00.000Z',
      id: 'ct_1',
      messages: [
        {
          contentMarkdown: 'Use knowledge mode.',
          createdAt: '2026-06-26T12:01:00.000Z',
          id: 'cm_u1',
          role: 'user',
        },
        {
          contentMarkdown: 'Knowledge-grounded answer.',
          createdAt: '2026-06-26T12:02:00.000Z',
          id: 'cm_a1',
          requestContext: { knowledgeCollectionIds: ['kc_1', 'kc_2'] },
          role: 'assistant',
        },
      ],
      preview: 'Knowledge-grounded answer.',
      source: 'api',
      title: 'Knowledge retry',
      updatedAt: '2026-06-26T12:02:00.000Z',
    }

    const parsed = parseChatThread(serializeChatThread(thread).contents)

    expect(parsed.messages[1].requestContext).toEqual({ knowledgeCollectionIds: ['kc_1', 'kc_2'] })
    expect(parsed.messages.map((message) => message.contentMarkdown)).toEqual([
      'Use knowledge mode.',
      'Knowledge-grounded answer.',
    ])
  })
})

describe('serializeFileAsset / parseFileAsset', () => {
  it('round-trips an asset with all metadata', () => {
    const asset = makeAsset('f1', 'alpha', {
      groupId: 'g1',
      origin: 'chat',
      pageCount: 3,
      parseStatus: 'partial',
      parseWarning: 'Document shortened',
      deletionError: 'Storage unavailable',
      deletionOperationId: 'del-1',
      deletionStage: 'delete_failed',
      lifecycleStatus: 'delete_failed',
      serverSynced: true,
      sizeBytes: 42,
      textTruncated: true,
    })
    const file = serializeFileAsset(asset)
    expect(file.path).toBe('files/alpha.md')
    expect(parseFileAsset(file.contents)).toEqual(asset)
  })

  it('round-trips a null group and page count', () => {
    const asset = makeAsset('f2', 'beta')
    expect(parseFileAsset(serializeFileAsset(asset).contents)).toEqual(asset)
  })
})

describe('project file export plan', () => {
  it('exports completed research runs but skips knowledge-mode runs', () => {
    const researchRun = makeResearchRun('run-research')
    const knowledgeRun = makeResearchRun('run-knowledge', { mode: 'knowledge' })
    const state: ProjectState = {
      ...createEmptyProjectState(),
      researchRunOrder: ['run-research', 'run-knowledge'],
      researchRuns: {
        'run-knowledge': knowledgeRun,
        'run-research': researchRun,
      },
    }

    const paths = buildProjectFiles(state).map((file) => file.path)

    expect(paths.some((path) => path.includes('run-research'))).toBe(true)
    expect(paths.some((path) => path.includes('run-knowledge'))).toBe(false)
  })

  it('never materializes shared resources into recipient-owned project files', () => {
    const ownedRun = makeResearchRun('run-owned', {
      access: { mode: 'owner' },
    })
    const sharedRun = makeResearchRun('run-shared', {
      access: { mode: 'shared', permission: 'edit' },
    })
    const ownedRule = makeRule({
      access: { mode: 'owner' },
      id: 'rule-owned',
      label: 'owned-rule',
    })
    const sharedRule = makeRule({
      access: { mode: 'shared', permission: 'view' },
      id: 'rule-shared',
      label: 'shared-rule',
    })
    const ownedDocument = makeEditorDocument('doc-owned')
    const sharedDocument = makeEditorDocument('doc-shared', {
      access: { mode: 'shared', permission: 'suggest' },
      contentMode: 'collaboration',
    })
    const state: ProjectState = {
      ...createEmptyProjectState(),
      chatRuleOrder: ['rule-owned', 'rule-shared'],
      chatRules: {
        'rule-owned': ownedRule,
        'rule-shared': sharedRule,
      },
      editorDocumentOrder: ['doc-owned', 'doc-shared'],
      editorDocuments: {
        'doc-owned': ownedDocument,
        'doc-shared': sharedDocument,
      },
      researchRunOrder: ['run-owned', 'run-shared'],
      researchRuns: {
        'run-owned': ownedRun,
        'run-shared': sharedRun,
      },
    }

    const paths = buildProjectFiles(state).map((file) => file.path)

    expect(paths.some((path) => path.includes('run-owned'))).toBe(true)
    expect(paths.some((path) => path.includes('run-shared'))).toBe(false)
    expect(paths).toContain('rules/owned-rule.md')
    expect(paths).not.toContain('rules/shared-rule.md')
    expect(paths.some((path) => path.includes('doc-owned'))).toBe(true)
    expect(paths.some((path) => path.includes('doc-shared'))).toBe(false)

    // Das Manifest darf nichts versprechen, was im Archiv nicht liegt: kaemen
    // die Ordnungsfelder ungefiltert aus dem Zustand, waehrend der Exportplan
    // Fremdfreigaben ueberspringt, faende ein Import eine Id ohne Datei.
    const manifest = parseProjectManifest(serializeProjectManifest(state).contents)

    expect(manifest.editor_document_order).toEqual(['doc-owned'])
    expect(manifest.rule_order).toEqual(['rule-owned'])
  })

  it.each(['remote_deleted', 'generation_replaced'] as const)('round-trips server provenance and a local recovery boundary (%s)', (reason) => {
    const recoveryDocument = makeEditorDocument('recovery-document', {
      recovery: {
        capturedAt: '2026-01-03T00:00:00.000Z',
        originalDocumentId: 'deleted-server-document',
        reason,
      },
      revision: 0,
      serverSynced: false,
    })

    const parsed = parseEditorDocument(serializeEditorDocument(
      recoveryDocument,
      { editorComments: {} },
    ).contents)

    expect(parsed.document).toMatchObject({
      id: recoveryDocument.id,
      recovery: recoveryDocument.recovery,
      revision: 0,
      serverSynced: false,
    })
  })

  it('round-trips a collaboration export with entirely detached document and comment identities', () => {
    const document = makeEditorDocument('live-document', {
      access: { mode: 'owner', permission: 'edit' },
      collaboration: {
        generation: 3,
        persistedSequence: 41,
        projectionSequence: 41,
        projectionUpdatedAt: '2026-01-02T00:00:00.000Z',
        schemaVersion: 1,
      },
      contentMode: 'collaboration',
      diffAnchorMarkdown: '# Earlier projection',
      diffAnchorUpdatedAt: '2026-01-01T12:00:00.000Z',
      folderId: 'owner-folder',
      metadataRevision: 4,
      revision: 17,
    })
    const comments: EditorCommentThreadRecord[] = [
      {
        anchor: {
          blockId: 'paragraph-1',
          from: 2,
          quoteAfter: ' after one',
          quoteBefore: 'before one ',
          selectedMarkdown: '**first**',
          selectedText: 'first',
          to: 7,
        },
        commentMarkdown: 'First private note',
        createdAt: '2026-01-01T13:00:00.000Z',
        documentId: document.id,
        id: 'live-comment-1',
        kind: 'collect',
        suggestionDraft: {
          anchorVersion: 1,
          createdAt: '2026-01-01T13:01:00.000Z',
          groupId: 'private-group',
          patchId: '00000000-0000-4000-8000-000000000003',
          proposedText: 'PRIVATE PROVIDER RESULT MUST NOT BE EXPORTED',
          publicationCommandId: '00000000-0000-4000-8000-000000000002',
          revision: 1,
          suggestionId: 'private-suggestion',
          updatedAt: '2026-01-01T13:01:00.000Z',
        },
        status: 'open',
        updatedAt: '2026-01-01T13:05:00.000Z',
      },
      {
        anchor: {
          from: 11,
          quoteAfter: ' after two',
          quoteBefore: 'before two ',
          selectedText: 'second',
          to: 17,
        },
        commentMarkdown: 'Second private note',
        createdAt: '2026-01-01T14:00:00.000Z',
        documentId: document.id,
        id: 'live-comment-2',
        kind: 'evidence_review',
        status: 'resolved',
        updatedAt: '2026-01-01T14:05:00.000Z',
      },
    ]

    const serialized = serializeEditorDocument(document, {
      editorComments: Object.fromEntries(comments.map((comment) => [comment.id, comment])),
    }).contents
    const parsed = parseEditorDocument(serialized)

    expect(serialized).not.toContain('PRIVATE PROVIDER RESULT MUST NOT BE EXPORTED')
    expect(serialized).not.toContain('suggestionDraft')

    expect(parsed.document.id).not.toBe(document.id)
    expect(parsed.document.id).toMatch(/^editor-doc-/)
    expect(parsed.importIdentity?.documentId).toEqual({
      sourceId: document.id,
      targetId: parsed.document.id,
    })
    expect(parsed.document.contentMarkdown).toBe(document.contentMarkdown)
    expect(parsed.document.contentMode).toBeUndefined()
    expect(parsed.document.collaboration).toBeUndefined()
    expect(parsed.document.access).toBeUndefined()
    expect(parsed.document.diffAnchorMarkdown).toBe('# Earlier projection')
    expect(parsed.document.diffAnchorUpdatedAt).toBe('2026-01-01T12:00:00.000Z')
    expect(parsed.document.folderId).toBeNull()
    expect(parsed.document.revision).toBe(0)
    expect(parsed.comments).toHaveLength(2)
    expect(new Set(parsed.comments.map((comment) => comment.id)).size).toBe(2)
    expect(parsed.comments.every((comment) => comment.id.startsWith('editor-comment-'))).toBe(true)
    expect(parsed.comments.every((comment) => !comments.some((source) => source.id === comment.id))).toBe(true)
    expect(parsed.comments.every((comment) => comment.documentId === parsed.document.id)).toBe(true)
    expect(parsed.comments.every((comment) => comment.suggestionDraft === undefined)).toBe(true)
    expect(parsed.importIdentity?.commentIds).toEqual(comments.map((comment, index) => ({
      sourceId: comment.id,
      targetId: parsed.comments[index].id,
    })))
    expect(parsed.comments.map((comment) => ({
      anchor: comment.anchor,
      commentMarkdown: comment.commentMarkdown,
      kind: comment.kind,
      status: comment.status,
    }))).toEqual(comments.map((comment) => ({
      anchor: comment.anchor,
      commentMarkdown: comment.commentMarkdown,
      kind: comment.kind,
      status: comment.status,
    })))
  })
})

describe('project manifest file library', () => {
  it('round-trips pinned explorer UI state additively', () => {
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      ui: {
        ...base.ui,
        pinnedExplorer: {
          chatThreadIds: ['ct-1'],
          editorDocumentIds: ['doc-1'],
          knowledgeSessionIds: ['ks-1'],
          agentSessionIds: [],
        },
      },
    }

    const data = parseProjectManifest(serializeProjectManifest(state).contents)

    expect((data.ui as Record<string, unknown>).pinnedExplorer).toEqual({
      chatThreadIds: ['ct-1'],
      editorDocumentIds: ['doc-1'],
      knowledgeSessionIds: ['ks-1'],
      agentSessionIds: [],
    })
  })

  it('round-trips the agent session selection intent', () => {
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      ui: { ...base.ui, selectedAgentSessionId: 'agent-session-x' },
    }

    const data = parseProjectManifest(serializeProjectManifest(state).contents)

    expect((data.ui as Record<string, unknown>).selectedAgentSessionId).toBe(
      'agent-session-x',
    )
  })

  it('serializes and re-parses sections, groups and asset order', () => {
    const sections = createDefaultFileLibrarySections('2026-01-01T00:00:00.000Z')
    const group: FileGroupRecord = {
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'g1',
      sectionId: sections[1].id,
      title: 'Group',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const asset = makeAsset('f1', 'alpha', { groupId: 'g1', sectionId: sections[1].id })
    const state: ProjectState = {
      ...createEmptyProjectState(),
      fileAssetOrder: ['f1'],
      fileAssets: { f1: asset },
      fileGroupOrder: ['g1'],
      fileGroups: { g1: group },
      fileLibrarySectionOrder: sections.map((section) => section.id),
      fileLibrarySections: Object.fromEntries(sections.map((section) => [section.id, section])),
    }

    const data = parseProjectManifest(serializeProjectManifest(state).contents)

    expect(data.file_section_order).toEqual(sections.map((section) => section.id))
    expect((data.file_sections as unknown[]).length).toBe(3)
    expect(data.file_group_order).toEqual(['g1'])
    expect(data.file_asset_order).toEqual(['f1'])
  })
})

describe('project manifest vector indexes', () => {
  it('round-trips vector index order and pending/embedded members', () => {
    const index: VectorIndexRecord = {
      createdAt: '2026-01-01T00:00:00.000Z',
      dims: 3072,
      handle: 'eu-recht',
      id: 'idx1',
      members: [
        { fileId: 'f1', state: 'embedded' },
        { fileId: 'f2', state: 'pending' },
      ],
      model: 'text-embedding-3-large',
      status: 'stale',
      title: 'EU Recht',
      updatedAt: '2026-01-02T00:00:00.000Z',
    }
    const state: ProjectState = {
      ...createEmptyProjectState(),
      vectorIndexOrder: ['idx1'],
      vectorIndexes: { idx1: index },
    }

    const data = parseProjectManifest(serializeProjectManifest(state).contents)

    expect(data.vector_index_order).toEqual(['idx1'])
    const serialized = data.vector_indexes as Array<Record<string, unknown>>
    expect(serialized).toHaveLength(1)
    expect(serialized[0].handle).toBe('eu-recht')
    expect(serialized[0].members).toEqual([
      { fileId: 'f1', state: 'embedded' },
      { fileId: 'f2', state: 'pending' },
    ])
  })
})

describe('model selection is not project data', () => {
  it('never writes the model selection into the manifest', () => {
    // The account preferences own this value and carry a load-time
    // precedence rule (a loaded file must not bleed into the live account
    // row). The ui block has no such rule, so persisting it here would build
    // a second store that silently wins whenever a project is opened.
    const state = createEmptyProjectState()
    state.ui.selectedChatModelTier = 'high'
    state.ui.selectedChatModel = 'claude-opus-4-8'
    state.ui.selectedChatEffort = 'high'
    state.ui.selectedAgentModelTier = 'fast'
    state.ui.selectedAgentModel = 'claude-haiku-4-5'
    state.ui.selectedAgentEffort = 'low'

    const manifest = serializeProjectManifest(state)

    expect(manifest.contents).not.toContain('claude-opus-4-8')
    expect(manifest.contents).not.toContain('claude-haiku-4-5')
    const ui = (parseProjectManifest(manifest.contents) as {
      ui?: Record<string, unknown>
    }).ui
    expect(ui?.selectedChatModelTier).toBeNull()
    expect(ui?.selectedChatModel).toBeNull()
    expect(ui?.selectedChatEffort).toBeNull()
    expect(ui?.selectedAgentModelTier).toBeNull()
    expect(ui?.selectedAgentModel).toBeNull()
    expect(ui?.selectedAgentEffort).toBeNull()
  })
})

describe('chat thread file round-trip keeps the model pick', () => {
  it('serializes and parses model_selection as the thread\'s own property', () => {
    // In file-backed projects the thread file IS the durable home of the
    // pick — losing it here would silently strip the selection on every
    // export/import cycle.
    const file = serializeChatThread({
      createdAt: '2026-08-07T10:00:00.000Z',
      id: 'ct_file',
      messages: [],
      modelSelection: { model: 'gpt-5.4-nano', tier: null, effort: null },
      preview: '',
      source: 'api',
      title: 'T',
      updatedAt: '2026-08-07T10:00:00.000Z',
    })
    const parsed = parseChatThread(file.contents)
    expect(parsed.modelSelection).toEqual({
      model: 'gpt-5.4-nano',
      tier: null,
      effort: null,
    })

    const plain = serializeChatThread({
      createdAt: '2026-08-07T10:00:00.000Z',
      id: 'ct_plain',
      messages: [],
      preview: '',
      source: 'api',
      title: 'T',
      updatedAt: '2026-08-07T10:00:00.000Z',
    })
    expect(parseChatThread(plain.contents).modelSelection).toBeUndefined()
  })
})
