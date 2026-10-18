import { useState, type DragEvent } from 'react'
import {
  Database,
  Folder,
  HardDrive,
  Inbox,
  Info,
  Layers,
  Plus,
  Search,
  type LucideIcon,
} from '@/components/icons'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import { QuotaMeterSection } from '@/features/quota/QuotaMeterSection'
import type { EmbeddingQuota } from '@/features/quota/useEmbeddingQuota'
import type { VectorIndexStatus } from '@/features/project/types'
import type { ResearchRunAccess } from '@/features/researchRuns/types'
import { formatBytes } from './helpers'
import { FILE_QUOTA_BYTES, isInternalFileDrag, type ActiveTarget } from './constants'

export type RailCollection = { count: number; id: string; title: string }
export type RailIndex = { count: number; id: string; status: VectorIndexStatus; title: string }
export type RailServerCollection = { access: ResearchRunAccess; count: number; id: string; title: string }

const INDEX_DOT: Record<VectorIndexStatus, { className: string; pulse: boolean }> = {
  ready: { className: 'bg-success', pulse: false },
  indexing: { className: 'bg-brand', pulse: true },
  stale: { className: 'bg-warning', pulse: false },
  error: { className: 'bg-destructive', pulse: false },
  deleting: { className: 'bg-warning', pulse: true },
  delete_failed: { className: 'bg-destructive', pulse: false },
}

type DropProps = {
  onDragLeave: () => void
  onDragOver: (event: DragEvent) => void
  onDrop: (event: DragEvent) => void
  over: boolean
}

function NavItem({
  active,
  count,
  dot,
  icon: Icon,
  label,
  onClick,
  onPrefetch,
  drop,
}: {
  active: boolean
  count: number
  dot?: { className: string; pulse: boolean }
  icon: LucideIcon
  label: string
  onClick: () => void
  onPrefetch?: () => void
  drop?: DropProps
}) {
  return (
    <button
      className={cn(
        'flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left t-list transition-colors',
        active ? 'border-transparent bg-brand-subtle text-brand' : 'border-transparent text-foreground/90 hover:bg-accent',
        drop?.over && 'border-brand/60 bg-brand-subtle/60 ring-1 ring-brand/30',
      )}
      onClick={onClick}
      onDragLeave={drop?.onDragLeave}
      onDragOver={drop?.onDragOver}
      onDrop={drop?.onDrop}
      onFocus={onPrefetch}
      onPointerEnter={onPrefetch}
      type="button"
    >
      <span className="relative shrink-0">
        <Icon className={cn('size-4', active ? 'text-brand' : 'text-muted-foreground')} />
        {dot ? (
          <span className={cn('absolute -right-0.5 -top-0.5 size-1.5 rounded-full ring-2 ring-surface', dot.className, dot.pulse && 'inqtrix-running-dot')} />
        ) : null}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className={cn('shrink-0 t-meta-sm tabular-nums', active ? 'text-brand' : 'text-muted-foreground')}>{count}</span>
    </button>
  )
}

function AddButton({
  blockedReason = null,
  label,
  onClick,
}: {
  /** Disables the action and names why, visibly under the label. */
  blockedReason?: string | null
  label: string
  onClick: () => void
}) {
  return (
    <button
      className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left t-list font-normal text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent disabled:hover:text-muted-foreground"
      disabled={blockedReason !== null}
      onClick={onClick}
      type="button"
    >
      <Plus className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="block truncate">{label}</span>
        {blockedReason !== null ? <span className="block truncate t-meta-sm">{blockedReason}</span> : null}
      </span>
    </button>
  )
}

function UsageMeters({
  collectionCount,
  docCount,
  embeddingQuota,
  indexCount,
  usedBytes,
}: {
  collectionCount: number
  docCount: number
  embeddingQuota: EmbeddingQuota | null
  indexCount: number
  usedBytes: number
}) {
  const { locale, t } = useLocale()
  const pct = Math.min(100, (usedBytes / FILE_QUOTA_BYTES) * 100)
  const usage = t.fileLibrary.storageUsage
    .replace('{used}', formatBytes(usedBytes, locale))
    .replace('{total}', formatBytes(FILE_QUOTA_BYTES, locale))
  const quotaMonth =
    embeddingQuota && embeddingQuota.periodStart > 0
      ? new Date(embeddingQuota.periodStart * 1000).toLocaleDateString(locale, {
          month: 'long',
          timeZone: 'UTC',
        })
      : ''

  return (
    <div className="space-y-3 px-1 py-0.5">
      <section>
        <div className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-1.5 t-label text-foreground">
            <HardDrive className="size-3.5 text-muted-foreground" />
            {t.fileLibrary.storageTitle}
          </span>
          <span className="t-meta-sm tabular-nums text-muted-foreground">{usage}</span>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-brand" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 t-meta-sm text-muted-foreground">
          <span>{countLabel(docCount, t.fileLibrary.countDocumentsOne, t.fileLibrary.countDocuments)}</span>
          <span>{countLabel(collectionCount, t.fileLibrary.countCollectionsOne, t.fileLibrary.countCollections)}</span>
          <span>{countLabel(indexCount, t.fileLibrary.countIndexesOne, t.fileLibrary.countIndexes)}</span>
        </div>
      </section>
      {embeddingQuota ? (
        <QuotaMeterSection
          className="border-t border-border/70 pt-3"
          dimension="embedding_tokens"
          icon={Layers}
          label={t.vectorIndex.embeddingQuota}
          limit={embeddingQuota.limit}
          periodLabel={quotaMonth || undefined}
          unitLabel={t.vectorIndex.tokensUnit}
          unlimitedLabel={t.quota.unlimited}
          used={embeddingQuota.used}
        />
      ) : null}
    </div>
  )
}

export function Rail({
  active,
  className,
  collections,
  indexes,
  newIndexBlockedReason = null,
  onDropToCollection,
  onNewCollection,
  onNewIndex,
  onQueryChange,
  onSelectAll,
  onSelectCollection,
  onSelectIndex,
  onSelectServerCollection,
  onPrefetchServerCollection,
  query,
  serverCollections,
  storage,
  embeddingQuota,
  totalDocCount,
}: {
  active: ActiveTarget
  className?: string
  collections: RailCollection[]
  embeddingQuota: EmbeddingQuota | null
  indexes: RailIndex[]
  /** Why no index can be created here (shown under the disabled action);
   * `null` keeps "new index" available. */
  newIndexBlockedReason?: string | null
  onDropToCollection: (sectionId: string, fileId: string) => void
  onNewCollection: () => void
  onNewIndex: () => void
  onQueryChange: (value: string) => void
  onSelectAll: () => void
  onSelectCollection: (sectionId: string) => void
  onSelectIndex: (indexId: string) => void
  onSelectServerCollection: (collectionId: string) => void
  onPrefetchServerCollection?: (collectionId: string) => void
  query: string
  serverCollections: RailServerCollection[]
  storage: { collectionCount: number; docCount: number; indexCount: number; usedBytes: number }
  totalDocCount: number
}) {
  const { t } = useLocale()
  const [dropTargetId, setDropTargetId] = useState<string | null>(null)

  const collectionDrop = (sectionId: string): DropProps => ({
    over: dropTargetId === sectionId,
    onDragLeave: () => setDropTargetId((current) => (current === sectionId ? null : current)),
    onDragOver: (event) => {
      if (!isInternalFileDrag(event)) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      setDropTargetId(sectionId)
    },
    onDrop: (event) => {
      if (!isInternalFileDrag(event)) return
      event.preventDefault()
      const fileId = event.dataTransfer.getData('application/x-inqtrix-file-id')
      if (fileId) onDropToCollection(sectionId, fileId)
      setDropTargetId(null)
    },
  })

  return (
    <aside className={cn('min-h-0 min-w-0 flex-col border-r border-border bg-surface/50', className)}>
      <div className="flex items-center gap-2.5 px-4 pb-3 pt-4">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-file/25 bg-file-subtle text-file">
          <Database className="size-4" />
        </span>
        <div className="min-w-0">
          <h1 className="truncate t-title text-foreground">{t.fileLibrary.title}</h1>
          <p className="truncate t-meta text-muted-foreground">{t.fileLibrary.headerSubtitle}</p>
        </div>
      </div>

      <div className="px-3 pb-2">
        <label className="flex items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:ring-2 focus-within:ring-ring">
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            className="min-w-0 flex-1 border-0 bg-transparent py-1.5 text-sm text-foreground outline-none placeholder:text-muted-foreground"
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder={active.kind === 'index' ? t.fileLibrary.searchPlaceholderIndex : t.fileLibrary.searchPlaceholderDocs}
            value={query}
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        <p className="px-1.5 pb-1 pt-2 t-caption text-muted-foreground">{t.fileLibrary.sectionCollections}</p>
        <div className="flex flex-col gap-0.5">
          <NavItem
            active={active.kind === 'all'}
            count={totalDocCount}
            icon={Inbox}
            label={t.fileLibrary.allCollections}
            onClick={onSelectAll}
          />
          {collections.map((collection) => (
            <NavItem
              active={active.kind === 'collection' && active.sectionId === collection.id}
              count={collection.count}
              drop={collectionDrop(collection.id)}
              icon={Folder}
              key={collection.id}
              label={collection.title}
              onClick={() => onSelectCollection(collection.id)}
            />
          ))}
          <AddButton label={t.fileLibrary.newCollection} onClick={onNewCollection} />
        </div>

        <div className="mt-3 flex items-center gap-1.5 px-1.5 pb-1 pt-1">
          <p className="t-caption text-muted-foreground">{t.fileLibrary.sectionServerCollections}</p>
        </div>
        <div className="flex flex-col gap-0.5">
          {serverCollections.map((collection) => (
            <NavItem
              active={active.kind === 'server-collection' && active.collectionId === collection.id}
              count={collection.count}
              icon={Database}
              key={collection.id}
              label={collection.title}
              onClick={() => onSelectServerCollection(collection.id)}
              onPrefetch={() => onPrefetchServerCollection?.(collection.id)}
            />
          ))}
        </div>

        <div className="mt-3 flex items-center gap-1.5 px-1.5 pb-1 pt-1">
          <p className="t-caption text-muted-foreground">{t.fileLibrary.sectionIndexes}</p>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex cursor-help items-center text-muted-foreground/60">
                <Info className="size-3" />
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-[240px]" side="top">{t.vectorIndex.indexHandleTooltip}</TooltipContent>
          </Tooltip>
        </div>
        <div className="flex flex-col gap-0.5">
          {indexes.map((index) => (
            <NavItem
              active={active.kind === 'index' && active.indexId === index.id}
              count={index.count}
              dot={INDEX_DOT[index.status]}
              icon={Layers}
              key={index.id}
              label={index.title}
              onClick={() => onSelectIndex(index.id)}
            />
          ))}
          <AddButton blockedReason={newIndexBlockedReason} label={t.vectorIndex.newIndex} onClick={onNewIndex} />
        </div>
      </div>

      <div className="border-t border-border p-3">
        <UsageMeters
          collectionCount={storage.collectionCount}
          docCount={storage.docCount}
          embeddingQuota={embeddingQuota}
          indexCount={storage.indexCount}
          usedBytes={storage.usedBytes}
        />
      </div>
    </aside>
  )
}
