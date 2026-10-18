import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/**
 * Placeholder for a genuinely cold conversation whose messages/items remain
 * unavailable after the shared structural-loading delay. Warm, prefetched and
 * background-refresh paths keep real content and never mount this component.
 * Shared by Chat, Agent and Knowledge Desk.
 *
 * A skeleton only reads as "the content, still loading" when it occupies the
 * silhouette the content will occupy. In a bounded conversation viewport,
 * `fill` distributes a small number of clusters over the complete height;
 * it must not leave the upper half blank on tall screens. `anchor` records
 * the surface's scroll orientation and controls compact, non-filled use.
 */
export function ConversationSkeleton({ anchor = 'top', fill = false }: {
  /** Scroll orientation of the owning transcript. */
  anchor?: 'bottom' | 'top'
  /** Stretch to the available height so the silhouette covers the region
   * instead of a strip of it. Pair with a parent that gives the skeleton
   * its height (the structural boundary or a `min-h-full` column). */
  fill?: boolean
}) {
  return (
    <div
      aria-hidden
      data-conversation-skeleton-anchor={anchor}
      className={cn(
        'flex flex-col gap-5 overflow-hidden',
        fill && 'h-full min-h-0 flex-1 justify-between',
        !fill && anchor === 'bottom' && 'justify-end',
      )}
    >
      {fill && (
        <>
          <div className="flex flex-col gap-2">
            <Skeleton className="h-3.5 w-24" />
            <Skeleton className="h-4 w-[78%]" />
            <Skeleton className="h-4 w-[84%]" />
            <Skeleton className="h-4 w-[52%]" />
          </div>
          <div className="flex justify-end">
            <Skeleton className="h-10 w-[38%] rounded-lg" />
          </div>
        </>
      )}
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-4 w-[86%]" />
        <Skeleton className="h-4 w-[72%]" />
        <Skeleton className="h-4 w-[64%]" />
      </div>
      <div className="flex justify-end">
        <Skeleton className="h-14 w-[46%] rounded-lg" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-4 w-[80%]" />
        <Skeleton className="h-4 w-[91%]" />
        <Skeleton className="h-4 w-[58%]" />
      </div>
    </div>
  )
}
