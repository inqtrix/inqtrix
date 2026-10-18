import { useCallback, useEffect, useState } from 'react'

import type { EditorCollaborationCommentThread } from '@/api/inqtrixClient'
import type { ReferenceDoc } from '@/features/files/referenceBlocks'
import { countLabel } from '@/i18n/plural'
import { editorCopy } from './editorCopy'

type DiscussionThread = Pick<EditorCollaborationCommentThread, 'id' | 'messages' | 'quote'>

/** A team thread handed to the editor assistant with "Use with assistant".
 *
 * It is context for the user's next request, never part of its text: the
 * composer stays the user's own, and what to do with the discussion is what
 * the user writes there. */
export type HandedOverDiscussion = {
  /** The reference document every assistant request attaches while the
   * discussion stays handed over; `null` once its thread is gone. */
  document: ReferenceDoc | null
  label: string
  /** Messages that have not been deleted. */
  messageCount: number
  quote: string
  threadId: string
}

/** The reference document one handed-over thread becomes: the quoted passage
 * and every message that has not been deleted, each with its author. A guest
 * chooses the name it is shown with, so a guest's name is marked as one.
 *
 * Everything in it is other people's text: their messages, their names and
 * the quoted passage of the shared document. It travels as source material,
 * which the server fences as data, so none of it can direct the assistant,
 * and an `@files:` token in it stays text that refers to nothing. The label
 * is a fixed text of the editor, so no one else's text stands outside the
 * fence. */
export function teamDiscussionReferenceDoc(
  thread: Pick<DiscussionThread, 'messages' | 'quote'>,
  label: string,
  locale: 'de' | 'en',
): ReferenceDoc {
  const guest = editorCopy[locale].teamDiscussionGuest
  const quote = thread.quote.split('\n').map((line) => `> ${line}`).join('\n')
  const messages = thread.messages
    .filter((message) => message.deleted_at === null)
    .map((message) => {
      const author = message.author.kind === 'guest'
        ? `${message.author.name} (${guest})`
        : message.author.name
      return `${author}: ${message.body_markdown ?? ''}`
    })
  return { content: `${quote}\n\n${messages.join('\n')}`, label }
}

/** The handed-over discussions in hand-over order, numbered when there is
 * more than one, each built from its thread as it is now: a message deleted
 * since the hand-over is gone, a later reply is in. */
export function handedOverDiscussions(
  threadIds: readonly string[],
  threads: readonly DiscussionThread[],
  locale: 'de' | 'en',
): HandedOverDiscussion[] {
  const base = editorCopy[locale].teamDiscussion
  return threadIds.map((threadId, index) => {
    const label = threadIds.length > 1 ? `${base} ${index + 1}` : base
    const thread = threads.find((candidate) => candidate.id === threadId)
    if (!thread) return { document: null, label, messageCount: 0, quote: '', threadId }
    return {
      document: teamDiscussionReferenceDoc(thread, label, locale),
      label,
      messageCount: thread.messages.filter((message) => message.deleted_at === null).length,
      quote: thread.quote,
      threadId,
    }
  })
}

/** The reference documents an assistant request sends: the composer's own
 * attachments first, so the `[N]` its pills write keeps pointing where it
 * did, then every handed-over discussion. A discussion whose thread is gone
 * refuses the request; it never goes out without what the user handed over.
 *
 * @throws Error with the localized reason when a handed-over thread is gone. */
export function withHandedOverDiscussions(
  attachments: readonly ReferenceDoc[],
  discussions: readonly HandedOverDiscussion[],
  locale: 'de' | 'en',
): ReferenceDoc[] {
  const documents = discussions.map((discussion) => discussion.document)
  if (documents.some((document) => document === null)) {
    throw new Error(editorCopy[locale].handedOverDiscussionMissing)
  }
  return [
    ...attachments,
    ...documents.filter((document): document is ReferenceDoc => document !== null),
  ]
}

/** The instruction a document request sends while discussions are handed
 * over: the user's own text, then one sentence that names every handed-over
 * discussion by the `[N]` marker of the reference document it goes out as.
 *
 * The user writes what to do with the discussion, often only as "the
 * discussion". The server labels every reference document with its `[N]` and
 * tells the model that the instruction points at a document with that
 * marker, so without this sentence nothing ties the request to the
 * discussion, and the model can read it as a passage of the document. The
 * sentence is prompt text, so it is English in every interface language. */
export function instructionWithHandedOverDiscussions(
  instruction: string,
  attachments: readonly ReferenceDoc[],
  discussions: readonly HandedOverDiscussion[],
): string {
  const markers = discussions
    .map((discussion) => (discussion.document ? attachments.indexOf(discussion.document) : -1))
    .filter((index) => index >= 0)
    .map((index) => `[${index + 1}]`)
  if (markers.length === 0) return instruction
  const pointer = countLabel(
    markers.length,
    'The user handed over the team discussion {count} with this request; it is the material the request refers to.',
    'The user handed over the team discussions {count} with this request; they are the material the request refers to.',
    markers.join(', '),
  )
  return `${instruction}\n\n${pointer}`
}

const NO_THREADS: readonly string[] = []

/** The team threads handed to the assistant of one document.
 *
 * They belong to the document they were handed over in: another document
 * shows none of them, and they do not come back when the user returns to
 * it. Hiding and showing the assistant keeps them; a reload drops them, as
 * it drops the assistant's other attachments. A thread is handed over once. */
export function useHandedOverDiscussions(documentId: string | null) {
  const [handedOver, setHandedOver] = useState<{
    documentId: string | null
    threadIds: readonly string[]
  }>({ documentId, threadIds: NO_THREADS })
  useEffect(() => {
    setHandedOver((current) => (
      current.documentId === documentId ? current : { documentId, threadIds: NO_THREADS }
    ))
  }, [documentId])
  const threadIds = handedOver.documentId === documentId ? handedOver.threadIds : NO_THREADS
  const handOver = useCallback((threadId: string) => {
    setHandedOver((current) => {
      const held = current.documentId === documentId ? current.threadIds : NO_THREADS
      return { documentId, threadIds: held.includes(threadId) ? held : [...held, threadId] }
    })
  }, [documentId])
  const remove = useCallback((threadId: string) => {
    setHandedOver((current) => ({
      ...current,
      threadIds: current.threadIds.filter((id) => id !== threadId),
    }))
  }, [])
  /** Drops this document's discussions after a request carried them. */
  const clear = useCallback(() => {
    setHandedOver((current) => (
      current.documentId === documentId ? { documentId, threadIds: NO_THREADS } : current
    ))
  }, [documentId])
  return { clear, handOver, remove, threadIds }
}
