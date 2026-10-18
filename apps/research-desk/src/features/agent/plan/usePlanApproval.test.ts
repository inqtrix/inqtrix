import { describe, expect, it } from 'vitest'

import type { AgentPlanRecord } from '../model'
import {
  approvalDecisionRequest,
  draftFromPlan,
  draftToWirePlan,
  planDraftBase,
  planDraftDiffers,
} from './usePlanApproval'

function plan(): AgentPlanRecord {
  return {
    assumptions: [],
    createdAt: 1,
    createdBy: 'agent',
    planId: 'p1',
    reason: '',
    status: 'pending',
    successCriteria: [],
    summaryMarkdown: '',
    tasks: [{
      budget: { timeout_seconds: 99 },
      childRunId: null,
      dependsOn: [],
      expectedOutput: '',
      gapIds: [],
      objective: 'Research one topic',
      ordinal: 0,
      params: { profile: 'compact' },
      queries: ['Question'],
      resultSummary: '',
      status: 'pending',
      taskId: 't1',
      title: 'Research',
      toolKind: 'web_research',
    }],
    version: 1,
    versions: [],
  }
}

describe('plan draft normalization', () => {
  it('preserves the server-stamped profile and strips legacy budgets', () => {
    // The server already stamped the tier-correct profile; the draft must
    // never silently deepen it (that would also turn approve into edit).
    const deepDraft = draftFromPlan(plan(), 'deep')
    expect(deepDraft.tasks[0].params.profile).toBe('compact')
    expect(deepDraft.tasks[0].budget).toEqual({})
    const wire = draftToWirePlan(deepDraft) as {
      tasks: Array<{ budget: Record<string, unknown>; params: Record<string, unknown> }>
    }
    expect(wire.tasks[0]).toMatchObject({ budget: {}, params: { profile: 'compact' } })
  })

  it('fills only a MISSING research profile from the run depth', () => {
    const bare = plan()
    bare.tasks[0].params = {}
    expect(draftFromPlan(bare, 'deep').tasks[0].params.profile).toBe('deep')
    expect(draftFromPlan(bare).tasks[0].params.profile).toBe('compact')
  })

  it('does not turn budget cleanup alone into an edited approval', () => {
    expect(planDraftDiffers(draftFromPlan(plan()), plan())).toBe(false)
  })

  it('keeps the schnell profile as a plain approve, not an edit', () => {
    const fast = plan()
    fast.tasks[0].params.profile = 'schnell'
    expect(planDraftDiffers(draftFromPlan(fast), fast)).toBe(false)
  })
})


describe('approval decision body', () => {
  // These cases are all about a requirement the user set OR emptied at
  // the gate — both are touches, and only a touch travels.
  const draft = (reportGuidance: string) => ({
    ...draftFromPlan(plan(), undefined),
    reportGuidance,
    reportRequirementTouched: true,
  })

  it('says nothing about the requirement when the gate was not touched', () => {
    // The gate draft always starts empty. Sending the field anyway meant
    // a plain click on Freigeben transmitted report_guidance:'' — which
    // the server reads as "clear it" — and silently deleted a
    // requirement the user had set in the composer before the run, and
    // which this gate never even showed them.
    const untouched = draftFromPlan(plan(), undefined)
    const body = approvalDecisionRequest({
      decision: 'approve',
      draft: untouched,
      edited: false,
    }) as Record<string, unknown>
    expect('report_guidance' in body).toBe(false)
    expect('report_rule_ids' in body).toBe(false)
  })

  it('sends an emptied requirement so it can be withdrawn', () => {
    // Truthiness made a once-set requirement permanent: the cleared
    // field never left the client, and the old wording kept shaping
    // every later revision.
    expect(
      approvalDecisionRequest({
        decision: 'approve',
        draft: draft(''),
        edited: false,
      }),
    ).toEqual({
      decision: 'approve',
      report_guidance: '',
      report_rule_ids: [],
    })
  })

  it('trims the requirement it does send', () => {
    expect(
      approvalDecisionRequest({
        decision: 'approve',
        draft: draft('  Als Tabelle.  '),
        edited: false,
      }).report_guidance,
    ).toBe('Als Tabelle.')
  })

  it('carries the rejection note the gate asks for', () => {
    expect(
      approvalDecisionRequest({
        decision: 'reject',
        draft: draft('Als Tabelle.'),
        edited: false,
        note: '  Bitte ohne Websuche.  ',
      }),
    ).toEqual({ decision: 'reject', note: 'Bitte ohne Websuche.' })
  })

  it('never attaches a requirement to a rejection', () => {
    // The server rejects guidance on anything but an approve, and a
    // rejection restarts the planning anyway.
    expect(
      approvalDecisionRequest({
        decision: 'reject',
        draft: draft('Als Tabelle.'),
        edited: false,
      }),
    ).toEqual({ decision: 'reject' })
  })

  it('keeps an edit an edit and still carries the requirement', () => {
    const request = approvalDecisionRequest({
      decision: 'approve',
      draft: draft('Als Tabelle.'),
      edited: true,
    })
    expect(request.decision).toBe('edit')
    expect(request.plan).toBeDefined()
    expect(request.report_guidance).toBe('Als Tabelle.')
  })
})


describe('a gate opens with the requirement in force', () => {
  const inForce = {
    freeText: 'Zielgruppe: Vorstand.',
    rules: [{ label: 'Executive Briefing', revision: 3, templateId: 'pt_brief' }],
  }

  it('prefills the rules and the typed text, untouched', () => {
    // An empty picker read as "no rules" while the run carried them; the
    // first touch then removed them without anybody choosing that.
    const draft = draftFromPlan(plan(), undefined, inForce)
    expect(draft.reportRuleIds).toEqual(['pt_brief'])
    expect(draft.reportGuidance).toBe('Zielgruppe: Vorstand.')
    expect(draft.reportRequirementTouched).toBe(false)
    // Prefilled, not edited: a plain approve stays a plain approve.
    expect(planDraftDiffers(draft, plan())).toBe(false)
    expect(approvalDecisionRequest({ decision: 'approve', draft, edited: false }))
      .toEqual({ decision: 'approve' })
  })

  it('keeps the rules in force when only the text is edited', () => {
    const draft = {
      ...draftFromPlan(plan(), undefined, inForce),
      reportGuidance: 'Zielgruppe: Aufsichtsrat.',
      reportRequirementTouched: true,
    }
    expect(approvalDecisionRequest({ decision: 'approve', draft, edited: false })).toEqual({
      decision: 'approve',
      report_guidance: 'Zielgruppe: Aufsichtsrat.',
      report_rule_ids: ['pt_brief'],
    })
  })

  it('opens empty when nothing is in force', () => {
    const draft = draftFromPlan(plan(), undefined, null)
    expect(draft.reportRuleIds).toEqual([])
    expect(draft.reportGuidance).toBe('')
  })
})


describe('the draft a gate shows and edits', () => {
  // ONE choice for both the shown draft and the base of every edit: the
  // first edit of the typed text must start from the prefilled rules, or
  // it sends an empty rule list and the server removes them.
  const inForce = {
    freeText: 'Zielgruppe: Vorstand.',
    rules: [{ label: 'briefing', revision: 3, templateId: 'pt_brief' }],
  }

  it('keeps a stored draft of the same plan version', () => {
    const stored = { ...draftFromPlan(plan()), reportGuidance: 'meins', reportRuleIds: ['pt_lektor'] }
    const base = planDraftBase({ depth: undefined, draft: stored, inForce, plan: plan() })
    expect(base.reportRuleIds).toEqual(['pt_lektor'])
    expect(base.reportGuidance).toBe('meins')
  })

  it('opens a stale draft afresh, prefilled with the requirement in force', () => {
    const stale = { ...draftFromPlan(plan()), reportRuleIds: ['pt_lektor'], version: 0 }
    const base = planDraftBase({ depth: undefined, draft: stale, inForce, plan: plan() })
    expect(base.reportRuleIds).toEqual(['pt_brief'])
    expect(base.reportGuidance).toBe('Zielgruppe: Vorstand.')
    expect(base.reportRequirementTouched).toBe(false)
  })

  it('opens empty without a requirement in force', () => {
    const base = planDraftBase({ depth: undefined, draft: null, inForce: null, plan: plan() })
    expect(base.reportRuleIds).toEqual([])
    expect(base.reportGuidance).toBe('')
  })

  it('fills a missing research profile from the run depth, also in a stored draft', () => {
    const fresh = draftFromPlan(plan())
    const stored = { ...fresh, tasks: [{ ...fresh.tasks[0], params: {} }] }
    expect(planDraftBase({ depth: 'deep', draft: stored, inForce, plan: plan() }).tasks[0].params.profile)
      .toBe('deep')
  })
})


describe('the rejection intent is shared, not local', () => {
  it('is part of the draft and never of the wire plan', () => {
    // The gate renders twice (canvas and composer tray). A local
    // useState in one of them let the other keep offering a plain
    // approve while a note was being typed — the click landed, the note
    // vanished, and nothing said so.
    const draft = {
      ...draftFromPlan(plan(), undefined),
      rejectNote: 'Bitte ohne Websuche.',
      rejectPending: true,
    }
    expect(draft.rejectPending).toBe(true)
    expect(Object.keys(draftToWirePlan(draft))).not.toContain('rejectPending')
    expect(Object.keys(draftToWirePlan(draft))).not.toContain('rejectNote')
    // ... and it must not turn an approve into an edit either.
    expect(planDraftDiffers(draft, plan())).toBe(false)
  })

  it('starts closed on a fresh draft', () => {
    const draft = draftFromPlan(plan(), undefined)
    expect(draft.rejectPending).toBe(false)
    expect(draft.rejectNote).toBe('')
  })
})
