import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import type { ResearchRunRecord } from '@/features/project/types'
import { reportFallbackDescription, resolveReportPanelState } from './ReportPanel'

const completed = (overrides: Partial<ResearchRunRecord> = {}): ResearchRunRecord => ({
  agentOverrides: {},
  createdAt: '2026-01-01T10:00:00Z',
  events: [],
  runId: 'run-done',
  source: 'api',
  stack: 'default',
  status: 'completed',
  submittedAt: '2026-01-01T10:00:00Z',
  summary: { title: 'Done' },
  ...overrides,
} as ResearchRunRecord)

describe('report of a completed run', () => {
  it('is loading while a server run has no result yet', () => {
    // After a reload the listing arrives before the results; the panel
    // showed "no report in the local mock" in that gap.
    expect(resolveReportPanelState(completed()).mode).toBe('completed-loading')
  })

  it('says the report failed to load when its result request failed', () => {
    const run = completed({ resultError: 'Zeitüberschreitung' })
    expect(resolveReportPanelState(run).mode).toBe('completed-result-failed')
    expect(reportFallbackDescription(run, translations.de.report)).toBe(translations.de.report.resultLoadFailedDescription)
  })

  it('says a server run delivered no report when its result has none', () => {
    const run = completed({ result: { markdown: '' } as ResearchRunRecord['result'] })
    expect(resolveReportPanelState(run).mode).toBe('completed-without-report')
    expect(reportFallbackDescription(run, translations.de.report)).toBe(translations.de.report.noReportDescription)
  })

  it('keeps the local-run text for runs that never came from the server', () => {
    const run = completed({ source: 'mock' })
    expect(resolveReportPanelState(run).mode).toBe('completed-without-report')
    expect(reportFallbackDescription(run, translations.de.report)).toBe(translations.de.report.unavailableDescription)
  })
})
