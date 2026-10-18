import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import {
  createContext,
  startTransition,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'

import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import {
  createStructuralLoadClock,
  scheduleAfterStructuralPaint,
  structuralLoadTiming,
  type StructuralLoadClock,
} from '@/motion/structuralLoadClock'

export { structuralLoadTiming }

export type StructuralLoadPhase = 'pending' | 'ready' | 'refreshing' | 'empty' | 'error'

export type StructuralVisibilityChange = {
  identity: string
  visible: boolean
}

type StructuralRenderRegistry = ReturnType<typeof createStructuralRenderRegistry>

/** Reference-counted registry for descendant work that can still change the
 * staged target's geometry. A release function owns only its registration and
 * is safe to call repeatedly during Strict Mode cleanup. */
export function createStructuralRenderRegistry() {
  const blockers = new Map<symbol, number>()
  const listeners = new Set<() => void>()

  const notify = () => {
    for (const listener of listeners) listener()
  }

  return {
    add(token: symbol) {
      blockers.set(token, (blockers.get(token) ?? 0) + 1)
      notify()

      let released = false
      return () => {
        if (released) return
        released = true

        const count = blockers.get(token)
        if (count === undefined) return
        if (count <= 1) blockers.delete(token)
        else blockers.set(token, count - 1)
        notify()
      }
    },

    clear() {
      if (blockers.size === 0) return
      blockers.clear()
      notify()
    },

    getSnapshot: () => blockers.size,

    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

type StructuralRenderContextValue = {
  accepting: boolean
  registry: StructuralRenderRegistry
}

const StructuralRenderContext = createContext<StructuralRenderContextValue | null>(null)

function StructuralLayer({
  children,
  context,
  mode,
}: {
  children: ReactNode
  context: StructuralRenderContextValue | null
  mode: 'retained' | 'staged' | 'visible'
}) {
  const staged = mode === 'staged'
  const inert = mode !== 'visible'

  // Every layer keeps this component and Provider shape while moving from
  // staged to visible. In particular, a Tiptap instance or decoded image that
  // released a blocker must not remount after the release it just proved.
  return (
    <StructuralRenderContext.Provider value={context}>
      <div
        aria-hidden={inert || undefined}
        className={cn(
          'flex min-h-0 min-w-0 flex-1 flex-col',
          staged && 'invisible pointer-events-none absolute inset-0 overflow-hidden',
        )}
        data-structural-layer={mode}
        inert={inert || undefined}
      >
        {children}
      </div>
    </StructuralRenderContext.Provider>
  )
}

/** Registers only work whose terminal commit can change the target region's
 * geometry. The boolean return lets a normally progressive child promote that
 * work while it is safely staged. Work discovered after visibility never
 * resurrects a structural fallback. */
export function useStructuralRenderBlocker(pending: boolean): boolean {
  const context = useContext(StructuralRenderContext)
  const tokenRef = useRef<symbol | null>(null)
  tokenRef.current ??= Symbol('structural-render-blocker')

  const blocking = Boolean(context?.accepting && pending)
  useLayoutEffect(() => {
    if (!blocking || !context) return undefined
    return context.registry.add(tokenRef.current as symbol)
  }, [blocking, context])

  return blocking
}

type FallbackState = 'hidden' | 'visible' | 'exiting'

type BoundaryMachine = {
  active: boolean
  cycle: number
  fallback: FallbackState
  observedPhase: StructuralLoadPhase
  registry: StructuralRenderRegistry
  requestedIdentity: string
  targetKey: string
  targetMounted: boolean
  visibleIdentity: string | null
  visibleKey: string | null
}

function isImmediatePhase(phase: StructuralLoadPhase) {
  return phase === 'refreshing' || phase === 'empty' || phase === 'error'
}

function createInitialMachine(identity: string, phase: StructuralLoadPhase): BoundaryMachine {
  const immediate = isImmediatePhase(phase)
  const targetKey = 'structural-cycle-0'
  return {
    active: !immediate,
    cycle: 0,
    fallback: 'hidden',
    observedPhase: phase,
    registry: createStructuralRenderRegistry(),
    requestedIdentity: identity,
    targetKey,
    targetMounted: phase !== 'pending',
    visibleIdentity: immediate ? identity : null,
    visibleKey: immediate ? targetKey : null,
  }
}

function beginCycle(
  current: BoundaryMachine,
  identity: string,
  phase: StructuralLoadPhase,
): BoundaryMachine {
  const cycle = current.cycle + 1
  const targetKey = `structural-cycle-${cycle}`
  const immediate = isImmediatePhase(phase)
  return {
    active: !immediate,
    cycle,
    fallback: 'hidden',
    observedPhase: phase,
    registry: createStructuralRenderRegistry(),
    requestedIdentity: identity,
    targetKey,
    targetMounted: phase !== 'pending',
    visibleIdentity: immediate ? identity : current.visibleIdentity,
    visibleKey: immediate ? targetKey : current.visibleKey,
  }
}

export function StructuralLoadBoundary({
  children,
  fallback,
  identity,
  phase,
  className,
  onVisibilityChange,
}: {
  children: ReactNode
  /** Complete silhouette for this bounded region. It is not requested until
   * the shared delay expires. */
  fallback: ReactNode
  /** Semantic identity whose header and body must become visible atomically. */
  identity: string
  /** Data/render state for this identity. */
  phase: StructuralLoadPhase
  className?: string
  /** Reports one hidden and one ready edge for each non-cancelled cycle. The
   * ready edge fires after the target commit, while an optional fallback is
   * still opaque, so scroll/focus preparation stays invisible. */
  onVisibilityChange?: (change: StructuralVisibilityChange) => void
}) {
  const reduceMotion = useReducedMotion()
  const [machine, setMachine] = useState(() => createInitialMachine(identity, phase))
  const visibleContentRef = useRef<ReactNode>(isImmediatePhase(phase) ? children : null)
  const targetContentRef = useRef<ReactNode>(children)
  const clockRef = useRef<StructuralLoadClock | null>(null)
  const reportedStartCycleRef = useRef<number | null>(null)
  const reportedVisibleCycleRef = useRef<number | null>(null)
  const exitingCycleRef = useRef<number | null>(null)

  // React's documented render-time state adjustment pattern makes an identity
  // switch synchronous: no old clock or fallback is allowed one extra commit.
  if (identity !== machine.requestedIdentity) {
    targetContentRef.current = children
    const next = beginCycle(machine, identity, phase)
    if (!next.active) visibleContentRef.current = children
    setMachine(next)
  } else if (phase !== machine.observedPhase) {
    if (!machine.active && phase === 'pending') {
      targetContentRef.current = children
      setMachine(beginCycle(machine, identity, phase))
    } else {
      setMachine((current) => ({
        ...current,
        observedPhase: phase,
        targetMounted: current.targetMounted || phase !== 'pending',
      }))
    }
  } else if (machine.active) {
    targetContentRef.current = children
  } else if (machine.visibleIdentity === identity) {
    visibleContentRef.current = children
  }

  const blockerCount = useSyncExternalStore(
    machine.registry.subscribe,
    machine.registry.getSnapshot,
    machine.registry.getSnapshot,
  )

  const stagedContext = useMemo<StructuralRenderContextValue>(() => ({
    accepting: machine.active,
    registry: machine.registry,
  }), [machine.active, machine.registry])

  // A genuinely pending target starts after the shell's first painted frame.
  // Cached/ready targets mount in the requesting commit and never pay this
  // scheduling cost.
  useLayoutEffect(() => {
    if (!machine.active || machine.targetMounted) return undefined
    const cycle = machine.cycle
    return scheduleAfterStructuralPaint(() => {
      startTransition(() => {
        setMachine((current) => current.cycle === cycle && current.active
          ? { ...current, targetMounted: true }
          : current)
      })
    })
  }, [machine.active, machine.cycle, machine.targetMounted])

  // Start a fresh clock for the active cycle and dispose every timer and
  // blocker when that cycle completes, is superseded, or unmounts.
  useLayoutEffect(() => {
    if (!machine.active) return undefined

    const cycle = machine.cycle
    const registry = machine.registry
    const clock = createStructuralLoadClock({
      onFallbackRequested: () => {
        setMachine((current) => current.cycle === cycle && current.active
          ? { ...current, fallback: 'visible' }
          : current)
      },
      onRelease: (fallbackWasShown) => {
        setMachine((current) => {
          if (current.cycle !== cycle || !current.active) return current
          visibleContentRef.current = targetContentRef.current
          if (fallbackWasShown) exitingCycleRef.current = cycle
          return {
            ...current,
            active: false,
            fallback: fallbackWasShown ? 'exiting' : 'hidden',
            visibleIdentity: current.requestedIdentity,
            visibleKey: current.targetKey,
          }
        })
      },
    })
    clockRef.current = clock
    clock.arm()

    return () => {
      clock.cancel()
      registry.clear()
      if (clockRef.current === clock) clockRef.current = null
    }
  }, [machine.active, machine.cycle, machine.registry])

  // Start the minimum-visible interval only once the fallback is actually in
  // the committed tree. This layout effect precedes the readiness effect.
  useLayoutEffect(() => {
    if (machine.fallback === 'visible') clockRef.current?.fallbackCommitted()
  }, [machine.fallback])

  // Child layout effects register first. Reading the registry directly here
  // closes the zero-count window before useSyncExternalStore re-renders us.
  useLayoutEffect(() => {
    if (!machine.active || !machine.targetMounted || machine.observedPhase === 'pending') return
    if (machine.observedPhase === 'ready' && machine.registry.getSnapshot() > 0) return
    clockRef.current?.release()
  }, [
    blockerCount,
    machine.active,
    machine.observedPhase,
    machine.registry,
    machine.targetMounted,
  ])

  useLayoutEffect(() => {
    if (reportedStartCycleRef.current !== machine.cycle) {
      reportedStartCycleRef.current = machine.cycle
      onVisibilityChange?.({ identity: machine.requestedIdentity, visible: false })
    }
    if (!machine.active && reportedVisibleCycleRef.current !== machine.cycle) {
      reportedVisibleCycleRef.current = machine.cycle
      onVisibilityChange?.({ identity: machine.requestedIdentity, visible: true })
    }
  }, [machine.active, machine.cycle, machine.requestedIdentity, onVisibilityChange])

  const handleExitComplete = useCallback(() => {
    const completedCycle = exitingCycleRef.current
    if (completedCycle === null) return
    setMachine((current) => current.cycle === completedCycle && current.fallback === 'exiting'
      ? { ...current, fallback: 'hidden' }
      : current)
    exitingCycleRef.current = null
  }, [])

  const state = machine.active
    ? machine.fallback === 'visible'
      ? 'fallback'
      : machine.visibleIdentity === null
        ? machine.observedPhase === 'pending' ? 'pending' : 'staging'
        : 'retained'
    : machine.fallback === 'exiting'
      ? 'revealing'
      : machine.observedPhase

  const visibleLayer = machine.visibleKey === null ? null : (
    <StructuralLayer
      context={null}
      key={machine.visibleKey}
      mode={machine.active ? 'retained' : 'visible'}
    >
      {visibleContentRef.current}
    </StructuralLayer>
  )

  const stagedLayer = machine.active && machine.targetMounted ? (
    <StructuralLayer context={stagedContext} key={machine.targetKey} mode="staged">
      {children}
    </StructuralLayer>
  ) : null

  return (
    <div
      className={cn('relative flex min-h-0 min-w-0 flex-col', className ?? 'h-full w-full')}
      data-structural-blockers={blockerCount}
      data-structural-phase={machine.observedPhase}
      data-structural-region=""
      data-structural-requested-identity={machine.requestedIdentity}
      data-structural-state={state}
      data-structural-visible-identity={machine.visibleIdentity ?? ''}
    >
      {visibleLayer}
      {stagedLayer}
      <AnimatePresence initial={false} onExitComplete={handleExitComplete}>
        {machine.fallback === 'visible' && (
          <motion.div
            aria-hidden
            className="absolute inset-0 z-10 flex min-h-0 min-w-0 flex-col overflow-hidden bg-background"
            data-structural-fallback=""
            exit={{ opacity: 0 }}
            key={`structural-fallback-${machine.cycle}`}
            transition={reduceMotion ? { duration: 0 } : appMotion.reveal}
          >
            {fallback}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
