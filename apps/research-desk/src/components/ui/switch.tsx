import * as React from "react"
import * as SwitchPrimitives from "@radix-ui/react-switch"

import { cn } from "@/lib/utils"

type SwitchDensity = 'default' | 'table'

const switchRootDensityClasses: Record<SwitchDensity, string> = {
  default:
    'h-5 w-9 border-2 shadow-sm data-[state=checked]:bg-primary data-[state=unchecked]:bg-input',
  table:
    'h-4 w-7 border shadow-none data-[state=checked]:bg-foreground/80 data-[state=unchecked]:bg-muted',
}

const switchThumbDensityClasses: Record<SwitchDensity, string> = {
  default: 'h-4 w-4 shadow-lg data-[state=checked]:translate-x-4',
  table: 'h-3 w-3 shadow-sm data-[state=checked]:translate-x-3',
}

type SwitchProps = React.ComponentPropsWithoutRef<
  typeof SwitchPrimitives.Root
> & {
  density?: SwitchDensity
}

const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitives.Root>,
  SwitchProps
>(({ className, density = 'default', ...props }, ref) => (
  <SwitchPrimitives.Root
    className={cn(
      "peer inline-flex shrink-0 cursor-pointer items-center rounded-full border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50",
      switchRootDensityClasses[density],
      className
    )}
    {...props}
    ref={ref}
  >
    <SwitchPrimitives.Thumb
      className={cn(
        "pointer-events-none block rounded-full bg-background ring-0 transition-transform data-[state=unchecked]:translate-x-0",
        switchThumbDensityClasses[density],
      )}
    />
  </SwitchPrimitives.Root>
))
Switch.displayName = SwitchPrimitives.Root.displayName

export { Switch }
