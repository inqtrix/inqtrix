import * as React from "react"
import * as TooltipPrimitive from "@radix-ui/react-tooltip"

import { cn } from "@/lib/utils"

const TooltipProvider = TooltipPrimitive.Provider

/** Hover-to-tooltip dwell for the whole app.
 *
 * A tooltip that appears the moment the pointer arrives fires on every
 * click-through and reads as noise; it must earn its reveal by DWELLING.
 * 1.5s is deliberately calmer than a sub-second tooltip delay (the Radix
 * default is 700ms), and it applies to EVERY icon: no warm-up
 * shortcut for neighbors. */
const TOOLTIP_APPEAR_DELAY_MS = 1_500

/** Radix's provider-level warm-up window. Kept at 0 for documentation value
 * only: the warm state resets ASYNCHRONOUSLY on close (verified against
 * @radix-ui/react-tooltip 1.2.8), so a pointer crossing from an icon with an
 * open tooltip to its neighbor within one input batch still reopens
 * instantly no matter what this says. The per-icon dwell is therefore
 * enforced by the Tooltip wrapper below, not by this prop. */
const TOOLTIP_WARMUP_SKIP_MS = 0

/** Cancel channel from the trigger to its Root wrapper.
 *
 * Why it must exist: the Root below is CONTROLLED, and Radix only reports an
 * open-state change when the desired state differs from the current `open`
 * prop. While a reveal is still pending (prop `false`), the pointer leaving
 * the trigger is therefore SWALLOWED — the armed timer would fire later and
 * strand an orphan tooltip nobody is hovering. Sweeping across an icon row
 * armed several timers at once and stacked multiple orphan tooltips (found
 * live). The trigger cancels through this context on every leave, press and
 * blur, independent of what Radix reports. */
const TooltipDwellContext = React.createContext<(() => void) | null>(null)

/** Radix Root with a DETERMINISTIC per-icon dwell.
 *
 * The provider runs with no delay of its own; every open request lands here
 * and waits the full dwell, and any close — reported by Radix or forced by
 * the trigger through the cancel channel — clears the pending reveal. This
 * is what guarantees "the delay applies to every icon": Radix's warm-state
 * shortcut can never race a timer that only this wrapper owns. */
const Tooltip = ({
  onOpenChange,
  open: controlledOpen,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Root>) => {
  const [open, setOpen] = React.useState(false)
  const revealTimerRef = React.useRef(0)
  React.useEffect(() => () => window.clearTimeout(revealTimerRef.current), [])
  const cancelAndClose = React.useCallback(() => {
    window.clearTimeout(revealTimerRef.current)
    setOpen(false)
  }, [])
  const handleOpenChange = React.useCallback((next: boolean) => {
    window.clearTimeout(revealTimerRef.current)
    if (next) {
      revealTimerRef.current = window.setTimeout(() => {
        setOpen(true)
        onOpenChange?.(true)
      }, TOOLTIP_APPEAR_DELAY_MS)
      return
    }
    setOpen(false)
    onOpenChange?.(false)
  }, [onOpenChange])
  return (
    <TooltipDwellContext.Provider value={cancelAndClose}>
      <TooltipPrimitive.Root
        {...props}
        onOpenChange={handleOpenChange}
        open={controlledOpen ?? open}
      />
    </TooltipDwellContext.Provider>
  )
}

const TooltipTrigger = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Trigger>
>(({ onBlur, onPointerDown, onPointerLeave, ...props }, ref) => {
  const cancelPendingReveal = React.useContext(TooltipDwellContext)
  return (
    <TooltipPrimitive.Trigger
      ref={ref}
      {...props}
      onBlur={(event) => {
        onBlur?.(event)
        cancelPendingReveal?.()
      }}
      onPointerDown={(event) => {
        onPointerDown?.(event)
        cancelPendingReveal?.()
      }}
      onPointerLeave={(event) => {
        onPointerLeave?.(event)
        cancelPendingReveal?.()
      }}
    />
  )
})
TooltipTrigger.displayName = TooltipPrimitive.Trigger.displayName

const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        "z-50 overflow-hidden rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 origin-[--radix-tooltip-content-transform-origin]",
        className
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
))
TooltipContent.displayName = TooltipPrimitive.Content.displayName

export {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
  TOOLTIP_APPEAR_DELAY_MS,
  TOOLTIP_WARMUP_SKIP_MS,
}
