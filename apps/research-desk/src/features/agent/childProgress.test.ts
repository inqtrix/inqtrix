import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { childDelegationLine, childProgressLine } from './childProgress'

const t = translations.de

describe('childProgressLine', () => {
  it('names the phase a delegated kernel run reports before its first step', () => {
    // The regression: for fifty minutes this returned nothing to render.
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'execution' } },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Führt Aufgaben aus')
  })

  it('says what a review role is doing, by its last settled step, the server count and its start', () => {
    // For minutes a role child's line read only the phase word. Its steps
    // say what it does. The start is a
    // clock time: a running duration computed from the last sampled step
    // stood still between samples and read like a stalled review.
    const startedAt = new Date(2026, 9, 10, 17, 49, 30).getTime() / 1000
    const role = {
      lastTool: 'read_evidence',
      runStatus: 'running',
      snapshot: { phase: 'execution' },
      startedAt,
      toolCalls: 5,
      updatedAt: startedAt + 7,
    }
    expect(childProgressLine(role, t, 'de')).toBe(
      `Unterauftrag · ${t.agent.tools.readEvidence} · 5 Schritte · seit 17:49`,
    )
    expect(childProgressLine(role, translations.en, 'en')).toMatch(/ · since 5:49\sPM$/)
  })

  it('names an unlabeled step neutrally and a single step in the singular', () => {
    expect(
      childProgressLine(
        { lastTool: 'write_todos', runStatus: 'running', snapshot: { phase: 'execution' }, toolCalls: 1 },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Arbeitsschritt · 1 Schritt')
  })

  it('says why nothing moves when the child parked on a decision', () => {
    expect(
      childProgressLine(
        { runStatus: 'waiting_for_approval', snapshot: { phase: 'planning' } },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · wartet auf eine Entscheidung')
  })

  it('never shows the child\'s raw progress message', () => {
    // The regression: with no phase the line fell back to the
    // child's message — the parent showed "Unterauftrag · done" and a
    // pipeline status ("Claims-Lage: verifiziert 0, …") verbatim.
    for (const message of ['done', 'Aussagenlage: verifiziert 0, vom Anbieter zugeschrieben 24']) {
      expect(childProgressLine({ runStatus: 'running', message }, t, 'de')).toBeNull()
    }
    const startedAt = new Date(2026, 9, 17, 13, 54, 30).getTime() / 1000
    expect(
      childProgressLine({ runStatus: 'running', message: 'done', startedAt }, t, 'de'),
    ).toBe('Unterauftrag · arbeitet · seit 13:54')
  })

  it('says what a research child does from its typed snapshot', () => {
    const searching = {
      runStatus: 'running',
      message: 'Durchsuche 6 Suchanfragen (Runde 1/1)...',
      snapshot: { current_node: 'search', total_queries: 6 },
    }
    expect(childProgressLine(searching, t, 'de')).toBe('Unterauftrag · Sucht · 6 Suchanfragen')
    expect(childProgressLine(searching, translations.en, 'en')).toBe(
      'Subtask · Searching · 6 search queries',
    )
    const writing = {
      runStatus: 'running',
      message: 'Schreibe Report-Abschnitt 2/2: Kernaussagen',
      snapshot: { current_node: 'answer', total_queries: 6, writing_section: 1, sections_total: 2 },
    }
    expect(childProgressLine(writing, t, 'de')).toBe(
      'Unterauftrag · Schreibt den Bericht · Abschnitt 1 von 2',
    )
  })

  it('shows nothing rather than inventing progress', () => {
    expect(childProgressLine({ runStatus: 'running' }, t, 'de')).toBeNull()
    // A phase of the retired mission engine names nothing any more.
    expect(childProgressLine({ runStatus: 'running', snapshot: { phase: 'planning' } }, t, 'de')).toBeNull()
  })

  it('shows nothing once the child has finished', () => {
    // The delegation row's own status already says it completed.
    expect(
      childProgressLine(
        { runStatus: 'completed', snapshot: { phase: 'done' } },
        t,
        'de',
      ),
    ).toBeNull()
    expect(childProgressLine({ runStatus: 'completed', checkedAnswers: 12 }, t, 'de')).toBeNull()
  })

  it('keeps saying so when the child failed', () => {
    // The tool call returns normally either way, so the row shows a
    // check. Without this line a failed subtask reads as a done one.
    expect(
      childProgressLine(
        { runStatus: 'failed', snapshot: { phase: 'execution' } },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · fehlgeschlagen')
  })

  it('names the reason a child failed when one arrived', () => {
    expect(
      childProgressLine(
        { runStatus: 'failed', error: 'Kein Projektwissen erreichbar' },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · fehlgeschlagen: Kein Projektwissen erreichbar')
  })

  it('says a cancelled child was cancelled', () => {
    expect(childProgressLine({ runStatus: 'cancelled' }, t, 'de')).toBe(
      'Unterauftrag · abgebrochen',
    )
  })
})

describe('childProgressLine liveness', () => {
  it('carries the count that actually moves', () => {
    // Everything else can stand still for ten minutes while the child
    // works; this cannot. It is what answers "is it still alive".
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          checkedAnswers: 12,
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Führt Aufgaben aus · 12 Belege geprüft')
  })

  it('says nothing about evidence before the first one is verified', () => {
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'execution' } },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Führt Aufgaben aus')
  })

  it('still reports progress when the phase is unknown', () => {
    expect(
      childProgressLine({ runStatus: 'running', checkedAnswers: 3 }, t, 'de'),
    ).toBe('Unterauftrag · 3 Belege geprüft')
  })
})

describe('childDelegationLine of a stored mission delegation', () => {
  const review = { final: 'completed', review: 'completed', summary: 'Geprüft · 4 Aussagen belegt', applied: 0 }

  it('says how the mission ended and how its review ended', () => {
    expect(childDelegationLine({ runStatus: 'completed', outcome: review }, t))
      .toBe('Unterauftrag · abgeschlossen · Geprüft · 4 Aussagen belegt')
    expect(childDelegationLine({ runStatus: 'completed', outcome: review }, translations.en))
      .toBe('Subtask · completed · Geprüft · 4 Aussagen belegt')
  })

  it('claims no review the record does not show', () => {
    // Without a record a completed mission reads "Ohne Ausgang", as the
    // recap does; a record of a waived review says nothing.
    expect(childDelegationLine({ runStatus: 'completed' }, t)).toBe('Unterauftrag · abgeschlossen · Ohne Ausgang')
    expect(childDelegationLine({ runStatus: 'completed', outcome: { ...review, review: 'waived' } }, t))
      .toBe('Unterauftrag · abgeschlossen')
  })

  it('says a mission broke off, with its reason when one arrived', () => {
    expect(childDelegationLine({ runStatus: 'failed', error: 'Zeitlimit erreicht' }, t))
      .toBe('Unterauftrag · fehlgeschlagen: Zeitlimit erreicht')
    expect(childDelegationLine({ runStatus: 'cancelled' }, t)).toBe('Unterauftrag · abgebrochen')
  })

  it('says nothing while the mission has not ended', () => {
    expect(childDelegationLine({ runStatus: 'running', outcome: review }, t)).toBeNull()
    expect(childDelegationLine({}, t)).toBeNull()
  })
})
