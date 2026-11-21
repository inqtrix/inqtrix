import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'
import type { ResearchRunRecord } from '@/features/project/types'
import type { ResearchMetrics } from '@/features/researchRuns/types'
import { attachRunResult } from '@/features/project/types'
import { serializeResearchRun, parseResearchRun } from '@/features/project/markdown'
import { agentArtifactReferences } from '@/features/agent/artifactCitations'
import { evidenceLineageFromArtifactPayload } from '@/features/agent/evidenceProvenance'
import { TooltipProvider } from '@/components/ui/tooltip'
import { LiveRunOverview, ReferenceStatements, ReportEvidence, ReportPanel } from './ReportPanel'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))

describe('research status overview', () => {
  it('retains the full API evidence for inspection without a second local text cache', () => {
    const record: ResearchRunRecord = {
      status: 'running', agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
      runId: 'run-evidence', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
      summary: { title: 'Evidence' }, phaseState: { activePhase: 'search', completedPhases: [] },
      metrics: { rounds: '1 / 1', sources: 1, queries: 1, claims: 0 },
    }
    const reference = { label: 'E1', tier: 'primary', url: 'https://source.example/document',
      source_run_id: 'run-evidence', query_id: 'q' }
    const answer = 'The API specifies the registration condition in full.'
    const ledger = { searches: { q: { source_run_id: 'run-evidence', citations: [{ url: reference.url }], provider_answer: answer } } }
    const completed = attachRunResult(record, {
      run_id: record.runId, status: 'completed', answer: 'A report.', references: [reference], top_sources: [], top_claims: [], web_search_ledger: ledger,
      metrics: { rounds: 1, elapsed_seconds: 2, total_queries: 1, total_citations: 1, confidence: 8,
        claims: { status_counts: {}, quality_score: 0 }, sources: { tier_counts: {}, quality_score: 0 },
        answer_bound_claims_count: 0, aspect_coverage: 0, completion_tokens: 0, prompt_tokens: 0,
        evidence_consistency: 0, evidence_contract_status: 'unknown', evidence_sufficiency: 0,
        unbound_answer_citations_count: 0, verified_claims_used_count: 0 },
    })
    expect(completed.result?.webSearchLedger).toEqual(ledger)
    const [bound] = agentArtifactReferences(completed.result!.references)
    const lineage = evidenceLineageFromArtifactPayload({ web_search_ledger: completed.result?.webSearchLedger }, bound)
    expect(lineage?.searches[0].providerAnswer).toBe(answer)
    const html = renderToStaticMarkup(<ReportEvidence run={completed} />)
    expect(html).toContain('data-testid="research-source-evidence"')
    expect(html).toContain('Beleg ansehen')
    const file = serializeResearchRun(completed)
    expect(file.contents).not.toContain(answer)
    expect(parseResearchRun(file.contents).result?.references[0].url).toBe(reference.url)
  })

  it('names each claim status in the reader language, an unknown one as written', () => {
    const record: ResearchRunRecord = {
      status: 'running', agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
      runId: 'run-claims', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
      summary: { title: 'Claims' }, phaseState: { activePhase: 'search', completedPhases: [] },
      metrics: { rounds: '1 / 1', sources: 1, queries: 1, claims: 0 },
    }
    const claim = (text: string, status: string) => ({
      text, status, claim_type: 'fact', needs_primary: false, status_reason: '',
      support_count: 2, contradict_count: 1, source_tier_counts: {}, sources: [],
    })
    const completed = attachRunResult(record, {
      run_id: record.runId, status: 'completed', answer: 'A report.', references: [], top_sources: [],
      top_claims: [claim('Attributed claim.', 'attributed'), claim('Checked claim.', 'verified'), claim('Odd claim.', 'reviewed')],
      metrics: { rounds: 1, elapsed_seconds: 2, total_queries: 1, total_citations: 1, confidence: 8,
        claims: { status_counts: { attributed: 1, verified: 1 }, quality_score: 0 }, sources: { tier_counts: {}, quality_score: 0 },
        answer_bound_claims_count: 0, aspect_coverage: 0, completion_tokens: 0, prompt_tokens: 0,
        evidence_consistency: 0, evidence_contract_status: 'unknown', evidence_sufficiency: 0,
        unbound_answer_citations_count: 0, verified_claims_used_count: 0 },
    })
    const html = renderToStaticMarkup(<ReportEvidence run={completed} />)
    expect(html).toContain('vom Anbieter zugeschrieben · 2 stützend · 1 widersprechend')
    expect(html).toContain('verifiziert · 2 stützend')
    expect(html).toContain('reviewed · 2 stützend')
    expect(html).not.toContain('support /')
  })

  it('lists the statements the research ties to a source in its details', () => {
    const record: ResearchRunRecord = {
      status: 'running', agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
      runId: 'run-statements', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
      summary: { title: 'Statements' }, phaseState: { activePhase: 'search', completedPhases: [] },
      metrics: { rounds: '1 / 1', sources: 1, queries: 1, claims: 0 },
    }
    const reference = { label: 'E1', tier: 'primary', url: 'https://source.example/law',
      statements: [
        { text: 'Teilregelungen gelten ab dem 2. Dezember 2026.', status: 'attributed', checked: false },
        { text: 'Die Frist endet am 2. August 2026.', status: 'verified', checked: true },
        // Verified through the excerpt of another source: only attributed here.
        { text: 'Die Übergangszeit dauert zwei Jahre.', status: 'verified', checked: false },
        { text: 'Die Behörde bestreitet die Frist.', status: 'contested', checked: false },
        { text: 'Die Zahl der Anträge steigt.', status: 'unverified', checked: false },
      ] }
    const completed = attachRunResult(record, {
      run_id: record.runId, status: 'completed', answer: 'A report.', references: [reference], top_sources: [], top_claims: [],
      metrics: { rounds: 1, elapsed_seconds: 2, total_queries: 1, total_citations: 1, confidence: 8,
        claims: { status_counts: {}, quality_score: 0 }, sources: { tier_counts: {}, quality_score: 0 },
        answer_bound_claims_count: 0, aspect_coverage: 0, completion_tokens: 0, prompt_tokens: 0,
        evidence_consistency: 0, evidence_contract_status: 'unknown', evidence_sufficiency: 0,
        unbound_answer_citations_count: 0, verified_claims_used_count: 0 },
    })
    // The details open even without a search lineage when statements exist.
    expect(renderToStaticMarkup(<ReportEvidence run={completed} />)).toContain('data-testid="research-source-evidence"')
    const html = renderToStaticMarkup(<ReferenceStatements statements={reference.statements} />)
    expect(html).toContain('Aussagen, die die Recherche dieser Quelle zuordnet')
    expect(html).toContain('Teilregelungen gelten ab dem 2. Dezember 2026.</span> <span class="t-label text-muted-foreground">(vom Anbieter zugeschrieben)</span>')
    expect(html).toContain('Die Frist endet am 2. August 2026.</span> <span class="t-label text-muted-foreground">(am Ausschnitt des Suchanbieters zu dieser Quelle geprüft)</span>')
    expect(html).toContain('Die Übergangszeit dauert zwei Jahre.</span> <span class="t-label text-muted-foreground">(vom Anbieter zugeschrieben)</span>')
    expect(html).not.toContain('(verifiziert)')
    expect(html).toContain('Die Behörde bestreitet die Frist.</span> <span class="t-label text-muted-foreground">(widersprüchlich)</span>')
    expect(html).toContain('Die Zahl der Anträge steigt.</span> <span class="t-label text-muted-foreground">(nicht ausreichend belegt)</span>')
    // One word for the search provider's excerpt; no text claims the page itself was checked.
    expect(html).not.toContain('Quelltext')
  })

  it('shows stored terminal duration and distinguishes an unknown duration from zero', () => {
    const record: ResearchRunRecord = {
      status: 'cancelled', durationSeconds: 57,
      agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
      runId: 'run-duration', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
      summary: { title: 'Stored run' },
      phaseState: { activePhase: 'search', completedPhases: [] }, metrics: { rounds: '1 / 1', sources: 5, queries: 1, claims: 0 },
    }
    expect(renderToStaticMarkup(<LiveRunOverview run={record} />)).toContain('00:00:57')
    record.durationSeconds = 0
    expect(renderToStaticMarkup(<LiveRunOverview run={record} />)).toContain('00:00:00')
    record.durationSeconds = undefined
    const missing = renderToStaticMarkup(<LiveRunOverview run={record} />)
    expect(missing).toContain('—')
    expect(missing).not.toContain('00:00:00')
  })
})

describe('the basis and the ceiling in the evidence tab', () => {
  const record: ResearchRunRecord = {
    status: 'running', agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
    runId: 'run-basis', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
    summary: { title: 'Basis' }, phaseState: { activePhase: 'answer', completedPhases: [] },
    metrics: { rounds: '1 / 1', sources: 3, queries: 2, claims: 3 },
  }
  const stored = {
    rounds: 1, elapsed_seconds: 2, total_queries: 2, total_citations: 3, confidence: 6,
    claims: { status_counts: {}, quality_score: 0 }, sources: { tier_counts: {}, quality_score: 0 },
    answer_bound_claims_count: 1, aspect_coverage: 1, completion_tokens: 0, prompt_tokens: 0,
    evidence_consistency: 0, evidence_contract_status: 'needs_review', evidence_sufficiency: 0,
    unbound_answer_citations_count: 0, verified_claims_used_count: 0,
  }
  const render = (metrics: Partial<ResearchMetrics>) => renderToStaticMarkup(<ReportEvidence run={attachRunResult(record, {
    run_id: record.runId, status: 'completed', answer: 'A report.', references: [], top_sources: [], top_claims: [],
    metrics: { ...stored, ...metrics },
  })} />)
  // Expected words are spelled out: a reworded or swapped text must fail here.
  const providerBasis = 'Die Angaben des Suchanbieters zu den zitierten Quellen; die Seiten selbst wurden nicht gelesen.'

  it('names the basis and the ceiling of a clean run the depth gap capped, without a status code', () => {
    const html = render({
      evidence_contract_status: 'clean', evidence_contract_causes: [], confidence_cap: 6,
      confidence_cap_reasons: ['no_cross_checked_claims', 'majority_single_source_claims'],
      evidence_basis: 'provider_context', cited_reference_count: 3, provider_only_reference_count: 3,
    })
    expect(html).toContain('>Grundlage</p>')
    expect(html).toContain(`>${providerBasis}</p>`)
    expect(html).toContain('>Obergrenze der Konfidenz</p>')
    expect(html).toContain('>6 / 10: keine der gestützten Aussagen zählt als gegengeprüft; die meisten gestützten Aussagen nennen nur eine Quelle</p>')
    for (const code of ['Evidence Contract', 'clean', 'no_cross_checked_claims', 'majority_single_source_claims']) {
      expect(html).not.toContain(code)
    }
  })

  it.each([
    [{ evidence_basis: 'provider_context' }, providerBasis],
    [{ evidence_basis: 'evidence', cited_reference_count: 4, provider_only_reference_count: 0 },
      'Das gespeicherte Material der zitierten Quellen, nicht allein die Angaben des Suchanbieters.'],
    [{ evidence_basis: 'evidence', cited_reference_count: 5, provider_only_reference_count: 2 },
      'Das gespeicherte Material der zitierten Quellen, nicht allein die Angaben des Suchanbieters. Zitierte Quellen nur mit den Angaben des Suchanbieters: 2 von 5.'],
    [{ evidence_basis: 'none' }, 'Keine, der Bericht zitiert keine Quelle.'],
  ])('reads the basis %j as the server line does', (metrics, words) => {
    // The whole cell text: a pure basis must not grow a mixed count.
    expect(render(metrics)).toContain(`>${words}</p>`)
  })

  it('shows nothing invented for a result stored before the fields', () => {
    // The stored needs_review may stand for the depth gap alone: no cause,
    // no ceiling and no basis are read into it.
    const html = render({})
    expect(html).toContain('>Grundlage</p>')
    expect(html).toContain('>Nicht erfasst.</p>')
    expect(html).not.toContain('Obergrenze der Konfidenz')
    expect(html).not.toContain('needs_review')
  })

  it('leaves out a reason it has no words for instead of showing its code', () => {
    const html = render({
      confidence_cap: 6, confidence_cap_reasons: ['citation_without_claim', 'reason_without_words'],
      evidence_basis: 'provider_context',
    })
    expect(html).toContain('>6 / 10: zu einer zitierten Quelle ist keine Aussage erfasst</p>')
    expect(html).not.toContain('reason_without_words')
  })

  it('lets the fifth figure fill its row and keeps it a figure', () => {
    // Two columns hold four figures; the wide cells follow the fifth, so it
    // would stand alone in its row. It spans the grid in the figure style.
    const html = render({ evidence_basis: 'provider_context' })
    const cell = (label: string) => html.match(
      new RegExp(`<div class="([^"]*)"><p class="[^"]*">${label}</p><p class="([^"]*)">`),
    )
    const coverage = cell('Teilfragen-Abdeckung')
    expect(coverage?.[1]).toContain('sm:col-span-2')
    expect(coverage?.[2]).toContain('font-semibold')
    for (const label of ['Konfidenz', 'Quellen', 'Quellenqualität', 'Aussagenqualität']) {
      const figure = cell(label)
      expect(figure, label).not.toBeNull()
      expect(figure?.[1], label).not.toContain('sm:col-span-2')
    }
  })
})

describe('the step log of a finished run after a reload', () => {
  // A reload brings the run's result, not its step log; the server still
  // holds every step. The log asks for them and says it is loading, not
  // that none were stored.
  const failed: ResearchRunRecord = {
    status: 'failed', agentOverrides: {}, createdAt: '2026-01-01T10:00:00Z', events: [],
    runId: 'run-steps', source: 'api', stack: 'default', submittedAt: '2026-01-01T10:00:00Z',
    summary: { title: 'Steps' }, phaseState: { activePhase: 'search', completedPhases: ['analysis'] },
    metrics: { rounds: '1 / 1', sources: 0, queries: 2, claims: 0 },
  }
  const render = (props: Partial<Parameters<typeof ReportPanel>[0]>) => renderToStaticMarkup(
    <TooltipProvider>
      <ReportPanel onHide={() => undefined} selectedRun={failed} {...props} />
    </TooltipProvider>,
  )

  it('says it is loading while the stored steps are on their way', () => {
    const html = render({ onRequestSteps: () => undefined, stepsLoad: 'loading' })
    expect(html).toContain(translations.de.report.agentStepsLoading)
    expect(html).not.toContain(translations.de.report.agentStepsEmpty)
  })

  it('says none were stored only once the server answered with none', () => {
    expect(render({ onRequestSteps: () => undefined, stepsLoad: 'done' }))
      .toContain(translations.de.report.agentStepsEmpty)
    // A run this view cannot ask the server for (a local one) has what it holds.
    expect(render({})).toContain(translations.de.report.agentStepsEmpty)
  })
})
