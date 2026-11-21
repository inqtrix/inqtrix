import { describe, expect, it } from 'vitest'

import {
  SEARCH_ANSWER_FORMS,
  canGrantForRun,
  choosesSearchAnswerForm,
  editableTextArgs,
  editedToolArgs,
  fullDelegationText,
  gateActionRow,
  gateKnowledgeScope,
  initialEditDraft,
  searchAnswerFormLabel,
  toolGateExplanation,
  toolGateHeadline,
} from './gateSummary'
import { translations } from '@/i18n/translations'

const de = translations.de
const en = translations.en

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

  it('names a document analysis by its tool label', () => {
    expect(
      toolGateHeadline(
        [{ tool: 'analyze_document', args: { objective: 'x', document_ids: ['kd_a'], fields: ['Frist'] } }],
        de,
      ),
    ).toBe('Freigabe: Dokumente auswerten')
  })

  it('keeps an unknown tool visible under its raw id', () => {
    expect(toolGateHeadline([{ tool: 'future_tool', args: {} }], de)).toBe(
      'Freigabe: future_tool',
    )
    expect(toolGateHeadline([{ tool: 'a' }, { tool: 'b' }], de)).toBe(
      'Freigabe: 2 Aktionen',
    )
  })

  it('names the retired delegation tool of a stored run by its name, not its id', () => {
    expect(toolGateHeadline([{ tool: 'run_deep_mission', args: { question: 'x' } }], de)).toBe(
      'Freigabe: Tiefen-Recherche (Unterauftrag)',
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

  it('shows what a document analysis is for', () => {
    const row = gateActionRow(
      {
        tool: 'analyze_document',
        args: {
          document_ids: ['kd_a', 'kd_b'],
          objective: 'Sonderregeln des Pilotbetriebs zu Kosten und Freigabe herausziehen',
          fields: ['Kosten', 'Freigabe'],
        },
      },
      de,
    )
    expect(row.label).toBe('Dokumente auswerten')
    expect(row.text).toBe('Sonderregeln des Pilotbetriebs zu Kosten und Freigabe herausziehen')
    expect(gateActionRow({ tool: 'analyze_document', args: { objective: 'Find the terms' } }, en).label)
      .toBe('Analyse documents')
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

  it('names the period a web research prefers beside its question', () => {
    const action = {
      tool: 'run_web_research',
      args: { question: 'Was hat sich geändert?', recency: 'week' },
    }
    expect(gateActionRow(action, de).note).toBe(
      'Bevorzugter Zeitraum der Quellen: letzte 7 Tage',
    )
    expect(gateActionRow(action, en).note).toBe(
      'Preferred period of the sources: past 7 days',
    )
    expect(gateActionRow(action, de).text).toBe('Was hat sich geändert?')
  })

  it.each([
    ['day', 'letzte 24 Stunden', 'past 24 hours'],
    ['month', 'letzte 30 Tage', 'past 30 days'],
    ['year', 'letzte 12 Monate', 'past 12 months'],
    // Read as the tool reads it: whatever its case and surrounding blanks.
    [' Week ', 'letzte 7 Tage', 'past 7 days'],
  ])('reads the period %j as %j', (sent, german, english) => {
    const action = {
      tool: 'run_web_research',
      args: { question: 'q', recency: sent },
    }
    expect(gateActionRow(action, de).note).toBe(
      `Bevorzugter Zeitraum der Quellen: ${german}`,
    )
    expect(gateActionRow(action, en).note).toBe(
      `Preferred period of the sources: ${english}`,
    )
  })

  it.each([
    ['fortnight', 'fortnight'],
    [' 2 Wochen ', '2 Wochen'],
    // A name every object carries is no period either.
    ['constructor', 'constructor'],
    ['__proto__', '__proto__'],
    // Not a text: the call does not pass the tool's own input check.
    [7, '7'],
    [['week'], '["week"]'],
  ])('says that the tool refuses the period %j', (sent, shown) => {
    const action = {
      tool: 'run_web_research',
      args: { question: 'q', recency: sent },
    }
    expect(gateActionRow(action, de).note).toBe(
      `Zeitraum „${shown}“ wird nicht angewendet: Das Werkzeug weist diesen Aufruf ab.`,
    )
    expect(gateActionRow(action, en).note).toBe(
      `Period "${shown}" is not applied: the tool refuses this call.`,
    )
  })

  it.each([[undefined], [''], ['  '], [null]])(
    'says nothing where the call names no period (%j)',
    (sent) => {
      const row = gateActionRow(
        { tool: 'run_web_research', args: { question: 'q', recency: sent } },
        de,
      )
      expect(row.note).toBe('')
    },
  )

  it('says nothing for a tool that takes no period', () => {
    const row = gateActionRow(
      { tool: 'web_instant', args: { query: 'q', recency: 'week' } },
      de,
    )
    expect(row.note).toBe('')
  })

  it('names the period of each fan-out research that carries one', () => {
    const row = gateActionRow(
      {
        tool: 'delegate_batch',
        args: {
          assignments: [
            { mode: 'research', objective: 'Aktuelle Urteile', recency: 'month' },
            { mode: 'research', objective: 'Grundlagen' },
            { mode: 'research', objective: 'Geschichte', recency: '' },
          ],
        },
      },
      de,
    )
    expect(row.items).toEqual([
      'Aktuelle Urteile (research) \u00b7 Bevorzugter Zeitraum der Quellen: letzte 30 Tage',
      'Grundlagen (research)',
      'Geschichte (research)',
    ])
    expect(row.note).toBe('')
  })

  it('announces no period where the fan-out refuses it', () => {
    // A mission plans the periods of its own research, and a period the
    // tool does not know starts none of the assignments.
    const row = gateActionRow(
      {
        tool: 'delegate_batch',
        args: {
          assignments: [
            { mode: 'deep_mission', objective: 'Memo', recency: 'week' },
            { mode: 'research', objective: 'Urteile', recency: 'hour' },
            { mode: 'deep_mission', objective: 'Bericht', recency: null },
          ],
        },
      },
      de,
    )
    expect(row.items).toEqual([
      'Memo (deep_mission) \u00b7 Zeitraum „week“ wird nicht angewendet: Das Werkzeug weist diesen Aufruf ab.',
      'Urteile (research) \u00b7 Zeitraum „hour“ wird nicht angewendet: Das Werkzeug weist diesen Aufruf ab.',
      'Bericht (deep_mission)',
    ])
  })

  it.each([
    ['table', 'Gewünschte Form der Suchantwort: Tabelle', 'Requested form of the search answer: Table'],
    ['timeline', 'Gewünschte Form der Suchantwort: Zeitleiste', 'Requested form of the search answer: Timeline'],
  ])('names the form %j a web search is asked to answer in', (form, german, english) => {
    const action = { tool: 'web_instant', args: { query: 'Inflationsrate 2026', answer_form: form } }
    expect(gateActionRow(action, de).note).toBe(german)
    expect(gateActionRow(action, en).note).toBe(english)
    expect(gateActionRow(action, de).text).toBe('Inflationsrate 2026')
  })

  it.each([[undefined], [null], ['overview']])(
    'says nothing where a web search asks for no form (%j)',
    (form) => {
      const row = gateActionRow(
        { tool: 'web_instant', args: { query: 'q', answer_form: form } },
        de,
      )
      expect(row.note).toBe('')
    },
  )

  it.each([
    // The tool takes the three names as written and nothing else.
    ['list', 'list'],
    ['Table', 'Table'],
    [' table ', ' table '],
    ['', ''],
    [7, '7'],
  ])('says that the tool refuses the form %j', (sent, shown) => {
    const action = { tool: 'web_instant', args: { query: 'q', answer_form: sent } }
    expect(gateActionRow(action, de).note).toBe(
      `Form „${shown}“ der Suchantwort wird nicht angewendet: Das Werkzeug weist diesen Aufruf ab.`,
    )
    expect(gateActionRow(action, en).note).toBe(
      `Form "${shown}" of the search answer is not applied: the tool refuses this call.`,
    )
  })

  it.each([['search_project_knowledge'], ['run_web_research']])(
    'says nothing about a form on %j, which takes none',
    (tool) => {
      const row = gateActionRow({ tool, args: { query: 'q', answer_form: 'table' } }, de)
      expect(row.note).toBe('')
    },
  )

  it('explains what approving actually starts', () => {
    expect(
      toolGateExplanation([{ tool: 'web_instant', args: {} }], de),
    ).toContain('kein Unterauftrag')
    expect(
      toolGateExplanation([{ tool: 'run_web_research', args: {} }], de),
    ).toContain('Recherche-Lauf')
  })
})

describe('fullDelegationText (recovering the clipped question column)', () => {
  const full = 'Erstelle ein kompaktes Memo zum EU AI Act. '.repeat(20).trim()
  const clipped = full.slice(0, 500) + '…'

  it('recovers the verbatim assignment behind the visible clip', () => {
    const approvals = [{
      payload: {
        actions: [{
          tool: 'run_web_research',
          args: { question: full },
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

describe('editing a tool approval', () => {
  const search = (args: Record<string, unknown>) => ({ tool: 'web_instant', args })

  it('offers the form of the search answer as a choice of its three names', () => {
    expect([...SEARCH_ANSWER_FORMS]).toEqual(['overview', 'table', 'timeline'])
    expect(SEARCH_ANSWER_FORMS.map((form) => searchAnswerFormLabel(form, de))).toEqual([
      'Überblick (wie üblich)',
      'Tabelle',
      'Zeitleiste',
    ])
    expect(choosesSearchAnswerForm(search({ query: 'q' }))).toBe(true)
    expect(choosesSearchAnswerForm({ tool: 'run_web_research', args: {} })).toBe(false)
    expect(choosesSearchAnswerForm(null)).toBe(false)
  })

  it('keeps the form of a web search out of the free text fields', () => {
    expect(editableTextArgs(search({ query: 'q', answer_form: 'table' }))).toEqual(['query'])
    // The same key on another tool is one of its text arguments.
    expect(
      editableTextArgs({ tool: 'run_web_research', args: { question: 'q', recency: 'week', depth: 2 } }),
    ).toEqual(['question', 'recency'])
  })

  it.each([
    [{ query: 'q' }, 'overview'],
    [{ query: 'q', answer_form: 'timeline' }, 'timeline'],
    // A value the tool refuses starts the choice at the default.
    [{ query: 'q', answer_form: 'list' }, 'overview'],
    [{ query: 'q', answer_form: null }, 'overview'],
  ])('starts the edit of %j at the form %j', (args, form) => {
    expect(initialEditDraft(search(args))).toEqual({ query: 'q', answer_form: form })
  })

  it('starts the edit of another tool at its text arguments only', () => {
    expect(
      initialEditDraft({ tool: 'run_web_research', args: { question: 'q', recency: 'week' } }),
    ).toEqual({ question: 'q', recency: 'week' })
  })

  it('sends the chosen form and leaves the default form out', () => {
    const action = search({ query: 'alt', answer_form: 'table', max_sources: 5 })
    expect(editedToolArgs(action, { query: 'neu', answer_form: 'timeline' })).toEqual({
      query: 'neu',
      answer_form: 'timeline',
      max_sources: 5,
    })
    // The default form asks for nothing: the edited call is the plain one.
    expect(editedToolArgs(action, { query: 'neu', answer_form: 'overview' })).toEqual({
      query: 'neu',
      max_sources: 5,
    })
    // A form may be chosen where the call named none.
    expect(editedToolArgs(search({ query: 'alt' }), { query: 'alt', answer_form: 'table' })).toEqual({
      query: 'alt',
      answer_form: 'table',
    })
  })

  it('sends the text fields of another tool as typed and nothing else', () => {
    const action = { tool: 'run_web_research', args: { question: 'alt', recency: 'week', depth: 2 } }
    expect(editedToolArgs(action, { question: 'neu', recency: 'month', answer_form: 'table' })).toEqual({
      question: 'neu',
      recency: 'month',
      depth: 2,
    })
  })
})
