import '@fontsource-variable/inter/index.css'
import 'katex/dist/katex.min.css'
import '@/styles/globals.css'

import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState, type MouseEvent } from 'react'
import { createRoot } from 'react-dom/client'
import Collaboration from '@tiptap/extension-collaboration'
import { Extension, getSchema } from '@tiptap/core'
import { EditorContent, useEditor, type Editor } from '@tiptap/react'
import { Plugin } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { prosemirrorJSONToYDoc } from '@tiptap/y-tiptap'
import {
  EDITOR_COLLABORATION_PROTOCOL_VERSION,
  EDITOR_SCHEMA_VERSION,
  EDITOR_YJS_FRAGMENT,
  containsStructureSuggestionAttribute,
  containsSuggestionBoundary,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { AppProviders } from '@/app/AppProviders'
import {
  createEditorCollaborationSession,
  type EditorCollaborationCommentThread,
} from '@/api/inqtrixClient'
import { Info } from '@/components/icons'
import { ExplorerHistoryRow } from '@/components/ui/explorer-list'
import { useModalFocusTrap } from '@/components/ui/use-modal-focus-trap'
import { selectedEditorDocument } from '@/features/project/selectors'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type {
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorSuggestionOrigin,
  EditorSuggestionRecord,
  ProjectState,
} from '@/features/project/types'
import { ProfileAvatar } from '@/features/researchDesk/components/ProfileAvatar'
import { researchDeskReducer } from '@/features/researchDesk/state'
import { ShareDialog } from '@/features/sharing/ShareDialog'
import type { MentionComposerHandle } from '@/features/composer/MentionComposer'
import {
  EditorAssistantComposer,
  EditorDocumentRecoveryNotice,
  EditorTopBar,
} from '../EditorWorkspace'
import {
  CollaborationEditRefusalLine,
  CollaborationRejectionBar,
  useCollaborationEditRefusal,
} from '../CollaborationNoticeBars'
import { workspaceRecoveryStore } from '../collaborationRecovery'
import { COLLABORATION_NOTICES } from '../collaborationNotices'
import { DocumentDiffView } from '../DocumentDiffView'
import { EditorDocumentChangesSection } from '../EditorDocumentChangesSection'
import { editorCopy } from '../editorCopy'
import {
  collaborationProjectionController,
} from '../collaborationProjection'
import {
  collaborationBindingForEditorDocument,
  collaborationSurfaceAcceptsLocalEdits,
  isCollaborationSurfaceSynced,
  MarkdownEditorSurface,
} from '../core/MarkdownEditorSurface'
import {
  editorForSurfaceIdentity,
  updateEditorSurfaceRegistration,
  type EditorSurfaceIdentity,
  type EditorSurfaceRegistration,
} from '../core/editorSurfaceRegistration'
import { EditorCollaborationStatus, EditorInspector } from '../inspector/EditorInspector'
import { useInspectorCollaborationActivity } from '../inspector/collaborationAdapter'
import { TeamCommentsPanel } from '../inspector/TeamCommentsPanel'
import { EditorTopBarLayout } from '../inspector/EditorTopBarLayout'
import {
  buildEditorCollaborationStatusModel,
  effectiveEditorWriteMode,
  localizeCollaborationEditRefusal,
  localizeEditorCollaborationNotice,
  type EditorWriteMode,
  type InspectorChange,
} from '../inspector/model'
import {
  applyCollaborationEditorPolicy,
  collaborationEditorPolicyUpdate,
  type CollaborationEditorPolicyInput,
} from '../inspector/editorPolicy'
import {
  createEditorExtensions,
  renderCollaborationCaret,
  serializeEditorFinalProjectionMarkdown,
} from '../tiptap'
import {
  CollaborationDocumentController,
  useCollaborationDocument,
  type CollaborationDocumentHandle,
} from '../useCollaborationDocument'
import type {
  CollaborationCommentsHandle,
} from '../useCollaborationComments'
import { planEditorDocumentAutosave } from '../useEditorHistoryApi'
import {
  createInstructionSuggestionRecords,
  editorAiReadOnlyReason,
  privateSuggestionPublishDisabledReason,
  useEditorSuggestions,
} from '../useEditorSuggestions'

type RenderSnapshot = {
  controllerRegistered: boolean
  handleDocumentId: string | null
  lifecycleStatus: string
  requestedDocumentId: string
}

declare global {
  interface Window {
    __collaborationRenderSnapshots?: RenderSnapshot[]
    __reconnectConnectionStatuses?: string[]
    __commentScaleFirstInteractiveMs?: number
    __commentScaleSelectionMs?: number
    __commentScaleSelectionStartedAt?: number
    __slashCollaborationUpdate?: number[]
    __slashCollaborationUpdates?: number[][]
    __slashCollaborationServerUpdate?: number[]
    __slashSuggestionBoundaryDetections?: boolean[]
    __slashStructureUpdateDetections?: boolean[]
  }
}

const noOp = () => undefined
const noOpAsync = async () => undefined
const textImprovement = { enabled: false, workspaceId: 'browser-fixture' }
const fixtureModuleStartedAt = performance.now()

function collaborationDocument(id: string, generation: number): EditorDocumentRecord {
  return {
    access: { mode: 'owner', permission: 'edit' },
    collaboration: {
      generation,
      persistedSequence: 0,
      projectionSequence: 0,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    contentMarkdown: `# Projection ${id.toUpperCase()}\n\nFallback body for ${id.toUpperCase()}.`,
    contentMode: 'collaboration',
    createdAt: '2026-07-15T08:00:00.000Z',
    folderId: null,
    id,
    metadataRevision: 2,
    revision: 1,
    source: 'blank',
    title: `${id.toUpperCase()}.md`,
    updatedAt: '2026-07-15T08:00:00.000Z',
  }
}

const switchDocuments = {
  'doc-a': collaborationDocument('doc-a', 1),
  'doc-b': collaborationDocument('doc-b', 2),
}

function Surface({
  collaboration,
  collaborationReviewPolicy,
  comments = [],
  document,
  onChange = noOp,
  onEditorReady,
  suggestions = [],
}: {
  collaboration?: ReturnType<typeof useCollaborationDocument> | null
  collaborationReviewPolicy?: Parameters<typeof MarkdownEditorSurface>[0]['collaborationReviewPolicy']
  comments?: Parameters<typeof MarkdownEditorSurface>[0]['comments']
  document: EditorDocumentRecord
  onChange?: (contentMarkdown: string) => void
  onEditorReady: Parameters<typeof MarkdownEditorSurface>[0]['onEditorReady']
  suggestions?: Parameters<typeof MarkdownEditorSurface>[0]['suggestions']
}) {
  return (
    <MarkdownEditorSurface
      collaboration={collaboration}
      collaborationReviewPolicy={collaborationReviewPolicy}
      comments={comments}
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
      suggestions={suggestions}
      textImprovement={textImprovement}
    />
  )
}

function DocumentSwitchFixture() {
  const [requestedDocumentId, setRequestedDocumentId] = useState<'doc-a' | 'doc-b'>('doc-a')
  const [registration, setRegistration] = useState<EditorSurfaceRegistration | null>(null)
  const document = switchDocuments[requestedDocumentId]
  const identity = useMemo<EditorSurfaceIdentity>(() => ({
    documentId: document.id,
    generation: document.collaboration?.generation ?? null,
  }), [document])
  const collaboration = useCollaborationDocument({
    active: true,
    apiKey: undefined,
    document,
    workspaceId: 'browser-fixture',
  })
  const controllerRegistered = collaborationProjectionController(
    collaboration,
    identity.documentId,
    identity.generation,
  ) !== null
  const binding = collaborationBindingForEditorDocument(document, collaboration)
  const onEditorReady = useCallback<Parameters<typeof Surface>[0]['onEditorReady']>(
    (editor) => setRegistration((current) => updateEditorSurfaceRegistration(
      current,
      identity,
      editor,
    )),
    [identity],
  )

  useLayoutEffect(() => {
    const snapshots = window.__collaborationRenderSnapshots ?? []
    snapshots.push({
      controllerRegistered,
      handleDocumentId: collaboration.documentId,
      lifecycleStatus: collaboration.lifecycleStatus,
      requestedDocumentId,
    })
    window.__collaborationRenderSnapshots = snapshots
  }, [collaboration.documentId, collaboration.lifecycleStatus, controllerRegistered, requestedDocumentId])

  return (
    <main className="h-screen bg-background p-4">
      <button data-testid="switch-document" onClick={() => setRequestedDocumentId('doc-b')}>
        Switch to B
      </button>
      <dl
        className="t-meta-sm mt-2"
        data-binding-document-id={binding ? document.id : 'none'}
        data-controller-registered={controllerRegistered ? 'true' : 'false'}
        data-handle-document-id={collaboration.documentId ?? 'none'}
        data-lifecycle-status={collaboration.lifecycleStatus}
        data-registered-editor-id={editorForSurfaceIdentity(registration, identity) ? document.id : 'none'}
        data-requested-document-id={requestedDocumentId}
        data-testid="switch-state"
      >
        <dt>Requested</dt>
        <dd>{requestedDocumentId}</dd>
      </dl>
      <section className="mt-3 h-80 overflow-hidden border border-border" data-testid="switch-surface">
        <Surface collaboration={collaboration} document={document} onEditorReady={onEditorReady} />
      </section>
    </main>
  )
}

const reconnectDocument: EditorDocumentRecord = {
  ...collaborationDocument('reconnect-doc', 1),
  // Deliberately unlike the live document: a fallback to the projection
  // during a reconnect is visible in the editor.
  contentMarkdown: 'Stale projection that the live document never contains.',
}

/** The real collaboration controller and editor surface for one document,
 * with the controller's connection states and the number of distinct editor
 * instances exposed for the reconnect-continuity browser test. The review
 * policy's write mode is derived and applied as the editor workspace does
 * it, so the policy gate behind the surface is the production one. */
function ReconnectContinuityFixture() {
  const collaboration = useCollaborationDocument({
    active: true,
    apiKey: undefined,
    document: reconnectDocument,
    workspaceId: 'browser-fixture',
  })
  const editorsRef = useRef(new WeakSet<Editor>())
  const [editor, setEditor] = useState<Editor | null>(null)
  const [editorInstances, setEditorInstances] = useState(0)
  const onEditorReady = useCallback((readyEditor: Editor | null) => {
    setEditor(readyEditor)
    if (!readyEditor || editorsRef.current.has(readyEditor)) return
    editorsRef.current.add(readyEditor)
    setEditorInstances((count) => count + 1)
  }, [])
  const reviewWriteMode = effectiveEditorWriteMode(
    collaboration.access,
    collaborationSurfaceAcceptsLocalEdits(collaboration),
    'edit',
  )
  const writeAuthorId = collaboration.user?.id ?? null
  useLayoutEffect(() => {
    applyCollaborationEditorPolicy(editor, reviewPolicy(reviewWriteMode, {
      documentId: reconnectDocument.id,
      writeAuthorId,
    }))
  }, [editor, reviewWriteMode, writeAuthorId])
  useLayoutEffect(() => {
    const statuses = window.__reconnectConnectionStatuses ?? []
    statuses.push(collaboration.connectionStatus)
    window.__reconnectConnectionStatuses = statuses
  }, [collaboration.connectionStatus])
  const model = buildEditorCollaborationStatusModel({
    access: collaboration.access,
    active: true,
    canEdit: collaboration.canEdit,
    connectionStatus: collaboration.connectionStatus,
    durabilityStatus: collaboration.durabilityStatus,
    hasUnconfirmedLocalChanges: collaboration.hasUnconfirmedLocalChanges,
    nextReconnectAt: collaboration.nextReconnectAt,
    notice: collaboration.error,
    participants: [],
    reconnectAttempt: collaboration.reconnectAttempt,
    recoverability: collaboration.recoverability,
    synced: isCollaborationSurfaceSynced(collaboration),
    unconfirmedSince: collaboration.unconfirmedSince,
  })

  return (
    <main className="h-screen bg-background p-4">
      <dl
        className="t-meta-sm"
        data-can-edit={collaboration.canEdit ? 'true' : 'false'}
        data-connection-status={collaboration.connectionStatus}
        data-durability-status={collaboration.durabilityStatus}
        data-editor-instances={editorInstances}
        data-lifecycle-status={collaboration.lifecycleStatus}
        data-testid="reconnect-state"
      >
        <dt>Connection</dt>
        <dd>{collaboration.connectionStatus}</dd>
      </dl>
      <div className="mt-2 w-72" data-testid="reconnect-status">
        <EditorCollaborationStatus collaborationExpected model={model} variant="topbar" />
      </div>
      <p className="mt-2 t-meta-sm" data-testid="reconnect-outside">
        Text outside the editor.
      </p>
      <section className="mt-3 h-80 overflow-hidden border border-border" data-testid="reconnect-surface">
        <Surface
          collaboration={collaboration}
          document={reconnectDocument}
          onEditorReady={onEditorReady}
        />
      </section>
    </main>
  )
}

type WriterPageMode = 'edit' | 'suggest'

declare global {
  interface Window {
    /** The live document of the writer fixture's controller, encoded. */
    __writerDocumentState?: () => number[] | null
    /** Appends plain text to the first paragraph of the writer fixture's
     * live document, past the editor and its suggestion rules. */
    __writerAppendPlainText?: (text: string) => void
  }
}

function writerDocumentRecord(id: string): EditorDocumentRecord {
  return {
    ...collaborationDocument(id, 1),
    // Unlike the live document: a fallback to the projection is visible.
    contentMarkdown: 'Stale projection that the live document never contains.',
  }
}

/** The controller the production hook provides, through the lifecycle
 * registry: an unmounted pane lingers and a remount takes it back. */
function RegisteredWriterPane(props: { documentId: string; pageMode: WriterPageMode }) {
  const document = useMemo(() => writerDocumentRecord(props.documentId), [props.documentId])
  const collaboration = useCollaborationDocument({
    active: true,
    apiKey: undefined,
    document,
    workspaceId: 'browser-fixture',
  })
  return <WriterSurface collaboration={collaboration} document={document} pageMode={props.pageMode} />
}

/** A controller built directly, whose socket is the engine's own WebSocket
 * to a relay on `relayPort`: the page's own frames at unload are what the
 * relay receives. */
function RelayedWriterPane(props: {
  documentId: string
  pageMode: WriterPageMode
  relayPort: string
}) {
  const document = useMemo(() => writerDocumentRecord(props.documentId), [props.documentId])
  const [collaboration, setCollaboration] = useState<CollaborationDocumentHandle | null>(null)
  useEffect(() => {
    const controller = new CollaborationDocumentController({
      documentId: document.id,
      generation: 1,
      initialPersistedSequence: 0,
      requestSession: (leaseToken, rotationCommandId) => createEditorCollaborationSession(
        document.id,
        {
          protocol_version: EDITOR_COLLABORATION_PROTOCOL_VERSION,
          schema_version: EDITOR_SCHEMA_VERSION,
          ...(leaseToken === undefined ? {} : { lease_token: leaseToken }),
          ...(rotationCommandId === undefined ? {} : { rotation_command_id: rotationCommandId }),
        },
        { workspaceId: 'browser-fixture' },
      ),
      resolveWebSocketUrl: (path) => `ws://127.0.0.1:${props.relayPort}${path}`,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    })
    const unsubscribe = controller.subscribe(setCollaboration)
    setCollaboration(controller.getSnapshot())
    void controller.start()
    return () => {
      unsubscribe()
      controller.release()
    }
  }, [document.id, props.relayPort])
  if (!collaboration) return null
  return <WriterSurface collaboration={collaboration} document={document} pageMode={props.pageMode} />
}

/** The real editor surface on one controller, with the page's write mode
 * applied as the editor workspace applies it: as the editor's initial policy
 * and through the policy switch while it is mounted. */
function WriterSurface({
  collaboration,
  comments,
  document,
  onEditorReady,
  pageMode,
}: {
  collaboration: CollaborationDocumentHandle
  /** The private comments of the writer's document, as the workspace hands
   * them to the editor surface. */
  comments?: EditorCommentThreadRecord[]
  document: EditorDocumentRecord
  /** Hands the live editor to a page that drives more of the workspace. */
  onEditorReady?: (editor: Editor | null) => void
  pageMode: WriterPageMode
}) {
  const [editor, setEditor] = useState<Editor | null>(null)
  useEffect(() => {
    onEditorReady?.(editor)
  }, [editor, onEditorReady])
  const editRefusal = localizeCollaborationEditRefusal(useCollaborationEditRefusal(editor), 'de')
  const reviewWriteMode = effectiveEditorWriteMode(
    collaboration.access,
    collaborationSurfaceAcceptsLocalEdits(collaboration),
    pageMode,
  )
  const writeAuthorId = collaboration.user?.id ?? null
  const policy = reviewPolicy(reviewWriteMode, { documentId: document.id, writeAuthorId })
  useLayoutEffect(() => {
    applyCollaborationEditorPolicy(editor, reviewPolicy(reviewWriteMode, {
      documentId: document.id,
      writeAuthorId,
    }))
  }, [document.id, editor, reviewWriteMode, writeAuthorId])
  const liveDocument = collaboration.document
  useEffect(() => {
    window.__writerDocumentState = () => (
      liveDocument ? Array.from(Y.encodeStateAsUpdate(liveDocument)) : null
    )
    window.__writerAppendPlainText = (text) => {
      const paragraph = liveDocument?.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0)
      const content = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
      if (!(content instanceof Y.XmlText)) {
        throw new Error('The first block of the writer document holds no text.')
      }
      content.insert(content.length, text)
    }
  }, [liveDocument])
  const model = buildEditorCollaborationStatusModel({
    access: collaboration.access,
    active: true,
    canEdit: collaboration.canEdit,
    connectionStatus: collaboration.connectionStatus,
    durabilityDelayed: collaboration.durabilityDelayed,
    durabilityStatus: collaboration.durabilityStatus,
    hasUnconfirmedLocalChanges: collaboration.hasUnconfirmedLocalChanges,
    nextReconnectAt: collaboration.nextReconnectAt,
    notice: collaboration.error,
    participants: [],
    reconnectAttempt: collaboration.reconnectAttempt,
    recoverability: collaboration.recoverability,
    synced: isCollaborationSurfaceSynced(collaboration),
    unconfirmedSince: collaboration.unconfirmedSince,
  })
  return (
    <>
      <dl
        className="t-meta-sm mt-2"
        data-access={collaboration.access ?? 'none'}
        data-can-edit={collaboration.canEdit ? 'true' : 'false'}
        data-connection-status={collaboration.connectionStatus}
        data-document-id={document.id}
        data-durability-delayed={collaboration.durabilityDelayed ? 'true' : 'false'}
        data-durability-status={collaboration.durabilityStatus}
        data-has-unconfirmed={collaboration.hasUnconfirmedLocalChanges ? 'true' : 'false'}
        data-recoverability={collaboration.recoverability}
        data-testid="writer-state"
        data-update-rejected={collaboration.updateRejected ? 'true' : 'false'}
        data-write-mode={reviewWriteMode}
      >
        <dt>Connection</dt>
        <dd>{collaboration.connectionStatus}</dd>
        <dt>Notice</dt>
        <dd data-testid="writer-notice">{collaboration.error ?? ''}</dd>
      </dl>
      <div className="mt-2 w-72" data-testid="writer-status">
        <EditorCollaborationStatus collaborationExpected model={model} variant="topbar" />
      </div>
      {collaboration.updateRejected ? (
        <CollaborationRejectionBar
          locale="de"
          notice={localizeEditorCollaborationNotice(
            collaboration.error ?? COLLABORATION_NOTICES.update_rejected,
            'de',
          ) ?? COLLABORATION_NOTICES.update_rejected}
          onDownloadBackup={null}
          onReload={collaboration.reloadDocument}
        />
      ) : editRefusal ? (
        <CollaborationEditRefusalLine message={editRefusal} />
      ) : null}
      <section className="mt-3 h-96 overflow-hidden border border-border" data-testid="writer-surface">
        <Surface
          collaboration={collaboration}
          collaborationReviewPolicy={collaborationEditorPolicyUpdate(policy)}
          comments={comments}
          document={document}
          onEditorReady={setEditor}
        />
      </section>
      {new URLSearchParams(window.location.search).get('history') === '1' ? (
        <WriterHistory collaboration={collaboration} documentId={document.id} />
      ) : null}
    </>
  )
}

/** The inspector's history of the writer's document, loaded from the
 * activity endpoint whenever the live document changes, as the editor
 * workspace loads it. */
function WriterHistory({
  collaboration,
  documentId,
}: {
  collaboration: CollaborationDocumentHandle
  documentId: string
}) {
  const activity = useInspectorCollaborationActivity({
    active: true,
    activityRevision: collaboration.activityRevision,
    authorityRevision: collaboration.authorityRevision,
    documentId,
    generation: 1,
    lifecycleKey: collaboration.lifecycleKey,
    locale: 'de',
    openSuggestionIds: [],
    readAuthority: collaboration.readAuthority,
    visible: true,
    workspaceId: 'browser-fixture',
  })
  return (
    <section className="mt-3 h-96" data-testid="writer-history">
      <EditorInspector
        activeTab="changes"
        assistant={<div />}
        canDecide={false}
        changes={[]}
        changesError={null}
        changesView="history"
        collaborationActive
        collaborationStatus={buildEditorCollaborationStatusModel({
          access: collaboration.access,
          active: true,
          canEdit: collaboration.canEdit,
          connectionStatus: collaboration.connectionStatus,
          durabilityDelayed: collaboration.durabilityDelayed,
          durabilityStatus: collaboration.durabilityStatus,
          hasUnconfirmedLocalChanges: collaboration.hasUnconfirmedLocalChanges,
          nextReconnectAt: collaboration.nextReconnectAt,
          notice: collaboration.error,
          participants: [],
          reconnectAttempt: collaboration.reconnectAttempt,
          recoverability: collaboration.recoverability,
          synced: isCollaborationSurfaceSynced(collaboration),
          unconfirmedSince: collaboration.unconfirmedSince,
        })}
        commentCount={0}
        comments={<div />}
        commentUnreadCount={0}
        decisionError={null}
        display="simple"
        history={activity.entries}
        historyError={activity.error}
        historyFilters={{ actorId: null, type: null }}
        historyLoading={activity.isLoading}
        isDecisionPending={false}
        onActiveTabChange={noOp}
        onChangesViewChange={noOp}
        onClose={noOp}
        onDecision={noOp}
        onDisplayChange={noOp}
        onHistoryFiltersChange={noOp}
        onOpenFiltersChange={noOp}
        onSelectedChangeIdChange={noOp}
        openFilters={{ authorId: null, type: null }}
        selectedChangeId={null}
      />
    </section>
  )
}

/** One writer on the real collaboration controller and editor surface, with
 * the controls a writer uses around the editor: the write mode, leaving and
 * reopening the document, and opening another document. The controls keep
 * the editor's focus and selection, as the editor's own toolbar does. */
function CollaborationWriterFixture() {
  const parameters = new URLSearchParams(window.location.search)
  const initialDocumentId = parameters.get('document') ?? 'writer-doc'
  const alternateDocumentId = parameters.get('alternate')
  const relayPort = parameters.get('relay')
  const [pageMode, setPageMode] = useState<WriterPageMode>('edit')
  const [mounted, setMounted] = useState(true)
  const [documentId, setDocumentId] = useState(initialDocumentId)
  const keepEditorFocus = (event: MouseEvent) => event.preventDefault()
  return (
    <main className="h-screen bg-background p-4">
      <div className="flex gap-2 t-meta-sm" role="toolbar">
        <button
          aria-pressed={pageMode === 'edit'}
          data-testid="writer-mode-edit"
          onClick={() => setPageMode('edit')}
          onMouseDown={keepEditorFocus}
          type="button"
        >
          Edit
        </button>
        <button
          aria-pressed={pageMode === 'suggest'}
          data-testid="writer-mode-suggest"
          onClick={() => setPageMode('suggest')}
          onMouseDown={keepEditorFocus}
          type="button"
        >
          Suggest
        </button>
        <button data-testid="writer-toggle-pane" onClick={() => setMounted((value) => !value)} type="button">
          {mounted ? 'Leave document' : 'Open document'}
        </button>
        {alternateDocumentId ? (
          <button
            data-testid="writer-switch-document"
            onClick={() => setDocumentId(alternateDocumentId)}
            type="button"
          >
            Open other document
          </button>
        ) : null}
      </div>
      {mounted ? (
        relayPort ? (
          <RelayedWriterPane documentId={documentId} pageMode={pageMode} relayPort={relayPort} />
        ) : (
          <RegisteredWriterPane documentId={documentId} pageMode={pageMode} />
        )
      ) : null}
    </main>
  )
}

const AI_PUBLICATION_GROUP_ID = 'ai-publication-group'
const AI_PUBLICATION_COMMENT_ID = 'ai-publication-comment'

/** The row a suggestion hangs on: the machine carrier of an assistant run,
 * or a note the user wrote. */
type AiPublicationCommentOrigin = Extract<EditorSuggestionOrigin, { kind: 'assistant_edit' | 'inline_edit' }>

function aiPublicationCommentOrigin(origin: string | null, index: number): AiPublicationCommentOrigin | null {
  const commentId = `${AI_PUBLICATION_COMMENT_ID}-${index + 1}`
  if (origin === 'carrier') return { commentId, kind: 'assistant_edit' }
  if (origin === 'note') return { commentId, kind: 'inline_edit' }
  return null
}

function aiPublicationState(document: EditorDocumentRecord): ProjectState {
  const base = createEmptyProjectState()
  return {
    ...base,
    editorDocumentOrder: [document.id],
    editorDocuments: { [document.id]: document },
    editorUi: {
      ...base.editorUi,
      activeDocumentId: document.id,
      openDocumentIds: [document.id],
    },
  }
}

/**
 * The writer fixture with the AI suggestion flow of the editor workspace: a
 * private AI suggestion anchored in the live collaboration document, shown
 * on the card of the document changes and accepted through the production
 * publication path (durability barrier, projection, target, publication
 * endpoint). The page's routes answer the API's two endpoints of that path.
 */
function AiPublicationFixture() {
  const parameters = new URLSearchParams(window.location.search)
  const documentId = parameters.get('document') ?? 'writer-doc'
  const document = useMemo(() => writerDocumentRecord(documentId), [documentId])
  const collaboration = useCollaborationDocument({
    active: true,
    apiKey: undefined,
    document,
    workspaceId: 'browser-fixture',
  })
  const [editor, setEditor] = useState<Editor | null>(null)
  const [state, dispatch] = useReducer(researchDeskReducer, document, aiPublicationState)
  const controller = useEditorSuggestions({
    activeDocument: document,
    activeEditor: editor,
    attachedCommentIds: [],
    attachedRefs: [],
    capabilities: null,
    collaboration,
    comments: [],
    dispatch,
    handedOverDiscussions: [],
    locale: 'de',
    onGlobalSuccess: noOp,
    selectedModelTier: null,
    state,
  })
  // One replacement, and optionally a second one (`find2` …) in the same run,
  // each on the row its `origin` (`origin2`) names.
  const suffixes = ['', '2'].filter((suffix) => parameters.has(`find${suffix}`))
  const edits = suffixes.map((suffix) => ({
    find: parameters.get(`find${suffix}`) ?? '',
    note: 'Wort ersetzt',
    position: 'replace' as const,
    quote_after: parameters.get(`after${suffix}`) ?? '',
    quote_before: parameters.get(`before${suffix}`) ?? '',
    text: parameters.get(`replace${suffix}`) ?? '',
  }))
  const origins = suffixes.map((suffix, index) => (
    aiPublicationCommentOrigin(parameters.get(`origin${suffix}`), index)
  ))
  // The records an instruction run creates for its replacements, with the
  // private draft identity the server would have stored for each.
  const seed = () => {
    if (!editor) throw new Error('The writer editor is not ready.')
    const now = new Date().toISOString()
    const records = createInstructionSuggestionRecords({
      activeEditor: editor,
      document,
      groupId: AI_PUBLICATION_GROUP_ID,
      locale: 'de',
      now,
      proposal: { assistantMessage: '', edits },
    })
    if (records.length !== edits.length) throw new Error('An AI suggestion could not be anchored.')
    records.forEach((record, index) => {
      const commentOrigin = origins[index]
      if (!commentOrigin) return
      const comment: EditorCommentThreadRecord = {
        anchor: record.anchor,
        commentMarkdown: 'Bitte ein Wort ersetzen.',
        createdAt: now,
        documentId: document.id,
        id: commentOrigin.commentId,
        kind: commentOrigin.kind,
        status: 'open',
        updatedAt: now,
      }
      // The page only needs the row in the state, as an instruction run
      // adopts its server-saved carrier: no outbox write.
      dispatch({ comment, type: 'adoptEditorCarrierComment' })
    })
    dispatch({
      group: {
        assistantMessage: '',
        createdAt: now,
        documentId: document.id,
        id: AI_PUBLICATION_GROUP_ID,
        origin: { kind: 'global_run' },
      },
      suggestions: records.map((record, index) => ({
        ...record,
        origin: origins[index] ?? record.origin,
        privateDraft: {
          patchId: `99999999-9999-4999-8999-00000000000${2 * index + 1}`,
          publicationCommandId: `99999999-9999-4999-8999-00000000000${2 * index + 2}`,
          revision: 1,
        },
      })),
      type: 'createEditorSuggestionGroup',
    })
  }
  const copy = editorCopy.de
  const changes = controller.documentSuggestions.filter((suggestion) => (
    suggestion.status === 'pending' || suggestion.status === 'stale'
  ))
  return (
    <main className="h-screen overflow-auto bg-background p-4">
      <div className="flex gap-2 t-meta-sm" role="toolbar">
        <button data-testid="ai-seed" disabled={!editor} onClick={seed} type="button">
          Seed AI suggestion
        </button>
        <button
          data-testid="ai-mark-stale"
          onClick={() => {
            // The first open suggestion goes stale, as when another writer
            // changes its passage; a second one stays open.
            const open = changes.find((suggestion) => suggestion.status === 'pending')
            if (open) dispatch({ suggestionId: open.id, type: 'markEditorSuggestionStale' })
          }}
          type="button"
        >
          Mark AI suggestion stale
        </button>
        <output data-testid="ai-statuses">
          {controller.documentSuggestions.map((suggestion) => suggestion.status).join(',')}
        </output>
        <output data-testid="ai-comments">{Object.keys(state.editorComments).join(',')}</output>
      </div>
      <WriterSurface
        collaboration={collaboration}
        comments={Object.values(state.editorComments)}
        document={document}
        onEditorReady={setEditor}
        pageMode="edit"
      />
      <section className="mt-3 w-96" data-testid="ai-changes">
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
    </main>
  )
}

function activationState(): ProjectState {
  const base = createEmptyProjectState()
  const document: EditorDocumentRecord = {
    access: { mode: 'owner', permission: 'edit' },
    contentMarkdown: '# Writable legacy body\n\nThis starts in Markdown mode.',
    createdAt: '2026-07-15T08:00:00.000Z',
    folderId: null,
    id: 'activation-document',
    metadataRevision: 4,
    revision: 7,
    source: 'blank',
    title: 'Clock skew document.md',
    updatedAt: '2099-01-01T00:00:00.000Z',
  }
  return {
    ...base,
    dirty: false,
    editorDocumentOrder: [document.id],
    editorDocuments: { [document.id]: document },
    editorUi: {
      ...base.editorUi,
      activeDocumentId: document.id,
      openDocumentIds: [document.id],
    },
  }
}

function ActivationFixture() {
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, activationState)
  const [bodyChangeCount, setBodyChangeCount] = useState(0)
  const document = state.editorDocuments['activation-document']
  const autosavePlan = planEditorDocumentAutosave(document)

  const activate = () => dispatch({
    collaboration: {
      generation: 5,
      persistedSequence: 0,
      projectionSequence: 0,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    documentId: document.id,
    metadataRevision: 5,
    type: 'activateEditorDocumentCollaboration',
  })
  const resolveExactDetail = () => dispatch({
    document: {
      ...document,
      collaboration: {
        generation: 5,
        persistedSequence: 2,
        projectionSequence: 2,
        projectionUpdatedAt: '2026-07-15T08:05:00.000Z',
        schemaVersion: EDITOR_SCHEMA_VERSION,
      },
      contentMarkdown: '# Exact hydrated projection\n\nThe delayed detail response won.',
      contentMode: 'collaboration',
      metadataRevision: 5,
      updatedAt: '2026-07-15T08:05:00.000Z',
    },
    type: 'setServerEditorDocumentDetail',
  })

  return (
    <main className="h-screen bg-background p-4">
      <div className="flex gap-2">
        <button data-testid="activation-success" onClick={activate}>Resolve activation</button>
        <button data-testid="detail-success" onClick={resolveExactDetail}>Resolve exact detail</button>
      </div>
      <dl
        className="t-meta-sm mt-2"
        data-autosave-kind={autosavePlan.kind}
        data-body-change-count={bodyChangeCount}
        data-content-mode={document.contentMode ?? 'markdown'}
        data-document-updated-at={document.updatedAt}
        data-testid="activation-state"
      >
        <dt>Mode</dt>
        <dd>{document.contentMode ?? 'markdown'}</dd>
      </dl>
      <section className="mt-3 h-80 overflow-hidden border border-border" data-testid="activation-surface">
        <Surface
          collaboration={null}
          document={document}
          onChange={(contentMarkdown) => {
            setBodyChangeCount((count) => count + 1)
            dispatch({
              contentMarkdown,
              documentId: document.id,
              type: 'updateEditorDocumentMarkdown',
            })
          }}
          onEditorReady={noOp}
        />
      </section>
    </main>
  )
}

const participants = [
  { color: '#2563EB', id: 'ada', name: 'Ada Lovelace' },
  { color: '#047857', id: 'lin', name: 'Lin Chen' },
  { color: '#B45309', id: 'max', name: 'Max Weber' },
  { color: '#7C3AED', id: 'zoe', name: 'Zoe Smith' },
]

function TopBarFixture() {
  const model = buildEditorCollaborationStatusModel({
    access: 'edit',
    active: true,
    canEdit: true,
    connectionStatus: 'connected',
    durabilityStatus: 'saved',
    participants,
    projectionUpdatedAt: '2026-07-15T10:02:00.000Z',
    synced: true,
  })
  return (
    <main className="w-screen bg-background">
      <EditorTopBarLayout
        actions={<EditorCollaborationStatus collaborationExpected model={model} variant="topbar" />}
        leading={(
          <span className="t-list-regular min-w-0 truncate" data-testid="long-title">
            A deliberately long collaboration document title that must remain inside its narrow track.md
          </span>
        )}
        toolbar={<span className="t-meta-sm whitespace-nowrap">Review</span>}
      />
    </main>
  )
}

function StatusHysteresisFixture() {
  // When the oldest unconfirmed change was made; null while none waits.
  const [unconfirmedSince, setUnconfirmedSince] = useState<number | null>(null)
  const model = buildEditorCollaborationStatusModel({
    access: 'edit',
    active: true,
    canEdit: true,
    connectionStatus: 'connected',
    durabilityStatus: unconfirmedSince === null ? 'saved' : 'pending',
    participants: [],
    synced: true,
    unconfirmedSince,
  })
  const pulse = (durationMs: number) => {
    setUnconfirmedSince(Date.now())
    window.setTimeout(() => setUnconfirmedSince(null), durationMs)
  }
  // A writer typing for five seconds whose every change is confirmed after
  // 300 ms: something is unconfirmed all the time, never for long.
  const type = () => {
    const startedAt = Date.now()
    setUnconfirmedSince(startedAt)
    const timer = window.setInterval(() => {
      const now = Date.now()
      if (now - startedAt >= 5_000) {
        window.clearInterval(timer)
        setUnconfirmedSince(null)
        return
      }
      setUnconfirmedSince(now - 300)
    }, 100)
  }
  return (
    <main className="space-y-3 p-4">
      <div className="w-64">
        <EditorCollaborationStatus collaborationExpected model={model} variant="topbar" />
      </div>
      <button data-testid="quick-save-pulse" onClick={() => pulse(100)} type="button">
        Quick pulse
      </button>
      <button data-testid="slow-save-pulse" onClick={() => pulse(2_600)} type="button">
        Slow pulse
      </button>
      <button data-testid="typing-save-pulses" onClick={type} type="button">
        Typing
      </button>
    </main>
  )
}

const compoundChange: InspectorChange = {
  author: { color: '#2563EB', id: 'test-user', name: 'Test User' },
  createdAt: 10,
  id: 'patch-compound',
  originalText: 'old',
  position: 1,
  proposedText: 'new',
  suggestionIds: ['delete-active', 'insert-active'],
  type: 'replacement',
}

const inactiveDeletionChange: InspectorChange = {
  ...compoundChange,
  id: 'patch-inactive',
  originalText: 'hidden',
  proposedText: '',
  suggestionIds: ['delete-inactive'],
  type: 'deletion',
}

function reviewPolicy(
  writeMode: EditorWriteMode,
  overrides: Partial<CollaborationEditorPolicyInput> = {},
): CollaborationEditorPolicyInput {
  return {
    changesView: 'open',
    collaboration: true,
    display: 'simple',
    documentId: 'policy-document',
    inspectorTab: 'changes',
    selectedChangeId: null,
    visibleChanges: [],
    writeAuthorId: 'test-user',
    writeMode,
    ...overrides,
  }
}

function insertAtDocumentEnd(editor: NonNullable<ReturnType<typeof useEditor>>, text: string) {
  editor.view.dispatch(editor.state.tr.insertText(text, editor.state.doc.content.size - 1))
}

function InitialSuggestPolicyFixture() {
  const initialSuggestEditor = useEditor({
    content: '<p>Initial</p>',
    extensions: createEditorExtensions({
      collaborationReview: collaborationEditorPolicyUpdate(reviewPolicy('suggest')),
    }),
    immediatelyRender: false,
    onCreate: ({ editor }) => {
      insertAtDocumentEnd(editor, ' immediate')
    },
  })

  return (
    <main className="h-screen bg-background p-4">
      <section data-testid="initial-suggest-editor">
        <EditorContent editor={initialSuggestEditor} />
      </section>
    </main>
  )
}

function ModeSwitchPolicyFixture() {
  const modeSwitchEditor = useEditor({
    content: '<p>Switch</p>',
    extensions: createEditorExtensions({
      collaborationReview: collaborationEditorPolicyUpdate(reviewPolicy('edit')),
    }),
    immediatelyRender: false,
  })

  const switchToSuggestAndType = () => {
    if (!modeSwitchEditor) return
    applyCollaborationEditorPolicy(modeSwitchEditor, reviewPolicy('suggest'))
    insertAtDocumentEnd(modeSwitchEditor, ' switched')
  }

  return (
    <main className="h-screen bg-background p-4">
      <section data-testid="mode-switch-editor">
        <button data-testid="switch-to-suggest" onClick={switchToSuggestAndType}>Suggest</button>
        <EditorContent editor={modeSwitchEditor} />
      </section>
    </main>
  )
}

function SuggestionUndoPolicyFixture() {
  const [undoAttempts, setUndoAttempts] = useState(0)
  const [undoPatchId, setUndoPatchId] = useState<string | null>(null)
  const [updateCount, setUpdateCount] = useState(0)
  const document = useMemo(() => prosemirrorJSONToYDoc(
    getSchema(createEditorExtensions()),
    {
      content: [{
        attrs: { textAlign: null },
        content: [{ text: 'Before', type: 'text' }],
        type: 'paragraph',
      }],
      type: 'doc',
    },
    EDITOR_YJS_FRAGMENT,
  ), [])
  const editor = useEditor({
    extensions: [
      ...createEditorExtensions({
        collaborationReview: collaborationEditorPolicyUpdate(reviewPolicy('suggest')),
        onCollaborationSuggestionUndo: async (patchId) => {
          setUndoPatchId(patchId)
          setUndoAttempts((count) => count + 1)
          throw new Error('Fixture rejects the durable Undo decision.')
        },
      }).map((extension) => (
        extension.name === 'starterKit'
          ? extension.configure({ undoRedo: false })
          : extension
      )),
      Collaboration.configure({
        document,
        field: EDITOR_YJS_FRAGMENT,
      }),
    ],
    immediatelyRender: false,
  })

  useEffect(() => {
    const handleUpdate = () => setUpdateCount((count) => count + 1)
    document.on('update', handleUpdate)
    return () => document.off('update', handleUpdate)
  }, [document])

  return (
    <main className="h-screen bg-background p-4">
      <dl
        data-testid="suggestion-undo-state"
        data-undo-attempts={undoAttempts}
        data-undo-patch-id={undoPatchId ?? 'none'}
        data-update-count={updateCount}
      >
        <dt>Undo patch</dt>
        <dd>{undoPatchId ?? 'none'}</dd>
      </dl>
      <section className="min-h-80 border border-border p-4" data-testid="suggestion-undo-editor">
        <EditorContent editor={editor} />
      </section>
    </main>
  )
}

function ViewPolicyFixture() {
  const viewEditor = useEditor({
    content: '<p>View</p>',
    extensions: createEditorExtensions({
      collaborationReview: collaborationEditorPolicyUpdate(reviewPolicy('view')),
    }),
    immediatelyRender: false,
    onCreate: ({ editor }) => insertAtDocumentEnd(editor, ' blocked'),
  })

  return (
    <main className="h-screen bg-background p-4">
      <section data-testid="initial-view-editor">
        <EditorContent editor={viewEditor} />
      </section>
    </main>
  )
}

function SimpleMarkupFixture() {
  const markupEditor = useEditor({
    content: {
      content: [{
        content: [
          {
            marks: [{
              attrs: {
                authorId: 'test-user',
                createdAt: 10,
                id: 'delete-active',
                kind: 'deletion',
                patchId: 'patch-compound',
                suggestionId: 'delete-active',
              },
              type: 'deletion',
            }],
            text: 'old',
            type: 'text',
          },
          {
            marks: [{
              attrs: {
                authorId: 'test-user',
                createdAt: 10,
                id: 'insert-active',
                kind: 'insertion',
                patchId: 'patch-compound',
                suggestionId: 'insert-active',
              },
              type: 'insertion',
            }],
            text: 'new',
            type: 'text',
          },
          {
            marks: [{
              attrs: {
                authorId: 'test-user',
                createdAt: 11,
                id: 'delete-inactive',
                kind: 'deletion',
                patchId: 'patch-inactive',
                suggestionId: 'delete-inactive',
              },
              type: 'deletion',
            }],
            text: 'hidden',
            type: 'text',
          },
        ],
        type: 'paragraph',
      }],
      type: 'doc',
    },
    editable: false,
    extensions: createEditorExtensions({
      collaborationReview: collaborationEditorPolicyUpdate(reviewPolicy('suggest', {
        selectedChangeId: compoundChange.id,
        visibleChanges: [compoundChange, inactiveDeletionChange],
      })),
    }),
    immediatelyRender: false,
  })
  const finalMarkdown = markupEditor
    ? serializeEditorFinalProjectionMarkdown(markupEditor)
    : ''

  return (
    <main className="h-screen bg-background p-4">
      <section data-testid="simple-markup-editor">
        <EditorContent editor={markupEditor} />
      </section>
      <pre aria-label="Canonical Source" data-testid="canonical-source">{finalMarkdown}</pre>
      <section data-testid="canonical-diff">
        <DocumentDiffView
          anchorMarkdown="old"
          copy={editorCopy.en}
          currentMarkdown={finalMarkdown}
        />
      </section>
    </main>
  )
}

function SlashCollaborationFixture() {
  const searchParams = new URLSearchParams(window.location.search)
  const slashWriteMode = searchParams.get('mode') === 'suggest'
    ? 'suggest'
    : 'edit'
  const slashSourceKind = searchParams.get('source') === 'heading'
    ? 'heading'
    : 'paragraph'
  const document = useMemo(() => {
    const initialDocument = prosemirrorJSONToYDoc(
      getSchema(createEditorExtensions()),
      {
        content: slashWriteMode === 'suggest'
          ? [
              {
                attrs: { level: 1, textAlign: null },
                content: [{ text: 'Slash Suggest fixture', type: 'text' }],
                type: 'heading',
              },
              {
                attrs: slashSourceKind === 'heading'
                  ? { level: 2, textAlign: null }
                  : { textAlign: null },
                content: [{ text: 'Suggest target', type: 'text' }],
                type: slashSourceKind,
              },
            ]
          : [{
              attrs: { textAlign: null },
              content: [{ text: 'Before', type: 'text' }],
              type: 'paragraph',
            }],
        type: 'doc',
      },
      EDITOR_YJS_FRAGMENT,
    )
    if (slashWriteMode !== 'suggest') return initialDocument
    const synchronizedDocument = new Y.Doc()
    Y.applyUpdate(
      synchronizedDocument,
      Y.encodeStateAsUpdate(initialDocument),
    )
    window.__slashCollaborationServerUpdate = Array.from(
      Y.encodeStateAsUpdate(synchronizedDocument),
    )
    initialDocument.destroy()
    return synchronizedDocument
  }, [slashSourceKind, slashWriteMode])
  const editor = useEditor({
    extensions: [
      ...createEditorExtensions({
        collaborationReview: collaborationEditorPolicyUpdate(
          reviewPolicy('edit'),
        ),
        slash: {
          labels: {
            closeHint: editorCopy.de.slashClose,
            empty: editorCopy.de.slashEmpty,
            groupInsert: editorCopy.de.slashGroupInsert,
            groupStyle: editorCopy.de.slashGroupStyle,
            heading1: editorCopy.de.slashHeading1,
            heading2: editorCopy.de.slashHeading2,
            heading3: editorCopy.de.slashHeading3,
            navHint: editorCopy.de.slashNav,
            selectHint: editorCopy.de.slashSelect,
            text: editorCopy.de.slashText,
            title: editorCopy.de.slashTitle,
            bulletList: editorCopy.de.slashBulletList,
            orderedList: editorCopy.de.slashOrderedList,
            taskList: editorCopy.de.slashTaskList,
            blockquote: editorCopy.de.slashBlockquote,
            codeBlock: editorCopy.de.slashCodeBlock,
            table: editorCopy.de.slashTable,
            divider: editorCopy.de.slashDivider,
            suggestUnavailable: editorCopy.de.slashSuggestUnavailable,
          },
        },
      }).map((extension) => (
        extension.name === 'starterKit'
          ? extension.configure({ undoRedo: false })
          : extension
      )),
      Collaboration.configure({
        document,
        field: EDITOR_YJS_FRAGMENT,
      }),
    ],
    immediatelyRender: false,
  })

  useLayoutEffect(() => {
    if (!editor || slashWriteMode !== 'suggest') return
    applyCollaborationEditorPolicy(editor, reviewPolicy('suggest', {
      writeAuthorId: '11111111-1111-4111-8111-111111111111',
    }))
  }, [editor, slashWriteMode])

  useEffect(() => {
    const publish = (update?: Uint8Array) => {
      window.__slashCollaborationUpdate = Array.from(Y.encodeStateAsUpdate(document))
      if (update) {
        const updates = window.__slashCollaborationUpdates ?? []
        updates.push(Array.from(update))
        window.__slashCollaborationUpdates = updates
        const detections = window.__slashStructureUpdateDetections ?? []
        detections.push(containsStructureSuggestionAttribute(update))
        window.__slashStructureUpdateDetections = detections
        const suggestionBoundaries = window.__slashSuggestionBoundaryDetections ?? []
        suggestionBoundaries.push(containsSuggestionBoundary(update))
        window.__slashSuggestionBoundaryDetections = suggestionBoundaries
      }
    }
    window.__slashCollaborationUpdates = []
    window.__slashSuggestionBoundaryDetections = []
    window.__slashStructureUpdateDetections = []
    publish()
    document.on('update', publish)
    return () => document.off('update', publish)
  }, [document])

  return (
    <main className="h-screen bg-background p-4">
      <section className="min-h-80 border border-border p-4" data-testid="slash-collaboration-editor">
        <EditorContent editor={editor} />
      </section>
    </main>
  )
}

const COMMENT_FIXTURE_THREADS: EditorCollaborationCommentThread[] = Array.from(
  { length: 60 },
  (_, index) => {
    const revision = index + 1
    const createdAt = 1_784_390_000 + index * 60
    const author = {
      id: `user-${index % 6}`,
      kind: 'user' as const,
      name: `Reviewer ${index % 6 + 1}`,
    }
    return {
      anchor: { quote: `Passage ${revision}` },
      author,
      can_delete: true,
      can_resolve: true,
      created_at: createdAt,
      deleted_at: null,
      document_id: 'comment-scale-document',
      generation: 1,
      id: `thread-${String(revision).padStart(2, '0')}`,
      messages: Array.from({ length: 3 }, (_, messageIndex) => ({
        author: messageIndex === 0
          ? author
          : {
              id: `user-${(index + messageIndex) % 6}`,
              kind: 'user' as const,
              name: `Reviewer ${(index + messageIndex) % 6 + 1}`,
            },
        body_markdown: messageIndex === 1 && index % 17 === 0
          ? null
          : `Review note ${revision}.${messageIndex + 1} with a deliberately long explanation that verifies compact wrapping without horizontal overflow in an enterprise inspector.`,
        can_delete: messageIndex > 0,
        can_edit: messageIndex > 0,
        created_at: createdAt + messageIndex * 10,
        deleted_at: messageIndex === 1 && index % 17 === 0
          ? createdAt + 30
          : null,
        edited_at: messageIndex === 2 && index % 11 === 0
          ? createdAt + 40
          : null,
        id: `message-${revision}-${messageIndex + 1}`,
        mentions: messageIndex === 2 && index % 7 === 0
          ? [{ id: 'user-1', kind: 'user' as const, name: 'Reviewer 2' }]
          : [],
        revision,
      })),
      quote: `Selected source passage ${revision} with enough text to exercise the compact quote preview.`,
      resolved_at: index >= 55 ? createdAt + 120 : null,
      resolved_by: index >= 55 ? author : null,
      revision,
      status: index >= 55 ? 'resolved' : 'open',
      updated_at: createdAt + 20,
    }
  },
)

function CommentScaleFixture() {
  const [loadedCount, setLoadedCount] = useState(50)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const threads = COMMENT_FIXTURE_THREADS.slice(0, loadedCount)
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      window.__commentScaleFirstInteractiveMs = (
        performance.now() - fixtureModuleStartedAt
      )
    })
    return () => cancelAnimationFrame(frame)
  }, [])
  useEffect(() => {
    if (!selectedThreadId || window.__commentScaleSelectionStartedAt === undefined) return
    const frame = requestAnimationFrame(() => {
      window.__commentScaleSelectionMs = (
        performance.now() - window.__commentScaleSelectionStartedAt!
      )
      delete window.__commentScaleSelectionStartedAt
    })
    return () => cancelAnimationFrame(frame)
  }, [selectedThreadId])
  const comments = useMemo<CollaborationCommentsHandle>(() => ({
    createThread: async () => COMMENT_FIXTURE_THREADS[0]!,
    deleteMessage: noOpAsync,
    deleteThread: noOpAsync,
    drafts,
    editMessage: noOpAsync,
    error: null,
    hasMore: loadedCount < COMMENT_FIXTURE_THREADS.length,
    isLoading: false,
    isLoadingMore,
    lastReadRevision: 42,
    loadMore: async () => {
      setIsLoadingMore(true)
      await Promise.resolve()
      setLoadedCount((current) => Math.min(current + 50, 60))
      setIsLoadingMore(false)
    },
    markRead: noOpAsync,
    mentionEventVersion: 0,
    participants: Array.from({ length: 6 }, (_, index) => ({
      id: `user-${index}`,
      kind: 'user' as const,
      name: `Reviewer ${index + 1}`,
    })),
    pendingIds: new Set<string>(),
    reply: noOpAsync,
    revision: 60,
    setDraft: (key, value) => setDrafts((current) => ({
      ...current,
      [key]: value,
    })),
    setStatus: noOpAsync,
    threads,
    unreadCount: threads.filter((thread) => thread.revision > 42).length,
  }), [drafts, isLoadingMore, loadedCount, threads])
  const orphanedThreadIds = useMemo(
    () => new Set(
      COMMENT_FIXTURE_THREADS
        .filter((_, index) => index % 13 === 0)
        .map((thread) => thread.id),
    ),
    [],
  )
  const positionByThreadId = useMemo(
    () => new Map(
      COMMENT_FIXTURE_THREADS.map((thread, index) => [thread.id, index * 10]),
    ),
    [],
  )

  return (
    <main className="h-screen min-w-0 bg-background">
      <aside
        className="h-full w-full min-w-0 overflow-hidden border-l border-border sm:ml-auto sm:w-[400px]"
        data-testid="comment-scale-panel"
      >
        <TeamCommentsPanel
          assistantAvailable={false}
          canComment
          comments={comments}
          currentUserId="user-1"
          onSelectThread={(threadId) => {
            window.__commentScaleSelectionStartedAt = performance.now()
            setSelectedThreadId(threadId)
          }}
          onUseWithAssistant={noOp}
          orphanedThreadIds={orphanedThreadIds}
          positionByThreadId={positionByThreadId}
          selectedThreadId={selectedThreadId}
        />
      </aside>
    </main>
  )
}

function ViewOnlyAiFixture() {
  const composerRef = useRef<MentionComposerHandle | null>(null)
  const [access, setAccess] = useState<'edit' | 'view'>('edit')
  const [anchorInvocations, setAnchorInvocations] = useState(0)
  const [decisionInvocations, setDecisionInvocations] = useState(0)
  const [invocations, setInvocations] = useState(0)
  const [publishInvocations, setPublishInvocations] = useState(0)
  const document: EditorDocumentRecord = {
    ...collaborationDocument('private-ai-document', 4),
    access: { mode: 'owner', permission: 'edit' },
  }
  const collaboration = {
    access,
    canEdit: access === 'edit',
    documentId: document.id,
    generation: document.collaboration?.generation ?? null,
    readAuthority: () => ({
      access,
      blockingFailure: null,
      canEdit: access === 'edit',
      connectionStatus: access === 'edit' ? 'connected' as const : 'read_only' as const,
      documentId: document.id,
      generation: document.collaboration?.generation ?? null,
      lifecycleStatus: access === 'edit' ? 'saved' as const : 'read_only' as const,
      revision: access === 'edit' ? 0 : 1,
      synced: true,
    }),
  } as ReturnType<typeof useCollaborationDocument>
  const reason = editorAiReadOnlyReason(document, collaboration.access, 'en')
  const publishDisabledReason = privateSuggestionPublishDisabledReason(
    document,
    collaboration,
    'en',
  )
  const collaborationStatus = buildEditorCollaborationStatusModel({
    access,
    active: true,
    canEdit: access === 'edit',
    connectionStatus: access === 'edit' ? 'connected' : 'read_only',
    durabilityStatus: 'saved',
    participants: [],
    synced: true,
  })
  const suggestion: EditorSuggestionRecord = {
    anchor: {
      from: 1,
      quoteAfter: '',
      quoteBefore: '',
      selectedText: 'old',
      to: 4,
    },
    blockId: 'paragraph-1',
    createdAt: document.createdAt,
    documentId: document.id,
    groupId: 'private-ai-group',
    id: 'private-ai-suggestion',
    originalText: 'old',
    origin: { kind: 'global_run' },
    proposedText: 'new',
    status: 'pending',
    updatedAt: document.updatedAt,
  }
  return (
    <main className="h-screen bg-background">
      <button data-testid="downgrade-ai-access" onClick={() => setAccess('view')}>Downgrade</button>
      <output data-testid="anchor-invocations">{anchorInvocations}</output>
      <output data-testid="decision-invocations">{decisionInvocations}</output>
      <output data-testid="ai-invocations">{invocations}</output>
      <output data-testid="publish-invocations">{publishInvocations}</output>
      <EditorTopBar
        canFlushForShare={false}
        collaborationAccess={access}
        collaborationActive
        collaborationCanEdit={access === 'edit'}
        collaborationStatus={collaborationStatus}
        copy={editorCopy.en}
        diffAnchorDisabledReason={publishDisabledReason}
        diffAnchorError={null}
        diffAnchorPending={false}
        dispatch={noOp}
        document={document}
        editor={null}
        isCommentPanelVisible={false}
        isDiffVisible={false}
        isDirty={false}
        isTreeVisible={false}
        onExportWordMarkdown={async () => document.contentMarkdown}
        onSetDiffAnchor={() => setAnchorInvocations((count) => count + 1)}
        onShareDocument={noOp}
        onWriteModeChange={noOp}
        sharingAvailable={false}
        viewMode="live"
        writeMode={access === 'edit' ? 'edit' : 'view'}
      />
      <section data-testid="private-suggestion-controls">
        <EditorDocumentChangesSection
          labels={{
            accept: 'Accept',
            acceptAll: 'Accept all',
            documentChanges: 'Document changes',
            proposedChange: 'Proposed change',
            reject: 'Reject',
            rejectAll: 'Reject all',
            stale: 'The passage changed. Please regenerate the suggestion.',
          }}
          onAcceptGroup={() => setPublishInvocations((count) => count + 1)}
          onAcceptSuggestion={() => setPublishInvocations((count) => count + 1)}
          onRejectGroup={noOp}
          onRejectSuggestion={noOp}
          onSelectSuggestion={noOp}
          publishDisabledReason={publishDisabledReason}
          suggestionErrors={{}}
          suggestions={[suggestion]}
        />
      </section>
      <section className="h-96" data-testid="batch-inspector">
        <EditorInspector
          activeTab="changes"
          assistant={<div />}
          canDecide={access === 'edit'}
          changes={[compoundChange]}
          changesError={null}
          changesView="open"
          collaborationActive
          collaborationStatus={collaborationStatus}
          commentCount={0}
          comments={<div />}
          commentUnreadCount={0}
          decisionError={null}
          display="simple"
          history={[]}
          historyError={null}
          historyFilters={{ actorId: null, type: null }}
          historyLoading={false}
          isDecisionPending={false}
          onActiveTabChange={noOp}
          onChangesViewChange={noOp}
          onClose={noOp}
          onDecision={() => setDecisionInvocations((count) => count + 1)}
          onDisplayChange={noOp}
          onHistoryFiltersChange={noOp}
          onOpenFiltersChange={noOp}
          onSelectedChangeIdChange={noOp}
          openFilters={{ authorId: null, type: null }}
          selectedChangeId={null}
        />
      </section>
      <EditorAssistantComposer
        aiReadOnlyReason={reason}
        attachedCommentIds={[]}
        attachmentChips={[]}
        chatModelOptions={[]}
        chatModelOptionsStatus="missing"
        comments={[]}
        composerRef={composerRef}
        copy={editorCopy.en}
        defaultChatModel={null}
        dispatch={noOp}
        draft="Rewrite this paragraph"
        editorContextBase={{ conversation: 0, documents: 0, reports: 0, rules: 0 }}
        editorContextCapacity={{ contextWindowTokens: null, reservedOutputTokens: 0 }}
        fileGroupOptions={[]}
        fileOptions={[]}
        handedOverDiscussions={[]}
        instructionFeedback={null}
        isAttachActive={false}
        isRunning={false}
        isVisible
        isWideCanvas={false}
        onAttachFiles={noOp}
        onAttachRule={noOp}
        onDismissInstructionFeedback={noOp}
        onRefsChange={noOp}
        onRemoveAttachedComment={noOp}
        onRemoveChip={noOp}
        onRemoveHandedOverDiscussion={noOp}
        onReorderPending={noOp}
        onReorderPill={noOp}
        onSend={() => setInvocations((count) => count + 1)}
        onStop={noOp}
        onToggleAttach={noOp}
        pendingKeys={[]}
        pillKeys={[]}
        replaceableSuggestionCount={1}
        reportOptions={[]}
        ruleOptions={[]}
        selectedEffort={null}
        selectedModel={null}
        selectedModelTier={null}
        textImprovement={textImprovement}
      />
    </main>
  )
}

function ModalFocusRaceFixture() {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLElement | null>(null)
  const initialFocusRef = useRef<HTMLInputElement | null>(null)
  useModalFocusTrap({
    initialFocusRef,
    onClose: () => setOpen(false),
    open,
    panelRef,
  })
  useEffect(() => {
    if (open) triggerRef.current?.focus()
  }, [open])
  return (
    <main className="h-screen bg-background p-8">
      <button
        data-testid="modal-launcher"
        onClick={() => setOpen(true)}
        ref={triggerRef}
        type="button"
      >
        Open modal
      </button>
      {open ? (
        <section
          aria-label="Focus race dialog"
          aria-modal="true"
          ref={panelRef}
          role="dialog"
          tabIndex={-1}
        >
          <input aria-label="Preferred modal control" ref={initialFocusRef} />
          <button onClick={() => setOpen(false)} type="button">Close</button>
        </section>
      ) : null}
    </main>
  )
}

function ShareMenuFocusFixture() {
  const [open, setOpen] = useState(false)
  const [returnFocusTarget, setReturnFocusTarget] = useState<HTMLElement | null>(null)
  const document = useMemo(() => collaborationDocument('share-menu', 1), [])
  const collaborationStatus = buildEditorCollaborationStatusModel({
    access: 'edit',
    active: true,
    canEdit: true,
    connectionStatus: 'connected',
    durabilityStatus: 'saved',
    participants: [],
    synced: true,
  })
  return (
    <main className="h-screen bg-background">
      <EditorTopBar
        canFlushForShare
        collaborationAccess="edit"
        collaborationActive
        collaborationCanEdit
        collaborationStatus={collaborationStatus}
        copy={editorCopy.en}
        diffAnchorDisabledReason={null}
        diffAnchorError={null}
        diffAnchorPending={false}
        dispatch={noOp}
        document={document}
        editor={null}
        isCommentPanelVisible={false}
        isDiffVisible={false}
        isDirty={false}
        isTreeVisible={false}
        onExportWordMarkdown={async () => document.contentMarkdown}
        onSetDiffAnchor={noOp}
        onShareDocument={(_document, target) => {
          setReturnFocusTarget(target ?? null)
          setOpen(true)
        }}
        onWriteModeChange={noOp}
        sharingAvailable
        viewMode="live"
        writeMode="edit"
      />
      {open ? (
        <ShareDialog
          demo
          initialTab="access"
          onClose={() => setOpen(false)}
          ownerEmail="owner@example.de"
          ownerName="Owner"
          refreshToken={0}
          resourceId={document.id}
          resourceTitle={document.title}
          resourceType="editor_document"
          returnFocusTarget={returnFocusTarget}
        />
      ) : null}
    </main>
  )
}

function ExplorerActionModalFixture() {
  const [open, setOpen] = useState(false)
  const [pinCount, setPinCount] = useState(0)
  const [returnFocusTarget, setReturnFocusTarget] = useState<HTMLElement | null>(null)
  const title = 'Geteiltes Dokument.md'
  const detailsLabel = 'Dokumentdetails'

  return (
    <main className="h-screen bg-background p-8">
      <div className="w-72 rounded-lg border border-border bg-surface p-2">
        <ExplorerHistoryRow
          actions={[{
            ariaLabel: `${detailsLabel}: ${title}`,
            icon: <Info className="icon-sm" />,
            label: detailsLabel,
            onSelect: () => {
              const activeElement = window.document.activeElement
              setReturnFocusTarget(activeElement instanceof HTMLElement ? activeElement : null)
              setOpen(true)
            },
          }]}
          onSelect={noOp}
          timeLabel="gerade eben"
          title={title}
        />
        <ExplorerHistoryRow
          actions={[{
            ariaLabel: 'Anheften: Lokales Dokument.md',
            icon: <span aria-hidden="true">P</span>,
            label: 'Anheften',
            onSelect: () => setPinCount((count) => count + 1),
          }]}
          onSelect={noOp}
          timeLabel="1 Minute"
          title="Lokales Dokument.md"
        />
        <output data-testid="explorer-pin-count">{pinCount}</output>
      </div>
      {open ? (
        <ShareDialog
          demo
          documentDetails={{
            createdAt: '2026-08-03T17:30:14.000Z',
            openCommentCount: 1,
            openSuggestionCount: 1,
            participantCount: 2,
            updatedAt: '2026-08-03T17:30:14.000Z',
            wordCount: 42,
          }}
          initialTab="overview"
          onClose={() => setOpen(false)}
          ownerEmail="owner@example.invalid"
          ownerName="Fixture Owner"
          refreshToken={0}
          resourceId="shared-document"
          resourceTitle={title}
          resourceType="editor_document"
          returnFocusTarget={returnFocusTarget}
        />
      ) : null}
    </main>
  )
}

const LONG_COLLABORATOR_NAME = (
  'Dr. Alexandra-Maria von Beispielhausen 🧪 · International Collaboration Reviewer'
)

const presenceLabelSamples = [
  { color: '#2563eb', kind: 'current', side: 'left', top: 72 },
  { color: '#7c3aed', kind: 'current', side: 'right', top: 152 },
  { color: '#0f766e', kind: 'legacy', side: 'left', top: 232 },
  { color: '#be123c', kind: 'legacy', side: 'right', top: 312 },
] as const

function legacyCollaborationCaret(name: string, color: string): HTMLElement {
  const caret = document.createElement('span')
  caret.className = 'collaboration-carets__caret'
  caret.style.borderColor = color

  const label = document.createElement('span')
  label.className = 'collaboration-carets__label'
  label.style.backgroundColor = color
  label.textContent = name
  caret.append(label)
  return caret
}

const PresenceLabelFixtureExtension = Extension.create({
  name: 'presenceLabelFixture',
  addProseMirrorPlugins() {
    return [
      new Plugin({
        props: {
          decorations(state) {
            const position = Math.min(1, state.doc.content.size)
            const widgets = presenceLabelSamples.map((sample) => {
              const sampleId = `${sample.kind}-${sample.side}`
              return Decoration.widget(position, () => {
                const anchor = document.createElement('span')
                anchor.contentEditable = 'false'
                anchor.dataset.presenceAnchor = sampleId
                anchor.style.position = 'absolute'
                anchor.style.top = `${sample.top}px`
                if (sample.side === 'left') anchor.style.left = '4px'
                else anchor.style.right = '4px'

                const name = `${LONG_COLLABORATOR_NAME} · ${sample.kind} ${sample.side}`
                const caret = sample.kind === 'current'
                  ? renderCollaborationCaret({ color: sample.color, name })
                  : legacyCollaborationCaret(name, sample.color)
                caret.dataset.presenceSample = sampleId
                anchor.append(caret)
                return anchor
              }, { key: `presence-label-${sampleId}`, side: -1 })
            })
            return DecorationSet.create(state.doc, widgets)
          },
        },
      }),
    ]
  },
})

function PresenceLabelFixture() {
  const editor = useEditor({
    content: [
      '<p>Remote identities must remain readable without covering the active paragraph.</p>',
      '<p>The same presentation contract applies at either horizontal edge.</p>',
      '<p>Unicode names and emoji are ordinary collaboration identities.</p>',
    ].join(''),
    editorProps: {
      attributes: {
        class: 'relative min-h-[28rem] px-3 py-12 outline-none',
        'data-testid': 'presence-label-editor',
      },
    },
    extensions: [...createEditorExtensions(), PresenceLabelFixtureExtension],
    immediatelyRender: true,
  })

  return (
    <main className="min-h-screen overflow-x-hidden bg-background p-3 text-foreground">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h1 className="t-title">Collaboration presence</h1>
        <output
          className="max-w-full rounded-full border border-border bg-surface px-2 py-1 t-meta"
          data-testid="presence-demo-state"
        >
          Demo-Modus eingeschaltet
        </output>
      </header>
      <section
        className="overflow-visible rounded-lg border border-border bg-surface"
        data-labels-mounted={editor ? 'true' : 'false'}
        data-testid="presence-label-boundary"
      >
        <EditorContent editor={editor} />
      </section>
    </main>
  )
}

function ProfileMenuFixture() {
  const [logoutCount, setLogoutCount] = useState(0)
  return (
    <main className="relative h-screen bg-background">
      <output data-testid="profile-logout-count">{logoutCount}</output>
      <div className="absolute bottom-2 left-2">
        <ProfileAvatar
          authMode="local"
          onLogin={noOp}
          onLogout={() => setLogoutCount((count) => count + 1)}
          onOpenSecuritySettings={noOp}
          session={{
            projectNamespace: 'ws_browser_fixture',
            status: 'authenticated',
            user: {
              displayName: 'Fixture Owner',
              email: 'owner@example.invalid',
              id: 'fixture-owner',
              role: 'admin',
            },
          }}
        />
      </div>
    </main>
  )
}

const RECOVERY_ORIGINAL_ID = 'recovery-original'

function recoveryCopyState(): ProjectState {
  const base = createEmptyProjectState()
  const document: EditorDocumentRecord = {
    access: { mode: 'owner', permission: 'edit' },
    contentMarkdown: '# Plan Q3\n\nBestätigter Stand.',
    createdAt: '2026-09-28T08:00:00.000Z',
    folderId: null,
    id: RECOVERY_ORIGINAL_ID,
    revision: 3,
    serverSynced: true,
    source: 'blank',
    title: 'Plan Q3',
    updatedAt: '2026-09-28T08:00:00.000Z',
  }
  return {
    ...base,
    dirty: false,
    editorDocumentOrder: [document.id],
    editorDocuments: { [document.id]: document },
    editorUi: {
      ...base.editorUi,
      activeDocumentId: document.id,
      openDocumentIds: [document.id],
    },
  }
}

/** A document whose collaboration generation was replaced while the
 * service could not confirm its local state: the lifecycle registry hands
 * that state to the workspace's recovery store, and the document says where
 * it went through the editor workspace's own recovery notice. */
function RecoveryCopyNoticeFixture() {
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, recoveryCopyState)
  const documentsRef = useRef(state.editorDocuments)
  useEffect(() => {
    documentsRef.current = state.editorDocuments
  }, [state.editorDocuments])
  const active = selectedEditorDocument(state)
  const capture = () => {
    const local = prosemirrorJSONToYDoc(
      getSchema(createEditorExtensions()),
      {
        content: [{
          attrs: { textAlign: null },
          content: [{ text: 'Unbestätigter lokaler Satz.', type: 'text' }],
          type: 'paragraph',
        }],
        type: 'doc',
      },
      EDITOR_YJS_FRAGMENT,
    )
    try {
      workspaceRecoveryStore({
        dispatch,
        documents: () => documentsRef.current,
        workspaceId: 'browser-fixture',
      })({ document: local, documentId: RECOVERY_ORIGINAL_ID, generation: 2, scope: 'browser-fixture' })
    } finally {
      local.destroy()
    }
  }
  return (
    <main className="h-screen w-[360px] bg-background" data-testid="recovery-copy-fixture">
      <button data-testid="capture-recovery" onClick={capture} type="button">
        Capture
      </button>
      <button
        data-testid="open-original"
        onClick={() => dispatch({ documentId: RECOVERY_ORIGINAL_ID, type: 'openEditorDocument' })}
        type="button"
      >
        Open original
      </button>
      <p data-document-id={active?.id ?? 'none'} data-testid="active-document">{active?.title}</p>
      {active ? (
        <EditorDocumentRecoveryNotice
          dispatch={dispatch}
          document={active}
          documents={state.editorDocuments}
          locale="de"
        />
      ) : null}
      <pre className="t-meta-sm whitespace-pre-wrap" data-testid="active-markdown">
        {active?.contentMarkdown}
      </pre>
    </main>
  )
}

const ANSWER_DOCUMENT_A = 'answer-document-a'
const ANSWER_DOCUMENT_B = 'answer-document-b'

function answerDocument(id: string, title: string, contentMarkdown: string): EditorDocumentRecord {
  return {
    access: { mode: 'owner', permission: 'edit' },
    contentMarkdown,
    createdAt: '2026-07-15T08:00:00.000Z',
    folderId: null,
    id,
    metadataRevision: 1,
    revision: 1,
    source: 'blank',
    title,
    updatedAt: '2026-07-15T08:00:00.000Z',
  }
}

function instructionAnswerState(): ProjectState {
  const base = createEmptyProjectState()
  const documents = [
    answerDocument(
      ANSWER_DOCUMENT_A,
      'Dokument A.md',
      '# Dokument A\n\nDer Bau dauert ca. sechs Wochen.',
    ),
    answerDocument(
      ANSWER_DOCUMENT_B,
      'Dokument B.md',
      '# Dokument B\n\nGäste kommentieren dieses Dokument.',
    ),
  ]
  return {
    ...base,
    dirty: false,
    editorDocumentOrder: documents.map((document) => document.id),
    editorDocuments: Object.fromEntries(documents.map((document) => [document.id, document])),
    editorUi: {
      ...base.editorUi,
      activeDocumentId: ANSWER_DOCUMENT_A,
      openDocumentIds: [ANSWER_DOCUMENT_A],
    },
  }
}

/** The editor assistant of two Markdown documents as the editor workspace
 * wires it: one suggestion hook that stays mounted while the reducer's own
 * "open document" action switches the document, and the real composer with
 * the hook's answer above it. The page's route answers the assistant
 * request; the outputs count the suggestions each document holds. */
function InstructionAnswerSwitchFixture() {
  const composerRef = useRef<MentionComposerHandle | null>(null)
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, instructionAnswerState)
  const activeDocument = selectedEditorDocument(state)
  const collaboration = useCollaborationDocument({
    active: true,
    apiKey: undefined,
    document: activeDocument,
    workspaceId: 'browser-fixture',
  })
  const controller = useEditorSuggestions({
    activeDocument,
    activeEditor: null,
    attachedCommentIds: [],
    attachedRefs: [],
    capabilities: null,
    collaboration,
    comments: [],
    dispatch,
    handedOverDiscussions: [],
    locale: 'de',
    // A request that went out empties the composer, as in the workspace.
    onGlobalSuccess: () => composerRef.current?.clear(),
    selectedModelTier: null,
    state,
  })
  const suggestionCount =(documentId: string) => Object.values(state.editorSuggestions)
    .filter((suggestion) => suggestion.documentId === documentId)
    .length
  return (
    <main className="flex h-screen flex-col gap-3 bg-background p-4">
      <div className="flex gap-2 t-meta-sm" role="toolbar">
        <button
          onClick={() => dispatch({ documentId: ANSWER_DOCUMENT_A, type: 'openEditorDocument' })}
          type="button"
        >
          Dokument A öffnen
        </button>
        <button
          onClick={() => dispatch({ documentId: ANSWER_DOCUMENT_B, type: 'openEditorDocument' })}
          type="button"
        >
          Dokument B öffnen
        </button>
        <button
          onClick={() => {
            for (const suggestion of controller.documentSuggestions) {
              if (suggestion.status === 'pending') void controller.handleRejectSuggestion(suggestion.id)
            }
          }}
          type="button"
        >
          Offene Vorschläge ablehnen
        </button>
      </div>
      <p data-document-id={activeDocument?.id ?? 'none'} data-testid="active-document">
        {activeDocument?.title}
      </p>
      <output data-testid="suggestions-a">{suggestionCount(ANSWER_DOCUMENT_A)}</output>
      <output data-testid="suggestions-b">{suggestionCount(ANSWER_DOCUMENT_B)}</output>
      <EditorAssistantComposer
        aiReadOnlyReason={controller.aiReadOnlyReason}
        attachedCommentIds={[]}
        attachmentChips={[]}
        chatModelOptions={[]}
        chatModelOptionsStatus="missing"
        comments={[]}
        composerRef={composerRef}
        copy={editorCopy.de}
        defaultChatModel={null}
        dispatch={dispatch}
        draft={state.editorUi.assistantDraft}
        editorContextBase={{ conversation: 0, documents: 0, reports: 0, rules: 0 }}
        editorContextCapacity={{ contextWindowTokens: null, reservedOutputTokens: 0 }}
        fileGroupOptions={[]}
        fileOptions={[]}
        handedOverDiscussions={[]}
        instructionFeedback={controller.instructionFeedback}
        isAttachActive={false}
        isRunning={controller.isGlobalRunning}
        isVisible={state.editorUi.isAssistantVisible}
        isWideCanvas={false}
        onAttachFiles={noOp}
        onAttachRule={noOp}
        onDismissInstructionFeedback={controller.clearInstructionFeedback}
        onRefsChange={noOp}
        onRemoveAttachedComment={noOp}
        onRemoveChip={noOp}
        onRemoveHandedOverDiscussion={noOp}
        onReorderPending={noOp}
        onReorderPill={noOp}
        onSend={() => {
          const instruction = composerRef.current?.getInstructionText().trim() ?? ''
          void controller.handleInstructionRun(instruction)
        }}
        onStop={controller.handleStopRun}
        onToggleAttach={noOp}
        pendingKeys={[]}
        pillKeys={[]}
        replaceableSuggestionCount={controller.replaceableSuggestionCount}
        reportOptions={[]}
        ruleOptions={[]}
        selectedEffort={null}
        selectedModel={null}
        selectedModelTier={null}
        textImprovement={textImprovement}
      />
    </main>
  )
}

const numberDiffDocument: EditorDocumentRecord = {
  contentMarkdown: 'Budget 18.000 Euro für 1,5 Jahre',
  createdAt: '2026-07-15T08:00:00.000Z',
  folderId: null,
  id: 'number-diff',
  revision: 1,
  source: 'blank',
  title: 'number-diff.md',
  updatedAt: '2026-07-15T08:00:00.000Z',
}

const numberDiffSuggestion: EditorSuggestionRecord = {
  anchor: {
    from: 1,
    quoteAfter: '',
    quoteBefore: '',
    selectedText: 'Budget 18.000 Euro für 1,5 Jahre',
    to: 33,
  },
  blockId: 'paragraph-1',
  createdAt: numberDiffDocument.createdAt,
  documentId: numberDiffDocument.id,
  groupId: 'number-diff-group',
  id: 'number-diff-suggestion',
  originalText: 'Budget 18.000 Euro für 1,5 Jahre',
  origin: { kind: 'global_run' },
  proposedText: 'Budget 21.500 Euro für 2,5 Jahre',
  status: 'pending',
  updatedAt: numberDiffDocument.updatedAt,
}

function SuggestionNumberDiffFixture() {
  return (
    <main className="h-screen bg-background">
      <Surface
        document={numberDiffDocument}
        onEditorReady={noOp}
        suggestions={[numberDiffSuggestion]}
      />
    </main>
  )
}

const scenario = new URLSearchParams(window.location.search).get('scenario')
const fixture = scenario === 'switch'
  ? <DocumentSwitchFixture />
  : scenario === 'reconnect-continuity'
    ? <ReconnectContinuityFixture />
  : scenario === 'collaboration-writer'
    ? <CollaborationWriterFixture />
  : scenario === 'ai-publication'
    ? <AiPublicationFixture />
  : scenario === 'activation'
    ? <ActivationFixture />
    : scenario === 'initial-suggest-policy'
      ? <InitialSuggestPolicyFixture />
      : scenario === 'suggestion-undo-policy'
        ? <SuggestionUndoPolicyFixture />
      : scenario === 'mode-switch-policy'
        ? <ModeSwitchPolicyFixture />
        : scenario === 'view-policy'
          ? <ViewPolicyFixture />
          : scenario === 'simple-markup'
            ? <SimpleMarkupFixture />
            : scenario === 'slash-collaboration'
              ? <SlashCollaborationFixture />
              : scenario === 'comment-scale'
                ? <CommentScaleFixture />
                : scenario === 'status-hysteresis'
                  ? <StatusHysteresisFixture />
                  : scenario === 'modal-focus-race'
                    ? <ModalFocusRaceFixture />
                    : scenario === 'share-menu-focus'
                      ? <ShareMenuFocusFixture />
                      : scenario === 'explorer-action-modal'
                        ? <ExplorerActionModalFixture />
                      : scenario === 'presence-label'
                        ? <PresenceLabelFixture />
                      : scenario === 'profile-menu'
                        ? <ProfileMenuFixture />
                      : scenario === 'recovery-copy-notice'
                        ? <RecoveryCopyNoticeFixture />
                      : scenario === 'instruction-answer-switch'
                        ? <InstructionAnswerSwitchFixture />
      : scenario === 'view-ai'
        ? <ViewOnlyAiFixture />
      : scenario === 'suggestion-number-diff'
        ? <SuggestionNumberDiffFixture />
        : <TopBarFixture />

createRoot(document.getElementById('root')!).render(<AppProviders>{fixture}</AppProviders>)
