import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import type { AgentRunOutcome } from '../model'
import { documentOutcome } from '../runPresentation'

// Source pin (DOM-less lane): the document canvas must request the body
// even when the anchor run is not a desk record — a delegated child's
// memo has no run record, and an `if (!run) return` guard would leave
// the canvas on "Hier entsteht das Memo." forever.
describe('DocumentCanvasView loads without a desk record', () => {
  const source = readFileSync(
    path.resolve(__dirname, './views.tsx'),
    'utf-8',
  )

  it('requests the body regardless of a loaded run record', () => {
    const start = source.indexOf('// The body loads on demand')
    const effect = source.slice(start, source.indexOf('}, [', start))
    expect(effect).not.toContain('if (!run) return')
    expect(effect).toContain(
      'loadDocumentBody(effectiveRunId, descriptor.artifactId)',
    )
  })

  it('renders the placeholder only while the artifact itself is missing', () => {
    expect(source).toMatch(
      /if \(!artifact\) \{\s*return <CanvasMissing label=\{t\.agent\.canvas\.empty\}/,
    )
    expect(source).not.toContain('if (!run || !artifact)')
  })
})

describe('DocumentCanvasView outcome line', () => {
  const record: AgentRunOutcome = {
    outcomeId: 'run_doc:outcome', final: 'completed', review: 'completed',
    summary: 'Geprüft · ohne Befund', applied: 0, lines: [], marks: [],
  }

  it.each([
    ['completed', 'agent', true],
    ['unavailable', 'agent', true],
    ['waived', 'agent', false],
    ['not_run', 'agent', false],
    ['completed', 'user', false],
  ] as const)(
    "shows the anchor run's outcome where a review ran or was owed and hides it after a user edit (%s, %s)",
    (review, updatedBy, shown) => {
      // The record describes the text the run delivered; after the user
      // edited the document it would describe text that is gone. A Stufe
      // that waived the review and a run that ended without one owe the
      // reader no line.
      expect(documentOutcome({ ...record, review }, { updatedBy }) !== undefined).toBe(shown)
    },
  )

  it('renders the line from that rule and from the anchor run alone', () => {
    // Source pin (DOM-less lane): the view must not bypass the rule.
    const source = readFileSync(path.resolve(__dirname, './views.tsx'), 'utf-8')
    expect(source).toContain('const outcome = artifact ? documentOutcome(run?.outcome, artifact) : undefined')
    expect(source).toContain('<RunOutcomeLine className="mt-4" outcome={outcome} t={t} />')
    // Its marks come from that shown record, and only those of this text.
    expect(source).toContain('artifactClaimMarks(outcome, artifact)')
    expect(source).toContain('claimMarks={claimMarks}')
  })
})
