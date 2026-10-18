import { describe, expect, it } from 'vitest'

import {
  answersRequestFromDraft,
  clarificationAnswerSummary,
  isRoundComplete,
  setFreeText,
  toggleOption,
  type RoundAnswerDraft,
} from './clarificationAnswers'
import { agentClarificationFromWire, type AgentClarificationQuestion } from './model'
import type { AgentClarificationWire } from './types'

const SINGLE: AgentClarificationQuestion = {
  id: 'q1',
  prompt: 'Welcher Markt?',
  options: [
    { id: 'q1_o1', label: 'Europa', description: '' },
    { id: 'q1_o2', label: 'USA', description: '' },
  ],
  multiSelect: false,
}

const MULTI: AgentClarificationQuestion = {
  id: 'q2',
  prompt: 'Welche Aspekte?',
  options: [
    { id: 'q2_o1', label: 'Preise', description: '' },
    { id: 'q2_o2', label: 'Anbieter', description: '' },
  ],
  multiSelect: true,
}

describe('toggleOption', () => {
  it('replaces the pick on single-select and untoggles on repeat', () => {
    let draft: RoundAnswerDraft = {}
    draft = toggleOption(draft, SINGLE, 'q1_o1')
    expect(draft.q1.optionIds).toEqual(['q1_o1'])
    draft = toggleOption(draft, SINGLE, 'q1_o2')
    expect(draft.q1.optionIds).toEqual(['q1_o2'])
    draft = toggleOption(draft, SINGLE, 'q1_o2')
    expect(draft.q1.optionIds).toEqual([])
  })

  it('accumulates picks on multi-select', () => {
    let draft: RoundAnswerDraft = {}
    draft = toggleOption(draft, MULTI, 'q2_o1')
    draft = toggleOption(draft, MULTI, 'q2_o2')
    expect(draft.q2.optionIds).toEqual(['q2_o1', 'q2_o2'])
    draft = toggleOption(draft, MULTI, 'q2_o1')
    expect(draft.q2.optionIds).toEqual(['q2_o2'])
  })
})

describe('isRoundComplete', () => {
  it('requires every question resolved by pick or free text', () => {
    let draft: RoundAnswerDraft = {}
    expect(isRoundComplete([SINGLE, MULTI], draft)).toBe(false)
    draft = toggleOption(draft, SINGLE, 'q1_o1')
    expect(isRoundComplete([SINGLE, MULTI], draft)).toBe(false)
    draft = setFreeText(draft, 'q2', 'Fokus auf B2B')
    expect(isRoundComplete([SINGLE, MULTI], draft)).toBe(true)
  })

  it('treats whitespace-only free text as unresolved', () => {
    const draft = setFreeText({}, 'q1', '   ')
    expect(isRoundComplete([SINGLE], draft)).toBe(false)
  })

  it('is false for an empty round (nothing to submit)', () => {
    expect(isRoundComplete([], {})).toBe(false)
  })
})

describe('answersRequestFromDraft', () => {
  it('emits wire-cased entries for every question, trimmed', () => {
    let draft: RoundAnswerDraft = toggleOption({}, SINGLE, 'q1_o1')
    draft = setFreeText(draft, 'q2', '  Fokus auf B2B  ')
    expect(answersRequestFromDraft([SINGLE, MULTI], draft)).toEqual({
      q1: { option_ids: ['q1_o1'], text: '' },
      q2: { option_ids: [], text: 'Fokus auf B2B' },
    })
  })
})

describe('clarificationAnswerSummary', () => {
  const base = {
    clarificationId: 'clr_1',
    question: 'Welcher Markt?',
    options: [{ id: 'legacy_1', label: 'Europa', description: '' }],
    questions: [SINGLE, MULTI],
    answers: {},
    defaultAssumption: '',
    status: 'answered' as const,
    answer: '',
    optionId: '',
    createdAt: 1,
    answeredAt: 2,
  }

  it('composes structured answers per question (labels + text)', () => {
    const lines = clarificationAnswerSummary({
      ...base,
      answers: {
        q1: { optionIds: ['q1_o1'], text: '' },
        q2: { optionIds: ['q2_o1', 'q2_o2'], text: 'Fokus B2B' },
      },
    })
    expect(lines).toEqual([
      { prompt: 'Welcher Markt?', answer: 'Europa' },
      { prompt: 'Welche Aspekte?', answer: 'Preise; Anbieter — Fokus B2B' },
    ])
  })

  it('falls back to the legacy answer / option label', () => {
    expect(
      clarificationAnswerSummary({
        ...base,
        questions: [],
        answer: 'Der europaeische Markt.',
      }),
    ).toEqual([
      { prompt: 'Welcher Markt?', answer: 'Der europaeische Markt.' },
    ])
    expect(
      clarificationAnswerSummary({
        ...base,
        questions: [],
        optionId: 'legacy_1',
      }),
    ).toEqual([{ prompt: 'Welcher Markt?', answer: 'Europa' }])
  })

  it('is empty while pending', () => {
    expect(clarificationAnswerSummary(base)).toEqual([])
  })
})

describe('agentClarificationFromWire', () => {
  it('maps structured questions/answers and defaults for legacy rows', () => {
    const wire: AgentClarificationWire = {
      clarification_id: 'clr_1',
      run_id: 'run_1',
      question: 'Welcher Markt?',
      options: [{ id: 'q1_o1', label: 'Europa' }],
      questions: [
        {
          id: 'q1',
          prompt: 'Welcher Markt?',
          options: [
            { id: 'q1_o1', label: 'Europa', description: 'EU-27' },
          ],
          multi_select: false,
        },
      ],
      answers: { q1: { option_ids: ['q1_o1'], text: '' } },
      default_assumption: '',
      status: 'answered',
      answer: '',
      option_id: '',
      answered_by_user_id: null,
      created_at: 1,
      answered_at: 2,
    }
    const record = agentClarificationFromWire(wire)
    expect(record.questions).toEqual([
      {
        id: 'q1',
        prompt: 'Welcher Markt?',
        options: [{ id: 'q1_o1', label: 'Europa', description: 'EU-27' }],
        multiSelect: false,
      },
    ])
    expect(record.answers).toEqual({
      q1: { optionIds: ['q1_o1'], text: '' },
    })
    // Legacy option rows without description default it to ''.
    expect(record.options[0].description).toBe('')

    const legacy = agentClarificationFromWire({
      ...wire,
      questions: undefined,
      answers: undefined,
    })
    expect(legacy.questions).toEqual([])
    expect(legacy.answers).toEqual({})
  })
})
