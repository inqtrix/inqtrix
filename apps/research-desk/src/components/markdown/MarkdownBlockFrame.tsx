import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { AlertTriangle, Check, LoaderCircle, type LucideIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export type MarkdownBlockActionStatus = 'error' | 'idle' | 'pending' | 'success'

export type MarkdownBlockAction = {
  disabled?: boolean
  icon: LucideIcon
  id: string
  labels: {
    error: string
    idle: string
    pending: string
    success: string
  }
  onClick: () => void
  status: MarkdownBlockActionStatus
}

export function MarkdownBlockFrame({
  actions,
  children,
  className,
}: {
  actions: readonly MarkdownBlockAction[]
  children: ReactNode
  className?: string
}) {
  const frameRef = useRef<HTMLDivElement | null>(null)
  const actionPanelRef = useRef<HTMLDivElement | null>(null)
  const [outboardActions, setOutboardActions] = useState(false)

  useLayoutEffect(() => {
    const frame = frameRef.current
    if (!frame) return

    const boundary = nearestHorizontalClipBoundary(frame)
    const measure = () => {
      const frameRect = frame.getBoundingClientRect()
      const actionPanelWidth = actionPanelRef.current?.getBoundingClientRect().width ?? 42
      const boundaryRight = boundary
        ? boundary.getBoundingClientRect().right
        : document.documentElement.clientWidth
      setOutboardActions(boundaryRight - frameRect.right >= actionPanelWidth + 8)
    }
    measure()

    const observer = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(measure)
    observer?.observe(frame)
    if (actionPanelRef.current) observer?.observe(actionPanelRef.current)
    if (boundary) observer?.observe(boundary)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [])

  const liveMessage = actions
    .find((action) => action.status !== 'idle')
  const liveLabel = liveMessage
    ? liveMessage.labels[liveMessage.status]
    : ''

  return (
    <div
      className={cn('inqtrix-markdown-block group/markdown-block relative w-full', className)}
      ref={frameRef}
    >
      {children}
      {actions.length > 0 ? (
        <div
          className={cn(
            'inqtrix-markdown-block-actions absolute top-2 z-20',
            outboardActions ? 'left-full pl-2' : 'right-2',
          )}
          data-outboard={outboardActions ? 'true' : 'false'}
        >
          <div
            className="flex flex-col gap-1 rounded-md border border-border/70 bg-card/90 p-1 shadow-[0_8px_24px_var(--shadow-soft)] backdrop-blur"
            ref={actionPanelRef}
          >
            {actions.map((action) => {
              const label = action.labels[action.status]
              const Icon = action.status === 'pending'
                ? LoaderCircle
                : action.status === 'success'
                  ? Check
                  : action.status === 'error'
                    ? AlertTriangle
                    : action.icon
              return (
                <Tooltip key={action.id}>
                  <TooltipTrigger asChild>
                    <Button
                      aria-disabled={action.disabled || undefined}
                      aria-label={label}
                      className={cn(
                        'size-8 bg-background/80 text-muted-foreground hover:bg-surface hover:text-foreground',
                        action.status === 'success' && 'text-success hover:text-success',
                        action.status === 'error' && 'text-destructive hover:text-destructive',
                        action.disabled && 'cursor-not-allowed opacity-50 hover:bg-background/80 hover:text-muted-foreground',
                      )}
                      disabled={action.status === 'pending'}
                      onClick={() => {
                        if (!action.disabled) action.onClick()
                      }}
                      size="icon"
                      type="button"
                      variant="ghost"
                    >
                      <Icon className={cn('icon-sm', action.status === 'pending' && 'animate-spin')} />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="left">{label}</TooltipContent>
                </Tooltip>
              )
            })}
          </div>
        </div>
      ) : null}
      <span aria-live="polite" className="sr-only">{liveLabel}</span>
    </div>
  )
}

export function useMarkdownBlockAction() {
  const [status, setStatus] = useState<MarkdownBlockActionStatus>('idle')
  const resetTimerRef = useRef<number | null>(null)

  useEffect(() => () => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current)
    }
  }, [])

  const run = useCallback(async (
    operation: () => Promise<void> | void,
    warning: string,
  ) => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current)
      resetTimerRef.current = null
    }
    setStatus('pending')
    try {
      await operation()
      setStatus('success')
    } catch (error) {
      console.warn(warning, error)
      setStatus('error')
    }
    resetTimerRef.current = window.setTimeout(() => {
      setStatus('idle')
      resetTimerRef.current = null
    }, 1800)
  }, [])

  return { run, status }
}

function nearestHorizontalClipBoundary(element: HTMLElement): HTMLElement | null {
  let ancestor = element.parentElement
  while (ancestor) {
    const overflowX = getComputedStyle(ancestor).overflowX
    if (overflowX === 'auto' || overflowX === 'clip' || overflowX === 'hidden' || overflowX === 'scroll') {
      return ancestor
    }
    ancestor = ancestor.parentElement
  }
  return null
}
