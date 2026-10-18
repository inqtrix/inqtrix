import { AlertTriangle, Check } from 'lucide-react'
import { useEffect, useState } from 'react'

import { formatDurationMsShort } from '@/lib/time'
import { cn } from '@/lib/utils'
import type { TranslationDictionary } from '../../../i18n/translations'
import { countLabel } from '@/i18n/plural'
import {
  childMissionFoldedLine,
  childMissionView,
  type ChildMissionView,
  type ChildStage,
  type ChildTaskRow,
} from '../childMission'
import { queryPreview } from '../childProgress'
import type { AgentChildProgressRecord } from '../model'

/**
 * The delegated mission's own progress, in one calm card under the
 * delegation row: the four stages, one row per plan task with its result
 * head, the latest decision, the sum. Everything comes from the child
 * projections the parent already receives; nothing is estimated. While
 * the mission runs the card ticks once a second (like the run clock);
 * once the tool row settled it folds to its retrospective line.
 */
export function ChildMissionCard({
  child,
  folded,
  onOpenDetails,
  t,
}: {
  child: AgentChildProgressRecord
  folded: boolean
  onOpenDetails?: () => void
  t: TranslationDictionary
}) {
  const [open, setOpen] = useState(false)
  const [, setTick] = useState(0)
  const terminal = child.runStatus === 'completed'
    || child.runStatus === 'failed'
    || child.runStatus === 'cancelled'
  useEffect(() => {
    if (terminal) return undefined
    const id = window.setInterval(() => setTick((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [terminal])
  const view = childMissionView(child, Date.now() / 1000)
  if (!view) return null
  const showBody = !folded || open
  const foldedLine = folded ? childMissionFoldedLine(view, t) : undefined
  return (
    <div
      className="rounded-lg border border-border/60 bg-muted/30 px-2.5 py-1.5"
      data-testid="agent-child-mission"
    >
      <p className="flex items-baseline justify-between gap-2 t-caption uppercase tracking-wide text-muted-foreground/70">
        {folded ? (
          <button
            aria-expanded={open}
            className="inline-flex shrink-0 items-center gap-1 uppercase tracking-wide hover:text-foreground"
            data-testid="agent-child-mission-toggle"
            onClick={() => setOpen((value) => !value)}
            type="button"
          >
            <span aria-hidden="true">{open ? '▾' : '▸'}</span>
            {t.agent.timeline.childRun}
          </button>
        ) : (
          <span className="shrink-0">{t.agent.timeline.childRun}</span>
        )}
        {folded ? (
          // A narrow column cuts the line; the hover text keeps the rest
          // of it reachable.
          <span
            className="min-w-0 truncate normal-case tracking-normal t-hint tabular-nums"
            data-testid="agent-child-mission-folded"
            title={foldedLine}
          >
            {foldedLine}
          </span>
        ) : (
          <span className="shrink-0 tabular-nums" data-testid="agent-child-mission-clock">
            {formatDurationMsShort(view.elapsedSeconds * 1000)}
          </span>
        )}
      </p>
      {showBody && <CardBody t={t} view={view} />}
      {onOpenDetails && (
        <button className="mt-1 t-hint text-muted-foreground underline decoration-border underline-offset-2 hover:text-foreground" onClick={onOpenDetails} type="button">
          {t.agent.timeline.childDetails}
        </button>
      )}
    </div>
  )
}

function stageLabel(stage: ChildStage, t: TranslationDictionary): string {
  switch (stage) {
    case 'plan':
      return t.agent.timeline.childStagePlan
    case 'research':
      return t.agent.timeline.childStageResearch
    case 'memo':
      return t.agent.timeline.childStageMemo
    default:
      return t.agent.timeline.childStageCheck
  }
}

function searchCount(count: number, t: TranslationDictionary): string {
  return countLabel(
    count,
    t.agent.timeline.childSearchCountOne,
    t.agent.timeline.childSearchCount,
  )
}

/** "2:14 · läuft · 3 Suchen" while a task runs (its own clock, stopped by
 * its settle), "4:37 · 3 Suchen" once done — a reader expects a stopwatch
 * per task, not a word. */
function rowMeta(row: ChildTaskRow, t: TranslationDictionary): string {
  if (row.state === 'failed') return t.agent.timeline.childTaskFailedShort
  const parts: string[] = []
  if (row.elapsedSeconds !== undefined) {
    parts.push(formatDurationMsShort(row.elapsedSeconds * 1000))
  }
  if (row.state === 'running') parts.push(t.agent.timeline.childTaskRunning)
  if (row.searches !== undefined && (row.searches > 0 || row.state === 'running')) {
    parts.push(searchCount(row.searches, t))
  }
  return parts.join(' · ')
}

function CardBody({ t, view }: { t: TranslationDictionary; view: ChildMissionView }) {
  const sum = (view.searches > 0 ? t.agent.timeline.childCardSum : t.agent.timeline.childCardSumNoSearch)
    .replace('{tasks}', countLabel(view.tasks, t.agent.timeline.taskCountOne, t.agent.timeline.taskCount))
    .replace('{done}', String(view.done))
    .replace('{searches}', searchCount(view.searches, t))
    + (view.failed > 0
      ? t.agent.timeline.childCardSumFailed.replace('{failed}', String(view.failed))
      : '')
  return (
    <>
      <ol
        aria-label={t.agent.timeline.childStages}
        className="mt-1.5 grid grid-cols-4 gap-1.5"
        data-testid="agent-child-mission-stages"
      >
        {view.stages.map((stage) => (
          <li
            className={cn(
              'border-t-2 pt-1 t-hint uppercase tracking-wide',
              stage.state === 'now'
                ? 'border-brand text-foreground/90'
                : stage.state === 'done'
                  ? 'border-success/60 text-muted-foreground'
                  : 'border-border/60 text-muted-foreground/60',
            )}
            data-stage-state={stage.state}
            key={stage.key}
          >
            {stageLabel(stage.key, t)}
          </li>
        ))}
      </ol>
      {view.rows.length > 0 && (
        <ul className="mt-1.5 space-y-0.5" data-testid="agent-child-mission-tasks">
          {view.rows.map((row) => (
            <li
              className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-baseline gap-x-1.5 t-meta"
              data-task-state={row.state}
              key={row.ordinal}
            >
              {row.state === 'done' ? (
                <Check className="relative top-0.5 icon-xs shrink-0 text-success/80" />
              ) : row.state === 'failed' ? (
                <AlertTriangle className="relative top-0.5 icon-xs shrink-0 text-destructive/80" />
              ) : (
                <span
                  aria-hidden="true"
                  className="relative -top-px inline-block size-1.5 shrink-0 rounded-full bg-brand inqtrix-running-dot"
                />
              )}
              <span className="min-w-0">
                <span
                  className={cn(
                    'block truncate',
                    row.state === 'running'
                      ? 'text-foreground/90'
                      : row.state === 'failed'
                        ? 'text-destructive/90'
                        : 'text-muted-foreground',
                  )}
                  title={row.title}
                >
                  {row.title || t.agent.timeline.childTask.replace('{number}', String(row.ordinal + 1))}
                </span>
                {row.result && (
                  <span className="block truncate t-hint text-muted-foreground/70" title={row.result}>
                    {row.result}
                  </span>
                )}
                {row.query && (
                  <span className="block truncate t-hint text-muted-foreground/70" title={row.query}>
                    {t.agent.timeline.childSearch.replace('{query}', queryPreview(row.query))}
                  </span>
                )}
              </span>
              <span className="whitespace-nowrap t-hint tabular-nums text-muted-foreground/70">
                {rowMeta(row, t)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {view.memoLine && (
        <p className="mt-1.5 truncate t-hint text-muted-foreground" data-testid="agent-child-mission-memo" title={view.memoLine}>
          {view.memoLine}
        </p>
      )}
      {view.decision && (
        <p className="mt-1.5 t-hint text-muted-foreground" data-testid="agent-child-mission-decision">
          <span className="text-foreground/80">{view.decision.label}</span>
          {view.decision.detail ? ` · ${view.decision.detail}` : ''}
        </p>
      )}
      <p
        className="mt-1.5 flex justify-between gap-2 border-t border-border/50 pt-1 t-hint tabular-nums text-muted-foreground/70"
        data-testid="agent-child-mission-sum"
      >
        <span className="shrink-0">{sum}</span>
        {view.lastQuery && (
          <span className="min-w-0 truncate" title={view.lastQuery}>
            {t.agent.timeline.childSearch.replace('{query}', queryPreview(view.lastQuery))}
          </span>
        )}
      </p>
    </>
  )
}
