import { describe, expect, it } from 'vitest'
import {
  DEFAULT_ATTACHMENT_BUDGET_TOKENS,
  estimateTokens,
  evaluateBudget,
  MAX_DOC_CHARS_SOFT,
  shouldShowAttachmentBudgetNotice,
} from './budget'

describe('estimateTokens', () => {
  it('uses the ~4-chars-per-token heuristic and rounds up', () => {
    expect(estimateTokens(0)).toBe(0)
    expect(estimateTokens(4)).toBe(1)
    expect(estimateTokens(5)).toBe(2)
  })
})

describe('evaluateBudget', () => {
  it('reports within-budget for small documents', () => {
    const result = evaluateBudget([{ content: 'short', label: 'a' }])
    expect(result.withinBudget).toBe(true)
    expect(result.overBy).toBe(0)
    expect(result.offenders).toEqual([])
    expect(result.limitTokens).toBe(DEFAULT_ATTACHMENT_BUDGET_TOKENS)
  })

  it('flags documents over the per-document soft cap as offenders', () => {
    const result = evaluateBudget([{ content: 'x'.repeat(MAX_DOC_CHARS_SOFT + 4), label: 'big' }])
    expect(result.offenders).toEqual(['big'])
  })

  it('reports over-budget against a passed model context limit', () => {
    const result = evaluateBudget(
      [{ content: 'x'.repeat(4000), label: 'a' }],
      { limitTokens: 500 },
    )
    expect(result.withinBudget).toBe(false)
    expect(result.overBy).toBe(estimateTokens(4000) - 500)
  })

  it('shows the attachment notice only after the fixed request budget is exceeded', () => {
    const halfBudget = evaluateBudget([{
      content: 'x'.repeat((DEFAULT_ATTACHMENT_BUDGET_TOKENS / 2) * 4 + 4),
      label: 'half',
    }])
    const overBudget = evaluateBudget([{
      content: 'x'.repeat((DEFAULT_ATTACHMENT_BUDGET_TOKENS + 1) * 4),
      label: 'over',
    }])

    expect(shouldShowAttachmentBudgetNotice(halfBudget)).toBe(false)
    expect(shouldShowAttachmentBudgetNotice(overBudget)).toBe(true)
  })
})
