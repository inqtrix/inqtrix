import { useLayoutEffect, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { AlertTriangle, Check, Info } from '@/components/icons'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import type { KnowledgeRunStepRecord } from '@/features/project/types'
import { knowledgeStepLine } from './stepLines'
import {
  knowledgeStepGlyphState,
  knowledgeStepFollowOffset,
  knowledgeStepViewportState,
  type KnowledgeStepListVariant,
} from './stepListView'

/**
 * The agent step list, shared by the live `KnowledgeRunCard` (running view) and
 * the source panel's "Schritte" tab (after-the-fact review). One renderer so
 * the live run and the retained steps look identical (no redundancy).
 *
 * Entry animation is presence-driven, not a prop: `AnimatePresence
 * initial={false}` means the steps already present when this list first
 * renders appear in place (a card mounting with history — opening the desk,
 * or a reload where the server replays a run's steps), while a step that
 * arrives later rises on its own. There is no stagger: a stagger only ever
 * looked right for a burst, and a burst is exactly the case that should not
 * animate at all.
 */
export function KnowledgeStepList({
  steps,
  collectionCount,
  failed = false,
  variant = 'default',
}: {
  steps: readonly KnowledgeRunStepRecord[]
  collectionCount: number
  failed?: boolean
  variant?: KnowledgeStepListVariant
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const isLive = variant === 'live'
  const motionTransition = reduceMotion ? { duration: 0 } : appMotion.list
  const viewport = knowledgeStepViewportState({ failed, steps, variant })
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const listRef = useRef<HTMLOListElement | null>(null)
  const rowRefs = useRef(new Map<string, HTMLLIElement>())
  const [followOffset, setFollowOffset] = useState(0)
  const [viewportHeight, setViewportHeight] = useState<number | null>(null)

  useLayoutEffect(() => {
    if (!viewport.managedScroll) {
      setFollowOffset(0)
      setViewportHeight(null)
      return
    }

    const viewportEl = viewportRef.current
    const listEl = listRef.current
    if (!viewportEl || !listEl) return

    const maxHeight = Number.parseFloat(getComputedStyle(viewportEl).maxHeight)
    const targetHeight = Math.min(
      Number.isFinite(maxHeight) && maxHeight > 0 ? maxHeight : listEl.scrollHeight,
      listEl.scrollHeight,
    )
    const maxOffset = Math.max(0, listEl.scrollHeight - targetHeight)
    const followStepId = viewport.followStepId
    const followEl = followStepId ? rowRefs.current.get(followStepId) : null
    const nextOffset = followEl
      ? knowledgeStepFollowOffset({
        followBottom: followEl.offsetTop + followEl.offsetHeight,
        maxOffset,
        viewportHeight: targetHeight,
      })
      : maxOffset

    setViewportHeight((current) => (current !== null && Math.abs(current - targetHeight) < 1 ? current : targetHeight))
    setFollowOffset((current) => (Math.abs(current - nextOffset) < 1 ? current : nextOffset))
  }, [viewport.followStepId, viewport.managedScroll, steps])

  return (
    <div
      className={cn(
        isLive && 'inqtrix-step-viewport-shell',
        viewport.overflowing && 'inqtrix-step-viewport-shell-overflowing',
      )}
      data-active-step-id={viewport.activeStepId ?? undefined}
      data-follow-step-id={viewport.followStepId ?? undefined}
      data-knowledge-step-scroll={viewport.managedScroll ? 'managed' : 'static'}
      data-knowledge-step-viewport={viewport.smartFade ? 'smart-fade' : 'static'}
    >
      <motion.div
        animate={isLive && viewportHeight !== null ? { height: viewportHeight } : undefined}
        className={cn(isLive && 'inqtrix-step-viewport')}
        data-knowledge-step-viewport-height={viewportHeight === null ? undefined : Math.round(viewportHeight)}
        ref={viewportRef}
        transition={motionTransition}
      >
        <motion.ol
          animate={isLive ? { y: -followOffset } : undefined}
          className={cn('relative space-y-1.5', isLive && 'inqtrix-step-list-live')}
          data-follow-offset={Math.round(followOffset)}
          ref={listRef}
          transition={motionTransition}
        >
          {isLive && steps.length > 1 && (
            <span aria-hidden="true" className="inqtrix-step-spine" />
          )}
          <AnimatePresence initial={false}>
          {steps.map((step) => {
            const line = knowledgeStepLine(step, { collectionCount, t: t.knowledge })
            const glyphState = knowledgeStepGlyphState({ failed, status: line.status, variant })
            const isRunning = glyphState === 'running'
            const rowInitial = reduceMotion
              ? false
              : { opacity: 0, y: isLive ? 2 : 4 }
            return (
              <motion.li
                animate={{ opacity: 1, y: 0 }}
                className={cn(
                  'relative grid min-w-0 items-start gap-2',
                  isLive
                    ? 'inqtrix-step-row-live rounded-md py-1 pr-1'
                    : 'grid-cols-[14px_minmax(0,1fr)]',
                  isLive && isRunning && 'bg-brand-subtle/25 ring-1 ring-inset ring-brand/20',
                )}
                data-knowledge-step-glyph={glyphState}
                data-knowledge-step-id={line.id}
                data-knowledge-step-status={line.status}
                initial={rowInitial}
                key={line.id}
                ref={(node) => {
                  if (node) {
                    rowRefs.current.set(line.id, node)
                  } else {
                    rowRefs.current.delete(line.id)
                  }
                }}
                transition={motionTransition}
              >
                <span className="relative z-10 flex h-5 items-center justify-center">
                  {isRunning ? (
                    <span
                      aria-hidden="true"
                      className={cn(
                        'grid size-4 place-items-center rounded-full border border-brand/35 bg-card',
                        !reduceMotion && 'inqtrix-step-running-shell',
                      )}
                    >
                      <span
                        className={cn(
                          'size-2 rounded-full bg-brand shadow-[0_0_0_3px_var(--brand-subtle)]',
                          !reduceMotion && 'inqtrix-running-dot',
                        )}
                      />
                    </span>
                  ) : (
                    <span
                      aria-hidden="true"
                      className={cn(
                        'grid size-4 place-items-center rounded-full border',
                        glyphState === 'complete'
                          ? 'inqtrix-step-complete'
                          : 'border-border bg-card text-muted-foreground/70',
                        glyphState === 'complete' && !reduceMotion && 'inqtrix-step-complete-motion',
                      )}
                    >
                      <Check
                        className={cn(
                          'size-3',
                          glyphState === 'complete' && 'inqtrix-step-complete-check',
                          glyphState === 'complete' && !reduceMotion && 'inqtrix-step-complete-check-motion',
                        )}
                      />
                    </span>
                  )}
                </span>
                <span className="min-w-0">
                  <span
                    className={cn(
                      'block t-list',
                      !isLive && 'truncate',
                      isRunning ? 'text-foreground' : 'text-muted-foreground',
                    )}
                  >
                    {line.primary}
                  </span>
                  {line.secondary && (
                    <span
                      className={cn(
                        'block t-meta-sm text-muted-foreground/80',
                        !isLive && 'truncate',
                        isLive && 'break-words',
                      )}
                    >
                      {line.secondary}
                    </span>
                  )}
                  {line.warning && (
                    <span className="mt-0.5 flex items-start gap-1 t-meta-sm text-warning">
                      <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                      <span className="min-w-0 break-words">{line.warning}</span>
                    </span>
                  )}
                  {line.information && (
                    <span className="mt-0.5 flex items-start gap-1 t-meta-sm text-brand">
                      <Info className="mt-0.5 size-3 shrink-0" />
                      <span className="min-w-0 break-words text-foreground/75">
                        {line.information}
                      </span>
                    </span>
                  )}
                </span>
              </motion.li>
            )
          })}
          </AnimatePresence>
          {isLive && (
            <li aria-hidden="true" className="inqtrix-step-tail list-none" />
          )}
        </motion.ol>
      </motion.div>
    </div>
  )
}
