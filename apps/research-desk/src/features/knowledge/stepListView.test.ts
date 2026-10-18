import { describe, expect, it } from 'vitest'
import type { KnowledgeRunStepRecord } from '@/features/project/types'
import {
  KNOWLEDGE_STEP_FOLLOW_SAFE_BOTTOM_PX,
  knowledgeStepFollowOffset,
  knowledgeStepGlyphState,
  knowledgeStepViewportState,
} from './stepListView'

function step(
  id: string,
  status: KnowledgeRunStepRecord['status'] = 'done',
): KnowledgeRunStepRecord {
  return {
    facts: {},
    id,
    kind: id.startsWith('gate') ? 'gate' : 'retrieval',
    status,
  }
}

describe('knowledgeStepViewportState', () => {
  it('enables smart-fade for long live ledgers and tracks the running step', () => {
    const steps = [
      step('profile'),
      step('vocabulary'),
      step('retrieval'),
      step('decompose'),
      step('gate-1'),
      step('gate-2'),
      step('gate-3'),
      step('answer', 'running'),
    ]

    expect(knowledgeStepViewportState({ steps, variant: 'live' })).toEqual({
      activeStepId: 'answer',
      followStepId: 'answer',
      managedScroll: true,
      overflowing: true,
      smartFade: true,
    })
  })

  it('keeps review/default ledgers static even when they contain many steps', () => {
    const steps = [
      step('profile'),
      step('vocabulary'),
      step('retrieval'),
      step('decompose'),
      step('gate-1'),
      step('gate-2'),
      step('gate-3'),
      step('answer', 'running'),
    ]

    expect(knowledgeStepViewportState({ steps, variant: 'default' })).toEqual({
      activeStepId: 'answer',
      followStepId: 'answer',
      managedScroll: false,
      overflowing: false,
      smartFade: false,
    })
  })

  it('does not follow a running step in failed live ledgers', () => {
    const steps = [step('retrieval'), step('answer', 'running')]

    expect(knowledgeStepViewportState({ failed: true, steps, variant: 'live' })).toMatchObject({
      activeStepId: null,
      followStepId: null,
      managedScroll: true,
      smartFade: true,
    })
  })

  it('follows the latest step when no step is currently running', () => {
    const steps = [
      step('profile'),
      step('vocabulary'),
      step('retrieval'),
    ]

    expect(knowledgeStepViewportState({ steps, variant: 'live' })).toMatchObject({
      activeStepId: null,
      followStepId: 'retrieval',
      managedScroll: true,
    })
  })
})

describe('knowledgeStepGlyphState', () => {
  it('keeps completed live steps in the blue completed state', () => {
    expect(knowledgeStepGlyphState({ status: 'done', variant: 'live' })).toBe('complete')
  })

  it('marks only non-failed running steps as pulsing', () => {
    expect(knowledgeStepGlyphState({ status: 'running', variant: 'live' })).toBe('running')
    expect(knowledgeStepGlyphState({ failed: true, status: 'running', variant: 'live' })).toBe('complete')
  })

  it('keeps review/default completed steps visually static', () => {
    expect(knowledgeStepGlyphState({ status: 'done', variant: 'default' })).toBe('review-complete')
  })
})

describe('knowledgeStepFollowOffset', () => {
  it('keeps the followed step above the lower milkglass fade when tail room exists', () => {
    const followBottom = 374
    const viewportHeight = 288
    const offset = knowledgeStepFollowOffset({
      followBottom: 374,
      maxOffset: 114,
      viewportHeight: 288,
    })

    expect(offset).toBe(114)
    expect(viewportHeight - (followBottom - offset)).toBe(KNOWLEDGE_STEP_FOLLOW_SAFE_BOTTOM_PX)
  })

  it('caps the offset when there is not enough tail room', () => {
    expect(knowledgeStepFollowOffset({
      followBottom: 374,
      maxOffset: 20,
      viewportHeight: 288,
    })).toBe(20)
  })
})
