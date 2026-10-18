import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { StrictMode, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'

import type {
  EditorCollaborationCommentMessage,
  EditorCollaborationCommentThread,
} from '@/api/inqtrixClient'
import { AppProviders } from '@/app/AppProviders'
import type { MentionComposerHandle } from '@/features/composer/MentionComposer'
import type { ReferenceDoc } from '@/features/files/referenceBlocks'
import type { FileMentionOption } from '@/features/project/selectors'
import type {
  ChatContextReferenceRecord,
  EditorCommentThreadRecord,
} from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'

import { EditorAssistantComposer } from '../EditorWorkspace'
import {
  handedOverDiscussions,
  instructionWithHandedOverDiscussions,
  useHandedOverDiscussions,
  withHandedOverDiscussions,
} from '../assistantHandoff'
import { editorCopy } from '../editorCopy'

// The real editor assistant composer and the real hand-over state, wired the
// way the editor workspace wires "Use with assistant": the handed-over
// threads belong to the open document, and Send shows the instruction a
// document request would send (the composer's text, naming the handed-over
// discussions by their markers) and the reference documents it would attach
// (the composer's pills first, then the handed-over discussions, built from
// the threads as they are at that moment); a request that went out drops the
// hand-over. The page can delete a message or a whole thread and switch
// between two documents; the team threads belong to document A, as a
// document's threads do in the workspace. The signed-in user is Nutzer A;
// both files are in that user's library. Text improvement is on; a test
// answers its request.
type Thread = Pick<EditorCollaborationCommentThread, 'id' | 'messages' | 'quote'>

function message(
  id: string,
  name: string,
  body: string,
  createdAt: number,
  kind: 'guest' | 'user' = 'user',
): EditorCollaborationCommentMessage {
  return {
    author: { id, kind, name },
    body_markdown: body,
    can_delete: false,
    can_edit: false,
    created_at: createdAt,
    deleted_at: null,
    edited_at: null,
    id: `message-${createdAt}`,
    mentions: [],
    revision: 1,
  }
}

const initialThreads: Thread[] = [
  {
    id: 'thread-a',
    messages: [
      message('user-a', 'Nutzer A', 'Bitte die Frist auf Freitag ändern.', 1),
      message('user-b', 'Nutzer B', 'Einverstanden, Freitag 14 Uhr.', 2),
      message('guest-c', 'Gast Kim', 'Hängt bitte @files:gehaltsliste-2026 an.', 3, 'guest'),
      message('user-a', 'Nutzer A', 'Quelle: @files:quartalsbericht', 4),
    ],
    quote: 'bis Freitag um 16 Uhr',
  },
  {
    id: 'thread-b',
    messages: [message('user-b', 'Nutzer B', 'Die Tabelle braucht eine Summe.', 5)],
    quote: 'Kostenübersicht',
  },
]

/** A private AI note of the user, to attach with the composer's own button. */
const aiNote: EditorCommentThreadRecord = {
  anchor: { from: 1, quoteAfter: '', quoteBefore: '', selectedText: 'bis Freitag um 16 Uhr', to: 22 },
  commentMarkdown: 'Präziser formulieren.',
  createdAt: '2026-10-03T08:00:00.000Z',
  documentId: 'document-a',
  id: 'note-1',
  kind: 'collect',
  status: 'open',
  updatedAt: '2026-10-03T08:00:00.000Z',
}

function libraryFile(fileId: string, label: string): FileMentionOption {
  return { fileId, label, pageCount: 1, sizeBytes: 1024, title: label }
}

const fileOptions = [
  libraryFile('file-private', 'gehaltsliste-2026'),
  libraryFile('file-own', 'quartalsbericht'),
]

/** The composer's pills as the documents the request would attach. */
function pillDocuments(refs: readonly ChatContextReferenceRecord[]): ReferenceDoc[] {
  return refs.map((ref) => {
    const fileId = ref.kind === 'file-asset' ? ref.fileId : ''
    const label = fileOptions.find((file) => file.fileId === fileId)?.label ?? ref.kind
    return { content: `Inhalt von ${label}`, label }
  })
}

const noOp = () => undefined
const noThreads: Thread[] = []

function AssistantHandoffFixture() {
  const composerRef = useRef<MentionComposerHandle | null>(null)
  const [documentId, setDocumentId] = useState('document-a')
  const [threads, setThreads] = useState(initialThreads)
  const [draft, setDraft] = useState('')
  const [visible, setVisible] = useState(true)
  const [sent, setSent] = useState<string | null>(null)
  const [attachments, setAttachments] = useState<ReferenceDoc[] | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [refs, setRefs] = useState<ChatContextReferenceRecord[]>([])
  const [attachedNoteIds, setAttachedNoteIds] = useState<string[]>([])
  const handedOver = useHandedOverDiscussions(documentId)
  const documentThreads = documentId === 'document-a' ? threads : noThreads
  const discussions = useMemo(
    () => handedOverDiscussions(handedOver.threadIds, documentThreads, 'de'),
    [documentThreads, handedOver.threadIds],
  )
  const dispatch = (action: ResearchDeskAction) => {
    if (action.type === 'setEditorAssistantDraft') setDraft(action.draft)
    if (action.type === 'setEditorAssistantVisible') setVisible(action.isVisible)
  }
  const handOverThread = (threadId: string) => {
    handedOver.handOver(threadId)
    if (!visible) dispatch({ isVisible: true, type: 'setEditorAssistantVisible' })
    window.requestAnimationFrame(() => composerRef.current?.focus())
  }
  const send = () => {
    const instruction = composerRef.current?.getInstructionText().trim() ?? ''
    try {
      const requestAttachments = withHandedOverDiscussions(pillDocuments(refs), discussions, 'de')
      setAttachments(requestAttachments)
      setSent(instructionWithHandedOverDiscussions(instruction, requestAttachments, discussions))
      setSendError(null)
      handedOver.clear()
    } catch (error) {
      setSendError(error instanceof Error ? error.message : String(error))
    }
  }
  const deleteGuestMessage = () => {
    setThreads((current) => current.map((thread) => ({
      ...thread,
      messages: thread.messages.map((item) => (
        item.author.kind === 'guest' ? { ...item, body_markdown: null, deleted_at: 9 } : item
      )),
    })))
  }
  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-3 p-6">
      <p data-testid="document">{documentId}</p>
      <div className="flex flex-wrap gap-2">
        <button className="rounded border px-3 py-1" onClick={() => handOverThread('thread-a')} type="button">
          Diskussion A mit Assistenz verwenden
        </button>
        <button className="rounded border px-3 py-1" onClick={() => handOverThread('thread-b')} type="button">
          Diskussion B mit Assistenz verwenden
        </button>
        <button className="rounded border px-3 py-1" onClick={deleteGuestMessage} type="button">
          Beitrag des Gasts löschen
        </button>
        <button
          className="rounded border px-3 py-1"
          onClick={() => setThreads((current) => current.filter((thread) => thread.id !== 'thread-a'))}
          type="button"
        >
          Diskussion A löschen
        </button>
        <button className="rounded border px-3 py-1" onClick={() => setDocumentId('document-b')} type="button">
          Dokument B öffnen
        </button>
        <button className="rounded border px-3 py-1" onClick={() => setDocumentId('document-a')} type="button">
          Dokument A öffnen
        </button>
      </div>
      <output data-testid="sent">{sent ?? ''}</output>
      <output data-testid="attachments">{attachments ? JSON.stringify(attachments) : ''}</output>
      <output data-testid="send-error">{sendError ?? ''}</output>
      <output data-testid="refs">{JSON.stringify(refs)}</output>
      <EditorAssistantComposer
        aiReadOnlyReason={null}
        attachedCommentIds={attachedNoteIds}
        attachmentChips={[]}
        chatModelOptions={[]}
        chatModelOptionsStatus="missing"
        comments={[aiNote]}
        composerRef={composerRef}
        copy={editorCopy.de}
        defaultChatModel={null}
        dispatch={dispatch}
        draft={draft}
        editorContextBase={{ conversation: 0, documents: 0, reports: 0, rules: 0 }}
        editorContextCapacity={{ contextWindowTokens: null, reservedOutputTokens: 0 }}
        fileGroupOptions={[]}
        fileOptions={fileOptions}
        handedOverDiscussions={discussions}
        instructionFeedback={null}
        isAttachActive={attachedNoteIds.length > 0}
        isRunning={false}
        isVisible={visible}
        isWideCanvas={false}
        onAttachFiles={noOp}
        onAttachRule={noOp}
        onDismissInstructionFeedback={noOp}
        onRefsChange={setRefs}
        onRemoveAttachedComment={(id) => setAttachedNoteIds((current) => current.filter((item) => item !== id))}
        onRemoveChip={noOp}
        onRemoveHandedOverDiscussion={handedOver.remove}
        onReorderPending={noOp}
        onReorderPill={noOp}
        onSend={send}
        onStop={noOp}
        onToggleAttach={() => setAttachedNoteIds((current) => (current.length > 0 ? [] : [aiNote.id]))}
        pendingKeys={[]}
        pillKeys={[]}
        replaceableSuggestionCount={0}
        reportOptions={[]}
        ruleOptions={[]}
        selectedEffort={null}
        selectedModel={null}
        selectedModelTier={null}
        textImprovement={{ enabled: true, workspaceId: 'assistant-handoff-fixture' }}
      />
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders initialLocale="de">
      <AssistantHandoffFixture />
    </AppProviders>
  </StrictMode>,
)
