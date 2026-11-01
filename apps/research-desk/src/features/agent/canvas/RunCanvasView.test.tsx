import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { translations } from '@/i18n/translations'
import type { ResearchRunSummary } from '@/features/researchRuns/types'
import { agentRunFromSummary } from '../model'
import { AgentCanvasReactContext, type AgentCanvasContextValue } from './context'
import { RunCanvasView } from './views'

const localeState = vi.hoisted(() => ({ locale: 'de' as 'de' | 'en' }))

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: localeState.locale, t: translations[localeState.locale] }),
}))

function overview(error: ResearchRunSummary['error']): string {
  const run = agentRunFromSummary({
    access: { mode: 'owner' },
    agent_overrides: {},
    created_at: 1,
    elapsed_seconds: 1,
    error,
    events_url: '/events',
    finished_at: 2,
    kind: 'agent',
    mode: 'agent_kernel',
    question: 'Frage',
    queue_position: null,
    result_url: '/result',
    run_id: 'run-1',
    snapshot: {},
    stack: 'default',
    started_at: 1,
    status: 'failed',
  })
  const context = {
    pollingRunIds: [],
    runs: { 'run-1': run },
  } as unknown as AgentCanvasContextValue
  return renderToStaticMarkup(
    <AgentCanvasReactContext.Provider value={context}>
      <RunCanvasView descriptor={{ runId: 'run-1', view: 'run' }} />
    </AgentCanvasReactContext.Provider>,
  )
}

describe('the failure line of the run overview', () => {
  afterEach(() => {
    localeState.locale = 'de'
  })

  const revoked = {
    message: 'access to a pinned knowledge collection was revoked',
    type: 'authorization_revoked',
  }

  it('says what happened in the reader\'s language, not the server\'s English detail', () => {
    const html = overview(revoked)
    expect(html).toContain('Der Auftrag wurde gestoppt, weil ein benötigter Zugriff nicht mehr erlaubt ist.')
    expect(html).not.toContain(revoked.message)
  })

  it('does so in English as well', () => {
    localeState.locale = 'en'
    expect(overview(revoked)).toContain('The task was stopped because access it needs is no longer permitted.')
  })

  it('still shows a failure the interface has no sentence for', () => {
    // An unknown failure stays visible as it arrived.
    expect(overview({ message: 'Unbekannter Ausfall', type: 'brand_new_code' }))
      .toContain('Unbekannter Ausfall')
  })
})
