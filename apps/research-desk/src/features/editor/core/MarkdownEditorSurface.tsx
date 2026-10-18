/**
 * The markdown editing surface: owns the Tiptap editor instance and switches
 * between the live WYSIWYG view (with bubble menu, block handle and table
 * controls), the raw markdown source editor and the document diff view.
 * Extracted from `EditorWorkspace` so other canvases (e.g. the Agent Desk) can
 * reuse the surface without the editor shell.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import Collaboration from '@tiptap/extension-collaboration'
import type { Extensions } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorContent, useEditor, type Editor } from '@tiptap/react'
import { BubbleMenu } from '@tiptap/react/menus'
import { EDITOR_YJS_FRAGMENT } from '@inqtrix/editor-schema'
import { useReducedMotion } from 'motion/react'
import { AtSign, Check, CornerDownLeft, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Textarea } from '@/components/ui/textarea'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import type {
  EditorCommentAnchorRecord,
  EditorCommentKind,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorSuggestionRecord,
  ProjectState,
} from '@/features/project/types'
import {
  TextImproveButton,
  TextImproveFloatingLayer,
  useTextImprovement,
  type TextImprovementApiOptions,
} from '@/features/textImprove'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { CitationChips } from '../citationChips'
import { InqtrixCollaborationCaret, commentDecorationPluginKey, createEditorExtensions, editorMarkdownForTiptap, normalizeEditorMarkdownForTiptap, serializeEditorFinalProjectionMarkdown, serializeEditorMarkdown, suggestionDecorationPluginKey, type CollaborationReviewOverlayUpdate } from '../tiptap'
import type { CollaborationDocumentHandle } from '../useCollaborationDocument'
import { MarkAwareCollaborationTextDiff } from '../collaborationTextDiff'
import { BlockHandle } from '../BlockHandle'
import { TableControls } from '../TableControls'
import { SelectionToolbar } from '../SelectionToolbar'
import { MarkdownSourceEditor } from '../MarkdownSourceEditor'
import { DocumentDiffView } from '../DocumentDiffView'
import { suggestionDiffPlan } from '../suggestionDiff'
import { resolveSuggestionTarget } from '../useEditorSuggestions'
import {
  blockWidgetPositionForRange,
  createCommentFromSelection,
  materializeAnchorForRange,
  resolveMaterializedAnchor,
  shouldParsePastedMarkdown,
} from '../anchoring'
import { AUTHORED_COMMENT_KIND, isAuthoredComment } from '../commentKinds'
import type { EditorCopy } from '../editorCopy'
import { hasCollaborationRelativeAnchor } from '../inspector/relativeAnchors'
import {
  applyScrollableTableSemantics,
  escapeCssIdentifier,
} from '../editorDom'
import type {
  EditorCollaborationCommentActor,
  EditorCollaborationCommentThread,
} from '@/api/inqtrixClient'

export type MarkdownEditorSurfaceProps = {
  /** Citation labels shown as chips that open their evidence (the agent
   * canvas). Whether there are chips is decided at mount; the labels are
   * read again with every change of the document. */
  citations?: CitationChips
  collaboration?: CollaborationDocumentHandle | null
  collaborationReviewPolicy?: CollaborationReviewOverlayUpdate
  comments: EditorCommentThreadRecord[]
  copy: EditorCopy
  diffAnchorMarkdown: string | null
  document: EditorDocumentRecord
  /** Hosted inside an already-padded, already-scrolling container (the
   * agent canvas): skip the surface's own ScrollArea, min-height and
   * `px-10 py-8` so toggling read <-> edit never shifts the layout.
   * The Editor desk keeps the default standalone geometry. */
  embedded?: boolean
  isDiffVisible: boolean
  mode: ProjectState['editorUi']['viewMode']
  onChange: (contentMarkdown: string) => void
  onCreateComment: (comment: EditorCommentThreadRecord) => void
  /** When set, the bubble menu's comment button hands the MATERIALIZED
   * anchor and the selection's viewport position to the host instead of
   * opening the built-in form (anchor extraction must happen at click
   * time — the host popover steals the selection). */
  externalCommentComposer?: (payload: {
    anchor: EditorCommentAnchorRecord
    position: { left: number; top: number }
  }) => void
  onCreateTeamComment?: (input: {
    anchor: EditorCommentThreadRecord['anchor']
    bodyMarkdown: string
    mentionUserIds: string[]
    quote: string
  }) => void
  onCollaborationSuggestionUndo?: (patchId: string) => Promise<void>
  onEditorReady: (editor: Editor | null) => void
  onAcceptSuggestion: (suggestion: EditorSuggestionRecord) => void
  onEditSuggestion: (suggestionId: string, proposedText: string) => void
  onMarkSuggestionStale: (suggestionId: string) => void
  onRefineSuggestion: (suggestionId: string, instruction: string) => Promise<void>
  onRejectSuggestion: (suggestionId: string) => void
  onSelectComment: (commentId: string) => void
  onSelectTeamComment?: (commentId: string) => void
  onStopSuggestion: (suggestionId: string) => void
  onTeamCommentDraftChange?: (value: string) => void
  privateCommentsEnabled?: boolean
  runningSuggestionIds: readonly string[]
  selectedCommentId: string | null
  suggestionActionsDisabled?: boolean
  suggestionErrors: Record<string, string>
  suggestions: EditorSuggestionRecord[]
  teamCommentParticipants?: readonly EditorCollaborationCommentActor[]
  teamCommentDraft?: string
  teamComments?: readonly EditorCollaborationCommentThread[]
  selectedTeamCommentId?: string | null
  textImprovement: Omit<TextImprovementApiOptions, 'locale'>
}

export type ReadyCollaborationBinding = {
  canEdit: boolean
  document: NonNullable<CollaborationDocumentHandle['document']>
  /** Ends the write-mode report of the editor that writes into `document`. */
  endLocalWriteMode: CollaborationDocumentHandle['endLocalWriteMode']
  lifecycleKey: string
  /** Receives the write-mode report of the editor that writes into
   * `document`: the same controller, never another one. */
  markLocalWriteMode: CollaborationDocumentHandle['markLocalWriteMode']
  provider: NonNullable<CollaborationDocumentHandle['provider']>
  user: NonNullable<CollaborationDocumentHandle['user']>
}

export type EditorSurfaceLifecyclePolicy = {
  applyExternalContent: boolean
  editable: boolean
  emitFullBodyChanges: boolean
  renderSourceEditor: boolean
}

export type EditorSurfaceInitialContent = {
  content?: string
  contentType?: 'markdown'
}

/** Use the last durable projection only until the live Yjs binding exists. */
export function editorSurfaceInitialContent({
  collaborationMode,
  collaborationReady,
  contentMarkdown,
}: {
  collaborationMode: boolean
  collaborationReady: boolean
  contentMarkdown: string
}): EditorSurfaceInitialContent {
  if (collaborationMode && collaborationReady) return {}
  return { content: contentMarkdown, contentType: 'markdown' }
}

export function editorSurfaceLifecyclePolicy({
  collaborationMode,
  collaborationReady,
  collaborationCanEdit,
  mode,
}: {
  collaborationCanEdit: boolean
  collaborationMode: boolean
  collaborationReady: boolean
  mode: ProjectState['editorUi']['viewMode']
}): EditorSurfaceLifecyclePolicy {
  if (!collaborationMode) {
    return {
      applyExternalContent: true,
      editable: mode === 'live',
      emitFullBodyChanges: true,
      renderSourceEditor: mode === 'source',
    }
  }
  return {
    applyExternalContent: !collaborationReady,
    editable: mode === 'live' && collaborationReady && collaborationCanEdit,
    emitFullBodyChanges: false,
    renderSourceEditor: false,
  }
}

/**
 * What the surface knows about the full Markdown body its host holds.
 *
 * `baseline` is the loaded document, normalized by the editor's own plugins
 * (a trailing paragraph after a last block of another kind), together with
 * the stored bytes it was loaded from. `emitted` is the document whose
 * Markdown the host was handed last. `ownBodies` are the bodies the host may
 * still hand back, oldest first: the stored bytes and every body emitted
 * since, up to the last one the host handed back. The baseline and the
 * emitted document are unknown until the first update after a load; the
 * stored bytes are the surface's own from the load on, since the surface can
 * look at the host's body before the editor reports its first update.
 */
export type EditorBodyChangeState = {
  baseline: { doc: ProseMirrorNode; markdown: string } | null
  emitted: ProseMirrorNode | null
  ownBodies: readonly string[]
}

export const INITIAL_EDITOR_BODY_CHANGE_STATE: EditorBodyChangeState = {
  baseline: null,
  emitted: null,
  ownBodies: [],
}

/** The state right after the editor was loaded from *storedMarkdown*. */
function loadedEditorBody(storedMarkdown: string): EditorBodyChangeState {
  return { ...INITIAL_EDITOR_BODY_CHANGE_STATE, ownBodies: [storedMarkdown] }
}

export type EditorBodyUpdate = {
  /** The document before the update's root transaction. */
  before: ProseMirrorNode
  /** The document after the update, appended transactions included. */
  doc: ProseMirrorNode
  /** Whether the host may receive a body now: the editor is editable. */
  emittable: boolean
  /** Whether the ROOT transaction carries steps. Only a step-bearing root is
   * a content edit: `setEditable`, a plugin that appends to a step-less root
   * (the trailing paragraph) and decoration or highlight meta are not. */
  rootChangedDocument: boolean
  /** The body the host stored, the bytes the surface was loaded from. */
  storedMarkdown: string
}

/**
 * Decide whether one editor update hands the host a new body.
 *
 * The first update after a load establishes the baseline: a step-less one
 * folds the editor's normalization into it; a step-bearing one takes the
 * document before its root. While the host holds the baseline, a step-less
 * update keeps folding and emits nothing, so opening, focusing or clicking
 * never saves. Every other update emits unless the host already holds its
 * document; returning to the baseline document emits the stored bytes, not
 * a fresh serialization, so an undo back to the loaded text is no change.
 */
export function editorBodyChangeDecision(
  state: EditorBodyChangeState,
  update: EditorBodyUpdate,
  serialize: () => string,
): { emit: string | null; state: EditorBodyChangeState } {
  if (!update.rootChangedDocument) {
    if (!state.baseline || !state.emitted || state.emitted.eq(state.baseline.doc)) {
      const markdown = state.baseline?.markdown ?? update.storedMarkdown
      return {
        emit: null,
        state: {
          baseline: { doc: update.doc, markdown },
          emitted: update.doc,
          ownBodies: state.baseline ? state.ownBodies : [markdown],
        },
      }
    }
    return emitBodyChange(state, state.baseline, update, serialize)
  }
  if (!state.baseline || !state.emitted) {
    const baseline = { doc: update.before, markdown: update.storedMarkdown }
    return emitBodyChange(
      { baseline, emitted: baseline.doc, ownBodies: [baseline.markdown] },
      baseline,
      update,
      serialize,
    )
  }
  return emitBodyChange(state, state.baseline, update, serialize)
}

function emitBodyChange(
  state: EditorBodyChangeState,
  baseline: NonNullable<EditorBodyChangeState['baseline']>,
  update: EditorBodyUpdate,
  serialize: () => string,
): { emit: string | null; state: EditorBodyChangeState } {
  if (!update.emittable || state.emitted?.eq(update.doc)) return { emit: null, state }
  const markdown = update.doc.eq(baseline.doc) ? baseline.markdown : serialize()
  return {
    emit: markdown,
    state: { baseline, emitted: update.doc, ownBodies: [...state.ownBodies, markdown] },
  }
}

/**
 * The host hands the surface *markdown* as its body: whether it is one of
 * the surface's own (the stored bytes it was loaded from or a body it
 * emitted since), and the state after it. An own body is no news to the
 * editor, which shows that document or the user's newer edits on top of it
 * (a save answered after further typing hands back an older body). The own
 * bodies before it are dropped: the host has moved past them.
 */
export function editorBodyFromHost(
  state: EditorBodyChangeState,
  markdown: string,
): { own: boolean; state: EditorBodyChangeState } {
  const index = state.ownBodies.lastIndexOf(markdown)
  if (index < 0) return { own: false, state }
  return { own: true, state: { ...state, ownBodies: state.ownBodies.slice(index) } }
}

export function configureEditorExtensionsForCollaboration(
  extensions: Extensions,
  collaboration: ReadyCollaborationBinding | null,
  collaborationMode = collaboration !== null,
): Extensions {
  if (!collaborationMode) return extensions
  const collaborationExtensions = extensions.map((extension) => (
    extension.name === 'starterKit'
      ? extension.configure({ undoRedo: false })
      : extension
  ))
  if (!collaboration) return collaborationExtensions
  return [
    ...collaborationExtensions,
    Collaboration.configure({
      document: collaboration.document,
      field: EDITOR_YJS_FRAGMENT,
    }),
    MarkAwareCollaborationTextDiff,
    InqtrixCollaborationCaret.configure({
      provider: collaboration.provider,
      user: collaboration.user,
    }),
  ]
}

function collaborationBelongsToDocument(
  collaboration: CollaborationDocumentHandle,
  document?: EditorDocumentRecord,
): boolean {
  return !document || (
    collaboration.documentId === document.id
    && collaboration.generation === (document.collaboration?.generation ?? null)
  )
}

/** Whether the live connection is synchronized right now (status model). */
export function isCollaborationSurfaceSynced(
  collaboration: CollaborationDocumentHandle | null,
  document?: EditorDocumentRecord,
): boolean {
  return collaboration?.synced === true
    && collaborationBelongsToDocument(collaboration, document)
}

/** Provider construction precedes Yjs sync, so the exact server projection is
 * shown until the first sync. From then on the surface stays bound to the
 * live document across reconnects; only a release, an access revocation or a
 * new generation (another controller) hand the surface back. */
export function isCollaborationSurfaceBound(
  collaboration: CollaborationDocumentHandle | null,
  document?: EditorDocumentRecord,
): boolean {
  return collaboration?.liveDocumentReady === true
    && collaborationBelongsToDocument(collaboration, document)
}

/** Whether local edits may still enter the live document. A bound surface
 * that only reconnects keeps accepting them wherever the surface itself lets
 * input through: it is read-only then, except that an input method
 * composition started before the switch may finish. A second gate that
 * refused every local change the moment the connection dropped would discard
 * exactly that composition. Accepted edits wait in the controller's ordered
 * log for the replay on the next socket. */
export function collaborationSurfaceAcceptsLocalEdits(
  collaboration: CollaborationDocumentHandle,
): boolean {
  return collaboration.canEdit || (
    isCollaborationSurfaceBound(collaboration)
    && (
      collaboration.connectionStatus === 'reconnecting'
      || collaboration.connectionStatus === 'connecting'
    )
  )
}

export function collaborationBindingForEditorDocument(
  document: EditorDocumentRecord,
  collaboration: CollaborationDocumentHandle | null,
): ReadyCollaborationBinding | null {
  if (
    document.contentMode !== 'collaboration'
    || !isCollaborationSurfaceBound(collaboration, document)
    || !collaboration?.document
    || !collaboration.provider
    || !collaboration.user
  ) return null
  return {
    canEdit: collaboration.canEdit,
    document: collaboration.document,
    endLocalWriteMode: collaboration.endLocalWriteMode,
    lifecycleKey: collaboration.lifecycleKey,
    markLocalWriteMode: collaboration.markLocalWriteMode,
    provider: collaboration.provider,
    user: collaboration.user,
  }
}

export function MarkdownEditorSurface({
  citations,
  collaboration = null,
  collaborationReviewPolicy,
  comments,
  copy,
  diffAnchorMarkdown,
  document,
  embedded = false,
  isDiffVisible,
  mode,
  onChange,
  onCreateComment,
  externalCommentComposer,
  onCreateTeamComment,
  onCollaborationSuggestionUndo,
  onEditorReady,
  onAcceptSuggestion,
  onEditSuggestion,
  onRejectSuggestion,
  onMarkSuggestionStale,
  onRefineSuggestion,
  onSelectComment,
  onSelectTeamComment,
  onStopSuggestion,
  onTeamCommentDraftChange,
  privateCommentsEnabled = true,
  runningSuggestionIds,
  selectedCommentId,
  selectedTeamCommentId = null,
  suggestionActionsDisabled = false,
  suggestionErrors,
  suggestions,
  teamCommentParticipants = [],
  teamCommentDraft = '',
  teamComments = [],
  textImprovement,
}: MarkdownEditorSurfaceProps) {
  const documentIdRef = useRef(document.id)
  const editorInstanceRef = useRef<Editor | null>(null)
  const bodyChangeRef = useRef<EditorBodyChangeState>(loadedEditorBody(document.contentMarkdown))
  const onAcceptSuggestionRef = useRef(onAcceptSuggestion)
  const onCollaborationSuggestionUndoRef = useRef(onCollaborationSuggestionUndo)
  const onEditSuggestionRef = useRef(onEditSuggestion)
  const onMarkSuggestionStaleRef = useRef(onMarkSuggestionStale)
  const onRefineSuggestionRef = useRef(onRefineSuggestion)
  const onRejectSuggestionRef = useRef(onRejectSuggestion)
  const onSelectCommentRef = useRef(onSelectComment)
  const onSelectTeamCommentRef = useRef(onSelectTeamComment)
  const onStopSuggestionRef = useRef(onStopSuggestion)
  const suggestionsRef = useRef(suggestions)
  const citationsRef = useRef(citations)
  const [citationChips] = useState(() => (citations ? () => citationsRef.current ?? null : undefined))
  const teamCommentIdsRef = useRef(new Set(teamComments.map((comment) => comment.id)))
  const previousModeRef = useRef(mode)
  // The editability the lifecycle asks for right now, read when a deferred
  // switch finally runs.
  const requestedEditableRef = useRef(false)
  // The editor whose focus a switch to read-only took away, with the
  // listeners that forget it once the user acts outside the editor.
  const focusIntentRef = useRef<{ dispose: () => void; editor: Editor } | null>(null)
  const forgetFocusIntent = useCallback(() => {
    focusIntentRef.current?.dispose()
    focusIntentRef.current = null
  }, [])
  useEffect(() => forgetFocusIntent, [forgetFocusIntent])
  const commentsSignature = [
    ...comments.map((comment) => (
      `${comment.id}:${comment.status}:${comment.kind}:${comment.anchor.from}:${comment.anchor.to}`
    )),
    ...teamComments.map((comment) => (
      `${comment.id}:${comment.status}:team:${String(comment.anchor.from)}:${String(comment.anchor.to)}`
    )),
  ].join('|')
  const suggestionsSignature = editorSuggestionDecorationSignature(suggestions)
  const suggestionUiSignature = suggestions.map((suggestion) =>
    `${suggestion.id}:${runningSuggestionIds.includes(suggestion.id) ? 'running' : 'idle'}:${suggestionErrors[suggestion.id] ?? ''}`).join('|')
  // Eigener, gespeicherter Inhalt: KEINE Einfuhr-Regel (siehe editorMarkdownForTiptap).
  const tiptapContentMarkdown = editorMarkdownForTiptap(document.contentMarkdown)
  const collaborationMode = document.contentMode === 'collaboration'
  const collaborationBinding = collaborationBindingForEditorDocument(document, collaboration)
  const lifecyclePolicy = editorSurfaceLifecyclePolicy({
    collaborationCanEdit: collaboration?.canEdit ?? false,
    collaborationMode,
    collaborationReady: collaborationBinding !== null,
    mode,
  })
  const initialContent = editorSurfaceInitialContent({
    collaborationMode,
    collaborationReady: collaborationBinding !== null,
    contentMarkdown: tiptapContentMarkdown,
  })
  const editorLifecycleKey = collaborationMode
    ? collaborationBinding?.lifecycleKey
      ?? `collaboration-projection:${document.id}:${document.collaboration?.projectionSequence ?? document.updatedAt}`
    : 'legacy'

  useEffect(() => {
    citationsRef.current = citations
    suggestionsRef.current = suggestions
    teamCommentIdsRef.current = new Set(teamComments.map((comment) => comment.id))
  }, [citations, suggestions, teamComments])

  useEffect(() => {
    onAcceptSuggestionRef.current = onAcceptSuggestion
    onCollaborationSuggestionUndoRef.current = onCollaborationSuggestionUndo
    onEditSuggestionRef.current = onEditSuggestion
    onMarkSuggestionStaleRef.current = onMarkSuggestionStale
    onRefineSuggestionRef.current = onRefineSuggestion
    onRejectSuggestionRef.current = onRejectSuggestion
    onSelectCommentRef.current = onSelectComment
    onSelectTeamCommentRef.current = onSelectTeamComment
    onStopSuggestionRef.current = onStopSuggestion
  }, [onAcceptSuggestion, onCollaborationSuggestionUndo, onEditSuggestion, onMarkSuggestionStale, onRefineSuggestion, onRejectSuggestion, onSelectComment, onSelectTeamComment, onStopSuggestion])

  const editor = useEditor({
    ...initialContent,
    editable: lifecyclePolicy.editable,
    editorProps: {
      attributes: {
        'aria-label': copy.documentEditor,
        'aria-multiline': 'true',
        'aria-readonly': String(!lifecyclePolicy.editable),
        class: cn('editor-prose min-h-full focus:outline-none', embedded && 'chat-markdown'),
        role: 'textbox',
      },
      handlePaste: (_view, event) => {
        const pastedMarkdown = event.clipboardData?.getData('text/plain') ?? ''
        const currentEditor = editorInstanceRef.current
        if (!currentEditor?.isEditable || !shouldParsePastedMarkdown(pastedMarkdown)) return false
        event.preventDefault()
        currentEditor.commands.insertContent(normalizeEditorMarkdownForTiptap(pastedMarkdown), {
          contentType: 'markdown',
        })
        return true
      },
    },
    extensions: configureEditorExtensionsForCollaboration(createEditorExtensions({
      citationChips,
      collaborationReview: collaborationReviewPolicy,
      onCollaborationSuggestionUndo: collaborationMode && onCollaborationSuggestionUndo
        ? async (patchId) => {
            const request = onCollaborationSuggestionUndoRef.current
            if (!request) {
              throw new Error('Suggestion undo is not available for this editor.')
            }
            await request(patchId)
          }
        : undefined,
      // From the binding of the same render as the editor: the report goes
      // to the controller whose document this editor binds.
      onLocalWriteMode: collaborationBinding?.markLocalWriteMode,
      onLocalWriteModeEnd: collaborationBinding?.endLocalWriteMode,
      syntaxMarkerRemoveLabel: copy.removeFormatting,
      placeholderEmpty: copy.placeholderEmpty,
      placeholderHeading: copy.placeholderHeading,
      teamCommentLabel: copy.bubbleTeamComment,
      slash: {
        labels: {
          title: copy.slashTitle,
          empty: copy.slashEmpty,
          navHint: copy.slashNav,
          selectHint: copy.slashSelect,
          closeHint: copy.slashClose,
          groupStyle: copy.slashGroupStyle,
          groupInsert: copy.slashGroupInsert,
          text: copy.slashText,
          heading1: copy.slashHeading1,
          heading2: copy.slashHeading2,
          heading3: copy.slashHeading3,
          bulletList: copy.slashBulletList,
          orderedList: copy.slashOrderedList,
          taskList: copy.slashTaskList,
          blockquote: copy.slashBlockquote,
          codeBlock: copy.slashCodeBlock,
          table: copy.slashTable,
          divider: copy.slashDivider,
          suggestUnavailable: copy.slashSuggestUnavailable,
        },
      },
      codeBlockLabels: {
        copied: copy.codeBlockCopied,
        copy: copy.codeBlockCopy,
        languageAria: copy.codeBlockLanguageAria,
        plainOption: copy.codeBlockLanguagePlain,
        unavailable: copy.codeBlockLanguageUnavailable,
      },
      mathLabels: {
        edit: copy.mathEdit,
        source: copy.mathSource,
        done: copy.mathDone,
        unavailable: copy.mathUnavailable,
      },
      onClick: (commentId) => {
        if (teamCommentIdsRef.current.has(commentId)) {
          onSelectTeamCommentRef.current?.(commentId)
          return
        }
        onSelectCommentRef.current(commentId)
      },
      onSuggestionAccept: (suggestionId) => {
        const suggestion = suggestionsRef.current.find((item) => item.id === suggestionId)
        if (suggestion) onAcceptSuggestionRef.current(suggestion)
      },
      onSuggestionReject: (suggestionId) => onRejectSuggestionRef.current(suggestionId),
      onSuggestionEdit: (suggestionId, proposedText) => onEditSuggestionRef.current(suggestionId, proposedText),
      onSuggestionRefine: (suggestionId, instruction) => {
        void onRefineSuggestionRef.current(suggestionId, instruction)
      },
      onSuggestionCancel: (suggestionId) => onStopSuggestionRef.current(suggestionId),
      onSuggestionSelect: (suggestionId) => {
        const suggestion = suggestionsRef.current.find((item) => item.id === suggestionId)
        if (suggestion?.origin.commentId) onSelectCommentRef.current(suggestion.origin.commentId)
      },
    }), collaborationBinding, collaborationMode),
    immediatelyRender: false,
    onCreate: ({ editor: createdEditor }) => {
      editorInstanceRef.current = createdEditor
    },
    onDestroy: () => {
      editorInstanceRef.current = null
    },
    onUpdate: ({ editor: currentEditor, transaction }) => {
      if (!lifecyclePolicy.emitFullBodyChanges) return
      const decision = editorBodyChangeDecision(
        bodyChangeRef.current,
        {
          before: transaction.before,
          doc: currentEditor.state.doc,
          emittable: currentEditor.isEditable,
          rootChangedDocument: transaction.docChanged,
          storedMarkdown: document.contentMarkdown,
        },
        () => serializeEditorMarkdown(currentEditor),
      )
      bodyChangeRef.current = decision.state
      if (decision.emit !== null) onChange(decision.emit)
    },
  }, [editorLifecycleKey])

  useEffect(() => {
    onEditorReady(editor && !editor.isDestroyed ? editor : null)
    return () => onEditorReady(null)
  }, [editor, onEditorReady])

  useEffect(() => {
    if (!editor || editor.isDestroyed) return
    const root = editor.view.dom
    const apply = () => applyScrollableTableSemantics(root, copy.scrollableTable)
    apply()
    const observer = new MutationObserver(apply)
    observer.observe(root, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [copy.scrollableTable, editor])

  useEffect(() => {
    if (!editor || editor.isDestroyed || documentIdRef.current === document.id) return
    documentIdRef.current = document.id
    if (!lifecyclePolicy.applyExternalContent) return
    editor.commands.setContent(tiptapContentMarkdown, {
      contentType: 'markdown',
      emitUpdate: false,
    })
    bodyChangeRef.current = loadedEditorBody(document.contentMarkdown)
  }, [document.id, editor, lifecyclePolicy.applyExternalContent, tiptapContentMarkdown])

  useEffect(() => {
    if (!editor || editor.isDestroyed) return
    const previousMode = previousModeRef.current
    previousModeRef.current = mode
    if (!lifecyclePolicy.applyExternalContent) return
    if (mode !== 'live') return
    const shouldReparseMarkdown = previousMode === 'source'
    if (!shouldReparseMarkdown) {
      // A body of this surface's own (the stored bytes it loaded or one it
      // emitted, however old) is already in the editor, maybe with the
      // user's newer edits on top: replacing it would drop them and move the
      // cursor under a typing user. Stored bytes need not be the
      // serializer's own output, so the comparison below alone would not
      // see that.
      const fromHost = editorBodyFromHost(bodyChangeRef.current, document.contentMarkdown)
      bodyChangeRef.current = fromHost.state
      if (fromHost.own || serializeEditorMarkdown(editor) === tiptapContentMarkdown) return
    }
    editor.commands.setContent(tiptapContentMarkdown, {
      contentType: 'markdown',
      emitUpdate: false,
    })
    bodyChangeRef.current = loadedEditorBody(document.contentMarkdown)
  }, [document.contentMarkdown, editor, lifecyclePolicy.applyExternalContent, mode, tiptapContentMarkdown])

  useEffect(() => {
    requestedEditableRef.current = lifecyclePolicy.editable
    if (!editor || editor.isDestroyed) return
    const root = editor.view.dom
    const applyEditability = () => {
      if (editor.isDestroyed) return
      const editable = requestedEditableRef.current
      const ownerDocument = root.ownerDocument
      if (!editable && editor.isEditable) {
        forgetFocusIntent()
        if (root.contains(ownerDocument.activeElement)) {
          // Any pointer press or focus change outside the editor means the
          // user went on elsewhere: a selection in another panel, a control,
          // or plain page content. Taking the focus back would destroy that.
          const forgetOnOutsideAction = (event: Event) => {
            if (event.target instanceof Node && root.contains(event.target)) return
            forgetFocusIntent()
          }
          ownerDocument.addEventListener('pointerdown', forgetOnOutsideAction, true)
          ownerDocument.addEventListener('focusin', forgetOnOutsideAction, true)
          focusIntentRef.current = {
            dispose: () => {
              ownerDocument.removeEventListener('pointerdown', forgetOnOutsideAction, true)
              ownerDocument.removeEventListener('focusin', forgetOnOutsideAction, true)
            },
            editor,
          }
        }
      }
      // Editability is no content change: without `false` Tiptap emits an
      // update with an empty transaction. Node views (the code block and
      // formula headers) follow editability through editor transactions, so a
      // step-less one tells them.
      editor.setEditable(editable, false)
      editor.view.dispatch(editor.state.tr.setMeta('addToHistory', false))
      root.setAttribute('aria-readonly', String(!editable))
      if (!editable) return
      // A reconnect keeps this editor: give back the focus the read-only
      // switch took. The retained state carries the selection.
      const refocus = focusIntentRef.current?.editor === editor
      forgetFocusIntent()
      if (refocus) editor.view.focus()
    }
    if (lifecyclePolicy.editable || !editor.view.composing) {
      applyEditability()
      return
    }
    // ProseMirror handles `compositionend` only while the editor is editable.
    // Switching to read-only in the middle of an IME composition would leave
    // its composition state behind on this retained editor, so the switch
    // waits for the composition to end. The composed text enters the local
    // log and is sent after the ordered replay.
    const finishComposition = () => {
      root.removeEventListener('compositionend', finishComposition)
      root.removeEventListener('blur', finishComposition)
      applyEditability()
    }
    root.addEventListener('compositionend', finishComposition)
    root.addEventListener('blur', finishComposition)
    return () => {
      root.removeEventListener('compositionend', finishComposition)
      root.removeEventListener('blur', finishComposition)
    }
  }, [editor, forgetFocusIntent, lifecyclePolicy.editable])

  useEffect(() => {
    if (!editor || editor.isDestroyed) return
    editor.view.dom.setAttribute('aria-label', copy.documentEditor)
  }, [copy.documentEditor, editor])

  useEffect(() => {
    if (!editor || editor.isDestroyed || mode !== 'live') return
    const privateItems = comments
      // Only a note marks the text: the carrier row of an assistant
      // suggestion is none, and its mark would select a note no list shows.
      .filter((comment) => comment.status !== 'resolved' && isAuthoredComment(comment))
      .map((comment) => {
        const resolved = resolveMaterializedAnchor(editor, comment.anchor)
        if (!resolved) return null
        return {
          from: resolved.range.from,
          id: comment.id,
          kind: comment.kind,
          selected: selectedCommentId === comment.id,
          status: comment.status,
          to: resolved.range.to,
        }
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .filter((item) => item.from < item.to)
    const teamItems = teamComments
      .filter((comment) => comment.status !== 'resolved')
      .map((comment) => {
        const anchor = comment.anchor as EditorCommentThreadRecord['anchor']
        const resolved = resolveMaterializedAnchor(editor, anchor)
        if (!resolved) return null
        return {
          from: resolved.range.from,
          id: comment.id,
          kind: 'team' as const,
          selected: selectedTeamCommentId === comment.id,
          status: 'open' as const,
          to: resolved.range.to,
        }
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
    const items = [...privateItems, ...teamItems]
    editor.view.dispatch(editor.state.tr.setMeta(commentDecorationPluginKey, { items }))
    // Re-materialise comment anchors when the CONTENT changes (external
    // rebase/hydrate replaces the body) or the comment set changes. Keyed on
    // contentMarkdown, not revision: revision tracks the server base and
    // does not move per local edit, and the decorations already self-map
    // through transactions for in-place typing (see tiptap commentDecoration
    // plugin), so content is the correct, direct trigger.
  }, [commentsSignature, document.contentMarkdown, editor, mode, selectedCommentId, selectedTeamCommentId])

  useEffect(() => {
    if (!editor || editor.isDestroyed || mode !== 'live') return
    const staleSuggestionIds: string[] = []
    const items = suggestions.flatMap((suggestion) => {
      const target = resolveSuggestionDecorationTarget(editor, suggestion)
      if (!target) {
        if (editorSuggestionShouldAutoStaleOnMissingTarget(suggestion)) {
          staleSuggestionIds.push(suggestion.id)
        }
        return []
      }
      const plan = suggestionDiffPlan(suggestion.originalText, suggestion.proposedText)
      return [{
        acceptLabel: copy.accept,
        active: selectedCommentId === suggestion.origin.commentId,
        display: plan.display,
        editLabel: copy.editSuggestion,
        error: suggestionErrors[suggestion.id],
        from: target.from,
        id: suggestion.id,
        isRunning: runningSuggestionIds.includes(suggestion.id),
        proposedLabel: copy.proposedText,
        proposedText: suggestion.proposedText,
        providerActionsDisabled: suggestionActionsDisabled,
        refineLabel: copy.refineSuggestion,
        refinementPlaceholder: copy.refinementPlaceholder,
        rejectLabel: copy.reject,
        revision: suggestion.revision ?? 1,
        revisionLabel: copy.revision,
        reviewSurface: plan.reviewSurface,
        saveLabel: copy.saveSuggestion,
        segments: plan.segments,
        cancelLabel: copy.cancelEdit,
        sendLabel: copy.sendRefinement,
        runningLabel: copy.refiningSuggestion,
        stopLabel: copy.stopRun,
        to: target.to,
        widgetAt: plan.display === 'block' ? target.widgetAt : undefined,
      }]
    })
    editor.view.dispatch(editor.state.tr.setMeta(suggestionDecorationPluginKey, { items }))
    for (const suggestionId of staleSuggestionIds) {
      onMarkSuggestionStaleRef.current(suggestionId)
    }
  }, [copy.accept, copy.cancelEdit, copy.editSuggestion, copy.proposedText, copy.refineSuggestion, copy.refinementPlaceholder, copy.reject, copy.revision, copy.saveSuggestion, copy.sendRefinement, copy.refiningSuggestion, copy.stopRun, document.revision, editor, mode, selectedCommentId, suggestionActionsDisabled, suggestionUiSignature, suggestionsSignature])

  useEffect(() => {
    const selectedId = selectedTeamCommentId ?? selectedCommentId
    if (!selectedId) return
    const target = globalThis.document?.querySelector<HTMLElement>(
      `[data-editor-comment-anchor="${escapeCssIdentifier(selectedId)}"]`,
    )
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [selectedCommentId, selectedTeamCommentId])

  if (lifecyclePolicy.renderSourceEditor) {
    return (
      <MarkdownSourceEditor
        labels={{
          addColumn: copy.addColumn,
          addRow: copy.addRow,
          closeTableEditor: copy.closeTableEditor,
          columnLabel: copy.columnLabel,
          deleteColumn: copy.deleteColumn,
          deleteRow: copy.deleteRow,
          editor: copy.sourceEditor,
          formatTables: copy.formatTables,
          insertOrEditTable: copy.insertOrEditTable,
          lineWrap: copy.sourceLineWrap,
          tableAlignmentCenter: copy.tableAlignmentCenter,
          tableAlignmentLeft: copy.tableAlignmentLeft,
          tableAlignmentRight: copy.tableAlignmentRight,
          tableColumn: copy.tableColumn,
          tableEditor: copy.tableEditor,
          tableLines: copy.tableLines,
          tableRows: copy.tableRows,
        }}
        onChange={onChange}
        value={document.contentMarkdown}
      />
    )
  }

  if (isDiffVisible) {
    return (
      <DocumentDiffView
        anchorMarkdown={diffAnchorMarkdown}
        copy={copy}
        currentMarkdown={editor && !editor.isDestroyed
          ? collaborationMode
            ? serializeEditorFinalProjectionMarkdown(editor)
            : serializeEditorMarkdown(editor)
          : document.contentMarkdown}
      />
    )
  }

  const liveBody = (
      <div
        className={
          embedded
            ? 'w-full'
            : 'min-h-[calc(100svh-var(--header-h)-10rem)] w-full px-10 py-8'
        }
      >
        {editor && lifecyclePolicy.editable ? (
          <EditorBubbleMenu
            commentOnly={collaborationReviewPolicy?.writeMode === 'comment'}
            copy={copy}
            editor={editor}
            onCreateComment={(commentMarkdown, kind) => {
              const comment = createCommentFromSelection(editor, document.id, commentMarkdown, kind)
              if (!comment) return
              onCreateComment(comment)
            }}
            onExternalComment={externalCommentComposer
              ? () => {
                const { from, to } = editor.state.selection
                if (from === to) return
                const anchor = materializeAnchorForRange(editor, { from, to })
                const coords = editor.view.coordsAtPos(to)
                externalCommentComposer({
                  anchor,
                  position: { left: coords.left, top: coords.bottom + 8 },
                })
              }
              : undefined}
            onCreateTeamComment={onCreateTeamComment
              ? (bodyMarkdown, mentionUserIds) => {
                  const comment = createCommentFromSelection(
                    editor,
                    document.id,
                    bodyMarkdown,
                    'collect',
                  )
                  if (!comment) return
                  onCreateTeamComment({
                    anchor: comment.anchor,
                    bodyMarkdown,
                    mentionUserIds,
                    quote: comment.anchor.selectedText,
                  })
                }
              : undefined}
            onTeamCommentDraftChange={onTeamCommentDraftChange}
            privateCommentsEnabled={privateCommentsEnabled}
            teamCommentParticipants={teamCommentParticipants}
            teamCommentDraft={teamCommentDraft}
            textImprovement={textImprovement}
          />
        ) : null}
        {editor && lifecyclePolicy.editable ? (
          <BlockHandle
            editor={editor}
            labels={{
              ariaLabel: copy.blockHandleAria,
              turnInto: copy.blockTurnInto,
              duplicate: copy.blockDuplicate,
              deleteBlock: copy.blockDelete,
              moveUp: copy.blockMoveUp,
              moveDown: copy.blockMoveDown,
              text: copy.slashText,
              heading1: copy.slashHeading1,
              heading2: copy.slashHeading2,
              heading3: copy.slashHeading3,
              bulletList: copy.slashBulletList,
              orderedList: copy.slashOrderedList,
              taskList: copy.slashTaskList,
              blockquote: copy.slashBlockquote,
              codeBlock: copy.slashCodeBlock,
            }}
          />
        ) : null}
        {editor && lifecyclePolicy.editable ? (
          <TableControls
            editor={editor}
            labels={{
              columnOptions: copy.tableColumnOptions,
              rowOptions: copy.tableRowOptions,
              addColumn: copy.addColumn,
              addRow: copy.addRow,
              colInsertLeft: copy.tableColInsertLeft,
              colInsertRight: copy.tableColInsertRight,
              colMoveLeft: copy.tableColMoveLeft,
              colMoveRight: copy.tableColMoveRight,
              sortAsc: copy.tableSortAsc,
              sortDesc: copy.tableSortDesc,
              colDuplicate: copy.tableColDuplicate,
              colClear: copy.tableColClear,
              toggleHeaderRow: copy.tableToggleHeaderRow,
              colDelete: copy.deleteColumn,
              rowInsertAbove: copy.tableRowInsertAbove,
              rowInsertBelow: copy.tableRowInsertBelow,
              rowMoveUp: copy.tableRowMoveUp,
              rowMoveDown: copy.tableRowMoveDown,
              rowDuplicate: copy.tableRowDuplicate,
              rowDelete: copy.deleteRow,
            }}
          />
        ) : null}
        <EditorContent className="min-h-full" editor={editor} />
      </div>
  )
  // Embedded hosts own scrolling and padding — a nested ScrollArea here
  // is what made the report shift when entering edit mode.
  if (embedded) return liveBody
  return (
    <ScrollArea className="min-h-0 flex-1 bg-background">{liveBody}</ScrollArea>
  )
}

export function editorSuggestionDecorationSignature(
  suggestions: readonly EditorSuggestionRecord[],
): string {
  return suggestions.map((suggestion) => [
    suggestion.id,
    suggestion.revision ?? 1,
    suggestion.editPosition ?? 'replace',
    suggestion.anchor.from,
    suggestion.anchor.to,
    suggestion.anchorText ?? '',
    suggestion.originalText.length,
    suggestion.proposedText,
  ].join(':')).join('|')
}

export function editorSuggestionShouldAutoStaleOnMissingTarget(
  suggestion: EditorSuggestionRecord,
): boolean {
  return !hasCollaborationRelativeAnchor(suggestion.anchor)
}

function resolveSuggestionDecorationTarget(
  editor: Editor,
  suggestion: EditorSuggestionRecord,
): { from: number; to: number; widgetAt: number } | null {
  // ONE resolver decides where a suggestion lives. Re-resolving here from
  // the RAW stored anchor would read a stale absolute hint as soon as Yjs
  // shifts positions; delegating to resolveSuggestionTarget shares the
  // relative-anchor path and every future policy change, and this wrapper
  // only derives widget/insert positions from the shared result.
  const { target } = resolveSuggestionTarget(editor, suggestion)
  if (!target) return null
  if (target.kind === 'replace') {
    return {
      ...target.range,
      widgetAt: blockWidgetPositionForRange(editor, target.range),
    }
  }
  return { from: target.at, to: target.at, widgetAt: target.at }
}

function EditorBubbleMenu({
  commentOnly,
  copy,
  editor,
  onCreateComment,
  onExternalComment,
  onCreateTeamComment,
  onTeamCommentDraftChange,
  privateCommentsEnabled,
  teamCommentParticipants,
  teamCommentDraft,
  textImprovement,
}: {
  commentOnly: boolean
  copy: EditorCopy
  editor: Editor
  onCreateComment: (commentMarkdown: string, kind: EditorCommentKind) => void
  /** Host-owned comment composer (the agent canvas popover). When set,
   * the comment button hands off instead of opening the built-in editor
   * form — ONE comment field per surface. */
  onExternalComment?: () => void
  onCreateTeamComment?: (
    bodyMarkdown: string,
    mentionUserIds: string[],
  ) => void
  onTeamCommentDraftChange?: (value: string) => void
  privateCommentsEnabled: boolean
  teamCommentParticipants: readonly EditorCollaborationCommentActor[]
  teamCommentDraft: string
  textImprovement: Omit<TextImprovementApiOptions, 'locale'>
}) {
  const { locale, t } = useLocale()
  const reduceMotion = useReducedMotion()
  const [isCommenting, setIsCommenting] = useState(false)
  const [commentDraft, setCommentDraft] = useState('')
  const [commentVisibility, setCommentVisibility] = useState<'private' | 'team'>(
    privateCommentsEnabled ? 'private' : 'team',
  )
  const [mentionUserIds, setMentionUserIds] = useState<string[]>([])
  const [commentImproveError, setCommentImproveError] = useState<string | null>(null)
  const commentTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  // Keeps the bubble open while the "Turn into" dropdown is open (a transaction
  // could otherwise re-run shouldShow and hide it mid-interaction).
  const toolbarInteractingRef = useRef(false)
  const commentTextImprove = useTextImprovement({
    ...textImprovement,
    locale,
    messages: {
      requestFailed: (message) => `${t.textImprove.requestFailed}: ${message}`,
      sensitiveText: t.textImprove.sensitiveText,
      unavailable: t.textImprove.unavailable,
    },
  })

  useLayoutEffect(() => {
    if (!isCommenting) return
    resizeTextareaToRows(commentTextareaRef.current, 6)
  }, [commentDraft, isCommenting])

  const mentionSummary = mentionNames(mentionUserIds, teamCommentParticipants)

  const commentFormRef = useRef<HTMLFormElement | null>(null)
  const commentDraftRef = useRef(commentDraft)
  commentDraftRef.current = commentDraft

  /**
   * Let an untouched comment form go when the user clicks away.
   *
   * A click outside the content area leaves the editor without focus and
   * leaves the ProseMirror selection UNCHANGED. The bubble's `shouldShow`
   * reads only that selection, so it can never hide the form on its own,
   * and watching the selection would not help either — no selection event
   * fires. The close therefore has to come from the click itself: without
   * it, an empty form opened on a selection stays put and reappears with
   * the next selection.
   *
   * A form with text in it stays: discarding what someone typed is worse
   * than a box that lingers.
   */
  useEffect(() => {
    if (!isCommenting) return
    const onPointerDown = (event: PointerEvent) => {
      if (commentDraftRef.current.trim()) return
      const target = event.target
      if (target instanceof Node && commentFormRef.current?.contains(target)) {
        return
      }
      closeCommentComposer()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [isCommenting])

  function closeCommentComposer(preserveTeamDraft = false) {
    const collapseAt = editor.state.selection.to
    editor.commands.setTextSelection(collapseAt)
    editor.commands.blur()
    if (commentVisibility === 'team' && !preserveTeamDraft) {
      onTeamCommentDraftChange?.('')
    }
    setCommentDraft('')
    setCommentVisibility(privateCommentsEnabled ? 'private' : 'team')
    setMentionUserIds([])
    setCommentImproveError(null)
    commentTextImprove.clearProposal()
    toolbarInteractingRef.current = false
    setIsCommenting(false)
  }

  function cancelComment() {
    closeCommentComposer()
  }

  function submitComment() {
    const value = commentDraft.trim()
    if (!value) return
    if (commentVisibility === 'team' && onCreateTeamComment) {
      onCreateTeamComment(value, mentionUserIds)
      closeCommentComposer(true)
      return
    } else {
      onCreateComment(value, AUTHORED_COMMENT_KIND)
    }
    closeCommentComposer()
  }

  function handleCommentDraftChange(value: string) {
    setCommentDraft(value)
    if (commentVisibility === 'team') onTeamCommentDraftChange?.(value)
    setCommentImproveError(null)
    commentTextImprove.clearProposal()
  }

  async function improveCommentDraft() {
    setCommentImproveError(null)
    try {
      await commentTextImprove.improve('chat_input', commentDraft)
    } catch (error) {
      setCommentImproveError(messageFromUnknown(error))
    }
  }

  function acceptCommentImprovement(text: string) {
    handleCommentDraftChange(text)
    window.requestAnimationFrame(() => {
      commentTextareaRef.current?.focus()
      resizeTextareaToRows(commentTextareaRef.current, 6)
    })
  }

  return (
    <BubbleMenu
      editor={editor}
      appendTo={() => globalThis.document.body}
      options={{
        flip: { padding: { bottom: 132, left: 12, right: 12, top: 12 } },
        inline: true,
        offset: 8,
        placement: 'top-start',
        shift: { padding: { bottom: 132, left: 12, right: 12, top: 12 } },
        strategy: 'fixed',
      }}
      shouldShow={({ editor: currentEditor, state }) => {
        const { empty } = state.selection
        return currentEditor.isEditable && (!empty || toolbarInteractingRef.current)
      }}
    >
      <div className="z-50 flex min-w-0 items-center gap-1 rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg">
        {isCommenting ? (
          <form
            className="relative flex w-[24rem] max-w-[calc(100vw-5rem)] flex-col gap-1.5 p-1"
            ref={commentFormRef}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                cancelComment()
              }
            }}
            onSubmit={(event) => {
              event.preventDefault()
              submitComment()
            }}
          >
            <TextImproveFloatingLayer
              labels={{
                accept: t.textImprove.accept,
                changes: t.textImprove.changes,
                noChanges: t.textImprove.noChanges,
                reject: t.textImprove.reject,
                title: t.textImprove.title,
                warnings: t.textImprove.warnings,
              }}
              onAccept={acceptCommentImprovement}
              onReject={commentTextImprove.clearProposal}
              proposal={commentTextImprove.proposal}
              reduceMotion={reduceMotion}
            />
            {/* One growing line, and every control on it: a single sentence
                must not cost three rows plus a separate footer bar. Submit is
                the return key — the glyph states it rather than adding a
                labelled button; Escape closes. */}
            <div className="relative">
              <Textarea
                autoFocus
                className="t-body min-h-0 resize-none border-border/70 bg-background/60 py-1.5 pl-2.5 pr-16 focus-visible:ring-1 [scrollbar-width:thin]"
                onChange={(event) => handleCommentDraftChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault()
                    submitComment()
                  }
                }}
                placeholder={commentVisibility === 'team'
                  ? copy.inlineTeamComment
                  : copy.inlineComment}
                ref={commentTextareaRef}
                rows={1}
                value={commentDraft}
              />
              <div className="absolute inset-y-0 right-1.5 z-10 flex items-start gap-0.5 py-1">
                {commentVisibility === 'team' ? (
                  <TeamMentionPicker
                    onChange={setMentionUserIds}
                    participants={teamCommentParticipants}
                    selected={mentionUserIds}
                  />
                ) : null}
                <TextImproveButton
                  className={cn(
                    (commentVisibility === 'team' || !privateCommentsEnabled)
                      && 'hidden',
                  )}
                  disabled={!commentDraft.trim()}
                  isLoading={commentTextImprove.isImproving}
                  label={t.textImprove.improve}
                  loadingLabel={t.textImprove.improving}
                  onClick={() => void improveCommentDraft()}
                  reduceMotion={reduceMotion}
                />
                <button
                  aria-label={commentVisibility === 'team'
                    ? copy.inlineTeamCommentSubmit
                    : copy.inlineCommentSubmit}
                  className="inline-grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40 disabled:hover:bg-transparent"
                  disabled={!commentDraft.trim()}
                  type="submit"
                >
                  <CornerDownLeft className="size-3.5" />
                </button>
                {/* A written form does not close on a click outside (the
                    draft is worth more than the tidiness), and Escape cannot
                    reach it once the focus left — so exactly that state gets
                    a visible way out. The empty form keeps the clean line. */}
                {commentDraft.trim() ? (
                  <button
                    aria-label={copy.cancel}
                    className="inline-grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                    onClick={cancelComment}
                    type="button"
                  >
                    <X className="size-3.5" />
                  </button>
                ) : null}
              </div>
            </div>
            {commentImproveError ? (
              <p className="t-meta-sm rounded-md border border-destructive/20 bg-destructive/5 px-2 py-1 text-destructive">
                {commentImproveError}
              </p>
            ) : null}
            {/* The chosen names, and only when there are any — an empty
                mention row would cost a whole line of a one-line box.
                The @ trigger itself sits on the field. */}
            {mentionSummary ? (
              <p className="t-hint truncate px-1 text-muted-foreground">{mentionSummary}</p>
            ) : null}
          </form>
        ) : (
          <SelectionToolbar
            commentOnly={commentOnly}
            editor={editor}
            labels={{
              comment: onCreateTeamComment
                ? copy.bubbleTeamComment
                : copy.bubbleComment,
              privateComment: copy.bubblePrivateComment,
              turnInto: copy.blockTurnInto,
              bold: copy.bubbleBold,
              italic: copy.bubbleItalic,
              underline: copy.bubbleUnderline,
              strike: copy.bubbleStrike,
              code: copy.bubbleCode,
              highlight: copy.bubbleHighlight,
              text: copy.slashText,
              heading1: copy.slashHeading1,
              heading2: copy.slashHeading2,
              heading3: copy.slashHeading3,
              bulletList: copy.slashBulletList,
              orderedList: copy.slashOrderedList,
              taskList: copy.slashTaskList,
              blockquote: copy.slashBlockquote,
              codeBlock: copy.slashCodeBlock,
            }}
            onInteractingChange={(interacting) => {
              toolbarInteractingRef.current = interacting
            }}
            privateCommentEnabled={privateCommentsEnabled}
            onStartComment={(visibility) => {
              // A host composer (agent canvas) takes over private comments
              // entirely — team threads keep the built-in form.
              if (onExternalComment && visibility !== 'team') {
                onExternalComment()
                return
              }
              // The toolbar that owns this latch unmounts on the next line,
              // so nothing would ever clear it again.
              toolbarInteractingRef.current = false
              setCommentVisibility(visibility)
              setCommentDraft(visibility === 'team' ? teamCommentDraft : '')
              setIsCommenting(true)
            }}
            teamCommentEnabled={Boolean(onCreateTeamComment)}
          />
        )}
      </div>
    </BubbleMenu>
  )
}

function TeamMentionPicker({
  onChange,
  participants,
  selected,
}: {
  onChange: (selected: string[]) => void
  participants: readonly EditorCollaborationCommentActor[]
  selected: readonly string[]
}) {
  return (
    <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            aria-label="@Mention"
            className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
            size="icon"
            type="button"
            variant="ghost"
          >
            <AtSign className="icon-sm" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          {participants.map((participant) => {
            if (!participant.id) return null
            const active = selected.includes(participant.id)
            return (
              <DropdownMenuItem
                key={participant.id}
                onSelect={(event) => {
                  event.preventDefault()
                  onChange(active
                    ? selected.filter((id) => id !== participant.id)
                    : [...selected, participant.id!])
                }}
              >
                {participant.name}
                {active ? <Check className="ml-auto icon-sm" /> : null}
              </DropdownMenuItem>
            )
          })}
        </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** The chosen mentions as one line, empty when nothing is chosen. */
function mentionNames(
  selected: readonly string[],
  participants: readonly EditorCollaborationCommentActor[],
): string {
  return selected
    .map((id) => participants.find((participant) => participant.id === id)?.name)
    .filter(Boolean)
    .map((name) => `@${name}`)
    .join(' ')
}

function messageFromUnknown(error: unknown) {
  if (error instanceof Error) return error.message
  return String(error)
}
