import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

import type { AgentRuleSource } from '../contextCategories'
import type { AgentApprovalRecord } from '../model'
import {
  decidedReportGuidance,
  decidedReportRuleLabels,
  gateRequirementInForce,
  gateRuleCatalog,
  gateRuleChipTitle,
  gateRuleOptions,
} from './reportGuidance'

function approval(
  overrides: Partial<AgentApprovalRecord> = {},
): AgentApprovalRecord {
  return {
    approvalId: 'a1',
    kind: 'plan',
    status: 'approved',
    subjectType: 'plan',
    subjectId: 'p1',
    payload: {},
    decision: 'approve',
    note: '',
    createdAt: 0,
    decidedAt: 1,
    ...overrides,
  }
}

describe('decidedReportGuidance', () => {
  it('reads the requirement that is in force', () => {
    expect(
      decidedReportGuidance([
        approval({ decisionPayload: { report_guidance: ' Als Tabelle. ' } }),
      ]),
    ).toBe('Als Tabelle.')
  })

  it('lets a later gate clear it', () => {
    // The user withdrew the requirement at the replan gate; showing the
    // old text back would tell them something is in force that is not.
    expect(
      decidedReportGuidance([
        approval({ decisionPayload: { report_guidance: 'Als Tabelle.' } }),
        approval({
          approvalId: 'a2',
          kind: 'replan',
          decisionPayload: { report_guidance: '' },
        }),
      ]),
    ).toBe('')
  })

  it('keeps the standing one when a later gate says nothing', () => {
    // Absence is not a decision — the server's resume keeps the old
    // value in exactly this case, so the read-back must agree.
    expect(
      decidedReportGuidance([
        approval({ decisionPayload: { report_guidance: 'Als Tabelle.' } }),
        approval({ approvalId: 'a2', kind: 'replan', decisionPayload: {} }),
      ]),
    ).toBe('Als Tabelle.')
  })

  it('ignores gates that cannot carry it and undecided ones', () => {
    expect(
      decidedReportGuidance([
        approval({
          kind: 'tool',
          decisionPayload: { report_guidance: 'fremd' },
        }),
        approval({
          approvalId: 'a2',
          status: 'pending',
          decidedAt: null,
          decisionPayload: { report_guidance: 'noch nicht entschieden' },
        }),
      ]),
    ).toBe('')
  })
})

describe('decided requirement parts', () => {
  it('shows the typed text and the rule names, never a composed body', () => {
    // The approvals API returns the parts only — the composed text with
    // its `[Rule: …]` markers stays on the server, so a rule deleted
    // later is not readable through an old approval. The user sees what
    // they wrote, plus the rule names beside it.
    const approvals = [
      approval({
        decisionPayload: {
          report_requirement: {
            free_text: 'Zielgruppe: Laien.',
            rules: [
              { template_id: 'r1', label: 'sprechzettel', revision: 1 },
            ],
          },
        },
      }),
    ]
    expect(decidedReportGuidance(approvals)).toBe('Zielgruppe: Laien.')
    expect(decidedReportRuleLabels(approvals)).toEqual(['sprechzettel'])
  })

  it('falls back to the composed value for older decisions', () => {
    expect(
      decidedReportGuidance([
        approval({ decisionPayload: { report_guidance: 'Als Tabelle.' } }),
      ]),
    ).toBe('Als Tabelle.')
    expect(
      decidedReportRuleLabels([
        approval({ decisionPayload: { report_guidance: 'Als Tabelle.' } }),
      ]),
    ).toEqual([])
  })
})

describe('the requirement a gate opens with', () => {
  const inForce = {
    free_text: 'Zielgruppe: Vorstand.',
    rules: [{ label: 'Executive Briefing', revision: 3, template_id: 'pt_brief' }],
  }

  it('reads the parts in force from the pending gate', () => {
    expect(gateRequirementInForce(approval({
      payload: { report_requirement: inForce },
      status: 'pending',
    }))).toEqual({
      freeText: 'Zielgruppe: Vorstand.',
      rules: [{ label: 'Executive Briefing', revision: 3, templateId: 'pt_brief' }],
    })
  })

  it('reads nothing from a gate that carries no parts', () => {
    expect(gateRequirementInForce(approval({ payload: {}, status: 'pending' }))).toBeNull()
    expect(gateRequirementInForce(null)).toBeNull()
  })

  it('prefills nothing rather than half a requirement', () => {
    // A dropped rule would be removed by the next free-text edit.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      expect(gateRequirementInForce(approval({
        payload: {
          report_requirement: {
            free_text: '',
            rules: [...inForce.rules, { label: 'kaputt', template_id: '' }],
          },
        },
        status: 'pending',
      }))).toBeNull()
      expect(warn).toHaveBeenCalledTimes(1)
    } finally {
      warn.mockRestore()
    }
  })
})

describe('the rules a gate offers', () => {
  it('keeps a rule in force offered under its frozen version, also after the catalog lost it', () => {
    // The server keeps a listed rule that is in force at its frozen
    // revision, even when it was edited, unshared or deleted since —
    // the gate must not hide it, or a free-text edit drops it.
    expect(gateRuleOptions(
      [
        { label: 'briefing', ruleId: 'pt_brief', title: 'Executive Briefing' },
        { label: 'lektor', ruleId: 'pt_lektor', title: 'Lektor' },
      ],
      {
        freeText: '',
        rules: [
          { label: 'briefing', revision: 3, templateId: 'pt_brief' },
          { label: 'alt', revision: 1, templateId: 'pt_gone' },
        ],
      },
    )).toEqual([
      { frozenRevision: 3, label: 'briefing', ruleId: 'pt_brief', title: 'Executive Briefing' },
      { frozenRevision: 1, label: 'alt', ruleId: 'pt_gone', title: 'alt' },
      { label: 'lektor', ruleId: 'pt_lektor', title: 'Lektor' },
    ])
  })

  it('offers the catalog unchanged when nothing is in force', () => {
    const catalog = [{ label: 'lektor', ruleId: 'pt_lektor', title: 'Lektor' }]
    expect(gateRuleOptions(catalog, null)).toEqual(catalog)
  })
})

describe('the catalog the gate may add from', () => {
  const source = (id: string, overrides: Partial<AgentRuleSource> = {}): AgentRuleSource => ({
    composedChars: 100,
    id,
    label: id,
    linkedFiles: false,
    notSynced: false,
    revision: 1,
    title: `Regel ${id}`,
    ...overrides,
  })

  it('leaves out what the composer blocks: linked files and rules the server does not know', () => {
    // A pack with linked files would travel as a literal `{{context}}`
    // placeholder; a rule the server does not know is refused with 400.
    expect(gateRuleCatalog([
      source('pt_a'),
      source('pt_pack', { linkedFiles: true }),
      source('rule-local-1', { notSynced: true }),
    ])).toEqual([{ label: 'pt_a', ruleId: 'pt_a', title: 'Regel pt_a' }])
  })
})

describe('the tooltip of a gate rule chip', () => {
  const labels = {
    attach: 'zum Anhängen anklicken',
    detach: 'zum Lösen anklicken',
    frozen: (revision: number) => `Fassung Rev. ${revision}`,
  }
  const frozen = { frozenRevision: 3, label: 'briefing', ruleId: 'pt_brief', title: 'Executive Briefing' }

  it('keeps the frozen note apart from what a click does', () => {
    // Detached, the chip attaches on click — a "click to detach" hint
    // there would announce the opposite of what happens.
    expect(gateRuleChipTitle(frozen, true, labels))
      .toBe('@rules:briefing — Fassung Rev. 3 — zum Lösen anklicken')
    expect(gateRuleChipTitle(frozen, false, labels))
      .toBe('@rules:briefing — Fassung Rev. 3 — zum Anhängen anklicken')
  })

  it('names a catalog rule by its handle: the chip itself shows the title', () => {
    expect(gateRuleChipTitle({ label: 'lektor', ruleId: 'pt_lektor', title: 'Lektor' }, false, labels))
      .toBe('@rules:lektor')
  })
})

describe('a gate rule chip names the rule like its pill', () => {
  // The pill in the composer and the line under the question name a rule
  // by its title; the gate chip showed the handle ("briefing") instead.
  const source = readFileSync('src/features/agent/plan/PlanReviewBody.tsx', 'utf8')
  const picker = source.slice(
    source.indexOf('function ReportRulePicker('),
    source.indexOf('reportRulesHint}', source.indexOf('function ReportRulePicker(')),
  )

  it('shows the title on every chip', () => {
    expect(picker.length).toBeGreaterThan(500)
    expect(picker.match(/\{option\.title\}/g)?.length).toBe(2)
    expect(picker).not.toContain('{option.label}')
  })
})
