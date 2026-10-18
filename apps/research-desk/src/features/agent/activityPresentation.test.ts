import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import {
  agentActivityIconKind,
  activityDisplayText,
  discoveryProbeDisplay,
  normalizeAgentOperation,
  terminalActivityErrorIndex,
  activityStepDisplayText,
  kernelToolLabel,
} from './activityPresentation'
import type { AgentStepEntry } from './model'

describe('agent activity presentation', () => {
  const de = translations.de
  it('normalizes legacy capability ids without exposing them as labels', () => {
    expect(normalizeAgentOperation('knowledge.search')).toBe('knowledge_search')
    expect(activityDisplayText({
      count: 4,
      detail: 'knowledge.search',
      kind: 'searching',
      operation: 'knowledge_search',
    }, de)).toBe('Durchsucht Projektwissen · 4 Vorgänge')
  })

  it('names the kernel model turn with one word, running or settled', () => {
    // The regression: one row flipped between "Denkt nach" and
    // "Überlegung" each time a model turn started and settled. The state
    // is the glyph's job; the word stays.
    expect(normalizeAgentOperation('agent.model.turn')).toBe('model_turn')
    const turn = { detail: 'agent.model.turn', kind: 'working', operation: 'model_turn' } as const
    const running = activityDisplayText({ ...turn, status: 'running' }, de)
    expect(running).toBe('Überlegung')
    expect(activityDisplayText({ ...turn, status: 'completed' }, de)).toBe(running)
    expect(activityDisplayText(turn, de)).toBe(running)
    // A failed turn still says so in words.
    expect(activityDisplayText({ ...turn, count: 2, status: 'failed' }, de))
      .toContain('2 Vorgänge · Fehlgeschlagen')
  })

  it('assigns source-semantic timeline icon kinds', () => {
    expect(agentActivityIconKind('web_instant')).toBe('web')
    expect(agentActivityIconKind('knowledge_search')).toBe('knowledge')
    expect(agentActivityIconKind('knowledge_collections')).toBe('knowledge')
    expect(agentActivityIconKind('discovery_summary')).toBe('generic')
  })

  it('keeps the concrete purpose and deterministic progress', () => {
    expect(activityDisplayText({
      current: 2,
      detail: 'Regulatorische Auswirkungen in Deutschland',
      kind: 'searching',
      operation: 'knowledge_search',
      total: 4,
    }, de)).toBe(
      'Durchsucht Projektwissen · Regulatorische Auswirkungen in Deutschland · 2/4',
    )
  })

  it('uses one semantic presenter for discovery approvals', () => {
    expect(discoveryProbeDisplay({
      kind: 'web.search.instant',
      query: 'Welche Marktprognosen widersprechen sich?',
    }, de)).toEqual({
      detail: 'Welche Marktprognosen widersprechen sich?',
      title: 'Führt eine Instant-Websuche aus',
    })
  })

  it('leaves an unknown operation visible', () => {
    expect(discoveryProbeDisplay({ kind: 'custom.read' }, de).title).toBe('custom.read')
    expect(activityDisplayText({
      detail: '',
      kind: 'working',
      operationCode: 'vendor.custom.lookup',
    }, de)).toBe('vendor.custom.lookup')
  })

  it('names evidence retrieval and its registered subject without implying verification', () => {
    expect(normalizeAgentOperation('read_evidence')).toBe('evidence_read')
    expect(agentActivityIconKind('evidence_read')).toBe('knowledge')
    expect(kernelToolLabel('read_evidence', de)).toBe('Belege nachlesen')
    expect(activityDisplayText({ kind: 'searching', label: 'read_evidence', detail: 'Original study', status: 'running' }, de))
      .toBe('Liest vorhandene Belege · Original study')
    expect(activityDisplayText({ kind: 'searching', label: 'read_evidence', detail: 'Original study', status: 'completed' }, de))
      .toBe('Vorhandene Belege aufgerufen · Original study')
  })

  it('names the reading of an attached research report, never by the raw tool name', () => {
    // The timeline row of this tool showed 'read_research_report' itself.
    expect(kernelToolLabel('read_research_report', de)).toBe('Recherchebericht lesen')
    expect(kernelToolLabel('read_research_report', translations.en)).toBe('Read research report')
  })

  it('names the citation of read passages by what it does, never by the raw tool name', () => {
    expect(kernelToolLabel('cite_document_passages', de)).toBe('Stellen belegen')
    expect(kernelToolLabel('cite_document_passages', translations.en)).toBe('Cite passages')
    expect(activityDisplayText({
      kind: 'searching',
      label: 'cite_document_passages',
      detail: 'Dokumenttext ab Zeichen 40.001, 3 Stellen',
      status: 'completed',
    }, de)).toBe('Stellen belegen · Dokumenttext ab Zeichen 40.001, 3 Stellen')
  })

  it('shows a whole-document reading with its window progress against every planned window', () => {
    expect(normalizeAgentOperation('document_analysis')).toBe('document_analysis')
    expect(agentActivityIconKind('document_analysis')).toBe('knowledge')
    const reading = { kind: 'reading', operation: 'document_analysis', detail: 'KI-Verordnung', current: 4, total: 32 } as const
    expect(activityDisplayText({ ...reading, status: 'running' }, de))
      .toBe('Liest ein Dokument Abschnitt für Abschnitt · KI-Verordnung · 4/32')
    // A budget stop settles at 4 of 32: the row never claims the whole document.
    expect(activityDisplayText({ ...reading, status: 'completed' }, de))
      .toBe('Dokumentanalyse abgeschlossen · KI-Verordnung · 4/32')
  })

  // `model_turn` keeps ONE word across states (its own test below): every
  // turn rides one row, and its glyph carries running versus settled.
  it.each(['web_instant', 'knowledge_search', 'knowledge_collections', 'discovery_summary', 'document_analysis'] as const)(
    'does not describe a settled %s invocation as ongoing work', (operation) => {
      const activity = { kind: 'working', operation, detail: 'Subject', count: 2 }
      const running = activityDisplayText({ ...activity, status: 'running' }, de)
      const completed = activityDisplayText({ ...activity, status: 'completed' }, de)
      const failed = activityDisplayText({ ...activity, status: 'failed' }, de)
      expect(completed).not.toBe(running)
      expect(failed).not.toContain(running.split(' · ')[0])
      expect(failed).toContain('2 Vorgänge · Fehlgeschlagen')
    },
  )

  it('keeps technical operation ids behind a human fallback explanation', () => {
    expect(activityDisplayText({
      detail: 'Veraltetes Task-Budget wird ignoriert',
      kind: 'working',
      operationCode: 'task.legacy_budget_ignored',
    }, de)).toBe('Veraltetes Task-Budget wird ignoriert')
  })

  it('renders operation result metrics without relabelling them as sources', () => {
    expect(activityDisplayText({
      detail: 'Welche Marktprognosen widersprechen sich?',
      kind: 'searching',
      metrics: { result_count: 4 },
      operation: 'web_instant',
    }, de)).toContain('4 Ergebnisse')
  })

  it('counts a single result, reference and claim in the singular', () => {
    // A plural-only count would read
    // "Projektwissen-Suche · 1/1 · 1 Ergebnisse".
    const search = { detail: '', kind: 'searching', operation: 'knowledge_search', status: 'completed', current: 1, total: 1 } as const
    expect(activityDisplayText({ ...search, metrics: { result_count: 1, reference_count: 1, claim_count: 1 } }, de))
      .toBe('Projektwissen-Suche · 1/1 · 1 Ergebnis · 1 Beleg · 1 Aussage')
    expect(activityDisplayText({ ...search, metrics: { result_count: 2, reference_count: 0, claim_count: 3 } }, de))
      .toBe('Projektwissen-Suche · 1/1 · 2 Ergebnisse · 0 Belege · 3 Aussagen')
  })

  it('counts what a collection listing returns as collections', () => {
    const listing = { detail: '', kind: 'searching', operation: 'knowledge_collections', status: 'completed', current: 1, total: 1 } as const
    expect(activityDisplayText({ ...listing, metrics: { result_count: 1 } }, de))
      .toBe('Wissenssammlungen · 1/1 · 1 Sammlung')
    expect(activityDisplayText({ ...listing, metrics: { result_count: 2 } }, de))
      .toBe('Wissenssammlungen · 1/1 · 2 Sammlungen')
  })

  it('says a failed reading failed instead of calling it finished', () => {
    const reading = { kind: 'reading', operation: 'document_analysis', detail: 'KI-Verordnung', total: 32 } as const
    const unread = activityDisplayText({ ...reading, current: 0, status: 'failed' }, de)
    expect(unread).toBe('Dokument konnte nicht gelesen werden · KI-Verordnung · 0/32 · Fehlgeschlagen')
    expect(unread).not.toContain(de.agent.activityOperations.documentAnalysisSummary)
  })

  it('calls a failed reading with windows already read a partial analysis, not an unread document', () => {
    // "konnte nicht gelesen werden … 4/32" would contradict its own
    // progress — the citation step can fail after windows were read.
    const reading = { kind: 'reading', operation: 'document_analysis', detail: 'KI-Verordnung', current: 4, total: 32 } as const
    const partial = activityDisplayText({ ...reading, status: 'failed' }, de)
    expect(partial).toBe('Dokument nur teilweise ausgewertet · KI-Verordnung · 4/32 · Fehlgeschlagen')
    expect(activityDisplayText({ ...reading, status: 'failed' }, translations.en))
      .toBe('Document only partly analysed · KI-Verordnung · 4/32 · Failed')
  })

  it('suppresses only the latest activity copy of a terminal task error', () => {
    const history = [
      { error: 'Provider timeout', status: 'failed' },
      { error: 'Recovered', status: 'completed' },
      { error: 'Provider timeout', status: 'failed' },
    ]
    expect(terminalActivityErrorIndex(history, 'Provider timeout')).toBe(2)
    expect(terminalActivityErrorIndex(history, 'Different error')).toBe(-1)
  })
})

describe('activityStepDisplayText', () => {
  it('names a read canvas document by its title and never by its id', () => {
    // The row once read "Read canvas · {\"artifact_id\": \"art_…\"}" and
    // fell back to the bare id; the id now arrives as data beside the row.
    const names: Record<string, string> = { art_1: 'wochenueberblick.md' }
    const row: AgentStepEntry = {
      activityKind: 'searching',
      activityOperationCode: 'read_canvas',
      artifactId: 'art_1',
      at: 2,
      kind: 'activity',
      label: 'read_canvas',
      seq: 1,
      status: 'completed',
    }
    const de = translations.de
    expect(activityStepDisplayText(row, de, (id) => names[id])).toBe('Canvas lesen · wochenueberblick.md')
    expect(activityStepDisplayText({ ...row, artifactId: 'art_unknown' }, de, (id) => names[id]))
      .toBe('Canvas lesen')
  })
})
