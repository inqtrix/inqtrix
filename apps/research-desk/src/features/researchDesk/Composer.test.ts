import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import {
  applyComposerReportProfilePreset,
  buildComposerRequest,
  composerReportProfilePresets,
  defaultComposerFormState,
  ResearchSubmissionAlert,
  runComposerSubmission,
} from './components/Composer'

describe('Research Desk composer profile presets', () => {
  it('sends the profile alone when the human changed nothing', () => {
    // The backend skips profile application for every field the request
    // states, so sending untouched preset values made the profile
    // decorative: the run used the composer's numbers even where the
    // profile disagreed.
    const request = buildComposerRequest(
      { ...defaultComposerFormState, question: '  What changed?  ' },
      '  What changed?  ',
      'default',
    )

    expect(request).toMatchObject({
      agentOverrides: { reportProfile: 'deep' },
      mode: 'research',
      question: 'What changed?',
      stack: 'default',
    })
    expect(request.agentOverrides).not.toHaveProperty('confidenceStop')
    expect(request.agentOverrides).not.toHaveProperty('firstRoundQueries')
    expect(request.agentOverrides).not.toHaveProperty('maxRounds')
    expect(request.agentOverrides).not.toHaveProperty('minRounds')
  })

  it('sends exactly the field the human moved away from the preset', () => {
    const request = buildComposerRequest(
      { ...defaultComposerFormState, firstRoundQueries: 4, question: 'Deep run' },
      'Deep run',
      'research',
    )

    expect(request.agentOverrides?.firstRoundQueries).toBe(4)
    expect(request.agentOverrides).not.toHaveProperty('confidenceStop')
  })

  it('keeps the schnell preset on the profile contract', () => {
    // The preset said 7 while the backend profile says 6, so every run
    // started from the UI stopped later — longer and costlier than the
    // profile advertises.
    const request = buildComposerRequest(
      {
        ...defaultComposerFormState,
        ...composerReportProfilePresets.schnell,
        question: 'Quick run',
      },
      'Quick run',
      'research',
    )

    expect(composerReportProfilePresets.schnell.confidenceStop).toBe(6)
    expect(request.agentOverrides).toEqual({ reportProfile: 'schnell' })
  })

  it('keeps the deep preset on the profile contract', () => {
    // The deep preset is the composer default, so a drift here changes
    // every untouched run; the numbers mirror report_profiles.py.
    expect(composerReportProfilePresets.deep).toEqual({
      confidenceStop: 7,
      firstRoundQueries: 6,
      maxRounds: 3,
      minRounds: 2,
      reportProfile: 'deep',
    })
  })

  it('switches profile presets while preserving the draft', () => {
    const base = {
      ...defaultComposerFormState,
      firstRoundQueries: 4,
      maxRounds: 3,
      question: 'Keep this draft',
    } as const

    expect(applyComposerReportProfilePreset(base, 'compact')).toMatchObject({
      confidenceStop: 7,
      firstRoundQueries: 6,
      maxRounds: 2,
      minRounds: 1,
      question: 'Keep this draft',
      reportProfile: 'compact',
    })
    expect(applyComposerReportProfilePreset(base, 'schnell')).toMatchObject({
      maxRounds: 1,
      minRounds: 1,
      question: 'Keep this draft',
      reportProfile: 'schnell',
    })
  })

  it('always submits a full research run', () => {
    const request = buildComposerRequest(
      { ...defaultComposerFormState, question: 'Q' },
      'Q',
      'default',
    )

    expect(request.mode).toBe('research')
  })

  it('retains the complete draft and profile when the server rejects submission', async () => {
    const form = {
      ...defaultComposerFormState,
      confidenceStop: 9 as const,
      question: 'Keep this carefully written question',
      reportProfile: 'compact' as const,
    }
    const setForm = vi.fn()
    const onSubmit = vi.fn(async () => false)

    await expect(runComposerSubmission({
      form,
      onSubmit,
      selectedStack: 'research',
      setForm,
    })).resolves.toBe(false)

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      agentOverrides: expect.objectContaining({
        confidenceStop: 9,
        reportProfile: 'compact',
      }),
      question: form.question,
    }))
    expect(setForm).not.toHaveBeenCalled()
    expect(form).toMatchObject({
      confidenceStop: 9,
      question: 'Keep this carefully written question',
      reportProfile: 'compact',
    })
  })

  it('clears only the accepted question without discarding newer typing', async () => {
    const form = {
      ...defaultComposerFormState,
      question: 'Accepted question',
      reportProfile: 'schnell' as const,
    }
    let update: ((current: typeof form) => typeof form) | undefined

    await expect(runComposerSubmission({
      form,
      onSubmit: async () => true,
      selectedStack: 'research',
      setForm: (next) => {
        update = next as (current: typeof form) => typeof form
      },
    })).resolves.toBe(true)

    expect(update?.(form)).toEqual({ ...form, question: '' })
    const newerDraft = { ...form, question: 'Typed while the request was pending' }
    expect(update?.(newerDraft)).toBe(newerDraft)
  })

  it('renders a live, element-owned submission error on the research surface', () => {
    const markup = renderToStaticMarkup(createElement(ResearchSubmissionAlert, {
      message: 'The research run could not be started. Your question is still here.',
    }))

    expect(markup).toContain('role="alert"')
    expect(markup).toContain('data-research-submission-error')
    expect(markup).toContain('Your question is still here')
  })

})
