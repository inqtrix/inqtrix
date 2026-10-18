import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import { ChildMissionCard } from './ChildMissionCard'

describe('the folded mission card', () => {
  it('keeps the whole folded line reachable when the column cuts it', () => {
    // The line truncates to its column; the review summary it closes with
    // can run to 240 characters, so the hover text carries all of it.
    const summary = 'Geprüft · 1 Befund überarbeitet, nicht nachgeprüft · 1 Punkt nicht geprüft'
    const html = renderToStaticMarkup(
      <ChildMissionCard
        child={{
          childRunId: 'run_child_1', taskId: 'call_1', runStatus: 'completed', snapshot: { phase: 'done' },
          startedAt: 0, updatedAt: 65, taskTitles: { 0: 'A' },
          outcome: { final: 'completed', review: 'completed', applied: 1, summary },
        }}
        folded
        t={translations.de}
      />,
    )
    const line = `1 Aufgabe · 0 Abschnitte · 1:05 · ${summary}`
    expect(html).toContain(`title="${line}">${line}</span>`)
  })
})
