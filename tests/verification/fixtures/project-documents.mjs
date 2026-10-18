export async function cleanupOwnedProjectDocuments({
  deleteDocument,
  fetchPage,
}) {
  if (typeof deleteDocument !== 'function' || typeof fetchPage !== 'function') {
    throw new Error('Owned-project cleanup requires fetch and delete operations.')
  }

  const cursors = new Set()
  const documentIds = []
  const seenDocumentIds = new Set()
  let cursor = null

  while (true) {
    const page = ownedDocumentPage(await fetchPage(cursor))
    for (const row of page.data) {
      if (seenDocumentIds.has(row.id)) continue
      seenDocumentIds.add(row.id)
      documentIds.push(row.id)
    }
    if (page.next_cursor === null) break
    if (cursors.has(page.next_cursor)) {
      throw new Error('Owned-project cleanup received a repeated page cursor.')
    }
    cursors.add(page.next_cursor)
    cursor = page.next_cursor
  }

  let firstFailure = null
  for (const documentId of documentIds) {
    try {
      await deleteDocument(documentId)
    } catch (error) {
      firstFailure ??= error
    }
  }
  if (firstFailure) throw firstFailure
  return documentIds.length
}

function ownedDocumentPage(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.data)) {
    throw new Error('Owned-project cleanup received an invalid document page.')
  }
  const data = value.data.map((row) => {
    if (!row || typeof row !== 'object' || typeof row.id !== 'string' || !row.id) {
      throw new Error('Owned-project cleanup received a document without an ID.')
    }
    return { id: row.id }
  })
  if (
    value.next_cursor !== null
    && (typeof value.next_cursor !== 'string' || !value.next_cursor)
  ) {
    throw new Error('Owned-project cleanup received an invalid page cursor.')
  }
  return { data, next_cursor: value.next_cursor }
}
