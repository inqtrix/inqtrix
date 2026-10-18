import { motion, useReducedMotion } from 'motion/react'
import {
  type ComponentProps,
  type ReactNode,
} from 'react'

import { ResizableHandle } from '@/components/ui/resizable'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'

type PanelSide = 'left' | 'right'

type AnimatedResizableHandleProps = ComponentProps<typeof ResizableHandle> & {
  expanded: boolean
}

export function AnimatedResizableHandle({
  className,
  disabled,
  expanded,
  orientation = 'horizontal',
  ...props
}: AnimatedResizableHandleProps) {
  return (
    <ResizableHandle
      className={cn(
        'inqtrix-resizable-panel-handle',
        expanded ? 'opacity-100' : 'pointer-events-none opacity-0',
        !expanded && (orientation === 'vertical' ? 'h-0 after:h-0' : 'w-0 after:w-0'),
        className,
      )}
      disabled={disabled ?? !expanded}
      orientation={orientation}
      {...props}
    />
  )
}

type AnimatedPanelBodyProps = {
  children: ReactNode
  className?: string
  expanded: boolean
  side: PanelSide
}

export function AnimatedPanelBody({
  children,
  className,
  expanded,
  side,
}: AnimatedPanelBodyProps) {
  const reduceMotion = Boolean(useReducedMotion())
  const hiddenOffset = side === 'left' ? -10 : 10

  return (
    <motion.div
      aria-hidden={!expanded}
      animate={expanded
        ? { opacity: 1, x: 0 }
        : { opacity: 0, x: reduceMotion ? 0 : hiddenOffset }}
      className={cn(
        'h-full min-h-0 w-full min-w-0 overflow-hidden',
        !expanded && 'pointer-events-none',
        className,
      )}
      inert={!expanded ? true : undefined}
      initial={false}
      transition={reduceMotion ? { duration: 0 } : appMotion.panel}
    >
      {children}
    </motion.div>
  )
}

type AnimatedFixedSidePanelProps = {
  children: ReactNode
  className?: string
  controlsId: string
  expanded: boolean
  expandedWidth: string
  side: PanelSide
}

export function AnimatedFixedSidePanel({
  children,
  className,
  controlsId,
  expanded,
  expandedWidth,
  side,
}: AnimatedFixedSidePanelProps) {
  const reduceMotion = Boolean(useReducedMotion())
  const hiddenOffset = side === 'left' ? -10 : 10

  return (
    <motion.div
      aria-hidden={!expanded}
      animate={reduceMotion
        ? { opacity: expanded ? 1 : 0, width: expanded ? expandedWidth : '0rem', x: 0 }
        : {
          opacity: expanded ? 1 : 0,
          width: expanded ? expandedWidth : '0rem',
          x: expanded ? 0 : hiddenOffset,
        }}
      className={cn(
        'flex h-full min-h-0 min-w-0 shrink-0 overflow-hidden',
        !expanded && 'pointer-events-none',
        className,
      )}
      id={controlsId}
      inert={!expanded ? true : undefined}
      initial={false}
      transition={reduceMotion ? { duration: 0 } : appMotion.panel}
      data-side-panel-expanded={expanded ? 'true' : 'false'}
    >
      {children}
    </motion.div>
  )
}
