import assert from 'node:assert/strict'
import test from 'node:test'

import { judgeCompleteReading, judgeRoleRelease } from './document-reading.mjs'

const original = { kind: 'knowledge_document', id: 'kd_guide', label: 'knowledge original' }

const reading = (overrides = {}) => ({
  kind: 'knowledge_document',
  source_id: 'kd_guide',
  total: 1_000,
  read_ranges: [[0, 1_000]],
  complete: true,
  continuation_open: false,
  ...overrides,
})

const analysis = (overrides = {}) => ({
  document_id: 'kd_guide',
  version: 'v1',
  total_chars: 1_000,
  complete: true,
  analysed_ranges: [[0, 1_000]],
  unanalysed: [],
  ...overrides,
})

test('a complete reading takes in the whole document, whether or not an analysis counts', () => {
  const execution = { source_coverage: [reading()], document_analyses: [] }
  assert.deepEqual(judgeCompleteReading(execution, original), { status: 'passed', by: 'reading' })
  assert.deepEqual(judgeCompleteReading(execution, original, { analysisCounts: true }), { status: 'passed', by: 'reading' })
})

test('a complete analysis takes in the whole document only where an analysis counts', () => {
  const execution = { source_coverage: [], document_analyses: [analysis()] }
  assert.deepEqual(judgeCompleteReading(execution, original, { analysisCounts: true }), {
    status: 'passed', by: 'analysis', onlyAnalysis: true,
  })
  const page = judgeCompleteReading(execution, original)
  assert.equal(page.status, 'failed')
  assert.equal(page.problem, 'never read the selected knowledge original.')
})

test('a complete analysis completes a partial reading of the same document', () => {
  const execution = {
    source_coverage: [reading({ read_ranges: [[0, 400]], complete: false, continuation_open: true })],
    document_analyses: [analysis()],
  }
  // The partial reading released the document already; the analysis is not
  // the only way the turn took it in.
  assert.deepEqual(judgeCompleteReading(execution, original, { analysisCounts: true }), {
    status: 'passed', by: 'analysis', onlyAnalysis: false,
  })
})

test('a coverage entry without a read range leaves the analysis the only way in', () => {
  const execution = {
    source_coverage: [reading({ read_ranges: [], complete: false, continuation_open: false })],
    document_analyses: [analysis()],
  }
  assert.deepEqual(judgeCompleteReading(execution, original, { analysisCounts: true }), {
    status: 'passed', by: 'analysis', onlyAnalysis: true,
  })
})

test('a reading that left a continuation open is not complete', () => {
  const execution = { source_coverage: [reading({ continuation_open: true })], document_analyses: [] }
  const verdict = judgeCompleteReading(execution, original)
  assert.equal(verdict.status, 'failed')
  assert.equal(verdict.problem, 'did not take in the knowledge original completely: [[0,1000]] of 1000 read.')
})

test('only an analysis of the asked document counts, and only a complete one', () => {
  const other = { source_coverage: [], document_analyses: [analysis({ document_id: 'kd_other' })] }
  assert.equal(judgeCompleteReading(other, original, { analysisCounts: true }).problem,
    'never read or analysed the selected knowledge original.')
  const partial = {
    source_coverage: [],
    document_analyses: [analysis({ complete: false, analysed_ranges: [[0, 600]] })],
  }
  assert.equal(judgeCompleteReading(partial, original, { analysisCounts: true }).problem,
    'did not take in the knowledge original completely: nothing read, [[[0,600]]] of 1000 analysed.')
})

test('a second, complete analysis of the same document counts', () => {
  const execution = {
    source_coverage: [],
    document_analyses: [analysis({ complete: false, analysed_ranges: [[0, 600]] }), analysis()],
  }
  assert.deepEqual(judgeCompleteReading(execution, original, { analysisCounts: true }), {
    status: 'passed', by: 'analysis', onlyAnalysis: true,
  })
})

test('an analysis never stands for a source the analysis cannot read', () => {
  const editor = { kind: 'editor_document', id: 'kd_guide', label: 'editor working version' }
  const execution = { source_coverage: [], document_analyses: [analysis()] }
  assert.equal(judgeCompleteReading(execution, editor, { analysisCounts: true }).problem,
    'never read the selected editor working version.')
})

test('a run without published coverage fails', () => {
  assert.deepEqual(judgeCompleteReading({ document_analyses: [analysis()] }, original, { analysisCounts: true }),
    { status: 'failed', problem: 'published no source coverage.' })
  assert.equal(judgeCompleteReading(undefined, original).status, 'failed')
})

const guide = { id: 'kd_guide', label: 'knowledge original', by: 'analysis', onlyAnalysis: true }
const attachment = { id: 'kd_attachment', label: 'file attachment', by: 'reading', onlyAnalysis: false }
// The kernel's refusal as the role's tool result shows it.
const refusal = (tool) => ({
  tool,
  status: 'error',
  result_preview: 'Tool blocked: This source is not part of your handover. A role re-reads what the assigning run released; name the gap in your result instead.',
})

test('a refused document read beside a document of the turn the role never re-read fails the role', () => {
  const finished = [
    { tool: 'read_evidence', status: 'success', result_preview: 'Evidence W1 …' },
    refusal('read_project_document'),
  ]
  assert.deepEqual(judgeRoleRelease([guide, attachment], [['knowledge_document', 'kd_attachment']], finished), {
    status: 'failed',
    problem: 'was refused 1 knowledge-document read(s) and never re-read the knowledge original, '
      + 'so its handover may withhold a document of the turn.',
  })
})

test('a refused read beside a complete re-read of the turn is the contract working, returned for a warning', () => {
  const verdict = judgeRoleRelease(
    [guide, attachment],
    [['knowledge_document', 'kd_guide'], ['knowledge_document', 'kd_attachment']],
    [refusal('read_project_document'), refusal('read_canvas')],
  )
  assert.equal(verdict.status, 'passed')
  assert.deepEqual(verdict.refused, ['read_project_document', 'read_canvas'])
})

test('a refused read of another kind of source never stands for a document of the turn', () => {
  const verdict = judgeRoleRelease([guide], [], [refusal('read_canvas'), refusal('read_context')])
  assert.deepEqual(verdict, { status: 'passed', reread: [], refused: ['read_canvas', 'read_context'] })
})

test('an analysed document the role re-read shows the release, with how the turn took it in', () => {
  const verdict = judgeRoleRelease(
    [guide, attachment],
    [['knowledge_document', 'kd_guide'], ['knowledge_document', 'kd_attachment']],
    [{ tool: 'read_project_document', status: 'success', result_preview: 'Text representation: knowledge_text' }],
  )
  assert.deepEqual(verdict, {
    status: 'passed',
    reread: [
      { label: 'knowledge original', by: 'analysis', onlyAnalysis: true },
      { label: 'file attachment', by: 'reading', onlyAnalysis: false },
    ],
    refused: [],
  })
})

test('only documents of the turn the role read count, and only as knowledge documents', () => {
  const verdict = judgeRoleRelease(
    [guide, attachment],
    [['conversation_source', 'kd_guide'], ['knowledge_document', 'kd_other']],
    [],
  )
  assert.deepEqual(verdict, { status: 'passed', reread: [], refused: [] })
})

test('a read that failed for another reason is no refusal of the handover', () => {
  const finished = [{
    tool: 'read_project_document',
    status: 'error',
    result_preview: 'Tool error (context.invalid_read): A continuation requires the source version and content fingerprint.',
  }]
  assert.deepEqual(judgeRoleRelease([guide], [], finished), { status: 'passed', reread: [], refused: [] })
})
