import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import type { AgentRunOutcome } from '../model'
import { documentOutcome } from '../runPresentation'

// Source pin for effect wiring no node-environment test can mount (this
// repo's vitest lane is DOM-less by design; component rendering is
// verified in the browser). The channel RESOLVES its promise after
// calling onUnavailable, so the .then handler runs one microtask after
// the error state was set — without the guard it overwrites 'error' with
// a calm 'settled', silencing the destructive styling the adjacent
// comment promises. This pin fails when `&& !unavailable` is dropped from
// the settled transition.
describe('TaskDetailView child transport wiring', () => {
  const source = readFileSync(
    path.resolve(__dirname, './views.tsx'),
    'utf-8',
  )

  it('latches unavailability before the channel resolves', () => {
    expect(source).toContain('let unavailable = false')
    const handler = source.slice(source.indexOf('onUnavailable: () => {'))
    const handlerBody = handler.slice(0, handler.indexOf('},'))
    expect(handlerBody).toContain('unavailable = true')
    expect(handlerBody).toContain("setTransport('error')")
  })

  it("guards the settled transition on the latch, not only on abort", () => {
    expect(source).toMatch(
      /!controller\.signal\.aborted && !unavailable/,
    )
  })
})

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

// Source pin (DOM-less lane): the run view cannot mount here. It groups its
// tasks with the exported grouping the reducer tests exercise, never with a
// local copy that could read the evidence note as a fallback again.
describe('run view task groups', () => {
  it('groups the tasks with the shared grouping', () => {
    const source = readFileSync(path.resolve(__dirname, './views.tsx'), 'utf-8')
    expect(source).toContain('const groups = agentTaskGroups(tasks, run.taskStates)')
  })
})

// Source pin (DOM-less lane): the task detail view cannot mount here. It
// shows an evidence limit as the same note as the task card, and its
// operations list marks the note's row with the note icon instead of the
// fallback warning.
describe('TaskDetailView evidence note', () => {
  const source = readFileSync(path.resolve(__dirname, './views.tsx'), 'utf-8')
  const detail = source.slice(
    source.indexOf('function TaskDetailView('),
    source.indexOf('function TaskResultSources('),
  )

  it('shows the evidence note in the detail view', () => {
    expect(detail).toMatch(
      /\{live\?\.evidenceLimit && \(\s*<p[^>]*text-muted-foreground">\s*<Info [^>]*\/>\s*\{t\.agent\.canvas\.child\.evidenceLimitNote\}/,
    )
  })

  it('marks an evidence-note row with the note icon, not the warning', () => {
    expect(detail).toMatch(/\) : activity\.evidenceLimit \? \(\s*<Info /)
  })

  it('never shows an evidence-note row of the operations list as a warning', () => {
    // The row warns (warning icon and colour) on an error, a fallback or a
    // failed step, nothing else: an evidence limit added here would show the
    // note as a warning again. A reworded condition must be caught here.
    const conditions = [...detail.matchAll(/const attention = Boolean\(([^)]*)\)/g)]
    expect(conditions).toHaveLength(1)
    const terms = conditions[0][1]
      .replace(/,\s*$/, '')
      .split('||')
      .map((term) => term.trim())
    expect(terms).toEqual(['activityError', 'activity.fallback', "activity.status === 'failed'"])
  })
})

// Source pin (DOM-less lane): the plan gate's rule chips are built from the
// catalog AND the requirement in force. Without the second half a rule the
// run is frozen under — edited, unshared or deleted in the library since —
// disappears from the gate, and the next free-text edit removes it.
describe('PlanCanvasView offers the rules in force', () => {
  const source = readFileSync(
    path.resolve(__dirname, './views.tsx'),
    'utf-8',
  ).replace(/\s+/g, '')

  it('passes the pending gate\'s requirement in force to the rule chips', () => {
    expect(source).toContain(
      'reportRuleOptions={gateRuleOptions(context.reportRuleOptions,planApproval.inForce)}',
    )
  })
})
