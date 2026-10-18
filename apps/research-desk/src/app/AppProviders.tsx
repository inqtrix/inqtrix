import type { ReactNode } from 'react'
import { TOOLTIP_WARMUP_SKIP_MS, TooltipProvider } from '@/components/ui/tooltip'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import type { Locale } from '@/i18n/translations'
import { ThemeProvider } from '@/theme/ThemeProvider'

type AppProvidersProps = {
  children: ReactNode
  /** The first-render locale; the account default when omitted. */
  initialLocale?: Locale
}

export function AppProviders({ children, initialLocale }: AppProvidersProps) {
  return (
    <ThemeProvider>
      <LocaleProvider initialLocale={initialLocale}>
        {/* delayDuration 0: the shared Tooltip wrapper owns the entire
            dwell (TOOLTIP_APPEAR_DELAY_MS) so it applies to EVERY icon —
            Radix's provider-level warm state resets asynchronously and
            would let fast crossings between neighbors reopen instantly.
            disableHoverableContent: every tooltip here is a plain label,
            never an interactive surface; without the flag Radix spans a
            grace polygon to the tooltip box, adjacent icons sit inside
            that hull, and the previous label sticks across the row. */}
        <TooltipProvider
          delayDuration={0}
          disableHoverableContent
          skipDelayDuration={TOOLTIP_WARMUP_SKIP_MS}
        >
          {children}
        </TooltipProvider>
      </LocaleProvider>
    </ThemeProvider>
  )
}
