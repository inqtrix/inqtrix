import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'

import {
  agentContextCategories,
  type AgentContextCategory,
  type AgentContextCategoryId,
  type AgentContextSources,
} from './contextCategories'
import { agentContextText } from './contextCategoryText'
import { DEMO_AGENT_REPORT_REQUIREMENT } from './demo'
import { reportComposedCharsMax, reportRuleIdsMax } from './reportRequirement'

const sources: AgentContextSources = {
  answerMode: false,
  canvasDocuments: [],
  collections: [],
  documents: [],
  engine: 'agent_kernel',
  pinnedCanvasDocumentId: null,
  reportIds: [],
  reportLimit: 3,
  reports: ['r1', 'r2', 'r3', 'r4'].map((runId) => ({ label: runId, runId, title: `Bericht ${runId}` })),
  reportsAttachable: true,
  ruleCharLimit: 20_000,
  ruleIds: [],
  ruleLimit: 3,
  rules: ['pt_a', 'pt_b', 'pt_c', 'pt_d'].map((id) => ({
    composedChars: 1_000,
    id,
    label: id,
    linkedFiles: false,
    notSynced: false,
    revision: 1,
    title: `Regel ${id}`,
  })),
  rulesAttachable: true,
  selectedCollectionIds: [],
  selectedDocumentId: null,
}

function category(input: AgentContextSources, id: AgentContextCategoryId): AgentContextCategory {
  const found = agentContextCategories(input).find((entry) => entry.id === id)
  if (!found) throw new Error(`No ${id} category`)
  return found
}

describe('a category at its cap', () => {
  const cappedReports = { ...sources, reportIds: ['r1', 'r2', 'r3'] }

  it('says a capped report in the report wording, on its row and on the category', () => {
    // A report row once read "at most 3 rules": every cap entry got the
    // rule sentence, whatever category it belonged to.
    for (const locale of ['de', 'en'] as const) {
      const t = translations[locale]
      const text = agentContextText(t, locale, { report: 3, rule: 3 })
      const capped = category(cappedReports, 'report')
      const reason = t.agent.composer.reportCap.replace('{max}', '3')
      expect(capped.items.map((item) => text.itemSecondary('report', item))).toEqual([reason])
      expect(text.categoryBlockText(capped)).toBe(reason)
      // The "@" root row says the reason instead of a count of zero.
      expect(text.categoryNote(capped)).toBe(reason)
    }
    const de = agentContextText(translations.de, 'de', { report: 3, rule: 3 })
    expect(de.itemSecondary('report', category(cappedReports, 'report').items[0]))
      .toBe('Höchstens 3 Berichte je Auftrag')
  })

  it('keeps the rule wording for a capped rule', () => {
    const t = translations.de
    const text = agentContextText(t, 'de', { report: 3, rule: 3 })
    const capped = category({ ...sources, ruleIds: ['pt_a', 'pt_b', 'pt_c'] }, 'rule')
    const reason = t.agent.plan.reportRulesCap.replace('{max}', '3')
    expect(capped.items.map((item) => text.itemSecondary('rule', item))).toEqual([reason])
    expect(text.categoryBlockText(capped)).toBe(reason)
  })

  it('says a taken slot in its own words, whatever report cap the server published', () => {
    // A set editing target once read "at most 3 reports per task" on its
    // category and "at most 1 reports" on each other document: the single
    // slots borrowed the report sentence and the report cap.
    const taken: AgentContextSources = {
      ...sources,
      canvasDocuments: ['a1', 'a2'].map((artifactId) => ({ artifactId, name: `${artifactId}.md`, title: artifactId })),
      documents: ['d1', 'd2'].map((id) => ({ id, title: `Dokument ${id}` })),
      pinnedCanvasDocumentId: 'a1',
      selectedDocumentId: 'd1',
    }
    // The session's only canvas document pinned: nothing else is listed,
    // and the category still says why.
    const onlyCanvas = { ...taken, canvasDocuments: taken.canvasDocuments.slice(0, 1) }
    for (const locale of ['de', 'en'] as const) {
      const t = translations[locale]
      for (const report of [3, null]) {
        const text = agentContextText(t, locale, { report, rule: 3 })
        for (const [input, id, reason] of [
          [taken, 'canvas', t.agent.composer.canvasSlotTaken],
          [taken, 'document', t.agent.composer.editTargetSlotTaken],
          [onlyCanvas, 'canvas', t.agent.composer.canvasSlotTaken],
        ] as const) {
          const slot = category(input, id)
          expect(slot.items.map((item) => text.itemSecondary(id, item)))
            .toEqual(slot.items.map(() => reason))
          expect(text.categoryBlockText(slot)).toBe(reason)
          expect(text.categoryNote(slot)).toBe(reason)
        }
      }
    }
    expect(category(onlyCanvas, 'canvas').items).toEqual([])
    const de = agentContextText(translations.de, 'de', { report: 3, rule: 3 })
    expect(de.categoryNote(category(taken, 'document')))
      .toBe('Höchstens ein Bearbeitungsziel je Auftrag — löse es, um ein anderes zu wählen')
    expect(de.itemSecondary('canvas', category(taken, 'canvas').items[0]))
      .toBe('Höchstens ein Canvas-Dokument je Auftrag — löse es, um ein anderes anzuhängen')
  })
})

describe('a report cap the server did not publish', () => {
  it('says on the report category that the server checks the number when you send', () => {
    for (const locale of ['de', 'en'] as const) {
      const t = translations[locale]
      const text = agentContextText(t, locale, { report: null, rule: 3 })
      const unchecked = category({ ...sources, reportIds: ['r1'], reportLimit: null }, 'report')
      const note = t.agent.composer.reportCapUnchecked
      // The scoped "@research:" header and the "+" header show this line.
      expect(text.limitsLabel(unchecked)).toBe(note)
      // The "@" root row: what can be picked, and that the count is not
      // pre-checked.
      expect(text.categoryNote(unchecked)).toBe(`3 · ${note}`)
      expect(text.categoryBlockText(unchecked)).toBeNull()
    }
    expect(translations.de.agent.composer.reportCapUnchecked).toBe('Die Anzahl prüft der Server beim Senden')
    expect(translations.en.agent.composer.reportCapUnchecked).toBe('The server checks the number when you send')
    // Control: a published cap leaves the report category without a
    // limits line.
    const published = agentContextText(translations.de, 'de', { report: 3, rule: 3 })
    expect(published.limitsLabel(category(sources, 'report'))).toBeUndefined()
    expect(published.categoryNote(category(sources, 'report'))).toBe('4')
  })
})

describe('the rule size in the demo', () => {
  it('says the server checks the size on sending, since demo rules carry none', () => {
    // The demo mirrors the server's ceiling, but its rules have no composed
    // size: the menus must say the size is not pre-checked rather than show
    // a budget the twin never measured.
    const demoRules = {
      ...sources,
      ruleCharLimit: reportComposedCharsMax(DEMO_AGENT_REPORT_REQUIREMENT),
      ruleIds: ['pt_a'],
      ruleLimit: reportRuleIdsMax(DEMO_AGENT_REPORT_REQUIREMENT),
      rules: sources.rules.map((rule) => ({ ...rule, composedChars: null })),
    }
    for (const locale of ['de', 'en'] as const) {
      const t = translations[locale]
      const text = agentContextText(t, locale, { report: 3, rule: 3 })
      const rules = category(demoRules, 'rule')
      expect(text.limitsLabel(rules)).toBe(t.agent.composer.ruleBudgetUnchecked)
      expect(text.categoryNote(rules)).toBe(`3 · ${t.agent.composer.ruleBudgetUnchecked}`)
    }
  })
})
