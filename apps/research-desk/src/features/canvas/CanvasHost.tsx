import {
  useLayoutEffect,
  useRef,
  useState,
  type ComponentType,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { FileText, MoreVertical, Pin, X } from '@/components/icons'
import type { LucideIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'
import { resolveCanvasRenderer, type CanvasViewRegistry } from './registry'
import { canvasTransitionKey } from './transition'
import {
  activeCanvasView,
  type CanvasState,
  type CanvasTab,
  type CanvasViewDescriptor,
} from './types'

export type CanvasHostLabels = {
  close: string
  closeTab: string
  /** Aria label of the tab-overflow dots menu. */
  tabOverflow: string
  follow: string
  pinTab: string
  pinned: string
  previewTab: string
  unpin: string
}

/**
 * The polymorphic canvas host: ONE right-hand panel
 * whose content is a TAB ROW rendered through an agent-agnostic
 * registry. Header grammar: tabs · add-slot · follow/pin chip · close.
 * View-specific toolbars live inside the views themselves.
 */
export function CanvasHost({
  addMenu,
  canvas,
  emptyState,
  iconFor,
  labelFor,
  labels,
  onActivateTab,
  onClose,
  onCloseTab,
  onPinTab,
  onSetPinned,
  registry,
  working = false,
}: {
  /** Feature-owned "+"-slot (e.g. an option menu opening more views). */
  addMenu?: React.ReactNode
  canvas: CanvasState
  /** Rendered when no tab is open (canvas opened before first artifact). */
  emptyState: React.ReactNode
  /** Optional feature-owned tab icon per descriptor. */
  iconFor?: (descriptor: CanvasViewDescriptor) => ComponentType<{
    className?: string
  }> | null
  /** Feature-owned descriptor label (tab captions). */
  labelFor: (descriptor: CanvasViewDescriptor) => string
  labels: CanvasHostLabels
  onActivateTab: (key: string) => void
  onClose: () => void
  onCloseTab: (key: string) => void
  /** Claims the preview tab as a user-owned (pinned) tab. */
  onPinTab: (key: string) => void
  /** Pin freezes follow; unpin resumes it (the chip toggles both ways). */
  onSetPinned: (pinned: boolean) => void
  registry: CanvasViewRegistry
  /** Agent actively working — drives the follow chip's breathing dot. */
  working?: boolean
}) {
  const reduceMotion = Boolean(useReducedMotion())
  const active = activeCanvasView(canvas)
  const Renderer = active ? resolveCanvasRenderer(registry, active) : null
  const transitionKey = canvasTransitionKey(active)
  const scrollPositionsRef = useRef(new Map<string, number>())
  const activePaneRef = useRef<HTMLDivElement>(null)
  // Tabs that no longer fit stay reachable through the dots menu
  // — detected by real overflow, so
  // the menu appears exactly when captions start to vanish.
  const tabStripRef = useRef<HTMLDivElement>(null)
  const [tabsOverflow, setTabsOverflow] = useState(false)
  useLayoutEffect(() => {
    const node = tabStripRef.current
    if (!node) return
    const measure = () =>
      setTabsOverflow(node.scrollWidth > node.clientWidth + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [canvas.tabs])

  useLayoutEffect(() => {
    const viewport = activePaneRef.current?.querySelector<HTMLElement>(
      '[data-radix-scroll-area-viewport]',
    )
    if (viewport) {
      viewport.scrollTop = scrollPositionsRef.current.get(transitionKey) ?? 0
    }
    return () => {
      if (viewport) {
        scrollPositionsRef.current.set(transitionKey, viewport.scrollTop)
      }
    }
  }, [transitionKey])

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col bg-background">
      <header className="z-10 flex inqtrix-panel-header shrink-0 items-center gap-2 border-b border-border bg-background px-3">
        <div
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          ref={tabStripRef}
          role="tablist"
        >
          {canvas.tabs.map((tab) => (
            <CanvasTabChip
              iconFor={iconFor}
              isActive={tab.key === canvas.activeTabId}
              key={tab.key}
              labelFor={labelFor}
              labels={labels}
              onActivate={() => onActivateTab(tab.key)}
              onCloseTab={() => onCloseTab(tab.key)}
              onPinTab={() => onPinTab(tab.key)}
              tab={tab}
            />
          ))}
          {addMenu}
        </div>

        {tabsOverflow && (
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={labels.tabOverflow}
                className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
                size="icon"
                type="button"
                variant="ghost"
              >
                <MoreVertical className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className={cn(optionMenuContentClassName, 'w-72')}
              side="bottom"
              sideOffset={6}
            >
              <div className="py-1">
                {canvas.tabs.map((tab) => (
                  <div className="group/tabrow relative" key={tab.key}>
                    <OptionMenuItem
                      active={tab.key === canvas.activeTabId}
                      icon={(iconFor?.(tab.descriptor) ?? FileText) as LucideIcon}
                      label={labelFor(tab.descriptor)}
                      onSelect={() => onActivateTab(tab.key)}
                    />
                    <button
                      aria-label={labels.closeTab}
                      className="absolute right-1.5 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover/tabrow:opacity-100"
                      onClick={(event) => {
                        event.stopPropagation()
                        onCloseTab(tab.key)
                      }}
                      type="button"
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                ))}
              </div>
            </DropdownMenuContent>
          </DropdownMenu>
        )}

        <button
          aria-pressed={canvas.pinned}
          className={cn(
            'inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border px-2.5 transition-colors',
            canvas.pinned
              ? 'border-border bg-surface text-muted-foreground hover:text-foreground'
              : 'border-brand/20 bg-brand-subtle text-brand hover:bg-brand-subtle/80',
          )}
          onClick={() => onSetPinned(!canvas.pinned)}
          title={canvas.pinned ? labels.unpin : labels.follow}
          type="button"
        >
          {canvas.pinned ? (
            <Pin aria-hidden="true" className="size-3" />
          ) : (
            <span
              aria-hidden="true"
              className={cn(
                'size-1.5 rounded-full bg-brand',
                working && !reduceMotion && 'inqtrix-running-dot',
              )}
            />
          )}
          <span className="t-hint font-semibold">
            {canvas.pinned ? labels.pinned : labels.follow}
          </span>
        </button>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={labels.close}
              className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
              onClick={onClose}
              size="icon"
              type="button"
              variant="ghost"
            >
              <X className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{labels.close}</TooltipContent>
        </Tooltip>
      </header>

      <div className="relative min-h-0 flex-1">
        {/* Tab switches cross-fade; run-internal drill-ins never reach
            this presence (one key per run) — the run view pushes its
            detail layer itself with the list kept mounted. */}
        <AnimatePresence initial={false} mode="sync">
          <motion.div
            animate="center"
            className="absolute inset-0 flex min-h-0 flex-col bg-background"
            exit={reduceMotion ? undefined : 'exit'}
            initial={reduceMotion ? false : 'enter'}
            key={transitionKey}
            ref={activePaneRef}
            transition={appMotion.panel}
            variants={{
              center: { opacity: 1 },
              enter: { opacity: 0 },
              exit: { opacity: 0 },
            }}
          >
            {Renderer && active ? (
              <Renderer descriptor={active} />
            ) : (
              emptyState
            )}
          </motion.div>
        </AnimatePresence>
      </div>
    </section>
  )
}

/**
 * One tab chip: caption + preview marker (unpinned = the agent-follow
 * slot, shown muted with a dot), hover affordances for pin (preview
 * only) and close. A div with button semantics — nesting real buttons
 * inside a button is invalid DOM.
 */
function CanvasTabChip({
  iconFor,
  isActive,
  labelFor,
  labels,
  onActivate,
  onCloseTab,
  onPinTab,
  tab,
}: {
  iconFor?: (descriptor: CanvasViewDescriptor) => ComponentType<{
    className?: string
  }> | null
  isActive: boolean
  labelFor: (descriptor: CanvasViewDescriptor) => string
  labels: CanvasHostLabels
  onActivate: () => void
  onCloseTab: () => void
  onPinTab: () => void
  tab: CanvasTab
}) {
  const Icon = iconFor?.(tab.descriptor) ?? null
  return (
    <div
      aria-selected={isActive}
      className={cn(
        'group flex h-6 shrink-0 cursor-pointer select-none items-center gap-1 rounded-md border px-2 transition-colors',
        isActive
          ? 'border-border bg-surface text-foreground'
          : 'border-transparent text-muted-foreground hover:bg-surface/60 hover:text-foreground',
      )}
      onClick={onActivate}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onActivate()
        }
      }}
      role="tab"
      tabIndex={0}
      title={tab.pinned ? undefined : labels.previewTab}
    >
      {Icon && <Icon aria-hidden="true" className="size-3 shrink-0" />}
      <span className="max-w-40 truncate t-hint font-medium">
        {labelFor(tab.descriptor)}
      </span>
      {!tab.pinned && (
        <span
          aria-hidden="true"
          className="size-1 shrink-0 rounded-full bg-muted-foreground/50 group-hover:hidden"
        />
      )}
      {!tab.pinned && (
        <button
          aria-label={labels.pinTab}
          className="hidden size-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground group-hover:flex"
          onClick={(event) => {
            event.stopPropagation()
            onPinTab()
          }}
          type="button"
        >
          <Pin aria-hidden="true" className="size-2.5" />
        </button>
      )}
      <button
        aria-label={labels.closeTab}
        className="hidden size-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground group-hover:flex"
        onClick={(event) => {
          event.stopPropagation()
          onCloseTab()
        }}
        type="button"
      >
        <X aria-hidden="true" className="size-2.5" />
      </button>
    </div>
  )
}
