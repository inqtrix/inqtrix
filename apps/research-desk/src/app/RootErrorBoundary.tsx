import type { ReactNode } from 'react'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Button } from '@/components/ui/button'
import { useLocale } from '@/i18n/LocaleProvider'

/**
 * Top-level resilience boundary. Catches any synchronous render/reducer error from
 * <App/> (e.g. a content parse failure during a state transition) and shows a
 * localized, diagnosable fallback with a reload affordance instead of a blank white
 * page. Rendered inside AppProviders so it has locale/theme context; provider-level
 * failures are intentionally out of its scope.
 */
export function RootErrorBoundary({ children }: { children: ReactNode }) {
  const { t } = useLocale()
  const copy = t.appError
  return (
    <ErrorBoundary
      fallback={(error) => (
        <div className="flex min-h-svh w-full items-center justify-center bg-background p-6">
          <div className="w-full max-w-md rounded-lg border border-border bg-card p-6 text-center shadow-sm">
            <p className="t-section text-foreground">{copy.title}</p>
            <p className="t-meta mt-2 text-muted-foreground">{copy.description}</p>
            <p className="t-meta-sm mt-3 break-words text-muted-foreground">
              {error.message || error.name}
            </p>
            <Button className="mt-5" onClick={() => globalThis.location.reload()}>
              {copy.reload}
            </Button>
          </div>
        </div>
      )}
      retryLabel={copy.reload}
      title={copy.title}
    >
      {children}
    </ErrorBoundary>
  )
}
