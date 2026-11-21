import '@fontsource-variable/inter/index.css'
import 'katex/dist/katex.min.css'
import '@/styles/globals.css'

import { StrictMode, useEffect, useReducer, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { Editor } from '@tiptap/react'

import type { ServerEditorDocument } from '@/api/inqtrixClient'
import { AppProviders } from '@/app/AppProviders'
import { selectedEditorDocument } from '@/features/project/selectors'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type { ProjectState } from '@/features/project/types'
import { researchDeskReducer } from '@/features/researchDesk/state'

import { MarkdownEditorSurface } from '../core/MarkdownEditorSurface'
import { EditorDocumentChangesSection } from '../EditorDocumentChangesSection'
import { conflictCopyOriginal, EditorConflictCopyNotice } from '../EditorWorkspace'
import { editorCopy } from '../editorCopy'
import { documentRecordFromServer } from '../editorSync'
import { useCollaborationDocument } from '../useCollaborationDocument'
import { useEditorHistoryApi } from '../useEditorHistoryApi'
import { useEditorSuggestions } from '../useEditorSuggestions'

// The desk's Markdown sync with a second writer, wired as the research desk
// wires it: the real history hook over the page's routes, the real editor
// surface on the open document, the notices of a conflict copy, and the
// suggestion hook whose acceptance of an agent patch saves the document at
// once and merges the server's text afterwards. Focusing the window
// refreshes the lists, as focusing, coming back online and a reconnect do
// in the desk. `sync=off` runs the desk without project sync.

declare global {
  interface Window {
    /** The merge of a server version and a keystroke in the editor, in one
     * task: the keystroke is sent on the text the editor showed before the
     * merge reached it. */
    __mergeAndType?: (server: ServerEditorDocument, text: string) => void
  }
}

const noOp = () => undefined
const noOpAsync = async () => undefined
const textImprovement = { enabled: false, workspaceId: 'browser-fixture' }

function fixtureState(): ProjectState {
  return { ...createEmptyProjectState(), dirty: false }
}

function ConcurrentWritersFixture() {
  const syncActive = new URLSearchParams(window.location.search).get('sync') !== 'off'
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, fixtureState)
  const [refreshToken, setRefreshToken] = useState(0)
  const history = useEditorHistoryApi({
    apiKey: undefined,
    dispatch,
    editorCommentOutbox: state.editorCommentOutbox,
    editorComments: state.editorComments,
    editorDocuments: state.editorDocuments,
    editorFolders: state.editorFolders,
    locale: 'de',
    projectEpoch: state.projectEpoch,
    refreshToken,
    selectedDocumentId: state.editorUi.activeDocumentId,
    syncActive,
    workspaceId: 'browser-fixture',
  })
  const active = selectedEditorDocument(state)
  const [editor, setEditor] = useState<Editor | null>(null)
  // The comparison of an open conflict copy with its original, as the
  // workspace shows it.
  const [comparingCopyId, setComparingCopyId] = useState<string | null>(null)
  const comparingWithOriginal = active && comparingCopyId === active.id
    ? conflictCopyOriginal(history.conflictCopies, state.editorDocuments, active.id)
    : null
  const collaboration = useCollaborationDocument({
    active: false,
    apiKey: undefined,
    document: active,
    workspaceId: 'browser-fixture',
  })
  const controller = useEditorSuggestions({
    activeDocument: active,
    activeEditor: editor,
    attachedCommentIds: [],
    attachedRefs: [],
    capabilities: null,
    collaboration,
    comments: [],
    dispatch,
    flushDocument: syncActive ? history.flushDocumentForShare : undefined,
    handedOverDiscussions: [],
    locale: 'de',
    mergeServerDocument: history.mergeServerDocument,
    onGlobalSuccess: noOp,
    selectedModelTier: null,
    state,
  })
  useEffect(() => {
    const refresh = () => setRefreshToken((token) => token + 1)
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [])
  const mergeRef = useRef(history.mergeServerDocument)
  mergeRef.current = history.mergeServerDocument
  useEffect(() => {
    window.__mergeAndType = (server, text) => {
      if (!editor || editor.isDestroyed) throw new Error('The editor is not ready.')
      const record = documentRecordFromServer(server)
      void mergeRef.current(record.id, record)
      // The keystroke at the caret, as ProseMirror's own key handling
      // sends it. The caret is read from the page: the editor reads a
      // selection change only after the event that made it.
      const caret = window.getSelection()
      const at = caret?.anchorNode && editor.view.dom.contains(caret.anchorNode)
        ? editor.view.posAtDOM(caret.anchorNode, caret.anchorOffset)
        : editor.state.selection.from
      editor.view.dispatch(editor.state.tr.insertText(text, at))
    }
    return () => {
      delete window.__mergeAndType
    }
  }, [editor])
  const copy = editorCopy.de
  const changes = controller.documentSuggestions.filter((suggestion) => (
    suggestion.status === 'pending' || suggestion.status === 'stale'
  ))
  const bodyReady = active !== null
    && history.selectedDocumentBodyHydration.documentId === active.id
    && history.selectedDocumentBodyHydration.phase === 'ready'
  return (
    <main className="flex h-screen gap-3 overflow-auto bg-background p-4">
      <nav aria-label="Dokumente" className="w-64 shrink-0 space-y-1" data-testid="tree">
        {state.editorDocumentOrder.map((documentId) => {
          const document = state.editorDocuments[documentId]
          if (!document) return null
          return (
            <button
              aria-current={document.id === active?.id ? 'page' : undefined}
              className="block w-full truncate text-left t-meta-sm"
              data-document-id={document.id}
              data-testid="tree-item"
              key={document.id}
              onClick={() => dispatch({ documentId: document.id, type: 'openEditorDocument' })}
              type="button"
            >
              {document.title}
            </button>
          )
        })}
      </nav>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap gap-2 t-meta-sm" role="toolbar">
          <output data-testid="active-document">{active?.id ?? ''}</output>
          <output data-testid="document-revision">{active?.revision ?? ''}</output>
          <output data-testid="document-mode">{active ? active.contentMode ?? 'markdown' : ''}</output>
          <output data-testid="conflict-copies">
            {history.conflictCopies
              .map((entry) => `${entry.originalDocumentId}>${entry.reason}:${entry.saved ? 'saved' : 'unsaved'}`)
              .join(',')}
          </output>
          <output data-testid="suggestion-statuses">
            {controller.documentSuggestions
              .map((suggestion) => `${suggestion.serverPatch?.editId ?? 'assistant'}:${suggestion.status}`)
              .join(',')}
          </output>
          <output data-testid="instruction-feedback">{controller.instructionFeedback?.message ?? ''}</output>
          <output data-testid="sync-error">{history.error ?? ''}</output>
        </div>
        {active ? (
          <EditorConflictCopyNotice
            comparing={comparingWithOriginal !== null}
            conflictCopies={history.conflictCopies}
            dispatch={dispatch}
            document={active}
            documents={state.editorDocuments}
            locale="de"
            onCompareChange={(comparing) => setComparingCopyId(comparing ? active.id : null)}
          />
        ) : null}
        <section className="mt-3 flex h-72 flex-col overflow-hidden border border-border" data-testid="surface">
          {active && bodyReady && active.contentMode !== 'collaboration' ? (
            <MarkdownEditorSurface
              collaboration={null}
              comments={[]}
              copy={editorCopy.de}
              diffAnchorMarkdown={comparingWithOriginal?.contentMarkdown ?? null}
              document={active}
              embedded
              isDiffVisible={comparingWithOriginal !== null}
              key={active.id}
              mode="live"
              onAcceptSuggestion={noOp}
              onChange={(contentMarkdown, baseContentMarkdown) => dispatch({
                baseContentMarkdown,
                contentMarkdown,
                documentId: active.id,
                type: 'updateEditorDocumentMarkdown',
              })}
              onCreateComment={noOp}
              onEditSuggestion={noOp}
              onEditorReady={setEditor}
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
          ) : active?.contentMode === 'collaboration' ? (
            <p className="t-meta p-3" data-testid="collaboration-mode">collaboration</p>
          ) : null}
        </section>
        <pre className="mt-2 whitespace-pre-wrap t-meta-sm" data-testid="record-markdown">
          {active?.contentMarkdown ?? ''}
        </pre>
        <section className="mt-3 w-96" data-testid="review-changes">
          <EditorDocumentChangesSection
            labels={{
              accept: copy.accept,
              acceptAll: copy.acceptAll,
              documentChanges: copy.documentChanges,
              proposedChange: copy.proposedChange,
              reject: copy.reject,
              rejectAll: copy.rejectAll,
              stale: copy.suggestionStale,
            }}
            onAcceptGroup={(groupId) => void controller.handleAcceptSuggestionGroup(groupId)}
            onAcceptSuggestion={(suggestion) => void controller.handleAcceptSuggestion(suggestion)}
            onRejectGroup={(groupId) => void controller.handleRejectSuggestionGroup(groupId)}
            onRejectSuggestion={(suggestionId) => void controller.handleRejectSuggestion(suggestionId)}
            onSelectSuggestion={noOp}
            publishDisabledReason={controller.suggestionPublishDisabledReason}
            suggestionErrors={controller.suggestionErrors}
            suggestions={changes}
          />
        </section>
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders initialLocale="de">
      <ConcurrentWritersFixture />
    </AppProviders>
  </StrictMode>,
)
