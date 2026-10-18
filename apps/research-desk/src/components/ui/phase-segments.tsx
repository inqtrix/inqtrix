import { cn } from '@/lib/utils'

/**
 * Phase-segment bar ("Atembalken") shared by run cards across features.
 * Done segments are solid brand, the active one shows a calmly breathing
 * fill (CSS `inqtrix-segment-breathe`), upcoming ones stay neutral.
 * Decorative — the phase is conveyed textually next to it; `withLabels`
 * adds the phase captions. Parameterized over the phase list so the
 * research (5-phase) and agent (6-station) tracks render identically.
 */
export function PhaseSegments<Phase extends string>({
  activePhase,
  completedPhases,
  labelFor,
  phases,
  thin = false,
  withLabels = false,
}: {
  activePhase: Phase
  completedPhases: readonly Phase[]
  /** Caption per phase; required when `withLabels` is set. */
  labelFor?: (phase: Phase) => string
  phases: readonly Phase[]
  thin?: boolean
  withLabels?: boolean
}) {
  const activeIndex = phases.indexOf(activePhase)

  return (
    <div aria-hidden="true" className="min-w-0 flex-1">
      <div className="flex items-center gap-1">
        {phases.map((phase, index) => {
          const isDone = index < activeIndex || completedPhases.includes(phase)
          const isActive = phase === activePhase
          return (
            <span
              className={cn(
                'relative flex-1 overflow-hidden rounded-full',
                thin ? 'h-1' : 'h-1.5',
                isDone ? 'bg-brand' : isActive ? 'bg-brand/15' : 'bg-muted',
              )}
              key={phase}
            >
              {isActive && (
                <span className="inqtrix-segment-breathe absolute inset-0 rounded-full bg-brand" />
              )}
            </span>
          )
        })}
      </div>
      {withLabels && labelFor && (
        <div className="mt-1.5 flex items-center gap-1">
          {phases.map((phase, index) => (
            <span
              className={cn(
                'min-w-0 flex-1 truncate text-center t-caption font-semibold',
                index === activeIndex
                  ? 'text-brand'
                  : index < activeIndex
                    ? 'text-muted-foreground'
                    : 'text-muted-foreground/45',
              )}
              key={phase}
            >
              {labelFor(phase)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
