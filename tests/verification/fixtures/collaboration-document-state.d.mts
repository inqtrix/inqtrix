export const LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS: 1500

export type LargeCollaborationDocumentSeed = {
  characterCount: number
  markdown: string
  paragraphCount: number
}

export function buildLargeCollaborationDocumentSeed(options: {
  runId: string
}): LargeCollaborationDocumentSeed
