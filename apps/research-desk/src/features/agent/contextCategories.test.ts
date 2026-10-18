import { describe, expect, it } from 'vitest'

import type { ChatRuleOption } from '@/features/project/selectors'

import {
  agentContextCategories,
  agentMentionEmptyState,
  agentMentionPick,
  agentMentionRows,
  agentMentionTarget,
  agentRuleSources,
  attachedReportsMax,
  engineRefusesAttachments,
  mentionEmptyOpensLibrary,
  pickableCount,
  pillKindsKeptAfterSend,
  pillKindsKeptByAnswer,
  ruleIdsForSubmit,
  settleRuleIdsAfterSubmit,
  type AgentContextSources,
  type AgentRuleSource,
} from './contextCategories'

function rule(id: string, overrides: Partial<AgentRuleSource> = {}): AgentRuleSource {
  return {
    composedChars: 1_000,
    id,
    label: id,
    linkedFiles: false,
    notSynced: false,
    revision: 1,
    title: `Regel ${id}`,
    ...overrides,
  }
}

const sources: AgentContextSources = {
  answerMode: false,
  canvasDocuments: [{ artifactId: 'a1', name: 'memo.md', title: 'Memo' }],
  collections: [{ id: 'c1', title: 'EU-AI-Act-vec' }, { id: 'c2', title: 'Verträge' }],
  documents: [{ id: 'd1', title: 'Untitled.md' }],
  engine: 'agent_kernel',
  pinnedCanvasDocumentId: null,
  reportIds: [],
  reportLimit: 3,
  reports: [{ runId: 'r1', title: 'Was sind die aktuellen CO2-Flottengrenzwerte', label: 'was-sind' }],
  reportsAttachable: true,
  ruleCharLimit: 20_000,
  ruleIds: [],
  ruleLimit: 3,
  rules: [rule('pt_brief', { label: 'briefing', title: 'Executive Briefing' })],
  rulesAttachable: true,
  selectedCollectionIds: ['c2'],
  selectedDocumentId: null,
}

const ruleCategory = (input: AgentContextSources) =>
  agentContextCategories(input).find((category) => category.id === 'rule')

const reportCategory = (input: AgentContextSources) =>
  agentContextCategories(input).find((category) => category.id === 'report')

/** Five finished reports; the published cap in `sources` is three. */
const fiveReports = ['r1', 'r2', 'r3', 'r4', 'r5'].map((runId) => ({
  label: runId,
  runId,
  title: `Bericht ${runId}`,
}))

describe('agentContextCategories', () => {
  it('lists rules, reports, canvas, edit target, collections — one order for "+" and "@"', () => {
    // Most frequent in the agent case first: a rule, then what the task
    // reads (reports, canvas), then where it writes, then the knowledge.
    const categories = agentContextCategories(sources)
    expect(categories.map((c) => [c.id, c.items.length])).toEqual([
      ['rule', 1], ['report', 1], ['canvas', 1], ['document', 1], ['collection', 1],
    ])
    expect(categories[4].items[0]).toEqual({ id: 'c1', label: 'EU-AI-Act-vec', secondary: '' })
  })

  it('offers a rule by its wire id, titled for the reader', () => {
    expect(ruleCategory(sources)?.items).toEqual([
      { id: 'pt_brief', label: 'Executive Briefing', secondary: 'briefing' },
    ])
  })

  it('drops empty categories and keeps a capped report category visible with its message', () => {
    const capped = agentContextCategories({
      ...sources, canvasDocuments: [], reportIds: ['x', 'y', 'z'], rules: [],
    })
    expect(capped.map((c) => c.id)).toEqual(['report', 'document', 'collection'])
    // The report not attached yet stays listed, with the reason.
    expect(capped[0]).toMatchObject({
      blocked: 'cap',
      items: [{ blocked: { kind: 'cap', max: 3 }, id: 'r1' }],
    })
  })

  it('keeps every report not attached yet listed at the cap, each with the reason', () => {
    // The listing rule of the rules: a search must find an existing report
    // and say why it cannot be attached, never claim there is none.
    const capped = reportCategory({ ...sources, reportIds: ['r1', 'r2', 'r3'], reports: fiveReports })
    expect(capped).toMatchObject({
      blocked: 'cap',
      items: [
        { blocked: { kind: 'cap', max: 3 }, id: 'r4' },
        { blocked: { kind: 'cap', max: 3 }, id: 'r5' },
      ],
    })
    expect(capped && pickableCount(capped)).toBe(0)
    // Control: below the cap nothing is blocked by count.
    const below = reportCategory({ ...sources, reportIds: ['r1', 'r2'], reports: fiveReports })
    expect(below?.blocked).toBeNull()
    expect(below?.items.map((item) => item.blocked ?? null)).toEqual([null, null, null])
  })

  it('pre-checks no report cap the server did not publish', () => {
    // No silent fallback number, like the rule cap: the server decides on
    // sending, and the category says so.
    const category = reportCategory({
      ...sources,
      reportIds: ['r1', 'r2', 'r3', 'r4'],
      reportLimit: null,
      reports: fiveReports,
    })
    expect(category?.blocked).toBeNull()
    expect(category?.items).toEqual([{ id: 'r5', label: 'Bericht r5', secondary: 'r5' }])
    expect(category?.capUnchecked).toBe(true)
    // Control: a published cap is pre-checked, and nothing says otherwise.
    expect(reportCategory(sources)?.capUnchecked).toBeUndefined()
  })

  it('offers reports and canvas documents to the kernel engine only', () => {
    // The server refuses both on a mission run; offering them there sent
    // the user into a 400 after the click.
    const mission = agentContextCategories({ ...sources, engine: 'workspace_agent' })
    expect(mission.map((c) => c.id)).toEqual(['rule', 'document', 'collection'])
  })

  it('keeps a context pack with linked files visible, but not pickable, with its reason', () => {
    const pack = rule('pt_pack', { linkedFiles: true })
    expect(ruleCategory({ ...sources, rules: [pack] })?.items[0]).toMatchObject({
      blocked: { kind: 'linkedFiles' },
      id: 'pt_pack',
    })
  })

  it('stops offering rules at the cap instead of evicting an attached one', () => {
    const attached = ['pt_a', 'pt_b', 'pt_c']
    const capped = ruleCategory({
      ...sources,
      ruleIds: attached,
      rules: [...attached.map((id) => rule(id)), rule('pt_d')],
    })
    // Still listed, each with the reason: a search must find it and say
    // why it cannot be picked, not claim there is no such rule.
    expect(capped).toMatchObject({
      blocked: 'cap',
      items: [{ blocked: { kind: 'cap', max: 3 }, id: 'pt_d' }],
    })
  })

  it('pre-checks no cap the server did not publish', () => {
    // No silent fallback number: without a published cap the composer
    // lets the server decide on sending and says so.
    const attached = ['pt_a', 'pt_b', 'pt_c', 'pt_d']
    const category = ruleCategory({
      ...sources,
      ruleIds: attached,
      ruleLimit: null,
      rules: [...attached.map((id) => rule(id)), rule('pt_e')],
    })
    expect(category?.blocked).toBeNull()
    expect(category?.items).toEqual([{ id: 'pt_e', label: 'Regel pt_e', secondary: 'pt_e' }])
    expect(category?.capUnchecked).toBe(true)
    // Control: a published cap is pre-checked, and nothing says otherwise.
    expect(ruleCategory(sources)?.capUnchecked).toBeUndefined()
  })

  it('counts one rule attached twice as one', () => {
    const category = ruleCategory({
      ...sources, ruleIds: ['pt_a', 'pt_a'], rules: [rule('pt_a'), rule('pt_b')],
    })
    expect(category?.blocked).toBeNull()
    expect(category?.budget).toEqual({ max: 20_000, used: 1_000 })
  })

  it('sums the published block sizes plus one separator per join, and blocks what no longer fits', () => {
    // Executive Briefing composes to 17,230 characters; the server joins
    // blocks with a blank line (two characters) and refuses anything
    // above the ceiling. The numbers come from the server — no client
    // re-count of the composition.
    const category = ruleCategory({
      ...sources,
      ruleIds: ['pt_brief'],
      rules: [
        rule('pt_brief', { composedChars: 17_230 }),
        rule('pt_fits', { composedChars: 2_768 }),
        rule('pt_over', { composedChars: 2_769 }),
        rule('pt_huge', { composedChars: 25_000 }),
      ],
    })
    expect(category?.budget).toEqual({ max: 20_000, used: 17_230 })
    const blocks = Object.fromEntries(
      (category?.items ?? []).map((item) => [item.id, item.blocked ?? null]),
    )
    expect(blocks).toEqual({
      pt_fits: null,
      pt_huge: { chars: 25_000, kind: 'tooLong', max: 20_000 },
      pt_over: { kind: 'overBudget', max: 20_000, total: 20_001 },
    })
  })

  it('counts a separator between every two attached rules', () => {
    const category = ruleCategory({
      ...sources,
      ruleIds: ['pt_a', 'pt_b'],
      rules: [rule('pt_a'), rule('pt_b'), rule('pt_c')],
    })
    expect(category?.budget).toEqual({ max: 20_000, used: 2_002 })
  })

  it('skips the size pre-check visibly when the server publishes no ceiling', () => {
    // No silent fallback number: an older server publishes nothing, so
    // the composer says the size is checked on sending instead of
    // inventing a limit of its own.
    const category = ruleCategory({
      ...sources,
      ruleCharLimit: null,
      rules: [rule('pt_huge', { composedChars: 99_000 })],
    })
    expect(category?.budget).toEqual({ unchecked: true })
    expect(category?.items[0].blocked).toBeUndefined()
  })

  it('skips the size pre-check visibly while an attached rule size is unknown', () => {
    const category = ruleCategory({
      ...sources,
      ruleIds: ['pt_new'],
      rules: [rule('pt_new', { composedChars: null }), rule('pt_a')],
    })
    expect(category?.budget).toEqual({ unchecked: true })
  })

  it('says the size is unknown when an attached rule left the catalog', () => {
    // A rule deleted while its pill sits in the draft: its size is not
    // zero, it is unknown — the server checks it on sending.
    const category = ruleCategory({
      ...sources,
      ruleIds: ['pt_gone'],
      rules: [rule('pt_a', { composedChars: 19_999 })],
    })
    expect(category?.budget).toEqual({ unchecked: true })
    expect(category?.items[0].blocked).toBeUndefined()
  })

  it('blocks a rule too long on its own, whatever another rule measures', () => {
    // Its own size against the published ceiling needs no other size; only
    // the sum with the attached rules does.
    const category = ruleCategory({
      ...sources,
      rules: [rule('pt_new', { composedChars: null }), rule('pt_huge', { composedChars: 25_000 })],
    })
    const blocks = Object.fromEntries(
      (category?.items ?? []).map((item) => [item.id, item.blocked ?? null]),
    )
    expect(blocks).toEqual({
      pt_huge: { chars: 25_000, kind: 'tooLong', max: 20_000 },
      pt_new: null,
    })
  })

  it('blocks a rule too long on its own while an attached size is unknown', () => {
    // An attached rule of unknown size leaves the sum to the server, but a
    // rule above the ceiling on its own is still refused before sending.
    const category = ruleCategory({
      ...sources,
      ruleIds: ['pt_new'],
      rules: [rule('pt_new', { composedChars: null }), rule('pt_huge', { composedChars: 25_000 })],
    })
    expect(category?.budget).toEqual({ unchecked: true })
    expect(category?.items.find((item) => item.id === 'pt_huge')?.blocked)
      .toEqual({ chars: 25_000, kind: 'tooLong', max: 20_000 })
  })

  it('blocks rules while a clarification is pending, with its reason', () => {
    // The send answers the question; a rule attached now would apply to
    // nothing, silently.
    expect(ruleCategory({ ...sources, answerMode: true })).toMatchObject({
      blocked: 'answerMode',
      items: [{ blocked: { kind: 'answerMode' }, id: 'pt_brief' }],
    })
  })

  it('tells "every enabled rule is attached" apart from "no rule enabled"', () => {
    // The only agent rule attached: the menu must not send the user to
    // the library to switch on what is switched on already.
    const category = ruleCategory({ ...sources, ruleIds: ['pt_brief'] })
    expect(category).toMatchObject({ blocked: 'allAttached', items: [] })
  })

  it('lists a rule the server does not know yet, never pickable', () => {
    const local = rule('rule-local-1', { notSynced: true })
    expect(ruleCategory({ ...sources, rules: [local] })?.items).toEqual([
      {
        blocked: { kind: 'notSynced' },
        id: 'rule-local-1',
        label: 'Regel rule-local-1',
        secondary: 'rule-local-1',
      },
    ])
  })

  it('offers no rule category without a handler for it', () => {
    expect(ruleCategory({ ...sources, rulesAttachable: false })).toBeUndefined()
  })
})

describe('agentMentionRows', () => {
  const categories = agentContextCategories(sources)

  it('opens on categories, searches across them with a query, and narrows inside one', () => {
    // A flat list would put 25 rows, taller than the viewport, under "@".
    expect(agentMentionRows(categories, null, '').map((r) => r.kind)).toEqual(
      ['category', 'category', 'category', 'category', 'category'],
    )
    expect(agentMentionRows(categories, null, 'memo')).toEqual([
      { kind: 'item', category: 'canvas', item: { id: 'a1', label: 'memo.md', secondary: 'Memo' } },
    ])
    expect(agentMentionRows(categories, null, 'exec')).toEqual([
      {
        kind: 'item',
        category: 'rule',
        item: { id: 'pt_brief', label: 'Executive Briefing', secondary: 'briefing' },
      },
    ])
    expect(agentMentionRows(categories, 'collection', '')).toHaveLength(1)
    expect(agentMentionRows(categories, 'collection', 'zzz')).toEqual([])
  })

  it('finds a rule of a blocked category and lists it with the reason', () => {
    // "No match" would be untrue: the rule exists, it just cannot be
    // attached right now — and the row says why.
    const rules = ['Alpha', 'Beta', 'Gamma', 'Lektor'].map((title) =>
      rule(`pt_${title.toLowerCase()}`, { title }))
    const capped = agentContextCategories({
      ...sources, ruleIds: ['pt_alpha', 'pt_beta', 'pt_gamma'], rules,
    })
    expect(agentMentionRows(capped, null, 'Lekt')).toEqual([{
      category: 'rule',
      item: { blocked: { kind: 'cap', max: 3 }, id: 'pt_lektor', label: 'Lektor', secondary: 'pt_lektor' },
      kind: 'item',
    }])
    const answering = agentContextCategories({ ...sources, answerMode: true, rules })
    expect(agentMentionRows(answering, null, 'Lekt')[0]).toMatchObject({
      item: { blocked: { kind: 'answerMode' }, id: 'pt_lektor' },
    })
  })

  it('finds a report at the cap, across categories and under "@research:", never pickable', () => {
    const capped = agentContextCategories({ ...sources, reportIds: ['r1', 'r2', 'r3'], reports: fiveReports })
    const row = {
      category: 'report',
      item: { blocked: { kind: 'cap', max: 3 }, id: 'r4', label: 'Bericht r4', secondary: 'r4' },
      kind: 'item',
    }
    expect(agentMentionRows(capped, null, 'Bericht r4')).toEqual([row])
    const { filter, scope } = agentMentionTarget('research:r4', null)
    const scoped = agentMentionRows(capped, scope, filter)
    expect(scoped).toEqual([row])
    // Click and Enter run this pick: it attaches nothing.
    expect(agentMentionPick(scoped[0], null, { libraryAvailable: true })).toEqual({ kind: 'none' })
  })
})

describe('pickableCount', () => {
  it('counts what can be picked, the same number in every menu', () => {
    const category = ruleCategory({
      ...sources,
      rules: [rule('pt_a'), rule('pt_pack', { linkedFiles: true }), rule('pt_b')],
    })
    expect(category?.items).toHaveLength(3)
    expect(category && pickableCount(category)).toBe(2)
  })
})

describe('agentMentionTarget', () => {
  it('reads "@rules:" and "@research:" as the rule and report scope', () => {
    expect(agentMentionTarget('rules:exec', null)).toEqual({ alias: true, filter: 'exec', scope: 'rule' })
    expect(agentMentionTarget('Research:', null)).toEqual({ alias: true, filter: '', scope: 'report' })
  })

  it('searches every category for a plain query', () => {
    expect(agentMentionTarget('memo', null)).toEqual({ alias: false, filter: 'memo', scope: null })
  })

  it('lets a scope the user stepped into win over an alias-shaped filter', () => {
    expect(agentMentionTarget('rules:x', 'collection')).toEqual({
      alias: false, filter: 'rules:x', scope: 'collection',
    })
  })
})

describe('agentMentionEmptyState', () => {
  const categories = agentContextCategories(sources)

  it('names the query and where it searched', () => {
    expect(agentMentionEmptyState({ categories, engine: 'agent_kernel', filter: 'zzz', scope: null }))
      .toEqual({ kind: 'noMatch', query: 'zzz', scope: null })
    expect(agentMentionEmptyState({ categories, engine: 'agent_kernel', filter: 'zzz', scope: 'rule' }))
      .toEqual({ kind: 'noMatch', query: 'zzz', scope: 'rule' })
  })

  it('says how to enable a rule when none is offered', () => {
    expect(agentMentionEmptyState({ categories: [], engine: 'agent_kernel', filter: '', scope: 'rule' }))
      .toEqual({ kind: 'noRules' })
  })

  it('says every enabled rule is attached instead of "none enabled"', () => {
    const attachedAll = agentContextCategories({ ...sources, ruleIds: ['pt_brief'] })
    expect(agentMentionEmptyState({ categories: attachedAll, engine: 'agent_kernel', filter: '', scope: 'rule' }))
      .toEqual({ kind: 'blocked', reason: 'allAttached', scope: 'rule' })
  })

  it('names the reason of a blocked category', () => {
    const answering = agentContextCategories({ ...sources, answerMode: true })
    expect(agentMentionEmptyState({ categories: answering, engine: 'agent_kernel', filter: '', scope: 'rule' }))
      .toEqual({ kind: 'blocked', reason: 'answerMode', scope: 'rule' })
  })

  it('says that reports need the kernel engine', () => {
    expect(agentMentionEmptyState({ categories, engine: 'workspace_agent', filter: '', scope: 'report' }))
      .toEqual({ kind: 'kernelOnly', scope: 'report' })
  })

  it('never claims a missing collection for an empty root', () => {
    expect(agentMentionEmptyState({ categories: [], engine: 'agent_kernel', filter: '', scope: null }))
      .toEqual({ kind: 'nothingAttachable' })
    expect(agentMentionEmptyState({ categories, engine: 'agent_kernel', filter: '', scope: 'document' }))
      .toEqual({ kind: 'emptyCategory', scope: 'document' })
  })
})

describe('mentionEmptyOpensLibrary', () => {
  it('links every state the prompt library can resolve, and only those', () => {
    expect(mentionEmptyOpensLibrary({ kind: 'noRules' })).toBe(true)
    expect(mentionEmptyOpensLibrary({ kind: 'nothingAttachable' })).toBe(true)
    expect(mentionEmptyOpensLibrary({ kind: 'blocked', reason: 'allAttached', scope: 'rule' })).toBe(true)
    // The library changes nothing about these.
    expect(mentionEmptyOpensLibrary({ kind: 'blocked', reason: 'cap', scope: 'rule' })).toBe(false)
    expect(mentionEmptyOpensLibrary({ kind: 'blocked', reason: 'answerMode', scope: 'rule' })).toBe(false)
    expect(mentionEmptyOpensLibrary({ kind: 'noMatch', query: 'x', scope: null })).toBe(false)
    expect(mentionEmptyOpensLibrary({ kind: 'emptyCategory', scope: 'collection' })).toBe(false)
  })
})

describe('agentMentionPick', () => {
  const library = { libraryAvailable: true }
  const rootRow = (categories: ReturnType<typeof agentContextCategories>) =>
    agentMentionRows(categories, null, '').find(
      (row) => row.kind === 'category' && row.category.id === 'rule',
    )

  it('steps into a blocked category, where its entries or its state say why', () => {
    // A chevron that does nothing on click or Enter teaches the user the
    // menu is broken; inside, the reason is on every row.
    const answering = agentContextCategories({ ...sources, answerMode: true })
    const attachedAll = agentContextCategories({ ...sources, ruleIds: ['pt_brief'] })
    const capped = agentContextCategories({
      ...sources,
      ruleIds: ['pt_a', 'pt_b', 'pt_c'],
      rules: [rule('pt_a'), rule('pt_b'), rule('pt_c'), rule('pt_d')],
    })
    for (const categories of [answering, attachedAll, capped]) {
      expect(rootRow(categories)?.kind).toBe('category')
      expect(agentMentionPick(rootRow(categories), null, library)).toEqual({ kind: 'enter', scope: 'rule' })
    }
    // Inside: the blocked entries with their reason, or the state row.
    expect(agentMentionRows(answering, 'rule', '')).toEqual([{
      category: 'rule',
      item: { blocked: { kind: 'answerMode' }, id: 'pt_brief', label: 'Executive Briefing', secondary: 'briefing' },
      kind: 'item',
    }])
    expect(agentMentionRows(attachedAll, 'rule', '')).toEqual([])
    expect(agentMentionEmptyState({ categories: attachedAll, engine: 'agent_kernel', filter: '', scope: 'rule' }))
      .toEqual({ kind: 'blocked', reason: 'allAttached', scope: 'rule' })
  })

  it('attaches a pickable entry and does nothing for a blocked one', () => {
    const [briefing] = agentMentionRows(agentContextCategories(sources), 'rule', '')
    expect(agentMentionPick(briefing, null, library)).toEqual({
      category: 'rule',
      item: { id: 'pt_brief', label: 'Executive Briefing', secondary: 'briefing' },
      kind: 'attach',
    })
    const [answering] = agentMentionRows(agentContextCategories({ ...sources, answerMode: true }), 'rule', '')
    expect(agentMentionPick(answering, null, library)).toEqual({ kind: 'none' })
  })

  it('gives the state row an action exactly where the library resolves it', () => {
    // The answer-mode row says why rules wait; the library changes nothing
    // about a pending question, so that row has no action — and is marked
    // so — while "every enabled rule is attached" opens the library.
    const blocked = (reason: 'answerMode' | 'allAttached' | 'cap') =>
      ({ kind: 'blocked' as const, reason, scope: 'rule' as const })
    expect(agentMentionPick(undefined, blocked('answerMode'), library)).toEqual({ kind: 'none' })
    expect(agentMentionPick(undefined, blocked('cap'), library)).toEqual({ kind: 'none' })
    expect(agentMentionPick(undefined, blocked('allAttached'), library)).toEqual({ kind: 'openLibrary' })
    expect(agentMentionPick(undefined, { kind: 'noRules' }, library)).toEqual({ kind: 'openLibrary' })
    expect(agentMentionPick(undefined, { kind: 'noMatch', query: 'x', scope: null }, library))
      .toEqual({ kind: 'none' })
    // No library to open on this surface: no action either.
    expect(agentMentionPick(undefined, blocked('allAttached'), { libraryAvailable: false }))
      .toEqual({ kind: 'none' })
  })
})

describe('engineRefusesAttachments', () => {
  it('flags reports and canvas context before a mission send, never for the kernel', () => {
    // The server refuses both on a mission run with 400; attached under
    // the kernel and then switched, the chips stayed and the send failed.
    const none = { canvasAttached: false, reportCount: 0 }
    expect(engineRefusesAttachments({ ...none, engine: 'workspace_agent', reportCount: 1 })).toBe(true)
    expect(engineRefusesAttachments({ ...none, canvasAttached: true, engine: 'workspace_agent' })).toBe(true)
    expect(engineRefusesAttachments({ ...none, engine: 'workspace_agent' })).toBe(false)
    expect(engineRefusesAttachments({ canvasAttached: true, engine: 'agent_kernel', reportCount: 2 })).toBe(false)
  })
})

describe('attachedReportsMax', () => {
  it('reads the published report cap and has no number of its own', () => {
    expect(attachedReportsMax({ max_reports: 5 })).toBe(5)
    // Not published (an older server): not pre-checked, said so.
    expect(attachedReportsMax(null)).toBeNull()
    expect(attachedReportsMax(undefined)).toBeNull()
    // A cap that admits nothing is no cap the composer can show.
    expect(attachedReportsMax({ max_reports: 0 })).toBeNull()
  })
})

describe('agentRuleSources', () => {
  function option(overrides: Partial<ChatRuleOption>): ChatRuleOption {
    return {
      category: 'instruction',
      includeInAutocomplete: true,
      label: 'briefing',
      linkedContextRefs: [],
      markdown: 'Gliedere klar.',
      ruleId: 'rule-local-1',
      title: 'Executive Briefing',
      visibility: { agent: true, chat: true, editor: true },
      ...overrides,
    }
  }

  it('sends the server template id, never the local rule id', () => {
    // A rule pushed to the server keeps its local id until the next
    // hydrate; sending that id is refused with 400.
    expect(agentRuleSources([
      option({ composedChars: 1_250, ruleId: 'rule-local-1', serverRevision: 3, serverTemplateId: 'pt_brief' }),
    ], { localIds: false })).toEqual([{
      composedChars: 1_250,
      id: 'pt_brief',
      label: 'briefing',
      linkedFiles: false,
      notSynced: false,
      revision: 3,
      title: 'Executive Briefing',
    }])
  })

  it('keeps a rule the server does not know yet, marked as not synced', () => {
    // Listed with its reason instead of silently missing; its local id
    // only keys the row — the entry is never pickable, so never sent.
    expect(agentRuleSources([option({})], { localIds: false })).toEqual([{
      composedChars: null,
      id: 'rule-local-1',
      label: 'briefing',
      linkedFiles: false,
      notSynced: true,
      revision: null,
      title: 'Executive Briefing',
    }])
  })

  it('marks a context pack with linked files', () => {
    const pack = option({
      category: 'context',
      linkedContextRefs: [{ fileId: 'f1', kind: 'file-asset' }],
      serverTemplateId: 'pt_pack',
    })
    expect(agentRuleSources([pack], { localIds: false })[0].linkedFiles).toBe(true)
  })

  it('uses the local id where there is no server catalog (demo)', () => {
    expect(agentRuleSources([option({})], { localIds: true })).toEqual([{
      composedChars: null,
      id: 'rule-local-1',
      label: 'briefing',
      linkedFiles: false,
      notSynced: false,
      revision: null,
      title: 'Executive Briefing',
    }])
  })
})

describe('rule ids on submit', () => {
  it('sends each rule once, in attachment order', () => {
    expect(ruleIdsForSubmit(['pt_b', 'pt_a', 'pt_b'])).toEqual(['pt_b', 'pt_a'])
  })

  it('clears the rules only after an accepted submission', () => {
    // Per task, like skills: a refused send keeps everything the user
    // assembled for the retry.
    expect(settleRuleIdsAfterSubmit(['pt_a'], true)).toEqual([])
    expect(settleRuleIdsAfterSubmit(['pt_a'], false)).toEqual(['pt_a'])
  })

  it('holds the rule and document pills back from an answer to a clarification', () => {
    // Rules and documents hold for a TASK; an answer starts none. Spelled
    // into the answer and cleared with it, they would vanish without effect.
    expect(pillKindsKeptByAnswer(true)).toEqual(['rule', 'report', 'canvas', 'editTarget'])
    expect(pillKindsKeptByAnswer(false)).toEqual([])
  })
})

describe('documents in the draft', () => {
  const category = (input: AgentContextSources, id: string) =>
    agentContextCategories(input).find((entry) => entry.id === id)

  it('blocks reports, canvas documents and the editing target while a clarification is pending', () => {
    // An answer starts no task: a document attached to it would do nothing.
    // Each category stays listed with the reason, like the rules.
    const answering = { ...sources, answerMode: true }
    for (const id of ['report', 'canvas', 'document']) {
      const entry = category(answering, id)
      expect(entry?.blocked, id).toBe('answerMode')
      expect(entry?.items.length, id).toBeGreaterThan(0)
      expect(entry?.items.every((item) => item.blocked?.kind === 'answerMode'), id).toBe(true)
    }
  })

  it('holds one editing target: a second document is listed with the cap reason', () => {
    const twoDocuments = {
      ...sources,
      documents: [{ id: 'd1', title: 'Vertrag' }, { id: 'd2', title: 'Anhang' }],
      selectedDocumentId: 'd1',
    }
    const entry = category(twoDocuments, 'document')
    expect(entry?.blocked).toBe('cap')
    expect(entry?.items).toEqual([{ blocked: { kind: 'cap', max: 1 }, id: 'd2', label: 'Anhang', secondary: '' }])
  })

  it('holds one canvas document: a second one is listed with the cap reason', () => {
    const twoCanvases = {
      ...sources,
      canvasDocuments: [
        { artifactId: 'a1', name: 'memo.md', title: 'Memo' },
        { artifactId: 'a2', name: 'notiz.md', title: 'Notiz' },
      ],
      pinnedCanvasDocumentId: 'a1',
    }
    const entry = category(twoCanvases, 'canvas')
    expect(entry?.blocked).toBe('cap')
    expect(entry?.items).toEqual([
      { blocked: { kind: 'cap', max: 1 }, id: 'a2', label: 'notiz.md', secondary: 'Notiz' },
    ])
  })

  it('keeps what an answer holds back, and after a task the editing target', () => {
    expect(pillKindsKeptAfterSend(true)).toEqual(['rule', 'report', 'canvas', 'editTarget'])
    expect(pillKindsKeptAfterSend(false)).toEqual(['editTarget'])
  })
})
