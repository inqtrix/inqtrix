/**
 * The phrase of the kernel's refusal when a role reads outside its handover
 * (`KernelDeps.require_released`). A test of the work profiles reads it from
 * this file and checks it against the refusal the kernel raises, so a reworded
 * refusal turns that test red instead of silencing the check below.
 */
export const HANDOVER_REFUSAL = 'not part of your handover'

/**
 * Whether a document turn took in the WHOLE source it was asked about.
 *
 * The kernel publishes what the model read per source version
 * (`execution.source_coverage`). A turn may instead have the server analyse
 * a knowledge document: `execution.document_analyses` lists each analysed
 * document with whether every window was analysed. Where a question asks for
 * facts out of a whole knowledge document, a complete analysis answers it as
 * well as a complete reading (`analysisCounts`); where it asks for the text
 * itself, a page-by-page summary say, only the reading does.
 *
 * Returns `{ status: 'passed', by: 'reading' }`,
 * `{ status: 'passed', by: 'analysis', onlyAnalysis }` or
 * `{ status: 'failed', problem }`. `onlyAnalysis` is true when the turn read
 * no range of the document at all, so the analysis is the only way it took
 * the document in. `problem` completes a sentence that starts with the turn's
 * marker.
 */
export function judgeCompleteReading(execution, expected, { analysisCounts = false } = {}) {
  const coverage = execution?.source_coverage
  if (!Array.isArray(coverage)) return { status: 'failed', problem: 'published no source coverage.' }
  const entry = coverage.find((row) => row.kind === expected.kind && row.source_id === expected.id)
  if (entry?.complete === true && entry.continuation_open === false) return { status: 'passed', by: 'reading' }
  // The analysis reads knowledge documents only.
  const analysable = analysisCounts && expected.kind === 'knowledge_document'
  const analyses = analysable
    ? (execution.document_analyses ?? []).filter((row) => row.document_id === expected.id)
    : []
  if (analyses.some((row) => row.complete === true)) {
    return { status: 'passed', by: 'analysis', onlyAnalysis: !(entry?.read_ranges?.length > 0) }
  }
  if (!entry && analyses.length === 0) {
    return { status: 'failed', problem: `never ${analysable ? 'read or analysed' : 'read'} the selected ${expected.label}.` }
  }
  const read = entry ? `${JSON.stringify(entry.read_ranges)} of ${entry.total} read` : 'nothing read'
  const analysed = analyses.length > 0
    ? `, ${JSON.stringify(analyses.map((row) => row.analysed_ranges))} of ${analyses[0].total_chars} analysed`
    : ''
  return { status: 'failed', problem: `did not take in the ${expected.label} completely: ${read}${analysed}.` }
}

/**
 * What the review role re-read of its turn, and whether its handover withheld
 * a document of the turn.
 *
 * A role re-reads only the sources its handover releases. A turn releases
 * every document it read a range of, and a knowledge document it had the
 * server analyse as well. The handover itself is not published; the role's
 * own reads show it. A read outside the handover is refused with a visible
 * result that tells the role to name the gap instead, so a refused read is the
 * contract working unless it withheld a document of the turn. The events of
 * a document read do not name its target: a refused knowledge-document read
 * (`read_project_document`) counts against the release only together with a
 * document of the turn the role did not re-read, and every other refusal is
 * returned for a warning.
 *
 * `takenIn` lists the documents the turn took in completely as
 * `{ id, label, by, onlyAnalysis }`, as `judgeCompleteReading` decided;
 * `readBeforeResult` holds the role's `read_before_result` (`[kind,
 * source_id]` pairs, each read successfully); `finishedTools` holds the
 * `data` of the role's `inqtrix.agent.tool.finished` events.
 *
 * Returns `{ status: 'passed', reread, refused }`, `reread` listing the
 * turn's documents the role read before its verdict as `{ label, by,
 * onlyAnalysis }` and `refused` the tool of every refused read, or
 * `{ status: 'failed', problem }`; `problem` completes a sentence that starts
 * with "The review role".
 */
export function judgeRoleRelease(takenIn, readBeforeResult, finishedTools) {
  const refused = finishedTools
    .filter((item) => String(item.result_preview ?? '').includes(HANDOVER_REFUSAL))
    .map((item) => item.tool)
  const read = new Set(
    (readBeforeResult ?? []).filter(([kind]) => kind === 'knowledge_document').map(([, id]) => id),
  )
  const missed = takenIn.filter((doc) => !read.has(doc.id))
  const refusedDocumentReads = refused.filter((tool) => tool === 'read_project_document').length
  if (refusedDocumentReads > 0 && missed.length > 0) {
    return {
      status: 'failed',
      problem: `was refused ${refusedDocumentReads} knowledge-document read(s) and never re-read the `
        + `${missed.map((doc) => doc.label).join(' or the ')}, so its handover may withhold a document of the turn.`,
    }
  }
  return {
    status: 'passed',
    reread: takenIn.filter((doc) => read.has(doc.id))
      .map((doc) => ({ label: doc.label, by: doc.by, onlyAnalysis: doc.onlyAnalysis === true })),
    refused,
  }
}
