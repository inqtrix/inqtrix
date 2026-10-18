import {
  getEditorDocument,
  type ClientOptions,
  type ServerEditorDocument,
} from '@/api/inqtrixClient'
import { documentRecordFromServer } from '@/features/editor/editorSync'
import type { EditorDocumentRecord } from '@/features/project/types'

type EditorDocumentLoader = (
  documentId: string,
  options: ClientOptions,
) => Promise<ServerEditorDocument>

/** Resolve an inbox target from exact server detail, never metadata cache. */
export async function hydrateSharedEditorTarget(
  documentId: string,
  options: ClientOptions,
  locale: 'de' | 'en',
  loadDocument: EditorDocumentLoader = getEditorDocument,
): Promise<EditorDocumentRecord> {
  const serverDocument = await loadDocument(documentId, options)
  if (options.signal?.aborted) throw abortError()
  if (
    serverDocument.content_mode === 'collaboration'
    && typeof serverDocument.content_markdown !== 'string'
  ) {
    throw new Error(locale === 'de'
      ? 'Das geteilte Collaboration-Dokument enthält keine vollständige Projektion.'
      : 'The shared collaboration document does not include a complete projection.')
  }
  const document = documentRecordFromServer(serverDocument)
  if (document.id !== documentId || document.access?.mode !== 'shared') {
    throw new Error(locale === 'de'
      ? 'Die Serverantwort enthält kein gültiges geteiltes Editor-Dokument.'
      : 'The server response does not contain a valid shared editor document.')
  }
  return document
}

function abortError(): Error {
  if (typeof DOMException === 'function') {
    return new DOMException('The shared editor target changed.', 'AbortError')
  }
  const error = new Error('The shared editor target changed.')
  error.name = 'AbortError'
  return error
}
