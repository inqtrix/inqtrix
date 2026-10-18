import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'

import { runSetupSummary } from './runSetupLabels'

describe('runSetupSummary', () => {
  it('names engine, permission and the Stufe — the cost class of the turn', () => {
    const t = translations.de
    const text = runSetupSummary(
      { autonomy: 'balanced', engineMode: 'agent_kernel', tierMode: 'gruendlich' },
      t,
    )
    expect(text).toBe(
      `${t.agent.composer.engineAuto} · ${t.agent.composer.autonomyBalanced} · ${t.agent.composer.tierGruendlich}`,
    )
    expect(text).toContain('Gründlich')
  })

  it('omits the Stufe only when the server publishes no ladder', () => {
    const t = translations.en
    expect(
      runSetupSummary(
        { autonomy: 'autonomous', engineMode: 'workspace_agent', tierMode: null },
        t,
      ),
    ).toBe(`${t.agent.overview.brainMission} · ${t.agent.composer.autonomyAutonomous}`)
    expect(
      runSetupSummary(
        { autonomy: 'strict', engineMode: 'agent_kernel', tierMode: 'tief' },
        t,
      ).split(' · '),
    ).toHaveLength(3)
  })
})

describe('runSetupSummary shows an armed response form', () => {
  // The capsule in the composer is where the user reads what the next turn
  // will do, so it has to carry the armed response form. Without it, picking
  // "Canvas" in the run-setup menu changes nothing visible and the armed
  // state stays legible only inside the opened menu.
  // Because the composer capsule already shows the form, the capsule button
  // beside it passes none; the summary keeps accepting one so the header of
  // the opened menu — where that chip capsule is not visible — can show it.
  it('appends the form once it is no longer auto', () => {
    const t = translations.de
    const base = {
      autonomy: 'balanced',
      engineMode: 'agent_kernel' as const,
      tierMode: 'gruendlich' as const,
    }
    expect(runSetupSummary({ ...base, responseForm: 'canvas' }, t))
      .toBe(
        `${t.agent.composer.engineAuto} · ${t.agent.composer.autonomyBalanced}`
        + ` · ${t.agent.composer.tierGruendlich} · ${t.agent.composer.responseFormCanvas}`,
      )
    expect(runSetupSummary({ ...base, responseForm: 'chat' }, t))
      .toContain(t.agent.composer.responseFormChat)
  })

  it('stays silent on auto and when the caller passes no form', () => {
    const t = translations.en
    const base = {
      autonomy: 'balanced',
      engineMode: 'agent_kernel' as const,
      tierMode: 'gruendlich' as const,
    }
    const withoutForm = runSetupSummary(base, t)
    expect(runSetupSummary({ ...base, responseForm: 'auto' }, t)).toBe(withoutForm)
    // Segment-wise, not substring: the English engine label is "Automatic"
    // and contains the form label "Auto" — a naive contains() would pass
    // for the wrong reason.
    expect(withoutForm.split(' · ')).toHaveLength(3)
    expect(withoutForm.split(' · ')).not.toContain(t.agent.composer.responseFormAuto)
  })
})
