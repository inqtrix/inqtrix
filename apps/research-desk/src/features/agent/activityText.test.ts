import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import type { AgentRunRecord } from './model'
import { activityText } from './timeline/AgentTimeline'

const t = translations.de

/** The fields `activityText` actually reads — a full run record would
 * bury what the case is about. */
function waitingForChildren(child: Record<string, unknown>): AgentRunRecord {
  return {
    children: {
      run_child: { childRunId: 'run_child', taskId: 'call_1', ...child },
    },
    status: 'waiting_for_children',
    taskStates: {},
  } as unknown as AgentRunRecord
}

describe('activityText while children work', () => {
  it('says what the child is doing, not what the parent last did', () => {
    // The regression: this fell through to the parent's own last tool
    // row, frozen since it delegated — one unchanging sentence for
    // twenty minutes, on both surfaces that render this text.
    expect(
      activityText(
        waitingForChildren({
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [5],
          checkedAnswers: 12,
        }),
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 6 · 12 Belege geprüft')
  })

  it('names the parallel wave through the same path', () => {
    expect(
      activityText(
        waitingForChildren({
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [0, 1, 2, 3, 4],
        }),
        t,
        'de',
      ),
    ).toBe('Unterauftrag · 5 Aufgaben parallel')
  })

  it('falls through when the child has reported nothing yet', () => {
    // No invented progress: the parent's own readout takes over again.
    expect(
      activityText(waitingForChildren({ runStatus: 'running' }), t, 'de'),
    ).not.toContain('Unterauftrag')
  })
})

describe('activityText shows the server label as the server wrote it', () => {
  // The server label follows the run's language, so these two kinds are not
  // re-worded here: a re-wording would say something different from the same
  // event in the transcript. The label is the single source.
  it('passes a discarded critic revision through unchanged', () => {
    const run = {
      status: 'running',
      taskStates: {},
      children: {},
      activity: {
        kind: 'critic_revision_discarded',
        label: 'Prüfung: Überarbeitung verworfen',
        detail: 'Die Überarbeitung hatte keine Abschnittsstruktur.',
      },
    } as unknown as AgentRunRecord
    expect(activityText(run, t, 'de')).toBe('Prüfung: Überarbeitung verworfen')
    // The UI language does not rewrite what the run said.
    expect(activityText(run, translations.en, 'en')).toBe(
      'Prüfung: Überarbeitung verworfen',
    )
  })

  it('passes an English run label through unchanged too', () => {
    const run = {
      status: 'running',
      taskStates: {},
      children: {},
      activity: {
        kind: 'replan_invalid',
        label: 'Replan discarded, continuing with the last plan',
        detail: 'Too many tasks (10, 8 allowed).',
      },
    } as unknown as AgentRunRecord
    expect(activityText(run, t, 'de')).toBe(
      'Replan discarded, continuing with the last plan',
    )
    expect(activityText(run, translations.en, 'en')).toBe(
      'Replan discarded, continuing with the last plan',
    )
  })

  it('keeps the three exhausted-research reasons apart', () => {
    // The old override flattened all three server reasons into one sentence,
    // so a reader could not tell which limit actually stopped the research.
    for (const label of [
      'Keine belegte Rechercheaufgabe',
      'Recherche-Limit erreicht',
      'Zeitbudget erreicht',
    ]) {
      const run = {
        status: 'running',
        taskStates: {},
        children: {},
        activity: { kind: 'critic_research_exhausted', label },
      } as unknown as AgentRunRecord
      expect(activityText(run, t, 'de')).toBe(label)
    }
  })
})

describe('activityText for kernel tool rows', () => {
  const running = (activity: Record<string, unknown>): AgentRunRecord =>
    ({ children: {}, status: 'running', taskStates: {}, activity }) as unknown as AgentRunRecord

  it('names the kernel tool and its subject instead of "Durchsucht Quellen"', () => {
    // The regression: a delegation row read "Durchsucht Quellen · <Auftrag>"
    // for the whole eighteen-minute mission.
    expect(
      activityText(
        running({
          kind: 'searching',
          label: 'run_deep_mission',
          detail: 'Erstelle ein belegtes Memo zur KI-Woche',
          status: 'running',
        }),
        t,
        'de',
      ),
    ).toBe('Tiefen-Recherche (Unterauftrag) · Erstelle ein belegtes Memo zur KI-Woche')
  })

  it('keeps the generic search line for rows without a tool label', () => {
    expect(
      activityText(running({ kind: 'searching', detail: 'Preisliste', status: 'running' }), t, 'de'),
    ).toBe('Durchsucht Quellen · Preisliste')
  })
})
