import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import {
  applyComposerReportProfilePreset,
  buildComposerRequest,
  composerFormForLastProfile,
  composerReportProfilePresets,
  composerSettingsForLastProfile,
  defaultComposerFormState,
  lastProfileFollowAfterEdit,
  lastProfileFollowAfterRestore,
  reportProfileOf,
  ResearchSubmissionAlert,
  runComposerSubmission,
  startLastProfileFollow,
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

describe('the research composer starts with the last report profile', () => {
  // Written out: the composer's default form is the deep preset.
  const DEEP = { confidenceStop: 7, firstRoundQueries: 6, maxRounds: 3, minRounds: 2, reportProfile: 'deep' }
  const COMPACT = { confidenceStop: 7, firstRoundQueries: 6, maxRounds: 2, minRounds: 1, reportProfile: 'compact' }
  const SCHNELL = { confidenceStop: 6, firstRoundQueries: 6, maxRounds: 1, minRounds: 1, reportProfile: 'schnell' }

  function opened(last: '' | 'schnell' | 'compact' | 'deep', question = 'Frage') {
    return {
      follow: startLastProfileFollow(last),
      form: { ...composerSettingsForLastProfile(last), question },
    }
  }

  it.each([
    ['compact', COMPACT],
    ['schnell', SCHNELL],
    ['deep', DEEP],
    // Without a pick the composer keeps its default form.
    ['', DEEP],
  ] as const)('opens on the preset of the last profile %s', (last, settings) => {
    expect(composerSettingsForLastProfile(last)).toEqual(settings)
  })

  it('follows a pick that arrives later while nothing was changed', () => {
    // The account row comes after the first paint, and another tab may pick
    // a profile too: an untouched form moves to that profile's preset and
    // keeps the question.
    const { follow, form } = opened('')
    const next = composerFormForLastProfile(lastProfileFollowAfterEdit(follow, form), form, 'schnell')
    expect(next.form).toEqual({ ...SCHNELL, question: 'Frage' })
    // ...and goes on following.
    const later = composerFormForLastProfile(
      lastProfileFollowAfterEdit(next.follow, next.form), next.form, 'compact',
    )
    expect(later.form).toEqual({ ...COMPACT, question: 'Frage' })
  })

  it('typing a question leaves the form following', () => {
    const { follow, form } = opened('')
    const typed = { ...form, question: 'Eine andere Frage' }
    const next = composerFormForLastProfile(lastProfileFollowAfterEdit(follow, typed), typed, 'compact')
    expect(next.form).toEqual({ ...COMPACT, question: 'Eine andere Frage' })
  })

  it('a changed setting makes the form the user\'s for this mount', () => {
    const { follow, form } = opened('compact')
    const edited = { ...form, confidenceStop: 9 as const }
    const touched = lastProfileFollowAfterEdit(follow, edited)
    expect(composerFormForLastProfile(touched, edited, 'schnell').form).toBe(edited)

    // Changing it back does not hand the form to the preference again.
    const back = { ...edited, confidenceStop: 7 as const }
    const stillTouched = lastProfileFollowAfterEdit(touched, back)
    expect(composerFormForLastProfile(stillTouched, back, 'schnell').form).toBe(back)
  })

  it('a profile picked in this composer is the user\'s too', () => {
    const { follow, form } = opened('deep')
    const picked = applyComposerReportProfilePreset(form, 'compact')
    const touched = lastProfileFollowAfterEdit(follow, picked)
    expect(composerFormForLastProfile(touched, picked, 'schnell').form).toBe(picked)
  })

  it('a draft restored after a sign-in is the user\'s, even where it equals the preset', () => {
    // The restored form reaches the composer before the account row, whose
    // pick is often the older one: the save of the last pick failed with the
    // same expired session.
    const { follow, form } = opened('deep')
    const restored = { ...form, question: 'Wiederhergestellt' }
    const touched = lastProfileFollowAfterEdit(lastProfileFollowAfterRestore(follow), restored)
    expect(composerFormForLastProfile(touched, restored, 'schnell').form).toBe(restored)
  })

  it('an unchanged pick leaves the form as it is', () => {
    const { follow, form } = opened('compact')
    const next = composerFormForLastProfile(lastProfileFollowAfterEdit(follow, form), form, 'compact')
    expect(next.form).toBe(form)
  })

  it.each([
    ['schnell', 'schnell'],
    ['compact', 'compact'],
    ['deep', 'deep'],
    ['tief', null],
    ['toString', null],
    [undefined, null],
  ] as const)('names %s as the report profile %s', (value, expected) => {
    expect(reportProfileOf(value)).toBe(expected)
  })
})
