import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { childProgressLine } from './childProgress'

const t = translations.de

describe('childProgressLine', () => {
  it('names the phase a delegated mission is in', () => {
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
    // A role child has no mission phase or tasks: for minutes its line
    // read only the phase word. Its steps say what it does. The start is a
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

  it('counts the current task from one, not from zero', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [1],
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 2')
  })

  it('drops the task once it settled, keeping the phase', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'evidence' },
          openTasks: [],
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Konsolidiert Belege')
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

describe('childProgressLine with parallel tasks', () => {
  it('names the parallel wave instead of one task of many', () => {
    // The regression: a mission starts five tasks in one burst, and a
    // line saying "Aufgabe 4" would hide the other four.
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [0, 1, 2, 3, 4],
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · 5 Aufgaben parallel')
  })

  it('names the straggler once its siblings settled', () => {
    // The case a reader most needs: four done, one still running.
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [3],
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 4')
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
          openTasks: [5],
          checkedAnswers: 12,
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 6 · 12 Belege geprüft')
  })

  it('says nothing about evidence before the first one is verified', () => {
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'discovery' } },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Erkundet den Bestand')
  })

  it('still reports progress when the phase is unknown', () => {
    expect(
      childProgressLine({ runStatus: 'running', checkedAnswers: 3 }, t, 'de'),
    ).toBe('Unterauftrag · 3 Belege geprüft')
  })
})

describe('childProgressLine with titles and failures', () => {
  it('names the open task by its plan title', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [4],
          taskTitles: { 4: 'Prüfe strittige Wochenmeldungen' },
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 5 „Prüfe strittige Wochenmeldungen“')
  })

  it('counts settled failures instead of keeping them open', () => {
    // The regression: t6 and t7 failed, the mission went back to
    // planning, and the line still read "2 Aufgaben parallel".
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'planning' },
          openTasks: [],
          failedTasks: [5, 6],
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Erstellt den Plan · 2 Aufgaben fehlgeschlagen')
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'planning' }, failedTasks: [5] },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Erstellt den Plan · 1 Aufgabe fehlgeschlagen')
  })
})

describe('childProgressLine says what the child does inside a task', () => {
  it('shows the latest search and the completed count during execution', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'execution' },
          openTasks: [0, 1, 2, 3],
          lastQuery: 'Regulatorische KI-Entwicklungen EU',
          searchesDone: 2,
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · 4 Aufgaben parallel · Suche: Regulatorische KI-Entwicklungen EU · 2 Suchen abgeschlossen')
  })

  it('clips a long query visibly and uses the singular for one search', () => {
    const line = childProgressLine(
      {
        runStatus: 'running',
        snapshot: { phase: 'execution' },
        lastQuery: 'q'.repeat(200),
        searchesDone: 1,
      },
      t,
      'de',
    )
    expect(line).toContain('Suche: ' + 'q'.repeat(79) + '…')
    expect(line).toContain('1 Suche abgeschlossen')
    expect(line?.length ?? 0).toBeLessThan(200)
  })

  it('shows the derived synthesis line, not a stale search, once writing', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'running',
          snapshot: { phase: 'synthesis' },
          lastQuery: 'stale',
          searchesDone: 4,
          section: "Abschnitt 'Unternehmens- und Marktbewegungen' geschrieben.",
        },
        t,
        'de',
      ),
    ).toBe("Unterauftrag · Schreibt das Memo · Abschnitt 'Unternehmens- und Marktbewegungen' geschrieben.")
  })
})

describe('childProgressLine after the mission finished', () => {
  it('leaves a summary line with counts and the elapsed time', () => {
    expect(
      childProgressLine(
        {
          runStatus: 'completed',
          taskTitles: { 0: 'A', 1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F', 6: 'G', 7: 'H' },
          failedTasks: [5, 6],
          openTasks: [],
          sectionsDone: 7,
          startedAt: 1_700_000_000,
          updatedAt: 1_700_001_083,
        },
        t,
        'de',
      ),
    ).toBe('Unterauftrag abgeschlossen · 8 Aufgaben · 6 erledigt, 2 fehlgeschlagen · 7 Abschnitte · 00:18:03')
  })

  it('keeps no summary for a research child (no task projections)', () => {
    expect(childProgressLine({ runStatus: 'completed', checkedAnswers: 12 }, t, 'de')).toBeNull()
  })

  it('names the phase only while no task is open', () => {
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'execution' }, openTasks: [2] },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Aufgabe 3')
    expect(
      childProgressLine(
        { runStatus: 'running', snapshot: { phase: 'planning' }, openTasks: [] },
        t,
        'de',
      ),
    ).toBe('Unterauftrag · Erstellt den Plan')
  })
})
