/**
 * Wire shape for an attached reference document (file or file group) sent to the
 * backend editor endpoints. The chat inline path labels its own context blocks
 * with matching `[N]` markers in `contentWithAttachmentContext`; the editor path
 * sends these DTOs as `attachments` and the backend renders the delimiter-wrapped
 * `<reference_documents>` block (see `server/reference_documents.py`).
 */
export type ReferenceDoc = {
  content: string
  label: string
  pageCount?: number | null
  sizeBytes?: number
}
