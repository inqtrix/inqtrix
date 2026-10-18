import '@fontsource-variable/inter/index.css'
import 'katex/dist/katex.min.css'
import '@/styles/globals.css'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { HocuspocusProvider } from '@hocuspocus/provider'
import {
  EDITOR_SCHEMA_VERSION,
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  parseEditorMarkdown,
} from '@inqtrix/editor-schema'
import { Editor as HeadlessEditor, getSchema } from '@tiptap/core'
import { skipTrailingNodeMeta } from '@tiptap/extensions'
import type { Editor } from '@tiptap/react'
import { prosemirrorJSONToYXmlFragment } from '@tiptap/y-tiptap'
import * as Y from 'yjs'

import { AppProviders } from '@/app/AppProviders'
import {
  AgentCanvasReactContext,
  type AgentCanvasContextValue,
} from '@/features/agent/canvas/context'
import { createCanvasSaveRegistry } from '@/features/agent/canvas/saveRegistry'
import { DocumentCanvasView } from '@/features/agent/canvas/views'
import type { AgentArtifactRecord, AgentRunRecord } from '@/features/agent/model'
import type { EditorDocumentRecord } from '@/features/project/types'

import { MarkdownEditorSurface } from '../core/MarkdownEditorSurface'
import { editorCopy } from '../editorCopy'
import { createEditorExtensions, serializeEditorMarkdown } from '../tiptap'
import type { CollaborationDocumentHandle } from '../useCollaborationDocument'

declare global {
  interface Window {
    /** Every body the surface handed its host, in order. */
    __bodyChanges?: string[]
    __bodyEditor?: {
      /** Whether the editor's own selection is a caret at the end of the
       * first block. */
      caretAtFirstBlockEnd: () => boolean
      /** Remove the trailing paragraph the editor appended while it was
       * mounted, without an update, and name the last block then. */
      removeTrailingParagraph: () => string | null
      /** Dispatch the step-less transaction the surface dispatches while it
       * mounts, when it tells node views about editability, and name the
       * last block afterwards: the editor appends a missing trailing
       * paragraph to it, as it does while a document opens. */
      tellEditability: () => string | null
      lastBlock: () => string | null
      /** Paste *text* through the editor view's own paste handling. */
      pasteText: (text: string) => boolean
      storedBody: string
    }
    /** Every save the canvas sent, in order. */
    __canvasSaves?: { expectedRevision: number; markdown: string }[]
    /** Every view the canvas asked to open, in order. */
    __canvasViews?: Parameters<AgentCanvasContextValue['openCanvasView']>[0][]
    /** The body the canvas was loaded with. */
    __canvasStoredBody?: string
    /** With `answer=held`: answer every save sent so far. */
    __answerCanvasSaves?: () => void
    /** Grant or withdraw the right to edit the collaborative document. */
    __setCollaborationCanEdit?: (canEdit: boolean) => void
  }
}

/** Stored bodies as an agent writes them, one per kind of last block, and
 * one an editor stored with escaped labels. */
const STORED_BODIES = {
  blockMath: 'Einleitung mit Beleg [K1].\n\n$$\nx^2 + y^2\n$$',
  blockquote: 'Einleitung mit Beleg [K1].\n\n> Zitat [W7]',
  bulletList: 'Einleitung mit Beleg [K1].\n\n- Punkt [W7]\n- Punkt zwei',
  codeBlock: 'Einleitung mit Beleg [K1].\n\n```\nconst wert = 1\n```',
  escapedTable: 'Einleitung mit Beleg \\[K1\\] und Gruppe \\[K2, W7\\].\n\n| Quelle | Wert |\n| --- | --- |\n| \\[K1\\] | 12 |',
  heading: 'Einleitung mit Beleg [K1].\n\n## Schluss [W7]',
  horizontalRule: 'Einleitung mit Beleg [K1].\n\n---',
  orderedList: 'Einleitung mit Beleg [K1].\n\n1. Erstens [W7]\n2. Zweitens',
  paragraph: 'Einleitung mit Beleg [K1].\n\nSchluss mit Gruppe [K2, W7].',
  table: 'Einleitung mit Beleg [K1] und Gruppe [K2, W7].\n\n| Quelle | Wert |\n|---|---|\n| [K1] | 12 |\n| [W7] | 7 |',
  taskList: 'Einleitung mit Beleg [K1].\n\n- [ ] Aufgabe [W7]\n- [x] Erledigt',
} as const

type BodyKind = keyof typeof STORED_BODIES

const noOp = () => undefined
const noOpAsync = async () => undefined
const textImprovement = { enabled: false, workspaceId: 'browser-fixture' }

/** The body the editor itself writes for *markdown*: its serializer's fixpoint. */
function serializerFixpoint(markdown: string): string {
  const editor = new HeadlessEditor({
    content: markdown,
    contentType: 'markdown',
    element: null,
    extensions: createEditorExtensions(),
    injectCSS: false,
  }) as unknown as Editor
  try {
    return serializeEditorMarkdown(editor)
  } finally {
    editor.destroy()
  }
}

function recordBodyChange(markdown: string) {
  window.__bodyChanges = [...(window.__bodyChanges ?? []), markdown]
}

/**
 * The live editor surface on a plain (non-collaborative) document, the way
 * the canvas and the editor desk host it. `echo` makes the host take every
 * emitted body as its document body, as the editor desk's state does.
 */
function SurfaceFixture({
  echo,
  storedBody,
}: {
  echo: boolean
  storedBody: string
}) {
  const [document, setDocument] = useState<EditorDocumentRecord>(() => ({
    contentMarkdown: storedBody,
    createdAt: '2026-09-01T08:00:00.000Z',
    folderId: null,
    id: 'body-change-document',
    revision: 1,
    source: 'blank',
    title: 'Body change.md',
    updatedAt: '2026-09-01T08:00:00.000Z',
  }))
  const [ready, setReady] = useState(false)
  const onChange = useCallback((markdown: string) => {
    recordBodyChange(markdown)
    if (echo) setDocument((current) => ({ ...current, contentMarkdown: markdown }))
  }, [echo])
  const onEditorReady = useCallback((editor: Editor | null) => {
    if (!editor) return
    window.__bodyEditor = {
      caretAtFirstBlockEnd: () => {
        const { doc, selection } = editor.state
        return selection.empty && selection.from === 1 + (doc.firstChild?.content.size ?? 0)
      },
      lastBlock: () => editor.state.doc.lastChild?.type.name ?? null,
      pasteText: (text) => editor.view.pasteText(text),
      removeTrailingParagraph: () => {
        const last = editor.state.doc.lastChild
        if (last?.type.name !== 'paragraph' || last.content.size !== 0) {
          throw new Error('The document ends in no appended empty paragraph.')
        }
        const end = editor.state.doc.content.size
        editor.view.dispatch(editor.state.tr
          .delete(end - last.nodeSize, end)
          .setMeta(skipTrailingNodeMeta, true)
          .setMeta('preventUpdate', true)
          .setMeta('addToHistory', false))
        return editor.state.doc.lastChild?.type.name ?? null
      },
      storedBody,
      tellEditability: () => {
        editor.view.dispatch(editor.state.tr.setMeta('addToHistory', false))
        return editor.state.doc.lastChild?.type.name ?? null
      },
    }
    setReady(true)
  }, [storedBody])

  return (
    <main className="h-screen bg-background p-4">
      <section
        className="h-[32rem] overflow-auto border border-border px-6 py-4"
        data-ready={ready ? 'true' : 'false'}
        data-testid="body-change-surface"
      >
        <MarkdownEditorSurface
          comments={[]}
          copy={editorCopy.en}
          diffAnchorMarkdown={null}
          document={document}
          embedded
          isDiffVisible={false}
          mode="live"
          onAcceptSuggestion={noOp}
          onChange={onChange}
          onCreateComment={noOp}
          onEditSuggestion={noOp}
          onEditorReady={onEditorReady}
          onMarkSuggestionStale={noOp}
          onRefineSuggestion={noOpAsync}
          onRejectSuggestion={noOp}
          onSelectComment={noOp}
          onStopSuggestion={noOp}
          runningSuggestionIds={[]}
          selectedCommentId={null}
          suggestionErrors={{}}
          suggestions={[]}
          textImprovement={textImprovement}
        />
      </section>
    </main>
  )
}

const RUN_ID = 'fixture-run'
const ARTIFACT_ID = 'fixture-memo'
const CREATED_AT = 1_788_000_000

function canvasArtifact(markdown: string, revision: number, updatedBy: 'agent' | 'user'): AgentArtifactRecord {
  return {
    artifactId: ARTIFACT_ID,
    contentMarkdown: markdown,
    createdAt: CREATED_AT,
    kind: 'memo',
    payload: {},
    refs: [
      { document_id: 'knowledge-document-1', label: 'K1', title: 'Wissensquelle eins' },
      { document_id: 'knowledge-document-2', label: 'K2', title: 'Wissensquelle zwei' },
      { label: 'W7', title: 'Webquelle sieben', url: 'https://example.org/w7' },
    ],
    refsCount: 3,
    revision,
    revisions: Array.from({ length: revision }, (_, index) => ({
      createdAt: CREATED_AT + index,
      createdBy: index === 0 ? 'agent' : 'user',
      revision: index + 1,
      status: 'ready',
    })),
    status: 'ready',
    title: 'Memo',
    updatedAt: CREATED_AT + revision,
    updatedBy,
  }
}

function canvasRun(artifact: AgentArtifactRecord): AgentRunRecord {
  return {
    approvals: [],
    approvalsStale: false,
    artifactOrder: [ARTIFACT_ID],
    artifacts: { [ARTIFACT_ID]: artifact },
    artifactsStale: false,
    childGates: {},
    children: {},
    clarifications: [],
    clarificationsStale: false,
    createdAt: '2026-09-01T08:00:00.000Z',
    kind: 'agent',
    lastSequence: 0,
    patchStale: false,
    phase: 'done',
    planStale: false,
    question: 'Fixture question',
    runId: RUN_ID,
    station: 'critic',
    status: 'completed',
    stepLog: [],
    taskStates: {},
    touchedArtifacts: [],
  }
}

function unavailable(name: string): never {
  throw new Error(`${name} is not part of the canvas body fixture.`)
}

/** Answers to saves that wait for the test (`answer=held`). */
const heldSaveAnswers: Array<() => void> = []

window.__answerCanvasSaves = () => {
  for (const answer of heldSaveAnswers.splice(0)) answer()
}

/** The server's answer to a save: the next task, or, when the test holds
 * answers, whenever it calls `__answerCanvasSaves`. */
function serverAnswer(held: boolean): Promise<void> {
  return new Promise((resolve) => {
    if (held) heldSaveAnswers.push(resolve)
    else window.setTimeout(resolve, 0)
  })
}

/**
 * The agent canvas document view with a server stand-in: every save is
 * recorded; once the server answers, the body is stored as the next
 * revision and reloaded into the run before the save resolves, as the
 * control API does. With `heldAnswers` the test decides when the server
 * answers, so the user can type while a save is on its way.
 */
function CanvasFixture({ heldAnswers, storedBody }: { heldAnswers: boolean; storedBody: string }) {
  const [runs, setRuns] = useState<Record<string, AgentRunRecord>>(() => ({
    [RUN_ID]: canvasRun(canvasArtifact(storedBody, 1, 'agent')),
  }))
  const [saveRegistry] = useState(createCanvasSaveRegistry)
  const context = useMemo<AgentCanvasContextValue>(() => ({
    applyPatch: async () => unavailable('applyPatch'),
    canvasCommentEdit: null,
    canvasComments: [],
    cancelTask: async () => unavailable('cancelTask'),
    clearCanvasCommentEdit: noOp,
    clientOptions: null,
    decideApproval: async () => unavailable('decideApproval'),
    exportArtifact: async () => unavailable('exportArtifact'),
    fileAssets: {},
    inspectChildRun: async () => unavailable('inspectChildRun'),
    knowledgeDataSource: {
      canLoadFileContent: null,
      loadDocumentText: async () => unavailable('loadDocumentText'),
      loadFileContent: null,
      search: async () => unavailable('search'),
    },
    loadArtifact: async () => unavailable('loadArtifact'),
    loadTaskResult: async () => unavailable('loadTaskResult'),
    openCanvasView: (view) => {
      window.__canvasViews = [...(window.__canvasViews ?? []), view]
    },
    planDrafts: {},
    planSource: { collections: [], vectorBackendLabel: null },
    pollingRunIds: [],
    prefetchTaskResult: noOp,
    queueCanvasComment: () => false,
    rejectPatch: async () => unavailable('rejectPatch'),
    renameArtifact: async () => unavailable('renameArtifact'),
    reportGuidanceMaxChars: 0,
    reportRuleIdsMax: 0,
    reportRuleOptions: [],
    requestPlanRefresh: noOp,
    runs,
    saveArtifact: async (_runId, _artifactId, markdown, expectedRevision) => {
      window.__canvasSaves = [...(window.__canvasSaves ?? []), { expectedRevision, markdown }]
      await serverAnswer(heldAnswers)
      const revision = expectedRevision + 1
      setRuns((current) => ({
        ...current,
        [RUN_ID]: canvasRun(canvasArtifact(markdown, revision, 'user')),
      }))
      await serverAnswer(false)
      return { kind: 'saved', revision }
    },
    saveRegistry,
    sessionArtifacts: {},
    sessionFileNames: {},
    setPlanDraft: noOp,
    updateCanvasComment: noOp,
    workspaceId: 'browser-fixture',
  }), [heldAnswers, runs, saveRegistry])

  return (
    <main className="flex h-screen flex-col bg-background">
      <AgentCanvasReactContext.Provider value={context}>
        <DocumentCanvasView descriptor={{ artifactId: ARTIFACT_ID, runId: RUN_ID, view: 'document' }} />
      </AgentCanvasReactContext.Provider>
    </main>
  )
}

const COLLABORATION_BODY = 'Einleitung mit Beleg [K1].\n\n```js\nconst wert = 1\n```\n\nSchluss.'
const COLLABORATION_DOCUMENT_ID = 'body-change-collaboration'

function collaborationRoom(): { document: Y.Doc; provider: HocuspocusProvider } {
  const document = new Y.Doc()
  prosemirrorJSONToYXmlFragment(
    getSchema(createEditorSchemaExtensions({ enableUndoRedo: false })),
    parseEditorMarkdown(COLLABORATION_BODY),
    document.getXmlFragment(EDITOR_YJS_FRAGMENT),
  )
  // No server takes part: a provider without awareness shows no carets.
  return { document, provider: {} as HocuspocusProvider }
}

/**
 * The live editor surface on a synced collaborative document whose right
 * to edit the test grants and withdraws, as a reconnect or a session
 * refresh does.
 */
function CollaborationFixture() {
  const [canEdit, setCanEdit] = useState(true)
  const [room] = useState(collaborationRoom)
  const [ready, setReady] = useState(false)
  useEffect(() => {
    window.__setCollaborationCanEdit = setCanEdit
    return () => {
      delete window.__setCollaborationCanEdit
    }
  }, [])
  const onEditorReady = useCallback((editor: Editor | null) => setReady(editor !== null), [])
  const document: EditorDocumentRecord = {
    access: { mode: 'owner', permission: 'edit' },
    collaboration: {
      generation: 1,
      persistedSequence: 0,
      projectionSequence: 0,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    contentMarkdown: COLLABORATION_BODY,
    contentMode: 'collaboration',
    createdAt: '2026-09-01T08:00:00.000Z',
    folderId: null,
    id: COLLABORATION_DOCUMENT_ID,
    metadataRevision: 1,
    revision: 1,
    source: 'blank',
    title: 'Collaboration.md',
    updatedAt: '2026-09-01T08:00:00.000Z',
  }
  const collaboration = {
    canEdit,
    document: room.document,
    documentId: COLLABORATION_DOCUMENT_ID,
    generation: 1,
    lifecycleKey: `${COLLABORATION_DOCUMENT_ID}:1`,
    liveDocumentReady: true,
    provider: room.provider,
    synced: true,
    user: { color: '#336699', id: 'fixture-user', name: 'Fixture' },
  } as CollaborationDocumentHandle

  return (
    <main className="h-screen bg-background p-4">
      <section
        className="h-[32rem] overflow-auto border border-border px-6 py-4"
        data-ready={ready ? 'true' : 'false'}
        data-testid="body-change-surface"
      >
        <MarkdownEditorSurface
          collaboration={collaboration}
          comments={[]}
          copy={editorCopy.en}
          diffAnchorMarkdown={null}
          document={document}
          embedded
          isDiffVisible={false}
          mode="live"
          onAcceptSuggestion={noOp}
          onChange={recordBodyChange}
          onCreateComment={noOp}
          onEditSuggestion={noOp}
          onEditorReady={onEditorReady}
          onMarkSuggestionStale={noOp}
          onRefineSuggestion={noOpAsync}
          onRejectSuggestion={noOp}
          onSelectComment={noOp}
          onStopSuggestion={noOp}
          runningSuggestionIds={[]}
          selectedCommentId={null}
          suggestionErrors={{}}
          suggestions={[]}
          textImprovement={textImprovement}
        />
      </section>
    </main>
  )
}

const parameters = new URLSearchParams(window.location.search)
const bodyKind = (parameters.get('body') ?? 'table') as BodyKind
const agentBody = STORED_BODIES[bodyKind]
if (agentBody === undefined) throw new Error(`Unknown body kind ${bodyKind}.`)
const storedBody = parameters.get('form') === 'fixpoint' ? serializerFixpoint(agentBody) : agentBody
window.__canvasStoredBody = storedBody

const scenario = parameters.get('scenario')

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    {scenario === 'canvas'
      ? <CanvasFixture heldAnswers={parameters.get('answer') === 'held'} storedBody={storedBody} />
      : scenario === 'collaboration'
        ? <CollaborationFixture />
        : (
            <SurfaceFixture
              echo={parameters.get('echo') === 'host'}
              storedBody={storedBody}
            />
          )}
  </AppProviders>,
)
