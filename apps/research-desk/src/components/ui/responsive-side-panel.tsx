import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { useId, useRef, type ReactNode } from 'react'

import { X } from '@/components/icons'
import { useModalFocusTrap } from '@/components/ui/use-modal-focus-trap'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'

type ResponsiveSidePanelProps = {
  children: ReactNode
  className?: string
  closeLabel: string
  controlsId: string
  onOpenChange: (open: boolean) => void
  open: boolean
  showHeader?: boolean
  side: 'left' | 'right'
  title: ReactNode
}

export function ResponsiveSidePanel({
  children,
  className,
  closeLabel,
  controlsId,
  onOpenChange,
  open,
  showHeader = true,
  side,
  title,
}: ResponsiveSidePanelProps) {
  const panelRef = useRef<HTMLElement | null>(null)
  const titleId = useId()
  const reduceMotion = Boolean(useReducedMotion())
  const hiddenX = side === 'left' ? -16 : 16

  useModalFocusTrap({
    onClose: () => onOpenChange(false),
    open,
    panelRef,
  })

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          animate={{ opacity: 1 }}
          className="fixed bottom-0 left-[var(--header-h)] right-0 top-[var(--header-h)] z-40 bg-background/70 backdrop-blur lg:hidden"
          exit={{ opacity: 0 }}
          initial={{ opacity: 0 }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onOpenChange(false)
          }}
          transition={reduceMotion ? { duration: 0 } : appMotion.panel}
        >
          <motion.section
            aria-labelledby={titleId}
            aria-modal="true"
            animate={{ opacity: 1, x: 0 }}
            className={cn(
              'absolute top-0 flex h-full w-full flex-col overflow-hidden border-border bg-background shadow-lg',
              side === 'left'
                ? 'left-0 max-w-[24rem] border-r'
                : 'right-0 border-l sm:w-4/5 sm:max-w-[56rem]',
              className,
            )}
            exit={{ opacity: 0, x: reduceMotion ? 0 : hiddenX }}
            id={controlsId}
            initial={{ opacity: 0, x: reduceMotion ? 0 : hiddenX }}
            ref={panelRef}
            role="dialog"
            tabIndex={-1}
            transition={reduceMotion ? { duration: 0 } : appMotion.panel}
          >
            {showHeader ? (
              <header className="flex inqtrix-panel-header shrink-0 items-center justify-between gap-3 border-b border-border px-3">
                <h2 className="min-w-0 truncate t-section text-foreground" id={titleId}>
                  {title}
                </h2>
                <button
                  aria-label={closeLabel}
                  className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => onOpenChange(false)}
                  type="button"
                >
                  <X className="icon-sm" />
                </button>
              </header>
            ) : (
              <h2 className="sr-only" id={titleId}>{title}</h2>
            )}
            <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
          </motion.section>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}
