import { describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'

import { bindingEndText, questionToAskAgain, restartChoice } from './bindingEnd'

const t = translations.de

describe('a run its workflow binding ended', () => {
  it.each([
    ['workflow_contract_changed', t.agent.timeline.cancelledWorkflowChanged],
    ['workflow_retired', t.agent.timeline.cancelledWorkflowRetired],
    ['cancelled_by_admin', t.agent.timeline.cancelledByAdmin],
  ])('names why it ended (%s)', (reason, text) => {
    expect(bindingEndText({ status: 'cancelled', cancelReason: reason }, t)).toBe(text)
  })

  it.each([
    [{ status: 'cancelled' as const, cancelReason: 'client_requested_cancel' }],
    [{ status: 'cancelled' as const, cancelReason: 'parent_cancelled' }],
    [{ status: 'cancelled' as const }],
    [{ status: 'failed' as const, cancelReason: 'workflow_contract_changed' }],
  ])('leaves every other end to the plain text (%o)', (run) => {
    expect(bindingEndText(run, t)).toBeNull()
  })

  it('reads the English text for the same reason', () => {
    expect(
      bindingEndText(
        { status: 'cancelled', cancelReason: 'workflow_contract_changed' },
        translations.en,
      ),
    ).toBe(translations.en.agent.timeline.cancelledWorkflowChanged)
  })
})

describe('the question a new run asks again', () => {
  it('takes the row text when the row holds the whole question', async () => {
    const readFull = vi.fn()

    await expect(
      questionToAskAgain({ runId: 'run_1', question: 'Was gilt?' }, readFull),
    ).resolves.toBe('Was gilt?')
    expect(readFull).not.toHaveBeenCalled()
  })

  it('reads the full question where the row holds only its start', async () => {
    const readFull = vi.fn(async () => ({ question_full: 'Was gilt seit dem ersten Januar?' }))

    await expect(
      questionToAskAgain(
        { runId: 'run_1', question: 'Was gilt seit', questionClipped: true },
        readFull,
      ),
    ).resolves.toBe('Was gilt seit dem ersten Januar?')
    expect(readFull).toHaveBeenCalledWith('run_1')
  })

  it('reports a failed read instead of putting only the start of the question back', async () => {
    const readFull = vi.fn(async () => {
      throw new Error('offline')
    })

    await expect(
      questionToAskAgain(
        { runId: 'run_1', question: 'Was gilt seit', questionClipped: true },
        readFull,
      ),
    ).rejects.toThrow('offline')
  })
})

describe('what a restart does with the input', () => {
  it('fills an empty input, or one that already holds the question', () => {
    expect(restartChoice('', 'Was gilt?')).toBe('fill')
    expect(restartChoice('  ', 'Was gilt?')).toBe('fill')
    expect(restartChoice('Was gilt? ', 'Was gilt?')).toBe('fill')
  })

  it('asks before it replaces a draft', () => {
    expect(restartChoice('Meine neue Frage', 'Was gilt?')).toBe('ask')
  })
})
