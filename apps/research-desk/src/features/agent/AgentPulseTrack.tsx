import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import {
  agentPulseActiveIndex,
  agentStationsFor,
  isActiveAgentRun,
  isGateAgentRun,
  type AgentRunRecord,
} from './model'

/**
 * The Agent Desk signature progress element ("Agenten-Puls"): a compact
 * metro line of phase stations. Completed stations are filled brand dots,
 * the active one carries the expanding halo pulse (`inqtrix-active-node-*`
 * — the agent's exclusive motion), the connector into it drifts a light
 * packet while working (`inqtrix-agent-flow`). A PARKED run (waiting for
 * approval/input) switches the active node to a calm warning breathe —
 * ambient -> attention without a blink. One row, ~16px tall.
 */
export function AgentPulseTrack({
  className,
  compact = false,
  run,
  withLabels = false,
}: {
  className?: string
  /** Micro variant for rail rows: dots only, tighter gaps. */
  compact?: boolean
  run: AgentRunRecord
  withLabels?: boolean
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  // Working = active minus gates: a children-wait keeps the pulse
  // flowing (the child runs ARE the work); human gates breathe warning.
  const isGate = isGateAgentRun(run.status)
  const isWorking = isActiveAgentRun(run.status) && !isGate
  const isDone = run.status === 'completed'
  const isStopped = run.status === 'failed' || run.status === 'cancelled'
  // The furthest station this run ACTUALLY reached. Filling the whole
  // line on completion would let a kernel run — which only ever reports
  // intake and execution — claim it had also run a discovery, a plan
  // and a verification pass. The track reads as a record of what
  // happened, so it stops where the run stopped and leaves the rest
  // pale.
  // Which line this engine actually travels: the kernel only ever
  // reports `execution` and `done`, so the mission's six stations would
  // describe a flow it never had.
  const stations = agentStationsFor(run.snapshot?.execution?.effective_mode)
  const activeIndex = agentPulseActiveIndex(run.station, isDone, stations)

  return (
    <div aria-hidden="true" className={cn('min-w-0', className)}>
      <div
        className={cn(
          'flex items-center',
          compact ? 'gap-0.5' : 'gap-1',
        )}
      >
        {stations.map((station, index) => {
          const stationDone = index < activeIndex
          const stationActive = index === activeIndex && !isDone
          return (
            <StationNode
              connectorFlowing={
                index > 0 && index === activeIndex && isWorking && !reduceMotion
              }
              connectorDone={index > 0 && index <= activeIndex - 1}
              done={stationDone}
              first={index === 0}
              gate={stationActive && isGate}
              key={station}
              active={stationActive && (isWorking || isGate)}
              pulse={stationActive && isWorking && !reduceMotion}
              stopped={stationActive && isStopped}
              compact={compact}
            />
          )
        })}
      </div>
      {withLabels && (
        <div className="mt-1 flex items-center">
          {stations.map((station, index) => (
            <span
              className={cn(
                'min-w-0 flex-1 truncate t-caption font-semibold',
                index === 0 ? 'text-left' : 'text-center',
                index === activeIndex && !isDone
                  ? isGate
                    ? 'text-warning'
                    : 'text-brand'
                  : index < activeIndex || isDone
                    ? 'text-muted-foreground'
                    : 'text-muted-foreground/45',
              )}
              key={station}
            >
              {stationLabel(station, t)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function StationNode({
  active,
  compact,
  connectorDone,
  connectorFlowing,
  done,
  first,
  gate,
  pulse,
  stopped,
}: {
  active: boolean
  compact: boolean
  connectorDone: boolean
  connectorFlowing: boolean
  done: boolean
  first: boolean
  gate: boolean
  pulse: boolean
  stopped: boolean
}) {
  return (
    <>
      {!first && (
        <span
          className={cn(
            'h-0.5 min-w-2 flex-1 rounded-full',
            connectorFlowing
              ? 'inqtrix-agent-flow'
              : connectorDone
                ? 'bg-brand/45'
                : 'bg-muted',
          )}
        />
      )}
      <span className="relative flex size-3 shrink-0 items-center justify-center">
        {pulse && !gate && (
          <>
            <span className="inqtrix-active-node-ring absolute inset-0 rounded-full border border-brand/50" />
            <span className="inqtrix-active-node-ring-delayed absolute inset-0 rounded-full border border-brand/35" />
          </>
        )}
        <span
          className={cn(
            'rounded-full',
            compact ? 'size-1.5' : 'size-2',
            done
              ? 'bg-brand'
              : active
                ? gate
                  ? 'inqtrix-running-dot bg-warning'
                  : 'bg-brand'
                : stopped
                  ? 'bg-destructive/70'
                  : 'bg-muted-foreground/30',
          )}
        />
      </span>
    </>
  )
}

function stationLabel(
  station: string,
  t: ReturnType<typeof useLocale>['t'],
): string {
  const labels = t.agent.stations as Record<string, string | undefined>
  return labels[station] ?? station
}

/**
 * The one-line live activity readout under the pulse track: crossfades
 * (never blinks) between what the agent is doing right now. Prefers the
 * explicit activity event, falls back to a phase/task summary the caller
 * derives. Companion piece of the pulse track — same recurring gestalt.
 */
export function AgentActivityLine({
  className,
  gate = false,
  text,
}: {
  className?: string
  /** Waiting state: warning tone instead of brand. */
  gate?: boolean
  text: string
}) {
  const reduceMotion = Boolean(useReducedMotion())
  return (
    <div
      aria-atomic="true"
      aria-live="polite"
      className={cn(
        'flex min-w-0 items-center gap-1.5 t-meta',
        gate ? 'text-warning' : 'text-muted-foreground',
        className,
      )}
      role="status"
    >
      <span
        aria-hidden="true"
        className={cn(
          'size-1.5 shrink-0 rounded-full',
          gate ? 'bg-warning' : 'bg-brand',
          !reduceMotion && 'inqtrix-running-dot',
        )}
      />
      <AnimatePresence initial={false} mode="wait">
        <motion.span
          animate={{ opacity: 1, y: 0 }}
          className="truncate"
          exit={reduceMotion ? undefined : { opacity: 0, y: -5 }}
          initial={reduceMotion ? false : { opacity: 0, y: 5 }}
          key={text}
          transition={appMotion.list}
        >
          {text}
        </motion.span>
      </AnimatePresence>
    </div>
  )
}
