import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * Single-line text input. Control primitive: `h-9` control height,
 * `text-sm` (a utility, not a `.t-*` role, which a utility would
 * override), `rounded-md` control
 * tier. Pass `className="h-8"` to drop into a dense toolbar row. Unifies
 * the raw `<input>` markup that was duplicated across feature screens.
 */
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<'input'>>(
  ({ className, type, ...props }, ref) => (
    <input
      className={cn(
        'flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-destructive/50 aria-[invalid=true]:focus-visible:ring-destructive/35',
        className,
      )}
      ref={ref}
      type={type}
      {...props}
    />
  ),
)
Input.displayName = 'Input'

export { Input }
