import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import type { ResearchJob } from '../types'
import { ResearchJobCard } from './ResearchJobCard'

function job(overrides: Partial<ResearchJob> = {}): ResearchJob {
  return {
    activePhase: 'search',
    completedPhases: ['analysis', 'planning'],
    events: [],
    id: 'run_lock_1',
    metrics: { claims: 0, queries: 6, rounds: '1/2', sources: 0 },
    phaseVisitCounts: {
      analysis: 1,
      planning: 1,
      search: 1,
      evaluation: 0,
      answer: 0,
    } as ResearchJob['phaseVisitCounts'],
    startedAt: '10:00',
    startedAtIso: '2026-01-01T10:00:00.000Z',
    status: 'running',
    submittedAt: '10:00',
    title: { de: 'Gesperrter Lauf', en: 'Locked run' },
    ...overrides,
  }
}

function renderCard(cardJob: ResearchJob, options: { expanded?: boolean } = {}) {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <TooltipProvider>
        <ResearchJobCard
          isExpanded={options.expanded ?? false}
          isSelected={options.expanded ?? false}
          job={cardJob}
          onCancel={() => undefined}
          onDelete={() => undefined}
          onSelect={() => undefined}
          onToggleExpanded={() => undefined}
        />
      </TooltipProvider>
    </LocaleProvider>,
  )
}

describe('ResearchJobCard unavailable lock', () => {
  it('renders the calm lock and retracts every liveness claim', () => {
    // The record keeps status "running" by design (the non-disclosing
    // 404 carries no status), so every running-keyed affordance must be
    // explicitly gated: badge, spinner, compact status, runtime clock.
    const markup = renderCard(job({ unavailable: true }))
    expect(markup).toContain('Nicht mehr verfügbar')
    expect(markup).toContain('Dieser Lauf ist nicht mehr verfügbar')
    expect(markup).not.toContain('Laufend')
    expect(markup).not.toContain('Laufzeit:')
    expect(markup).not.toContain('animate-spin')
  })

  it('routes the EXPANDED body away from the live view', () => {
    // RunningJobDetails renders the pulsing activity dot -- a false
    // liveness claim on a terminally dead channel.
    const live = renderCard(job(), { expanded: true })
    expect(live).toContain('inqtrix-running-dot')
    const locked = renderCard(job({ unavailable: true }), { expanded: true })
    expect(locked).not.toContain('inqtrix-running-dot')
  })

  it('keeps the normal running presentation without the flag', () => {
    const markup = renderCard(job())
    expect(markup).toContain('Laufend')
    expect(markup).toContain('Laufzeit:')
    expect(markup).not.toContain('Nicht mehr verfügbar')
  })
})
