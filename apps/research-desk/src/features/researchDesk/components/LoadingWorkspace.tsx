import { Skeleton } from '@/components/ui/skeleton'

export function LoadingWorkspace() {
  return (
    <div className="w-full px-4 py-4 md:px-5 lg:h-full lg:min-h-0 xl:px-8">
      <div className="mx-auto flex min-h-[calc(100svh-var(--header-h)-2rem)] w-full max-w-4xl flex-col rounded-lg border border-border bg-card p-4 shadow-[0_1px_2px_var(--shadow-hairline)] lg:min-h-0">
        <Skeleton className="h-5 w-36" />
        <div className="mt-6 grid gap-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-3/4" />
        </div>
      </div>
    </div>
  )
}
