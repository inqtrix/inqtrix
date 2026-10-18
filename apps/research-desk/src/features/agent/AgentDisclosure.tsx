import { useId, useState, type ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ChevronRight } from '@/components/icons'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'

/** Supplemental evidence details share keyboard, focus and motion behavior. */
export function AgentDisclosure({
  summary, children, icon, suffix, warning = false, animateEntrance = false,
  className, testId,
}: {
  summary: ReactNode
  children: ReactNode
  icon?: ReactNode
  suffix?: ReactNode
  warning?: boolean
  animateEntrance?: boolean
  className?: string
  testId?: string
}) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const reduceMotion = Boolean(useReducedMotion())
  const transition = reduceMotion ? { duration: 0 } : appMotion.list
  return (
    <motion.div className={className} data-testid={testId}
      initial={animateEntrance && !reduceMotion ? { opacity: 0 } : false}
      animate={{ opacity: 1 }} transition={transition}>
      <button type="button" aria-expanded={open} aria-controls={panelId}
        className={cn('flex w-full items-center gap-1.5 rounded py-1 text-left t-meta transition-colors hover:text-foreground',
          warning ? 'text-warning' : 'text-muted-foreground')}
        onClick={() => setOpen((value) => !value)}>
        {icon}
        <span className="min-w-0 flex-1">{summary}</span>
        {suffix}
        <ChevronRight className={cn('icon-xs shrink-0 transition-transform', open && 'rotate-90')} />
      </button>
      <AnimatePresence initial={false}>
        {open && <motion.div id={panelId} initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
          transition={transition} className="overflow-hidden">
          {children}
        </motion.div>}
      </AnimatePresence>
    </motion.div>
  )
}
