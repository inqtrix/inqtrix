import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Button } from '@/components/ui/button'
import { LocaleProvider, useLocale } from '@/i18n/LocaleProvider'
import { agentRunFromSummary, type AgentRunRecord, type AgentStepEntry } from '../../model'
import { applyAgentRunEvent } from '../../events'
import { AgentRunTurn, type AgentTimelineActions } from '../AgentTimeline'
import '@/styles/globals.css'

/** Controlled event states around the real transcript, without model or web calls. */
function initialRun(): AgentRunRecord {
  const start = Date.now() / 1000 - 92
  const run = agentRunFromSummary({
    run_id: 'fixture-agent', question: 'Vergleiche die vorhandenen Quellen und erstelle ein kurzes Memo mit offenen Punkten.',
    access: { mode: 'owner' }, mode: 'agent_kernel', kind: 'agent', stack: 'default',
    status: 'running', agent_overrides: { autonomy: 'balanced' },
    created_at: start, started_at: start, finished_at: null, elapsed_seconds: null,
    queue_position: null, snapshot: { current_node: 'agent_kernel' }, error: null,
    events_url: '', result_url: '',
  })
  run.phase = 'execution'
  run.todosAt = start + 2
  run.todos = [
    { content: 'Auftrag und Ausgabeformat festlegen', status: 'completed' },
    { content: 'Quellen recherchieren', status: 'in_progress' },
    { content: 'Belege nachlesen und Memo formulieren', status: 'pending' },
  ]
  run.stepLog = [
    { kind: 'narration', seq: 1, at: start + 1, text: 'Ich vergleiche die Quellen und halte offene Punkte im Memo fest.' },
    { kind: 'activity', seq: 2, at: start + 2, label: 'run_deep_mission', detail: 'Quellen vergleichen und Aussagen belegen', activityKey: 'tool:mission', status: 'completed' },
  ]
  run.children = {
    mission: {
      childRunId: 'mission', taskId: 'mission', runStatus: 'completed', startedAt: start + 2, updatedAt: start + 80,
      snapshot: { phase: 'done' }, openTasks: [], failedTasks: [],
      taskTitles: { 0: 'Primärquellen auswerten', 1: 'Memo zur Leitfrage formulieren' },
      taskIds: { research: 0, memo: 1 }, taskStartedAt: { 0: start + 5, 1: start + 65 },
      taskSettledAt: { 0: start + 64, 1: start + 80 }, taskKinds: { 0: 'web_research', 1: 'synthesis' },
      taskResults: { 0: 'Drei Quellen mit unterschiedlichen Anwendungsbereichen', 1: 'Vergleich mit offenen Punkten erstellt' },
    },
  }
  return run
}

function readEntries(status: 'running' | 'completed'): AgentStepEntry[] {
  return ['Originaldefinition im Standard', 'Bedingungen und Ausnahmen in der Dokumentation', 'Anwendungsbeispiel des Herausgebers', 'Eine längere Quellenüberschrift, deren gesamter Inhalt auch auf schmalen Oberflächen lesbar und ohne horizontales Scrollen erreichbar bleiben muss', '{"query_id":"query_legacy"}'].map((detail, index) => ({
    kind: 'activity', seq: index + 3, at: Date.now() / 1000, activityOperation: 'evidence_read',
    activityKey: `tool:read-${index}`, detail, status, arrivedLive: true,
  }))
}

function withAnswer(run: AgentRunRecord, writing: boolean, withSources = true): AgentRunRecord {
  return {
    ...run,
    artifactOrder: ['answer'],
    artifacts: {
      answer: {
        artifactId: 'answer', kind: 'answer', title: 'Antwort',
        status: writing ? 'writing' : 'ready', revision: 1, updatedBy: 'agent',
        createdAt: Date.now() / 1000, updatedAt: Date.now() / 1000,
        refsCount: withSources ? 1 : 0,
        contentMarkdown: 'Die vorhandenen Quellen sind verglichen. Die Aussagegrenzen bleiben im Ergebnis erkennbar.',
        refs: withSources ? [{ label: 'W1', url: 'https://example.org/source', title: 'Einordnung und Aussagegrenzen' }] : [],
      },
    },
  }
}

const MARKED_SENTENCE = 'Der Klimabonus sinkt 2026 auf 16 Prozent der förderfähigen Kosten.'
const MARK_NOTE = 'Nicht ausreichend belegt: W1 nennt den Klimabonus, aber keinen Prozentsatz.'
const FURTHER_SENTENCES = [
  'Die Grundförderung bleibt bei 30 Prozent der förderfähigen Kosten.',
  'Der Antrag läuft weiter über das Portal des Förderinstituts.',
  'Für Haushalte mit niedrigem Einkommen kann ein zusätzlicher Bonus greifen.',
  'Die förderfähigen Kosten sind je Wohneinheit gedeckelt.',
  'Ein Ergänzungskredit kann die Finanzierung abrunden.',
  'Die technischen Mindestanforderungen gelten unverändert.',
  'Anträge müssen vor Beginn der Maßnahme gestellt werden.',
  'Die Bewilligung folgt der Reihenfolge des Eingangs.',
]

/** A reviewed answer whose second sentence the review marked, long enough
 * that the end of the answer lies far below the mark. */
function withMarkedAnswer(run: AgentRunRecord, runId = run.runId): AgentRunRecord {
  const answered = withAnswer({
    ...run, runId, status: 'completed', phase: 'done', finishedAt: new Date().toISOString(), elapsedSeconds: 98,
    todos: run.todos?.map((todo) => ({ ...todo, status: 'completed' })),
  }, false)
  const answer = answered.artifacts.answer
  return {
    ...answered,
    artifacts: {
      ...answered.artifacts,
      answer: {
        ...answer,
        contentMarkdown: [
          `Die vorhandenen Quellen sind verglichen. ${MARKED_SENTENCE} [W1]`,
          ...FURTHER_SENTENCES.map((sentence) => `${sentence} [W1]`),
        ].join('\n\n'),
      },
    },
    outcome: {
      outcomeId: `${runId}:outcome`, final: 'completed', review: 'completed',
      summary: 'Geprüft · 9 Aussagen: 8 vom Anbieter zugeschrieben, 1 nicht ausreichend belegt',
      applied: 0, lines: [],
      marks: [{ outputId: 'chat', quote: `${MARKED_SENTENCE} [W1]`, state: 'insufficient', note: MARK_NOTE }],
    },
  }
}

function Fixture() {
  const { locale, setLocale } = useLocale()
  const [run, setRun] = useState(initialRun)
  const [second, setSecond] = useState<AgentRunRecord | null>(null)
  const [dark, setDark] = useState(true)
  const [notice, setNotice] = useState('')
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => { document.documentElement.classList.toggle('dark', dark) }, [dark])
  useEffect(() => () => clearTimeout(timer.current), [])
  function reads() {
    clearTimeout(timer.current)
    setRun((current) => ({ ...current, status: 'running', stepLog: [...current.stepLog.slice(0, 2), ...readEntries('running')], activity: { kind: 'searching', detail: '', at: 1, operation: 'evidence_read', status: 'running' } }))
  }
  function settle(status: AgentRunRecord['status']) {
    setRun((current) => ({ ...current, status, finishedAt: new Date().toISOString(), elapsedSeconds: 98 }))
  }
  const actions: AgentTimelineActions = {
    onCancelRun: () => settle('cancelled'), onOpenCanvas: () => setNotice('Detailansicht angefordert'),
    answerClarification: async () => undefined, decideApproval: async () => undefined,
    answerChildClarification: async () => undefined, decideChildApproval: async () => undefined,
    loadArtifactRevision: async () => ({ content_markdown: '' }),
    applyPatch: async () => ({ kind: 'applied', revision: 1, appliedEditIds: [] }), rejectPatch: async () => undefined,
    planDrafts: {}, setPlanDraft: () => undefined, planSource: { collections: [], vectorBackendLabel: null },
  }
  return (
    <main className="min-h-screen bg-background text-foreground">
      <nav aria-label="Prüfszenarien" className="flex flex-wrap gap-2 border-b border-border bg-surface p-3">
        <Button onClick={() => { clearTimeout(timer.current); setRun(initialRun()); setSecond(null) }} variant="outline">Zurücksetzen</Button>
        <Button onClick={reads} variant="outline">5 Belegabrufe starten</Button>
        <Button onClick={() => setRun((current) => ({ ...current, stepLog: current.stepLog.map((entry) => entry.seq === 4 ? { ...entry, status: 'completed' } : entry), activity: { kind: 'searching', detail: '', at: 1, operation: 'evidence_read', status: 'completed' } }))} variant="outline">1 Abruf fertig</Button>
        <Button onClick={() => setRun((current) => ({ ...current, stepLog: current.stepLog.map((entry) => entry.kind === 'activity' ? { ...entry, status: 'completed' } : entry) }))} variant="outline">Alle Abrufe fertig</Button>
        <Button onClick={() => setRun((current) => ({ ...current, stepLog: current.stepLog.map((entry) => entry.seq === 4 ? { ...entry, status: 'failed', error: 'Die gespeicherte Herkunft ist nicht mehr verfügbar.' } : entry) }))} variant="outline">Abruffehler</Button>
        <Button onClick={() => setRun((current) => ({ ...current, status: 'waiting_for_approval' }))} variant="outline">Freigabe erforderlich</Button>
        <Button onClick={() => setRun((current) => ({ ...current, status: 'waiting_for_input' }))} variant="outline">Rückfrage</Button>
        <Button onClick={() => { clearTimeout(timer.current); timer.current = setTimeout(() => settle('cancelled'), 1200) }} variant="outline">Abbruch nach 1 s</Button>
        <Button onClick={() => settle('completed')} variant="outline">Lauf beenden</Button>
        <Button onClick={() => setRun((current) => withAnswer({ ...current, status: 'running', phase: 'answer', finishedAt: undefined, elapsedSeconds: undefined }, true))} variant="outline">Antwort schreiben</Button>
        <Button onClick={() => setRun((current) => withAnswer({ ...current, status: 'completed', phase: 'done', finishedAt: new Date().toISOString(), elapsedSeconds: 98, todos: current.todos?.map((todo) => ({ ...todo, status: 'completed' })) }, false))} variant="outline">Antwort fertig</Button>
        <Button onClick={() => setRun((current) => withAnswer(current, false, false))} variant="outline">Antwort ohne Quellen</Button>
        <Button onClick={() => setRun((current) => withMarkedAnswer(current))} variant="outline">Antwort mit Marke</Button>
        <Button onClick={() => setSecond(withMarkedAnswer(initialRun(), 'fixture-agent-second'))} variant="outline">Zweite Antwort mit Marke</Button>
        <Button onClick={() => setRun((current) => {
          const sequence = Math.max(current.lastSequence, ...current.stepLog.map((entry) => entry.seq)) + 1
          const decided = applyAgentRunEvent({ ...current, status: 'completed', phase: 'critic' }, {
            type: 'inqtrix.agent.activity', run_id: 'fixture-agent', sequence, created_at: Date.now() / 1000,
            data: { kind: 'critic_unavailable', status: 'failed', label: 'Prüfung nicht auswertbar', detail: 'Es liegt kein gültiges, widerspruchsfreies Prüfurteil vor. Das vorhandene Ergebnis bleibt erhalten, ist dadurch aber nicht als geprüft bestätigt.' },
          })
          // The recap reads how the review ended from the record alone.
          return applyAgentRunEvent(decided, {
            type: 'inqtrix.agent.outcome', run_id: 'fixture-agent', sequence: sequence + 1, created_at: Date.now() / 1000,
            data: { outcome_id: 'fixture-agent:outcome', final: 'completed', review: 'unavailable', round: 1, revision: 'none', accounting: 'incomplete', applied: 0, codes: [], limits: [], open: { by_code: {}, by_kind: {} }, lines: [], summary: 'Prüfung nicht auswertbar, Ergebnis ungeprüft' },
          })
        })} variant="outline">Prüfurteil nicht auswertbar</Button>
        <Button onClick={() => setDark((value) => !value)} variant="outline">{dark ? 'Hell' : 'Dunkel'}</Button>
        <Button onClick={() => setLocale(locale === 'de' ? 'en' : 'de')} variant="outline">{locale === 'de' ? 'EN' : 'DE'}</Button>
      </nav>
      <div className="mx-auto max-w-4xl px-4 py-6">
        <p className="mb-5 t-hint text-muted-foreground">Kontrollierte UI-Zustände · echte Komponenten · keine Recherche</p>
        <AgentRunTurn run={run} actions={actions} historical />
        {second && <AgentRunTurn run={second} actions={actions} historical />}
        {notice && <p role="status">{notice}</p>}
      </div>
    </main>
  )
}

const root = createRoot(document.getElementById('root')!)
root.render(<LocaleProvider><Fixture /></LocaleProvider>)
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount())
