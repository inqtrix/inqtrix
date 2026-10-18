import { describe, expect, it } from 'vitest'
import { buildDemoAskScript } from './demo'

describe('buildDemoAskScript', () => {
  it('drives the deep RAG demo twin without a backend request', () => {
    const script = buildDemoAskScript('kn-demo-test')
    const eventTypes = script.steps.map((step) => step.event.type)
    const profile = script.steps.find((step) =>
      step.event.type === 'inqtrix.knowledge.profile.resolved')?.event
    const gates = script.steps.filter((step) =>
      step.event.type === 'inqtrix.knowledge.gate.evaluated')

    expect(profile?.data).toMatchObject({
      auto_selected: false,
      decompose: true,
      degraded_stages: ['rerank'],
      gate_rounds: 3,
      profile: 'tief',
      requested_profile: 'tief',
      vocabulary_bridge: true,
    })
    // Early-stop scenario: one gate judgement + a rewrite that adds no new
    // evidence → gate.exhausted, then the answer path including the single
    // visible regeneration (demonstrates the answer-retry step) and grounding.
    expect(eventTypes).toEqual([
      'inqtrix.knowledge.profile.resolved',
      'inqtrix.knowledge.decomposition.completed',
      'inqtrix.knowledge.retrieval.completed',
      'inqtrix.knowledge.gate.evaluated',
      'inqtrix.knowledge.gate.exhausted',
      'inqtrix.knowledge.answer.retry',
      'inqtrix.knowledge.grounding.checked',
    ])
    expect(gates).toHaveLength(1)
    expect(gates[0].event.data).toEqual(
      expect.objectContaining({ rewritten: true, round: 0, sufficient: false }),
    )
    expect(eventTypes).not.toContain('inqtrix.knowledge.evidence.truncated')
  })
})
