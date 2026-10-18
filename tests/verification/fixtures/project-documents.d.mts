export type OwnedProjectDocumentPage = {
  data: Array<{ id: string }>
  next_cursor: string | null
}

export function cleanupOwnedProjectDocuments(options: {
  deleteDocument: (documentId: string) => Promise<void>
  fetchPage: (cursor: string | null) => Promise<unknown>
}): Promise<number>
