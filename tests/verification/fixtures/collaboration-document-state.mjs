import { assertVerificationRunId } from './run-scope.mjs'

export const LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS = 1_500

export function buildLargeCollaborationDocumentSeed({ runId }) {
  assertVerificationRunId(runId)
  const paragraphs = Array.from(
    { length: LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS },
    (_, index) => (
      `inqtrix-load-seed-${runId}-${String(index + 1).padStart(4, '0')} `
      + 'stable collaboration marker.'
    ),
  )
  const markdown = `# System\n\n${paragraphs.join('\n\n')}`
  return {
    characterCount: markdown.length,
    markdown,
    paragraphCount: paragraphs.length,
  }
}
