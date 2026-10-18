import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import {
  CHILD_DECISION_KINDS,
  childCheckLabel,
  childMissionFoldedLine,
  childMissionView,
  childStage,
  isMissionChild,
  plainHead,
  stripTaskTitle,
} from './childMission'

const t = translations.de

const running = {
  runStatus: 'running',
  snapshot: { phase: 'execution' },
  startedAt: 1000,
  updatedAt: 1400,
  openTasks: [2],
  failedTasks: [3],
  taskTitles: {
    0: 'Modell-Releases belegen',
    1: 'Regulierung erfassen',
    2: 'Unternehmen erfassen',
    3: 'Forschung erfassen',
  },
  taskIds: { t1: 0, t2: 1, t3: 2, t4: 3 },
  taskStartedAt: { 0: 1000, 1: 1000, 2: 1000, 3: 1000 },
  taskSettledAt: { 0: 1112, 1: 1130, 3: 1090 },
  taskSearches: { 0: 4, 1: 3, 2: 2 },
  taskKinds: { 0: 'web_instant', 1: 'rag_query', 2: 'web_instant', 3: 'web_research' },
  taskResults: { 0: 'Fünf Releases datiert.' },
  lastQuery: 'Finanzierungsrunden KI-Startups September 2026',
  lastQueryTask: 2,
  searchesDone: 9,
}

describe('childMissionView', () => {
  it('folds the mission phases to four stages and tells a research child apart', () => {
    expect(childStage('planning')).toBe('plan')
    expect(childStage('evidence')).toBe('research')
    expect(childStage('synthesis')).toBe('memo')
    expect(childStage('critic')).toBe('check')
    expect(childStage('search')).toBeUndefined()
    expect(isMissionChild({ runStatus: 'running', snapshot: { phase: 'search' } })).toBe(false)
    expect(childMissionView({ runStatus: 'running', snapshot: { phase: 'search' } }, 2000)).toBeNull()
    expect(isMissionChild({ runStatus: 'running', snapshot: { phase: 'intake' } })).toBe(true)
  })

  it('builds one row per task with state, duration, searches, result and the running search', () => {
    const view = childMissionView(running, 1500)
    expect(view).not.toBeNull()
    expect(view?.stages.map((stage) => `${stage.key}:${stage.state}`)).toEqual([
      'plan:done', 'research:now', 'memo:todo', 'check:todo',
    ])
    expect(view?.rows).toEqual([
      { ordinal: 0, title: 'Modell-Releases belegen', state: 'done', elapsedSeconds: 112, searches: 4, result: 'Fünf Releases datiert.' },
      { ordinal: 1, title: 'Regulierung erfassen', state: 'done', elapsedSeconds: 130, searches: 3 },
      { ordinal: 2, title: 'Unternehmen erfassen', state: 'running', elapsedSeconds: 500, searches: 2, query: 'Finanzierungsrunden KI-Startups September 2026' },
      { ordinal: 3, title: 'Forschung erfassen', state: 'failed', elapsedSeconds: 90 },
    ])
    expect([view?.tasks, view?.done, view?.running, view?.failed]).toEqual([4, 2, 1, 1])
    expect(view?.searches).toBe(9)
    // The running clock derives from `now`; the query is joined to its
    // row, so the sum line does not repeat it.
    expect(view?.elapsedSeconds).toBe(500)
    expect(view?.lastQuery).toBeUndefined()
    expect(view?.decision).toBeUndefined()
  })

  it('moves the stage pointer back on a replan and keeps the decision that caused it', () => {
    const view = childMissionView(
      {
        ...running,
        snapshot: { phase: 'planning' },
        openTasks: [],
        researchRounds: 1,
        decision: {
          kind: 'critic_research',
          label: 'Critic fordert Recherche',
          detail: 'Das Memo braucht weitere Evidenz zu: Gegenposition belegt.',
          at: 1450,
        },
        lastQueryTask: undefined,
      },
      1500,
    )
    expect(view?.stages.map((stage) => stage.state)).toEqual(['now', 'todo', 'todo', 'todo'])
    expect(view?.decision).toEqual({
      kind: 'critic_research',
      label: 'Critic fordert Recherche',
      detail: 'Das Memo braucht weitere Evidenz zu: Gegenposition belegt.',
    })
    // No row runs, so the latest query is shown on the sum line instead.
    expect(view?.lastQuery).toBe('Finanzierungsrunden KI-Startups September 2026')
  })

  it('carries the memo line while the memo is written and drops it once the mission ended', () => {
    const writing = childMissionView(
      { ...running, snapshot: { phase: 'synthesis' }, openTasks: [], section: "Abschnitt 'Regulierung' geschrieben.", sectionsDone: 3 },
      1500,
    )
    expect(writing?.stages.map((stage) => stage.state)).toEqual(['done', 'done', 'now', 'todo'])
    expect(writing?.memoLine).toBe("Abschnitt 'Regulierung' geschrieben.")
    expect(writing?.sections).toBe(3)
    // The last search stays off the sum line once the memo is written.
    expect(writing?.lastQuery).toBeUndefined()
    // During the research stage a stale section line would mislead.
    expect(childMissionView({ ...running, section: 'Alt' }, 1500)?.memoLine).toBeUndefined()
    const ended = childMissionView(
      { runStatus: 'completed', snapshot: { phase: 'done' }, startedAt: 0, updatedAt: 10, taskTitles: { 0: 'A' }, section: 'x' },
      100,
    )
    expect(ended?.memoLine).toBeUndefined()
  })

  it('marks every stage done for a completed mission and clocks from its last update', () => {
    const view = childMissionView(
      {
        runStatus: 'completed',
        snapshot: { phase: 'done' },
        startedAt: 1000,
        updatedAt: 1773,
        taskTitles: { 0: 'A', 1: 'B', 2: 'C', 3: 'D', 4: 'E', 5: 'F' },
        searchesDone: 21,
        sectionsDone: 8,
      },
      9999,
    )
    expect(view?.terminal).toBe(true)
    expect(view?.stages.every((stage) => stage.state === 'done')).toBe(true)
    expect(view?.elapsedSeconds).toBe(773)
    // No record crossed: the line claims no review.
    expect(view && childMissionFoldedLine(view, t)).toBe(
      '6 Aufgaben · 21 Suchen · 8 Abschnitte · 12:53 · Ohne Ausgang',
    )
  })

  it('names the research rounds and a broken-off child on the folded line', () => {
    // A mission that still runs has no record yet: the line counts the
    // research rounds its review opened.
    const once = childMissionView(
      { runStatus: 'running', startedAt: 0, updatedAt: 65, taskTitles: { 0: 'A' }, researchRounds: 1 },
      100,
    )
    // No search projection arrived (a research task's searches stay in
    // the grandchild): the line does not claim "0 Suchen".
    expect(once && childMissionFoldedLine(once, t)).toBe(
      '1 Aufgabe · 0 Abschnitte · 1:40 · Prüfung mit 1 Nachrecherche',
    )
    const cancelled = childMissionView(
      { runStatus: 'cancelled', startedAt: 0, updatedAt: 65, taskTitles: { 0: 'A' }, error: 'Abbruch durch Nutzer' },
      100,
    )
    expect(cancelled && childMissionFoldedLine(cancelled, t)).toBe(
      'abgebrochen: Abbruch durch Nutzer · 1 Aufgabe · 0 Abschnitte · 1:05',
    )
  })

  it('claims no review for a mission that ended without a record', () => {
    // The record is the one carrier of how a review ended; a mission that
    // ended without one reads as having none, whatever its rounds were.
    const ended = (runStatus: string) => {
      const view = childMissionView(
        { runStatus, startedAt: 0, updatedAt: 65, taskTitles: { 0: 'A' }, researchRounds: 2 },
        100,
      )
      return view && childCheckLabel(view, t)
    }
    expect(['completed', 'cancelled', 'failed'].map(ended)).toEqual(['Ohne Ausgang', '', ''])
  })

  it('the terminal decision line comes from the record', () => {
    // The round decisions were steps on the way; once the record arrived
    // it says how the review ended, in the server's words.
    const view = childMissionView(
      {
        ...running,
        runStatus: 'completed',
        snapshot: { phase: 'done' },
        openTasks: [],
        decision: {
          kind: 'critic_revise', label: 'Prüfung fordert Nachbesserung',
          detail: 'Die benannten Stellen werden überarbeitet.', at: 1450,
        },
        outcome: {
          final: 'completed', review: 'completed', applied: 1,
          summary: 'Geprüft · 1 Befund überarbeitet, nicht nachgeprüft',
        },
      },
      1500,
    )
    expect(view?.decision).toEqual({
      kind: 'outcome', label: 'Geprüft · 1 Befund überarbeitet, nicht nachgeprüft',
    })
  })

  it('closes the folded line with the record, and stays quiet where no review was owed', () => {
    const ended = (review: string, summary: string) => childMissionView(
      {
        runStatus: 'completed', snapshot: { phase: 'done' }, startedAt: 0, updatedAt: 65,
        taskTitles: { 0: 'A' }, researchRounds: 1,
        outcome: { final: 'completed', review, applied: 0, summary },
      },
      100,
    )
    const reviewed = ended('completed', 'Geprüft · ohne Befund')
    const waived = ended('waived', 'Ohne Prüfung (Stufe)')
    expect([
      reviewed && childMissionFoldedLine(reviewed, t),
      waived && childMissionFoldedLine(waived, t),
    ]).toEqual([
      '1 Aufgabe · 0 Abschnitte · 1:05 · Geprüft · ohne Befund',
      '1 Aufgabe · 0 Abschnitte · 1:05',
    ])
  })

  it('strips the task title from its result line', () => {
    expect(stripTaskTitle('Regulierung erfassen: EU-Leitlinien vom 02.09.', 'Regulierung erfassen'))
      .toBe('EU-Leitlinien vom 02.09.')
    expect(stripTaskTitle('Ohne Titelpräfix.', 'Regulierung erfassen')).toBe('Ohne Titelpräfix.')
    expect(stripTaskTitle('  Text  ', undefined)).toBe('Text')
  })

  it('reads a result head as plain text, never as Markdown syntax', () => {
    // Otherwise a one-line row shows the raw syntax: "## Kurzfazit Im
    // Wochenfenster **31.08.–04.09.2026** ist OpenAI 'GPT-6 Astra' ...".
    expect(stripTaskTitle(
      "Modelle: ## Kurzfazit Im Wochenfenster **31.08.–04.09.2026** ist `GPT-6 Astra` die *wichtigste* Neuheit. - Punkt eins",
      'Modelle',
    )).toBe("Kurzfazit Im Wochenfenster 31.08.–04.09.2026 ist GPT-6 Astra die wichtigste Neuheit. Punkt eins")
    // Words with inner underscores or asterisks used as characters stay.
    expect(plainHead('snake_case bleibt, 3*4 auch')).toBe('snake_case bleibt, 3*4 auch')
  })
})

describe('decision kinds', () => {
  it('mirrors exactly the kinds the server lets cross to a parent', () => {
    // A kind only one side knows is a decision the card silently drops.
    const source = readFileSync(
      fileURLToPath(new URL('../../../../../src/inqtrix/runs/shared.py', import.meta.url)),
      'utf-8',
    )
    const block = source.split('_CHILD_DECISION_KINDS = frozenset(')[1].split(')')[0]
    const server = [...block.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]).sort()
    expect(server.length).toBeGreaterThan(0)
    expect([...CHILD_DECISION_KINDS].sort()).toEqual(server)
  })
})
