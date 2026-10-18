import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'
import type { ResearchRunRecord } from '@/features/project/types'
import { attachRunResult } from '@/features/project/types'
import { serializeResearchRun, parseResearchRun } from '@/features/project/markdown'
import { agentArtifactReferences } from '@/features/agent/artifactCitations'
import { evidenceLineageFromArtifactPayload } from '@/features/agent/evidenceProvenance'
import { LiveRunOverview, ReferenceStatements, ReportEvidence } from './ReportPanel'

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
    expect(html).toContain('Die Frist endet am 2. August 2026.</span> <span class="t-label text-muted-foreground">(am Quelltext dieser Quelle geprüft)</span>')
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
