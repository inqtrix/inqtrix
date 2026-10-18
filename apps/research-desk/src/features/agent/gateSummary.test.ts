import { describe, expect, it } from 'vitest'

import {
  canGrantForRun,
  fullDelegationText,
  gateActionRow,
  gateKnowledgeScope,
  replanGateSummary,
  toolGateExplanation,
  toolGateHeadline,
} from './gateSummary'
import { translations } from '@/i18n/translations'

const de = translations.de

describe('toolGateHeadline (the gate names its category)', () => {
  it('distinguishes a simple search from delegated research', () => {
    expect(
      toolGateHeadline([{ tool: 'web_instant', args: { query: 'x' } }], de),
    ).toBe('Freigabe: Einfache Websuche')
    expect(
      toolGateHeadline(
        [{ tool: 'run_web_research', args: { question: 'x' } }],
        de,
      ),
    ).toBe('Freigabe: Web-Recherche als Unterauftrag')
    expect(
      toolGateHeadline(
        [{ tool: 'run_deep_mission', args: { assignment: 'x' } }],
        de,
      ),
    ).toBe('Freigabe: Tiefen-Recherche als Unterauftrag')
  })

  it('names a fan-out with its parallel subtask count', () => {
    expect(
      toolGateHeadline(
        [{
          tool: 'delegate_batch',
          args: {
            assignments: [
              { objective: 'a' },
              { objective: 'b' },
              { objective: 'c' },
            ],
          },
        }],
        de,
      ),
    ).toBe('Freigabe: Fan-out — 3 parallele Unteraufträge')
  })

  it('keeps an unknown tool visible under its raw id', () => {
    expect(toolGateHeadline([{ tool: 'future_tool', args: {} }], de)).toBe(
      'Freigabe: future_tool',
    )
    expect(toolGateHeadline([{ tool: 'a' }, { tool: 'b' }], de)).toBe(
      'Freigabe: 2 Aktionen',
    )
  })
})

describe('gateActionRow (full text, never truncated)', () => {
  it('carries the complete primary text verbatim', () => {
    const long = 'Recherchiere den aktuellen Stand der EU-Batterieverordnung '
      .repeat(20)
      .trim()
    const row = gateActionRow(
      { tool: 'run_web_research', args: { question: long } },
      de,
    )
    expect(row.label).toBe('Web-Recherche (Unterauftrag)')
    expect(row.text).toBe(long)
    expect(row.text.length).toBeGreaterThan(1000)
  })

  it('lists every fan-out objective in full', () => {
    const row = gateActionRow(
      {
        tool: 'delegate_batch',
        args: {
          assignments: [
            { mode: 'research', objective: 'Erster vollständiger Auftrag' },
            { objective: 'Zweiter Auftrag ohne Modus' },
          ],
        },
      },
      de,
    )
    expect(row.items).toEqual([
      'Erster vollständiger Auftrag (research)',
      'Zweiter Auftrag ohne Modus',
    ])
  })

  it('explains what approving actually starts', () => {
    expect(
      toolGateExplanation([{ tool: 'web_instant', args: {} }], de),
    ).toContain('kein Unterauftrag')
    expect(
      toolGateExplanation([{ tool: 'run_deep_mission', args: {} }], de),
    ).toContain('Mission')
  })
})

describe('fullDelegationText (recovering the clipped question column)', () => {
  const full = 'Erstelle ein kompaktes Memo zum EU AI Act. '.repeat(20).trim()
  const clipped = full.slice(0, 500) + '…'

  it('recovers the verbatim assignment behind the visible clip', () => {
    const approvals = [{
      payload: {
        actions: [{
          tool: 'run_deep_mission',
          args: { assignment: full },
        }],
      },
    }]
    expect(fullDelegationText(approvals, clipped)).toBe(full)
  })

  it('returns null without a matching approval text', () => {
    expect(fullDelegationText([], clipped)).toBeNull()
    expect(
      fullDelegationText(
        [{ payload: { actions: [{ tool: 'x', args: { question: 'anders' } }] } }],
        clipped,
      ),
    ).toBeNull()
  })

  it('also matches fan-out objectives', () => {
    const approvals = [{
      payload: {
        actions: [{
          tool: 'delegate_batch',
          args: { assignments: [{ objective: full }] },
        }],
      },
    }]
    expect(fullDelegationText(approvals, clipped)).toBe(full)
  })
})

describe('canGrantForRun', () => {
  it('offers the run-wide grant only in balanced mode', () => {
    const actions = [{ tool: 'web_instant', args: { query: 'x' } }]
    expect(canGrantForRun('balanced', actions)).toBe(true)
    expect(canGrantForRun(undefined, actions)).toBe(true)
    expect(canGrantForRun('strict', actions)).toBe(false)
    expect(canGrantForRun('autonomous', actions)).toBe(false)
  })

  it('never offers a grant covering the always-gated patch tool', () => {
    expect(
      canGrantForRun('balanced', [
        { tool: 'propose_editor_patch', args: {} },
      ]),
    ).toBe(false)
    expect(canGrantForRun('balanced', [])).toBe(false)
  })
})

describe('gateKnowledgeScope', () => {
  it('names the collections the gated run may reach', () => {
    expect(
      gateKnowledgeScope({ knowledge_scope: ['EU-AI-Act-vec', 'Vertraege'] }),
    ).toEqual(['EU-AI-Act-vec', 'Vertraege'])
  })

  it('renders nothing when the gate carries no scope', () => {
    // An older run (or an unreadable catalog) must not be shown as an
    // EMPTY scope — that would read as "searches nothing".
    expect(gateKnowledgeScope({})).toEqual([])
    expect(gateKnowledgeScope({ knowledge_scope: 'EU-AI-Act-vec' })).toEqual([])
  })

  it('drops non-string and empty entries', () => {
    expect(gateKnowledgeScope({ knowledge_scope: ['A', '', 7, null] }))
      .toEqual(['A'])
  })
})

describe('replanGateSummary (the replan gate says why and what)', () => {
  it('translates the reason and lists the added tasks with their type', () => {
    const summary = replanGateSummary(
      {
        plan_id: 'plan-1',
        plan_version: 2,
        reason: 'critic_research',
        new_tasks: [
          { task_id: 't6', ordinal: 5, title: 'Verifiziere die strittige Modellmeldung', tool_kind: 'web_research' },
          { task_id: 't7', ordinal: 6, title: 'Ergänze Primärquellen zur EU-Leitlinie', tool_kind: 'web_instant' },
        ],
      },
      de,
    )
    expect(summary.hint).toBe('Der delegierte Lauf möchte den Plan erweitern (v2): Die Prüfung fordert weitere Evidenz')
    expect(summary.heading).toBe('Offene Aufgaben in Plan v2:')
    expect(summary.rows).toEqual([
      'Recherche-Agent (mehrstufig) · Verifiziere die strittige Modellmeldung',
      'Instant-Websuche · Ergänze Primärquellen zur EU-Leitlinie',
    ])
  })

  it('names the sufficiency gap and keeps an unknown reason or kind visible', () => {
    expect(replanGateSummary({ plan_version: 3, reason: 'replan' }, de).hint)
      .toBe('Der delegierte Lauf möchte den Plan erweitern (v3): Die Beleglage ist unvollständig')
    const odd = replanGateSummary(
      { plan_version: 2, reason: 'mystery', new_tasks: [{ title: 'X', tool_kind: 'oddkind' }] },
      de,
    )
    expect(odd.hint).toContain('mystery')
    expect(odd.rows).toEqual(['oddkind · X'])
    expect(replanGateSummary({ plan_version: 2, reason: 'replan' }, de).rows).toEqual([])
  })
})
