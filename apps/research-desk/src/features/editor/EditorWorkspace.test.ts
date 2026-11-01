import { readFileSync } from 'node:fs'

import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  advanceEditorIdentityPublication,
  collaborationSourceViewMarkdown,
  editorDocumentShareDisabled,
  editorDocumentShowsUnsavedMarker,
  editorDocumentStructuralLoadPhase,
  EditorDocumentRecoveryNotice,
  EditorRecoveryBar,
  editorRecoveryMarkdown,
  recoverySnapshotMarkdown,
  trackRecoveryTextOfEditor,
} from './EditorWorkspace'
import type { Editor } from '@tiptap/react'

import type { EditorDocumentRecord } from '@/features/project/types'

describe('recovery copy of unconfirmed changes', () => {
  const liveEditor = (text: string) => {
    const destroy: Array<() => void> = []
    const editor = {
      getJSON: vi.fn(() => ({
        content: [{ content: [{ text, type: 'text' }], type: 'paragraph' }],
        type: 'doc',
      })),
      isDestroyed: false,
      markdown: { serialize: (json: { content: Array<{ content: Array<{ text: string }> }> }) => json.content[0]!.content[0]!.text },
      on: (_event: 'destroy', listener: () => void) => { destroy.push(listener) },
      schema: { marks: {}, nodes: {} },
    }
    return { destroy, editor: editor as never as Editor, getJSON: editor.getJSON }
  }

  it('writes nothing out while the editor lives and nobody asks', () => {
    const { editor, getJSON } = liveEditor('Typed')
    const snapshots = new Map([['doc', { documentMarkdown: 'Stored', editor }]])

    trackRecoveryTextOfEditor(snapshots, editor)

    expect(getJSON).not.toHaveBeenCalled()
  })

  it('reads the live text when it is asked for', () => {
    const { editor } = liveEditor('Typed')
    expect(recoverySnapshotMarkdown({ documentMarkdown: 'Stored', editor })).toBe('Typed')
  })

  it('keeps the text from the moment the editor goes away', () => {
    const { destroy, editor } = liveEditor('Typed')
    const snapshot: { documentMarkdown: string; editor: Editor | null } = { documentMarkdown: 'Stored', editor }
    const snapshots = new Map([['doc', snapshot]])
    trackRecoveryTextOfEditor(snapshots, editor)

    destroy.forEach((listener) => listener())

    expect(snapshot).toEqual({ documentMarkdown: 'Typed', editor: null })
    expect(recoverySnapshotMarkdown(snapshot)).toBe('Typed')
  })

  it('leaves the snapshot of another editor alone when one goes away', () => {
    const going = liveEditor('Going')
    const staying = liveEditor('Staying')
    const snapshot = { documentMarkdown: 'Stored', editor: staying.editor }
    trackRecoveryTextOfEditor(new Map([['doc', snapshot]]), going.editor)

    going.destroy.forEach((listener) => listener())

    expect(snapshot).toEqual({ documentMarkdown: 'Stored', editor: staying.editor })
    expect(staying.getJSON).not.toHaveBeenCalled()
  })

  it('falls back to the last known text without a live editor', () => {
    expect(recoverySnapshotMarkdown({ documentMarkdown: 'Stored', editor: null })).toBe('Stored')
    const { editor } = liveEditor('Typed')
    ;(editor as unknown as { isDestroyed: boolean }).isDestroyed = true
    expect(recoverySnapshotMarkdown({ documentMarkdown: 'Stored', editor })).toBe('Stored')
  })
})

describe('collaboration source view', () => {
  const content = { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Live' }] }] }

  it('serializes nothing while the view is closed', () => {
    const getJSON = vi.fn(() => content)
    expect(collaborationSourceViewMarkdown({
      bound: true,
      editor: { getJSON } as never,
      fallback: 'Stored',
      open: false,
    })).toBe('')
    expect(getJSON).not.toHaveBeenCalled()
  })

  it('shows the live document of a bound editor while it is open', () => {
    const getJSON = vi.fn(() => content)
    expect(collaborationSourceViewMarkdown({
      bound: true,
      editor: { getJSON } as never,
      fallback: 'Stored',
      open: true,
    })).toContain('Live')
    expect(getJSON).toHaveBeenCalledTimes(1)
  })

  it('falls back to the stored document without a bound editor', () => {
    const getJSON = vi.fn(() => content)
    expect(collaborationSourceViewMarkdown({
      bound: false,
      editor: { getJSON } as never,
      fallback: 'Stored',
      open: true,
    })).toBe('Stored')
    expect(getJSON).not.toHaveBeenCalled()
  })
})

describe('editor unsaved marker', () => {
  it('stays away on a server-synced desk, whose project flag never clears', () => {
    expect(editorDocumentShowsUnsavedMarker({ canFlushForShare: true, isDirty: true })).toBe(false)
    expect(editorDocumentShowsUnsavedMarker({ canFlushForShare: true, isDirty: false })).toBe(false)
  })

  it('names unsaved work of a local project and only that', () => {
    expect(editorDocumentShowsUnsavedMarker({ canFlushForShare: false, isDirty: true })).toBe(true)
    expect(editorDocumentShowsUnsavedMarker({ canFlushForShare: false, isDirty: false })).toBe(false)
  })
})

describe('editor recovery markdown', () => {
  it('keeps the unconfirmed document body and appends unsent comment drafts', () => {
    expect(editorRecoveryMarkdown({
      commentDrafts: ['First local note', 'Second **draft**'],
      documentMarkdown: '# Unconfirmed body\n\nChanged locally.',
      locale: 'en',
      title: 'Original.md',
    })).toBe(
      '# Unconfirmed body\n\nChanged locally.'
      + '\n\n---\n\n## Unsent comment drafts'
      + '\n\n### Draft 1\n\nFirst local note'
      + '\n\n### Draft 2\n\nSecond **draft**',
    )
  })

  it('preserves a comment-only recovery without copying the confirmed body', () => {
    expect(editorRecoveryMarkdown({
      commentDrafts: ['Noch nicht gesendet'],
      documentMarkdown: '',
      locale: 'de',
      title: 'Remote gelöscht.md',
    })).toBe(
      '# Remote gelöscht.md'
      + '\n\n---\n\n## Nicht gesendete Kommentarentwürfe'
      + '\n\n### Entwurf 1\n\nNoch nicht gesendet',
    )
  })

  it('renders a visibly anchored recovery boundary with explicit decisions', () => {
    const markup = renderToStaticMarkup(createElement(
      EditorRecoveryBar,
      {
        locale: 'de',
        onDiscard: () => undefined,
        onSaveAsNew: () => undefined,
        reason: 'remote_deleted',
      },
    ))

    expect(markup).toContain('data-editor-recovery-artifact="remote_deleted"')
    expect(markup).toContain('role="alert"')
    expect(markup).toContain('Als neues Dokument speichern')
    expect(markup).toContain('Verwerfen')
    expect(markup).toContain('wurde remote gelöscht')
    expect(markup).toContain('wird nicht automatisch synchronisiert')
  })

  it('names a replaced generation, not a deletion, as the cause of a recovery copy', () => {
    const markup = renderToStaticMarkup(createElement(
      EditorRecoveryBar,
      {
        locale: 'de',
        onDiscard: () => undefined,
        onSaveAsNew: () => undefined,
        reason: 'generation_replaced',
      },
    ))

    expect(markup).toContain('data-editor-recovery-artifact="generation_replaced"')
    expect(markup).toContain('wurde inzwischen neu aufgebaut')
    expect(markup).not.toContain('gelöscht')
    expect(markup).toContain('Als neues Dokument speichern')
  })

  it('keeps sharing disabled until the recovery copy is promoted', () => {
    const document = {
      contentMarkdown: '# Local recovery',
      createdAt: '2026-07-29T10:00:00.000Z',
      folderId: null,
      id: 'editor-recovery-local',
      recovery: {
        capturedAt: '2026-07-29T10:00:00.000Z',
        originalDocumentId: 'deleted-server-document',
        reason: 'remote_deleted',
      },
      revision: 0,
      source: 'blank',
      title: 'Recovered.md',
      updatedAt: '2026-07-29T10:00:00.000Z',
    } satisfies EditorDocumentRecord

    expect(editorDocumentShareDisabled({
      canFlushForShare: true,
      document,
      isDirty: false,
      sharingAvailable: true,
    })).toBe(true)
    expect(editorDocumentShareDisabled({
      canFlushForShare: true,
      document: { ...document, recovery: undefined },
      isDirty: false,
      sharingAvailable: true,
    })).toBe(false)
  })

  it('shows a recovery copy its own bar, and the original of a replaced generation the notice that leads to its copy', () => {
    const record = (
      id: string,
      recovery?: EditorDocumentRecord['recovery'],
    ): EditorDocumentRecord => ({
      contentMarkdown: `# ${id}`,
      createdAt: '2026-09-28T08:00:00.000Z',
      folderId: null,
      id,
      ...(recovery ? { recovery } : {}),
      revision: 1,
      source: 'blank',
      title: `${id}.md`,
      updatedAt: '2026-09-28T08:00:00.000Z',
    })
    const documents = Object.fromEntries([
      record('original'),
      record('copy', {
        capturedAt: '2026-09-28T09:00:00.000Z',
        originalDocumentId: 'original',
        reason: 'generation_replaced',
      }),
      record('unrelated'),
    ].map((document) => [document.id, document]))
    const render = (id: string) => renderToStaticMarkup(createElement(
      EditorDocumentRecoveryNotice,
      {
        dispatch: () => undefined,
        document: documents[id]!,
        documents,
        locale: 'de',
      },
    ))

    const onCopy = render('copy')
    expect(onCopy).toContain('data-editor-recovery-artifact="generation_replaced"')
    expect(onCopy).not.toContain('data-collaboration-recovery-copy')
    const onOriginal = render('original')
    expect(onOriginal).toContain('data-collaboration-recovery-copy')
    expect(onOriginal).toContain('Ihr nicht gespeicherter Stand wurde als Wiederherstellungskopie abgelegt.')
    expect(onOriginal).toMatch(/<button[^>]*>Kopie öffnen<\/button>/)
    expect(onOriginal).not.toContain('data-editor-recovery-artifact')
    expect(render('unrelated')).toBe('')
  })
})

describe('editor structural loading', () => {
  it('accepts readiness only for the active document identity', () => {
    expect(editorDocumentStructuralLoadPhase('document-b', {
      documentId: 'document-a',
      error: null,
      phase: 'ready',
    })).toBe('pending')
    expect(editorDocumentStructuralLoadPhase('document-b', {
      documentId: 'document-b',
      error: null,
      phase: 'ready',
    })).toBe('ready')
  })

  it('renders the no-document state immediately', () => {
    expect(editorDocumentStructuralLoadPhase(null, {
      documentId: 'document-a',
      error: null,
      phase: 'pending',
    })).toBe('empty')
  })

  it('retains and deactivates the published header until the requested body is visible', () => {
    const current = {
      displayedIdentity: 'document-a:local',
      visible: true,
    }

    const hidden = advanceEditorIdentityPublication(current, 'document-b:local', {
      identity: 'document-b:local',
      visible: false,
    })
    expect(hidden).toEqual({
      displayedIdentity: 'document-a:local',
      visible: false,
    })

    expect(advanceEditorIdentityPublication(hidden, 'document-b:local', {
      identity: 'document-b:local',
      visible: true,
    })).toEqual({
      displayedIdentity: 'document-b:local',
      visible: true,
    })
  })

  it('ignores a late visibility edge from an aborted document switch', () => {
    const current = {
      displayedIdentity: 'document-a:local',
      visible: false,
    }

    expect(advanceEditorIdentityPublication(current, 'document-c:local', {
      identity: 'document-b:local',
      visible: true,
    })).toBe(current)
  })
})

describe('editor inspector activity wiring', () => {
  // Bound stated: this reads the SOURCE. The workspace is not rendered with
  // effects in these node tests, so the call is anchored at its one site.
  const source = readFileSync('src/features/editor/EditorWorkspace.tsx', 'utf8')
  const callStart = source.indexOf('useInspectorCollaborationActivity({')
  const call = source.slice(callStart, source.indexOf('})', callStart))

  it('loads the activity only while the changes tab of an open inspector shows it', () => {
    expect(source.split('useInspectorCollaborationActivity({').length - 1).toBe(1)
    expect(call).toContain('active: collaborationSessionActive,')
    expect(call).toContain('visible: inspectorActivityVisible,')
    expect(source).toContain(
      "const inspectorActivityVisible = inspectorTab === 'changes'\n"
      + '    && (isDesktop ? state.editorUi.isCommentPanelVisible : isMobileCommentsOpen)',
    )
    expect(source.indexOf('const inspectorActivityVisible')).toBeLessThan(callStart)
  })
})

describe('team discussion hand-over wiring', () => {
  // Bound stated: this reads the SOURCE. "Use with assistant" is wired inside
  // the workspace, which these node tests do not render, so each step is
  // anchored at its one site. The behaviour itself is covered by the
  // assistant hand-over browser fixture.
  const source = readFileSync('src/features/editor/EditorWorkspace.tsx', 'utf8')
  const handlerStart = source.indexOf('const handleUseTeamCommentWithAssistant = useCallback(')
  const handler = source.slice(handlerStart, source.indexOf('}, [', handlerStart))

  it('hands the thread over as context and leaves the composer text to the user', () => {
    expect(source.split('const handleUseTeamCommentWithAssistant = useCallback(').length - 1).toBe(1)
    expect(handler).toContain('handOverDiscussion(thread.id)')
    expect(handler).not.toContain('setEditorAssistantDraft')
    expect(handler).not.toContain('composerRef.current?.setMentionText')
  })

  it('keeps the hand-over to the open document and drops it once a request carried it', () => {
    expect(source.split('useHandedOverDiscussions(').length - 1).toBe(1)
    expect(source).toContain('useHandedOverDiscussions(activeDocument?.id ?? null)')
    const successStart = source.indexOf('onGlobalSuccess: () => {')
    expect(source.slice(successStart, source.indexOf('},', successStart)))
      .toContain('clearHandedOverDiscussions()')
  })

  it('attaches the hand-over to every AI run and counts it in the context meter', () => {
    expect(source).toContain('handedOverDiscussions: discussions,')
    expect(source).toContain('handedOverDiscussions={discussions}')
    const baseStart = source.indexOf('const editorContextBase = useMemo(')
    expect(source.slice(baseStart, source.indexOf('}, [', baseStart)))
      .toContain('for (const discussion of discussions) {')
  })
})
