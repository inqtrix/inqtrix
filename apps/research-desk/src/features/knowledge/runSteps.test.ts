import { describe, expect, it } from 'vitest'
import type { ResearchRunEvent } from '@/features/researchRuns/types'
import type { KnowledgeRunProgressRecord } from '@/features/project/types'
import { applyKnowledgeRunEvent } from './runSteps'

function event(type: string, data: Record<string, unknown>): ResearchRunEvent {
  return {
    created_at: 1_770_000_000,
    data,
    run_id: 'run-1',
    sequence: 1,
    type,
  }
}

function reduceEvents(events: ResearchRunEvent[]): KnowledgeRunProgressRecord {
  return events.reduce<KnowledgeRunProgressRecord>(
    (progress, item) => applyKnowledgeRunEvent(progress, item),
    { steps: [] },
  )
}

const profileResolved = event('inqtrix.knowledge.profile.resolved', {
  auto_reason: 'question_complexity',
  auto_selected: true,
  decompose: false,
  degraded_stages: ['rerank'],
  gate_rounds: 2,
  grounding: true,
  profile: 'gruendlich',
  report: false,
  requested_profile: null,
  rerank: false,
  vocabulary_bridge: true,
})

describe('applyKnowledgeRunEvent', () => {
  it('records follow-up contextualization before the retrieval plan', () => {
    const progress = reduceEvents([
      event('inqtrix.knowledge.contextualized', {
        marker: '_knowledge_query_context_applied',
        rewritten: true,
        used_history: true,
      }),
      profileResolved,
    ])

    expect(progress.steps.map((step) => [step.kind, step.status])).toEqual([
      ['context', 'done'],
      ['profile', 'done'],
      ['vocabulary', 'running'],
      ['retrieval', 'running'],
    ])
    expect(progress.steps[0].facts).toMatchObject({
      contextMarker: '_knowledge_query_context_applied',
      rewritten: true,
    })
  })

  it('captures the resolved plan and opens vocabulary + retrieval steps', () => {
    const progress = reduceEvents([profileResolved])

    expect(progress.plan).toMatchObject({
      autoSelected: true,
      degradedStages: ['rerank'],
      gateRounds: 2,
      grounding: true,
      profile: 'gruendlich',
      vocabularyBridge: true,
    })
    expect(progress.steps.map((step) => [step.kind, step.status])).toEqual([
      ['profile', 'done'],
      ['vocabulary', 'running'],
      ['retrieval', 'running'],
    ])
    expect(progress.steps[0].facts).toMatchObject({
      autoSelected: true,
      degradedStages: ['rerank'],
      profile: 'gruendlich',
    })
  })

  it('finishes vocabulary and retrieval on retrieval.completed and keeps the gate pending', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', {
        candidate_count: 24,
        embedding_model: 'embed',
        top_k: 8,
      }),
    ])

    expect(progress.steps.map((step) => [step.id, step.status])).toEqual([
      ['profile', 'done'],
      ['vocabulary', 'done'],
      ['retrieval', 'done'],
    ])
    expect(progress.steps[2].facts.candidateCount).toBe(24)
    // gate_rounds > 0: the answer step must wait for the gate verdict.
    expect(progress.steps.some((step) => step.kind === 'answer')).toBe(false)
  })

  it('retains and deduplicates technical retrieval degradation events', () => {
    const completeDegradation = {
      candidate_cap: 64,
      final_evidence_complete: true,
      final_top_k: 4,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 40,
      requested_top_k: 4,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 8,
      returned_hits: 4,
      stage: 'vector_candidate_pool',
    }
    const incompleteDegradation = {
      candidate_cap: null,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_candidate_stalled',
      requested_candidate_pool: 40,
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 7,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    }
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.degraded', completeDegradation),
      event('inqtrix.knowledge.retrieval.degraded', incompleteDegradation),
      event('inqtrix.knowledge.retrieval.completed', {
        candidate_count: 3,
        degradations: [completeDegradation, incompleteDegradation],
        final_k: 8,
        top_k: 8,
      }),
    ])

    expect(progress.steps.find((step) => step.id === 'retrieval')).toMatchObject({
      facts: {
        retrievalDegradations: [completeDegradation, incompleteDegradation],
      },
      status: 'done',
    })
  })

  it('retains cumulative source-integrity warnings from completion and live events', () => {
    const initial = {
      code: 'chunks_require_reindex',
      count: 1,
      message: 'server fallback',
      reason: 'source_unverified',
      recommended_action: 'reindex',
      stage: 'canonical_hydration',
    }
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', {
        candidate_count: 3,
        top_k: 8,
        warnings: [initial],
      }),
      event('inqtrix.knowledge.retrieval.warning', {
        ...initial,
        count: 4,
      }),
    ])

    expect(progress.steps.find((step) => step.id === 'retrieval')).toMatchObject({
      facts: {
        retrievalWarnings: [{ ...initial, count: 4 }],
      },
      status: 'done',
    })
  })

  it('appends the answer step directly after retrieval when no gate is planned', () => {
    const noGateProfile = event('inqtrix.knowledge.profile.resolved', {
      auto_selected: false,
      gate_rounds: 0,
      grounding: false,
      profile: 'schnell',
      vocabulary_bridge: false,
    })
    const progress = reduceEvents([
      noGateProfile,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 5, top_k: 8 }),
    ])

    expect(progress.steps.map((step) => step.kind)).toEqual(['profile', 'retrieval', 'answer'])
    expect(progress.steps.at(-1)?.status).toBe('running')
  })

  it('numbers gate rounds from 1 and starts the answer once sufficient', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 24, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', {
        marker: 'm',
        rewritten: true,
        round: 0,
        sufficient: false,
      }),
      event('inqtrix.knowledge.gate.evaluated', {
        marker: 'm',
        rewritten: false,
        round: 1,
        sufficient: true,
      }),
    ])

    const gates = progress.steps.filter((step) => step.kind === 'gate')
    expect(gates).toHaveLength(2)
    // round 0 = first judgement; total = rewrites (2) + initial = 3.
    expect(gates[0].facts).toMatchObject({ gateMarker: 'm', rewritten: true, round: 1, roundsTotal: 3, sufficient: false })
    expect(gates[1].facts).toMatchObject({ gateMarker: 'm', round: 2, roundsTotal: 3, sufficient: true })
    expect(progress.steps.at(-1)).toMatchObject({ kind: 'answer', status: 'running' })
  })

  it('records decomposition and evidence truncation facts', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.decomposition.completed', { marker: 'm', sub_query_count: 3 }),
      event('inqtrix.knowledge.evidence.truncated', { dropped: 18, kept: 6 }),
    ])

    expect(progress.steps.find((step) => step.kind === 'decompose')?.facts.subQueryCount).toBe(3)
    expect(progress.steps.find((step) => step.kind === 'evidence')?.facts).toMatchObject({
      dropped: 18,
      kept: 6,
    })
  })

  it('captures a deep demo-style run with four evidence gate rounds before answering', () => {
    const deepProfile = event('inqtrix.knowledge.profile.resolved', {
      auto_selected: false,
      decompose: true,
      degraded_stages: ['rerank'],
      gate_rounds: 3,
      grounding: true,
      profile: 'tief',
      requested_profile: 'tief',
      vocabulary_bridge: true,
    })
    const progress = reduceEvents([
      deepProfile,
      event('inqtrix.knowledge.decomposition.completed', { sub_query_count: 0 }),
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 8, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', { round: 0, rewritten: true, sufficient: false }),
      event('inqtrix.knowledge.gate.evaluated', { round: 1, rewritten: true, sufficient: false }),
      event('inqtrix.knowledge.gate.evaluated', { round: 2, rewritten: true, sufficient: false }),
      event('inqtrix.knowledge.gate.evaluated', { round: 3, rewritten: true, sufficient: false }),
    ])

    expect(progress.plan).toMatchObject({
      degradedStages: ['rerank'],
      gateRounds: 3,
      profile: 'tief',
      vocabularyBridge: true,
    })
    expect(progress.steps.map((step) => step.kind)).toEqual([
      'profile',
      'vocabulary',
      'retrieval',
      'decompose',
      'gate',
      'gate',
      'gate',
      'gate',
      'answer',
    ])
    expect(progress.steps.find((step) => step.kind === 'retrieval')?.facts.candidateCount).toBe(8)
    expect(progress.steps.find((step) => step.kind === 'decompose')?.facts.subQueryCount).toBe(0)
    const gates = progress.steps.filter((step) => step.kind === 'gate')
    expect(gates).toHaveLength(4)
    expect(gates.at(-1)?.facts).toMatchObject({
      rewritten: true,
      round: 4,
      roundsTotal: 4,
      sufficient: false,
    })
    expect(progress.steps.at(-1)).toMatchObject({ kind: 'answer', status: 'running' })
  })

  it('captures final_k and the override marker from retrieval.completed', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', {
        candidate_count: 16,
        final_k: 16,
        final_k_overridden: true,
        top_k: 8,
      }),
    ])
    const retrieval = progress.steps.find((step) => step.kind === 'retrieval')
    expect(retrieval?.facts.finalK).toBe(16)
    expect(retrieval?.facts.finalKOverridden).toBe(true)
  })

  it('closes the answer step and appends grounding on grounding.checked', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 24, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', { round: 0, rewritten: false, sufficient: true }),
      event('inqtrix.knowledge.grounding.checked', {
        marker: 'm',
        quotes_total: 6,
        quotes_verified: 6,
      }),
    ])

    expect(progress.steps.find((step) => step.kind === 'answer')?.status).toBe('done')
    expect(progress.steps.at(-1)).toMatchObject({
      facts: { quotesTotal: 6, quotesVerified: 6 },
      kind: 'grounding',
      status: 'done',
    })
  })

  it('represents the gate early-stop and still appends + resolves the answer step', () => {
    // The reported Deep scenario: round 1 insufficient + rewrite, the rewrite
    // adds no new evidence (gate.exhausted), then grounding runs. The ledger
    // must show the exhaustion AND a completed answer step (not a frozen list).
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 16, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', { round: 0, rewritten: true, sufficient: false }),
      event('inqtrix.knowledge.gate.exhausted', { reason: 'no_new_evidence', round: 1 }),
      event('inqtrix.knowledge.grounding.checked', { quotes_total: 28, quotes_verified: 28 }),
      event('inqtrix.run.completed', {}),
    ])

    expect(progress.steps.map((step) => step.kind)).toEqual([
      'profile',
      'vocabulary',
      'retrieval',
      'gate',
      'gate-exhausted',
      'answer',
      'grounding',
    ])
    expect(progress.steps.find((step) => step.kind === 'answer')?.status).toBe('done')
    expect(progress.steps.every((step) => step.status === 'done')).toBe(true)
  })

  it('appends + resolves the answer step on early-stop even when grounding is off', () => {
    // Grounding disabled (no grounding.checked): the answer step must still be
    // created by gate.exhausted and resolved by the terminal run event.
    const noGroundingProfile = event('inqtrix.knowledge.profile.resolved', {
      auto_selected: false,
      decompose: false,
      gate_rounds: 3,
      grounding: false,
      profile: 'tief',
      vocabulary_bridge: false,
    })
    const progress = reduceEvents([
      noGroundingProfile,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 12, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', { round: 0, rewritten: true, sufficient: false }),
      event('inqtrix.knowledge.gate.exhausted', { reason: 'no_new_evidence', round: 1 }),
      event('inqtrix.run.completed', {}),
    ])

    const answer = progress.steps.find((step) => step.kind === 'answer')
    expect(answer).toBeDefined()
    expect(answer?.status).toBe('done')
    expect(progress.steps.some((step) => step.kind === 'gate-exhausted')).toBe(true)
  })

  it('marks every remaining step done on terminal run events', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.run.failed', { error: { message: 'boom' } }),
    ])

    expect(progress.steps.every((step) => step.status === 'done')).toBe(true)
  })

  it('retains the grounding fallback marker even when no quote row exists', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.grounding.checked', {
        marker: '_knowledge_grounding_fallback',
        quotes_total: 0,
        quotes_verified: 0,
      }),
    ])

    expect(progress.steps.find((step) => step.kind === 'grounding')?.facts)
      .toMatchObject({
        groundingMarker: '_knowledge_grounding_fallback',
        quotesTotal: 0,
        quotesVerified: 0,
      })
  })

  it('ignores unrelated event types', () => {
    const before = reduceEvents([profileResolved])
    const after = applyKnowledgeRunEvent(before, event('inqtrix.progress.message', { message: 'x' }))
    expect(after).toBe(before)
  })

  it('keeps the answer retry as its OWN step between answer and grounding', () => {
    // Anti-overwrite proof: the retry must never upsert into the 'answer'
    // step — a hidden second attempt would be exactly the silent retry the
    // step exists to rule out.
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 16, top_k: 8 }),
      event('inqtrix.knowledge.gate.evaluated', { round: 0, rewritten: false, sufficient: true }),
      event('inqtrix.knowledge.answer.retry', {
        attempt: 2,
        quotes_total: 10,
        quotes_unverified: 1,
      }),
      event('inqtrix.knowledge.grounding.checked', {
        marker: 'm',
        quotes_total: 10,
        quotes_verified: 10,
      }),
    ])

    const kinds = progress.steps.map((step) => step.kind)
    expect(kinds.indexOf('answer')).toBeGreaterThanOrEqual(0)
    expect(kinds.indexOf('answer-retry')).toBe(kinds.indexOf('answer') + 1)
    expect(kinds.indexOf('grounding')).toBe(kinds.indexOf('answer-retry') + 1)
    // Both attempts coexist with their own ids and end resolved.
    expect(progress.steps.find((step) => step.id === 'answer')?.status).toBe('done')
    expect(progress.steps.find((step) => step.id === 'answer-retry')).toMatchObject({
      facts: { quotesTotal: 10, quotesUnverified: 1 },
      status: 'done',
    })
  })

  it('shows a running retry step while the second attempt is in flight', () => {
    const progress = reduceEvents([
      profileResolved,
      event('inqtrix.knowledge.retrieval.completed', { candidate_count: 16, top_k: 8 }),
      event('inqtrix.knowledge.answer.retry', {
        attempt: 2,
        quotes_total: 10,
        quotes_unverified: 2,
      }),
    ])

    expect(progress.steps.find((step) => step.id === 'answer')?.status).toBe('done')
    expect(progress.steps.at(-1)).toMatchObject({
      id: 'answer-retry',
      kind: 'answer-retry',
      status: 'running',
    })
  })

  it('renders the dispatch queue as a real step and settles it on start', () => {
    const progress = reduceEvents([
      event('inqtrix.run.queued', { queue_position: 1, status: 'queued' }),
    ])
    expect(progress.steps.map((step) => [step.kind, step.status])).toEqual([
      ['queued', 'running'],
    ])

    const started = applyKnowledgeRunEvent(
      progress,
      event('inqtrix.run.started', { status: 'running' }),
    )
    expect(started.steps).toEqual([
      { facts: {}, id: 'queued', kind: 'queued', status: 'done' },
    ])
  })

  it('settles the queued step on segment resume and re-opens it when a segment re-queues', () => {
    const requeued = reduceEvents([
      event('inqtrix.run.queued', { queue_position: 1, status: 'queued' }),
      event('inqtrix.run.resumed', { status: 'running' }),
      event('inqtrix.run.queued', { queue_position: 2, resumed: true, status: 'queued' }),
    ])
    expect(requeued.steps).toEqual([
      { facts: {}, id: 'queued', kind: 'queued', status: 'running' },
    ])
  })

  it('keeps a single queued step across duplicate queued frames', () => {
    const progress = reduceEvents([
      event('inqtrix.run.queued', { queue_position: 1, status: 'queued' }),
      event('inqtrix.run.queued', { queue_position: 1, status: 'queued' }),
    ])
    expect(progress.steps).toHaveLength(1)
  })
})
