import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { translations } from '@/i18n/translations'
import type { AgentPlanTaskRecord, AgentTaskLiveState } from '../model'
import { TaskWorkUnit } from './views'

vi.mock('@/i18n/LocaleProvider', () => ({ useLocale: () => ({ t: translations.de, locale: 'de' }) }))

describe('task work unit progress', () => {
  it('uses query progress only for local requests and keeps terminal status separate from old activity', () => {
    const task: AgentPlanTaskRecord = {
      taskId: 'task', ordinal: 0, title: 'Read sources', toolKind: 'web_research', objective: '',
      queries: ['a', 'b', 'c', 'd', 'e'], gapIds: [], dependsOn: [], budget: {}, params: {},
      expectedOutput: '', status: 'completed', childRunId: 'child', resultSummary: 'Sources retained',
    }
    const live: AgentTaskLiveState = {
      status: 'completed', attempt: 1, metrics: { total_queries: 6 },
      activityHistory: [{ at: 1, kind: 'working', detail: '', status: 'completed' }],
    }
    const props = { id: 'task', task, live, restoreFocus: false,
      onClick: () => undefined, onFocusRestored: () => undefined,
      child: { childRunId: 'child', taskId: 'task', runStatus: 'completed', message: 'Old activity message' } }
    const completed = renderToStaticMarkup(<TaskWorkUnit {...props} />)
    expect(completed).toContain('>6</strong> Suchanfragen')
    expect(completed).not.toContain('Suchen abgeschlossen')
    expect(completed).not.toContain('Old activity message')
    const local = renderToStaticMarkup(<TaskWorkUnit {...props} child={undefined} task={{ ...task, toolKind: 'rag_query', childRunId: null }} live={{ ...live, status: 'running' }} />)
    expect(local).toContain('1/5 Suchen abgeschlossen')
  })
})

describe('task work unit notes', () => {
  const task: AgentPlanTaskRecord = {
    taskId: 'task', ordinal: 0, title: 'Read sources', toolKind: 'rag_query', objective: '',
    queries: [], gapIds: [], dependsOn: [], budget: {}, params: {},
    expectedOutput: '', status: 'completed', childRunId: null, resultSummary: 'Sources retained',
  }
  const props = { id: 'task', task, child: undefined, restoreFocus: false,
    onClick: () => undefined, onFocusRestored: () => undefined }

  it('shows an evidence limit as a note, never as a fallback path', () => {
    const html = renderToStaticMarkup(
      <TaskWorkUnit {...props} live={{ status: 'completed', attempt: 1, evidenceLimit: true }} />,
    )
    // The German copy is spelled out: a reword must be caught here.
    expect(html).toContain('Die Belege dieser Aufgabe haben eine benannte Grenze; sie wird als offener Punkt weitergegeben.')
    expect(html).toContain('lucide-info')
    expect(html).not.toContain('Ersatzpfad')
    expect(html).not.toContain('Fallback')
    expect(html).not.toContain('lucide-alert-triangle')
  })

  it('keeps the warning for a real fallback path', () => {
    const html = renderToStaticMarkup(
      <TaskWorkUnit {...props} live={{ status: 'completed', attempt: 1, fallback: true }} />,
    )
    expect(html).toContain('Diese Aufgabe wurde mit einem sichtbaren Ersatzpfad ausgeführt.')
    expect(html).toContain('lucide-alert-triangle')
    expect(html).not.toContain('benannte Grenze')
  })
})
