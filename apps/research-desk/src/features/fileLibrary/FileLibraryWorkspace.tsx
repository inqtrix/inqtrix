import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type Dispatch } from 'react'
import { Check, ChevronLeft, ChevronRight, Folder, FolderOpen, Inbox, ListChecks, LoaderCircle, Minus, Plus, RotateCcw, Upload, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import {
  fileAssetReferenceCounts,
  projectFileAssets,
  projectFileGroups,
  projectFileLibrarySections,
  projectStorageTotalBytes,
  projectVectorIndexes,
  vectorIndexById,
  vectorIndexMembersResolved,
} from '@/features/project/selectors'
import type { EmbedModelDescriptor, EmbedModelId, FileAssetRecord, IndexingJobLive, ProjectState } from '@/features/project/types'
import type { KnowledgeCollectionInfo } from '@/features/researchRuns/types'
import { createDefaultFileParser } from '@/features/files/parsing'
import {
  createFileAssetPlaceholders,
  createFileUploadRegistry,
  runFileIngestPipeline,
  serverUploadFailureMessage,
  type FileUploadRegistry,
  type ServerFileUpload,
  type UploadBinding,
  uploadBindingForRecord,
} from '@/features/files/ingest'
import { Dropzone } from '@/features/files/Dropzone'
import { temporaryFileSectionId } from '@/features/files/sections'
import type { ResearchDeskAction } from '../researchDesk/state'
import {
  ingestNewVectorIndexMembers,
  createVectorIndexCollectionOnServer,
  isAbortError,
  KnowledgeReindexPartialError,
  type IngestProgress,
  type KnowledgeReindexResult,
  type KnowledgeSyncOptions,
  type MemberFailed,
  type MemberJobProgress,
  type MemberProgress,
  type MemberStart,
  type PausedMemberJob,
} from './knowledgeSync'
import { useIndexingJobApi } from './useIndexingJobApi'
import { useAssetDeletionApi } from './useAssetDeletionApi'
import { useCollectionContextOmissions } from './useCollectionContextOmissions'
import { resolveVectorIndexDeletionRoute } from './vectorIndexDeletion'
import { Rail } from './Rail'
import {
  prefetchServerCollectionDocuments,
  ServerCollectionPanel,
  type ServerCollectionJobState,
} from './ServerCollectionPanel'
import { useEmbeddingQuota } from '@/features/quota/useEmbeddingQuota'
import { IndexBar, knowledgeUnavailableLabel } from './IndexBar'
import { AddDocsPanel } from './AddDocsPanel'
import { FileCard, FileRow } from './FileItem'
import { FilePreviewPanel } from './FilePreviewPanel'
import { indexingJobDisposition } from './indexingTypes'
import {
  cancelIndexingJob,
  hasHttpStatus,
  resolveKnowledgeDocumentBySource,
  type ClientOptions,
} from '@/api/inqtrixClient'
import { BulkMoveMenu, ConfirmDelete, InlineText, SortSelect, ViewToggle, type MoveTarget } from './controls'
import {
  groupSlug,
  indexBackedCollectionIds,
  isMemberInRun,
  railVisibleServerCollections,
  rangeBetween,
} from './helpers'
import { isInternalFileDrag, type ActiveTarget, type SortMode, type ViewMode } from './constants'
import { carriedErrorText, readerError, readerTexts } from '@/api/requestErrorText'

const parser = createDefaultFileParser()

/** Abort handles for client-driven index runs, keyed by index id. Module
 * scope, NOT component state: a run deliberately outlives this view (its
 * dispatches land in the project state), so a view switch must not lose the
 * only handle that can stop it — otherwise "Abbrechen" would silently do
 * nothing while documents keep embedding. */
const indexRunControllers = new Map<string, AbortController>()

type UploadTarget = { groupId: string | null; indexId?: string; sectionId: string }

type IndexMemberRemovalFeedback = {
  error?: string
  status: 'blocked' | 'reconciling' | 'delete_failed'
}

function indexMemberRemovalKey(indexId: string, fileId: string): string {
  return `${indexId}\u0000${fileId}`
}

function assetSourceId(fileId: string): string {
  return fileId.startsWith('asset:') ? fileId : `asset:${fileId}`
}

type Band = {
  count: number
  deletionError?: string | null
  deletionOperationId?: string | null
  groupId: string | null
  isGroup: boolean
  lifecycleStatus?: 'active' | 'deleting' | 'delete_failed'
  sectionId: string
  title: string
}

type Block = {
  band: Band | null
  breadcrumb: boolean
  items: FileAssetRecord[]
  key: string
}

function BandHeader({
  band,
  dropOver,
  onDeleteGroup,
  onDrop,
  onDragLeave,
  onDragOver,
  onNavigate,
  onRenameGroup,
  onRetryGroup,
  onToggleSelectAll,
  onUpload,
  selectionState,
}: {
  band: Band
  dropOver: boolean
  onDeleteGroup: (groupId: string) => void
  onDrop: (event: DragEvent) => void
  onDragLeave: () => void
  onDragOver: (event: DragEvent) => void
  onNavigate?: () => void
  onRenameGroup: (groupId: string, title: string) => void
  onRetryGroup: (operationId: string) => void
  /** Selection mode: select/deselect every file in this band (indeterminate
   * semantics — 'some' selects the rest, 'all' clears). Absent = mode off,
   * the slot shows the folder glyph. */
  onToggleSelectAll?: () => void
  onUpload: () => void
  selectionState?: 'all' | 'some' | 'none'
}) {
  const { t } = useLocale()
  const groupDeleting = band.isGroup && band.lifecycleStatus === 'deleting'
  const groupDeleteFailed = band.isGroup && band.lifecycleStatus === 'delete_failed'
  return (
    <div
      aria-busy={groupDeleting || undefined}
      className={cn(
        'group/band mt-3 flex min-h-8 items-center gap-2 rounded-md border-b border-border/60 px-2 py-1 transition-colors',
        dropOver && 'bg-brand-subtle/60 ring-1 ring-brand/30',
      )}
      onDragLeave={groupDeleting || groupDeleteFailed ? undefined : onDragLeave}
      onDragOver={groupDeleting || groupDeleteFailed ? undefined : onDragOver}
      onDrop={groupDeleting || groupDeleteFailed ? undefined : onDrop}
    >
      {/* 28px (w-7) slot so the folder glyph centers exactly over the rows' type tiles. */}
      <span className="grid w-7 shrink-0 place-items-center">
        {onToggleSelectAll ? (
          <button
            aria-label={t.fileLibrary.selectBand.replace('{title}', band.title)}
            aria-pressed={selectionState === 'all' ? true : selectionState === 'some' ? 'mixed' : false}
            className={cn(
              'grid size-5 place-items-center rounded-full border transition-all',
              selectionState === 'none'
                ? 'border-border bg-surface text-transparent hover:border-brand/60'
                : 'border-brand bg-brand text-brand-foreground',
            )}
            onClick={onToggleSelectAll}
            type="button"
          >
            {selectionState === 'some' ? <Minus className="size-3" /> : <Check className="size-3" />}
          </button>
        ) : band.isGroup ? (
          <FolderOpen className="size-3.5 text-file" />
        ) : (
          <Folder className="size-3.5 text-muted-foreground" />
        )}
      </span>
      {band.isGroup && !groupDeleting && !groupDeleteFailed ? (
        <InlineText
          ariaLabel={t.fileLibrary.renameGroup}
          className="t-list text-foreground"
          onCommit={(title) => onRenameGroup(band.groupId as string, title)}
          value={band.title}
        />
      ) : band.isGroup ? (
        <span className="truncate px-1 t-list text-foreground">{band.title}</span>
      ) : onNavigate ? (
        <button className="inline-flex items-center gap-1 rounded-sm px-1 t-list text-foreground hover:bg-accent/60" onClick={onNavigate} type="button">
          {band.title}
          <ChevronRight className="size-3 text-muted-foreground" />
        </button>
      ) : (
        <span className="t-list text-foreground">{band.title}</span>
      )}
      <span className="shrink-0 rounded-full border border-border bg-surface/45 px-1.5 t-hint font-semibold leading-4 tabular-nums text-muted-foreground">{band.count}</span>
      {band.isGroup ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="hidden shrink-0 cursor-help items-center gap-1 rounded border border-border bg-surface px-1.5 py-0.5 font-mono t-hint text-muted-foreground sm:inline-flex">
              @filegroups:{groupSlug(band.title)}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-[240px]" side="top">{t.fileLibrary.groupBundleTooltip}</TooltipContent>
        </Tooltip>
      ) : null}
      {groupDeleting ? (
        <span className="ml-auto inline-flex items-center gap-1.5 t-meta-sm font-medium text-muted-foreground">
          <LoaderCircle className="size-3.5 animate-spin motion-reduce:animate-none" />
          {t.fileLibrary.groupDeletionRunning}
        </span>
      ) : groupDeleteFailed ? (
        <span className="ml-auto inline-flex min-w-0 items-center gap-1.5 t-meta-sm font-medium text-destructive">
          <span className="max-w-52 truncate">{band.deletionError ?? t.fileLibrary.groupDeletionFailed}</span>
          {band.deletionOperationId ? (
            <Button
              aria-label={t.fileLibrary.groupDeletionRetry}
              className="size-7"
              onClick={() => onRetryGroup(band.deletionOperationId as string)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <RotateCcw className="size-3.5" />
            </Button>
          ) : null}
        </span>
      ) : (
      <div className="ml-auto flex items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover/band:opacity-100">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button aria-label={t.fileLibrary.upload} className="size-7 text-muted-foreground hover:text-foreground" onClick={onUpload} size="icon" type="button" variant="ghost">
              <Upload className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">{t.fileLibrary.upload}</TooltipContent>
        </Tooltip>
        {band.isGroup ? (
          <ConfirmDelete ariaLabel={t.fileLibrary.removeGroup} hint={t.fileLibrary.removeGroupHint} onConfirm={() => onDeleteGroup(band.groupId as string)} />
        ) : null}
      </div>
      )}
    </div>
  )
}

function EmptyState({ onUpload, searching }: { onUpload: () => void; searching: boolean }) {
  const { t } = useLocale()
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border px-8 py-16 text-center">
      <span className="grid size-14 place-items-center rounded-2xl border border-border bg-surface text-muted-foreground">
        <Inbox className="size-6" />
      </span>
      <h3 className="mt-4 text-sm font-semibold text-foreground">{searching ? t.fileLibrary.emptySearchTitle : t.fileLibrary.emptyLibraryTitle}</h3>
      <p className="mt-1 max-w-xs text-xs leading-5 text-muted-foreground">{searching ? t.fileLibrary.emptySearchHint : t.fileLibrary.emptyLibraryHint}</p>
      {searching ? null : (
        <Button className="mt-4 gap-1.5" onClick={onUpload} size="sm" type="button" variant="outline">
          <Upload className="size-4" />
          {t.fileLibrary.uploadDocuments}
        </Button>
      )}
    </div>
  )
}

function IndexEmpty({ disabled, onAdd }: { disabled: boolean; onAdd: () => void }) {
  const { t } = useLocale()
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border px-8 py-14 text-center">
      <span className="grid size-14 place-items-center rounded-2xl border border-file/25 bg-file-subtle text-file">
        <Inbox className="size-6" />
      </span>
      <h3 className="mt-4 text-sm font-semibold text-foreground">{t.vectorIndex.indexEmptyTitle}</h3>
      <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{t.vectorIndex.indexEmptyHint}</p>
      <Button className="mt-4 gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90" disabled={disabled} onClick={onAdd} size="sm" type="button">
        <Plus className="size-4" />
        {t.vectorIndex.addDocuments}
      </Button>
    </div>
  )
}

export function FileLibraryWorkspace({
  dispatch,
  assetDeletionApiOptions,
  deletionRefreshToken = 0,
  deletionScopeKey,
  embedModels,
  ensureAssetBodiesLoaded,
  ensureUploadTarget,
  fileApiOptions,
  knowledgeSync,
  knowledgeUnavailableReason = null,
  onAssetsIngested,
  onRefreshServerCollections,
  onShareServerCollection,
  onVectorIndexServerDeleted,
  serverCollections = [],
  serverCollectionsLoaded = false,
  serverCollectionsRefreshToken = 0,
  contextualRetrievalEnabled = null,
  serverFeatureLabels = null,
  serverFileUpload,
  serverParserAvailable = false,
  retryServerUpload,
  uploadRegistry,
  state,
}: {
  dispatch: Dispatch<ResearchDeskAction>
  /** Server transport for persisted assets, groups and sections. Local-only
   * rows continue to use reducer-owned deletion when this is absent. */
  assetDeletionApiOptions: ClientOptions | null
  /** Stable project/principal/backend identity used to fence late deletion
   * responses after a workspace or authenticated-user switch. */
  deletionScopeKey: string
  /** Invalidations from another tab/user refresh the retained operation feed. */
  deletionRefreshToken?: number
  /** Active embedding catalog: server-provided when the knowledge engine
   * is enabled, the EMBED_MODELS fallback in demo/offline modes. */
  embedModels: readonly EmbedModelDescriptor[]
  /** Loads asset bodies on demand before a first-build reindex reads them
   * (load-on-use). Absent in demo/offline — bodies are always local then. */
  ensureAssetBodiesLoaded?: (assetIds: readonly string[]) => Promise<Map<string, string>>
  /** Persists the upload target (section + group) to the server BEFORE the
   * first byte moves, so each file's upload can carry its section binding
   * (the server rejects bindings into unknown sections). Resolves false when
   * the target could not be persisted; the upload remains visibly failed and
   * retryable instead of being stored under a different target. Absent =
   * persistence off (demo/offline or sync disabled). */
  ensureUploadTarget?: (sectionId: string, groupId: string | null) => Promise<boolean>
  /** Server options for the file preview (asset body + original download),
   * gated on the FILES/persistence tier (not knowledge); `null` in demo/offline.
   * The viewer additionally probes the current principal's live file access
   * before exposing original-binary controls. */
  fileApiOptions: ClientOptions | null
  /** Connection facts for real server-side embedding runs; `null` keeps
   * the client-side simulation (demo) unless `knowledgeUnavailableReason`
   * names why this deployment cannot build. */
  knowledgeSync: KnowledgeSyncOptions | null
  /** The server's reason when this deployment has no knowledge base
   * (`operator_disabled`, `store_unreachable`): indexes are read-only and
   * nothing is simulated. `null` in demo mode and whenever knowledge works. */
  knowledgeUnavailableReason?: string | null
  /** Kicks off the non-blocking background server (MarkItDown) parse for
   * just-ingested assets, upgrading the instant client parse. No-op without
   * a server parser. */
  onAssetsIngested?: (assets: FileAssetRecord[]) => void
  /** Refreshes the authoritative collection list after a document, job, or
   * lifecycle mutation. */
  onRefreshServerCollections?: () => Promise<void>
  onShareServerCollection?: (collection: KnowledgeCollectionInfo) => void
  /** Confirms that the durable aggregate already removed this server record,
   * so project autosave does not send a duplicate DELETE after local cleanup. */
  onVectorIndexServerDeleted?: (indexId: string) => void
  serverCollections?: KnowledgeCollectionInfo[]
  serverCollectionsLoaded?: boolean
  serverCollectionsRefreshToken?: number
  /** Whether this server actually enriches chunks with retrieval context. */
  contextualRetrievalEnabled?: boolean | null
  /** Labels of active server features for the visible mode indicator;
   * `null` hides the line (demo or no server connected). */
  serverFeatureLabels?: string[] | null
  /** Uploads the ORIGINAL file to the server file store when the
   * backend advertises `features.files`; absent = local-only mode. */
  serverFileUpload?: ServerFileUpload
  /** Whether a background server (MarkItDown) parse will deliver text for
   * uploaded files — decides who clears the per-row "Parsing…" badge (the
   * server-parse lifecycle, or the client parse itself). */
  serverParserAvailable?: boolean
  /** Resume a durable server operation that already owns its bytes. */
  retryServerUpload?: (assetId: string) => Promise<void>
  /** Shared browser File handles. The durable lifecycle stays server-owned;
   * this only preserves explicit same-session retry across desk navigation. */
  uploadRegistry?: FileUploadRegistry
  state: ProjectState
}) {
  const { locale, t } = useLocale()
  const embeddingQuota = useEmbeddingQuota()
  // A real deployment without a knowledge base: indexes stay readable, while
  // every action that creates, fills or builds one is blocked with the reason
  // in view, so the demo simulator never runs there.
  const indexesReadOnly = knowledgeUnavailableReason !== null
  const [active, setActive] = useState<ActiveTarget>({ kind: 'all' })
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<SortMode>('recent')
  const [view, setView] = useState<ViewMode>('list')
  const [dropKey, setDropKey] = useState<string | null>(null)
  const [pickerIndexId, setPickerIndexId] = useState<string | null>(null)
  const [previewAssetId, setPreviewAssetId] = useState<string | null>(null)
  const [isMobileDetailOpen, setIsMobileDetailOpen] = useState(false)
  const [serverJobs, setServerJobs] = useState<Record<string, ServerCollectionJobState>>({})
  const [reindexRecoveryPending, setReindexRecoveryPending] = useState<
    Record<string, 'raw' | 'resume'>
  >({})
  const reindexRecoveryInFlightRef = useRef(new Set<string>())
  // Why an index action (delete) could not be carried out — shown on the index
  // panel so a server refusal is never silent.
  const [indexActionError, setIndexActionError] = useState<{
    indexId: string
    message: string
    operationId?: string
  } | null>(null)
  const [indexMemberRemovalFeedback, setIndexMemberRemovalFeedback] = useState<
    Readonly<Record<string, IndexMemberRemovalFeedback>>
  >({})
  const indexMemberRemovalInFlightRef = useRef(new Set<string>())
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const targetRef = useRef<UploadTarget | null>(null)
  const resumeUploadAssetIdRef = useRef<string | null>(null)
  const uploadNeedsBytesRef = useRef(new Set<string>())
  const reindexTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const selectNewestIndex = useRef(false)
  // Live state for the async ingest pipeline (its closures outlive renders).
  const stateRef = useRef(state)
  stateRef.current = state
  const deletionScopeRef = useRef(deletionScopeKey)
  deletionScopeRef.current = deletionScopeKey
  const deletionApi = useAssetDeletionApi({
    assets: state.fileAssets,
    dispatch,
    groups: state.fileGroups,
    knowledgeOptions: knowledgeSync,
    options: assetDeletionApiOptions,
    refreshToken: deletionRefreshToken,
    scopeKey: deletionScopeKey,
    sections: state.fileLibrarySections,
  })
  // Retry source for failed uploads: the File object cannot live in
  // ProjectState (not serializable), so the root shares it across desks for
  // this browser session. Reload recovery asks the user to reselect the file.
  const localUploadRegistryRef = useRef<FileUploadRegistry | null>(null)
  if (localUploadRegistryRef.current === null) {
    localUploadRegistryRef.current = createFileUploadRegistry()
  }
  const activeUploadRegistry = uploadRegistry ?? localUploadRegistryRef.current
  // Multi-select (library views): "mode = at least one selected", plus an
  // explicit arm via the "Auswählen" button (touch/discoverability entry).
  // The anchor drives shift-click ranges over the visual order.
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(() => new Set())
  const [selectionArmed, setSelectionArmed] = useState(false)
  const [deletingIndexIds, setDeletingIndexIds] = useState<ReadonlySet<string>>(() => new Set())
  const completedIndexDeletionIdsRef = useRef(new Set<string>())
  const completedKnowledgeDeletionIdsRef = useRef(new Set<string>())
  const selectionAnchorRef = useRef<string | null>(null)

  useEffect(() => {
    setDeletingIndexIds(new Set())
    setIndexMemberRemovalFeedback({})
    indexMemberRemovalInFlightRef.current.clear()
    completedIndexDeletionIdsRef.current.clear()
    completedKnowledgeDeletionIdsRef.current.clear()
  }, [deletionScopeKey])

  useEffect(() => {
    const allOperations = Object.values(deletionApi.operations)
    const operations = allOperations.filter(
      (operation) => operation.target_kind === 'vector_index',
    )
    const activeIds = new Set(
      operations
        .filter((operation) => operation.status === 'queued' || operation.status === 'running')
        .map((operation) => operation.target_id),
    )
    if (!assetDeletionApiOptions) {
      const activeCollectionIds = new Set(
        allOperations
          .filter((operation) => (
            operation.target_kind === 'knowledge_collection'
            && (operation.status === 'queued' || operation.status === 'running')
          ))
          .map((operation) => operation.target_id),
      )
      for (const index of Object.values(state.vectorIndexes)) {
        if (
          index.serverCollectionId
          && activeCollectionIds.has(index.serverCollectionId)
        ) {
          activeIds.add(index.id)
        }
      }
    }
    setDeletingIndexIds(activeIds)
    for (const operation of operations) {
      if (operation.status === 'deleted') {
        if (!completedIndexDeletionIdsRef.current.has(operation.operation_id)) {
          completedIndexDeletionIdsRef.current.add(operation.operation_id)
          onVectorIndexServerDeleted?.(operation.target_id)
          dispatch({ indexId: operation.target_id, type: 'deleteVectorIndex' })
          void onRefreshServerCollections?.()
        }
      } else if (operation.status === 'delete_failed') {
        setIndexActionError({
          indexId: operation.target_id,
          message: carriedErrorText(operation.error, t.vectorIndex.statusDeleteFailed)
            ?? t.vectorIndex.statusDeleteFailed,
          operationId: operation.operation_id,
        })
      }
    }
  }, [
    deletionApi.operations,
    assetDeletionApiOptions,
    dispatch,
    onRefreshServerCollections,
    onVectorIndexServerDeleted,
    state.vectorIndexes,
    t.vectorIndex.statusDeleteFailed,
  ])

  useEffect(() => {
    for (const operation of Object.values(deletionApi.operations)) {
      if (
        operation.status !== 'deleted'
        || completedKnowledgeDeletionIdsRef.current.has(operation.operation_id)
        || (
          operation.target_kind !== 'knowledge_collection'
          && operation.target_kind !== 'knowledge_document'
        )
      ) continue
      completedKnowledgeDeletionIdsRef.current.add(operation.operation_id)
      if (
        operation.target_kind === 'knowledge_collection'
        && !assetDeletionApiOptions
      ) {
        for (const index of Object.values(stateRef.current.vectorIndexes)) {
          if (index.serverCollectionId !== operation.target_id) continue
          onVectorIndexServerDeleted?.(index.id)
          dispatch({ indexId: index.id, type: 'deleteVectorIndex' })
        }
      } else if (operation.target_kind === 'knowledge_document') {
        for (const index of Object.values(stateRef.current.vectorIndexes)) {
          for (const member of index.members) {
            if (member.serverDocumentId === operation.target_id) {
              dispatch({
                fileId: member.fileId,
                indexId: index.id,
                type: 'removeDocFromVectorIndex',
              })
            }
          }
        }
      }
      void onRefreshServerCollections?.()
    }
    if (!assetDeletionApiOptions) {
      for (const operation of Object.values(deletionApi.operations)) {
        if (
          operation.target_kind !== 'knowledge_collection'
          || operation.status !== 'delete_failed'
        ) continue
        for (const index of Object.values(stateRef.current.vectorIndexes)) {
          if (index.serverCollectionId !== operation.target_id) continue
          setIndexActionError({
            indexId: index.id,
            message: carriedErrorText(operation.error, t.vectorIndex.statusDeleteFailed)
            ?? t.vectorIndex.statusDeleteFailed,
            operationId: operation.operation_id,
          })
        }
      }
    }
  }, [
    assetDeletionApiOptions,
    deletionApi.operations,
    dispatch,
    onRefreshServerCollections,
    onVectorIndexServerDeleted,
    t.vectorIndex.statusDeleteFailed,
  ])

  const knowledgeDocumentDeletionById = useMemo(() => {
    const operations = Object.values(deletionApi.operations)
      .filter((operation) => operation.target_kind === 'knowledge_document')
      .sort((left, right) => right.created_at - left.created_at)
    const byDocument = new Map<string, (typeof operations)[number]>()
    for (const operation of operations) {
      if (!byDocument.has(operation.target_id)) {
        byDocument.set(operation.target_id, operation)
      }
    }
    return byDocument
  }, [deletionApi.operations])

  const indexRemovalFor = (
    indexId: string,
    fileId: string,
    serverDocumentId?: string,
  ) => {
    const operation = serverDocumentId
      ? knowledgeDocumentDeletionById.get(serverDocumentId)
      : undefined
    if (operation && operation.status !== 'deleted') {
      return {
        error: carriedErrorText(operation.error, readerTexts().vectorIndex.statusDeleteFailed) ?? undefined,
        status: operation.status === 'delete_failed'
          ? 'delete_failed' as const
          : 'deleting' as const,
      }
    }
    return indexMemberRemovalFeedback[indexMemberRemovalKey(indexId, fileId)]
  }

  useEffect(() => {
    const timers = reindexTimers.current
    return () => {
      timers.forEach((timer) => clearInterval(timer))
      timers.clear()
    }
  }, [])

  // Base projections are memoised on the raw state slices they read (not on
  // `state`, whose identity changes on every reducer dispatch): the selectors
  // rebuild fresh arrays each call, so without this every unrelated tick (e.g.
  // the demo run simulator) would re-run the whole derivation chain below.
  const sections = useMemo(() => projectFileLibrarySections(state), [state.fileLibrarySections, state.fileLibrarySectionOrder])
  const groups = useMemo(() => projectFileGroups(state), [state.fileGroups, state.fileGroupOrder])
  const assets = useMemo(() => projectFileAssets(state), [state.fileAssets, state.fileAssetOrder])
  const temporarySectionId = useMemo(() => temporaryFileSectionId(sections), [sections])
  const indexes = useMemo(() => projectVectorIndexes(state), [state.vectorIndexes, state.vectorIndexOrder])
  const storageTotalBytes = useMemo(() => projectStorageTotalBytes(state), [state.fileAssets, state.fileAssetOrder])

  const assetsInSection = (sectionId: string) => assets.filter((asset) => asset.sectionId === sectionId)
  const customCollections = useMemo(() => sections.filter((section) => section.kind === 'custom'), [sections])
  const defaultUploadSectionId = customCollections[0]?.id ?? temporarySectionId
  const activeUploadTarget: UploadTarget = active.kind === 'collection'
    ? { groupId: null, sectionId: active.sectionId }
    : { groupId: null, sectionId: defaultUploadSectionId }
  const railCollections = useMemo(
    () => sections.filter((section) => section.kind === 'custom' || assets.some((asset) => asset.sectionId === section.id)),
    [sections, assets],
  )

  // Reset selection if the active collection/index was deleted.
  useEffect(() => {
    if (active.kind === 'collection' && !sections.some((section) => section.id === active.sectionId)) setActive({ kind: 'all' })
    if (active.kind === 'index' && !indexes.some((index) => index.id === active.indexId)) {
      setActive({ kind: 'all' })
      setPickerIndexId(null)
    }
    if (active.kind === 'server-collection') {
      // A collection that became an index's storage while it was open is no
      // longer a thing of its own — follow it to the index that now owns it
      // rather than leaving the user on a view the rail no longer offers.
      const owningIndex = active.fromIndexId
        ? null
        : indexes.find((index) => index.serverCollectionId === active.collectionId)
      if (owningIndex) {
        setActive({ indexId: owningIndex.id, kind: 'index' })
      } else if (
        serverCollectionsLoaded
        && !serverCollections.some((collection) => collection.id === active.collectionId)
      ) {
        setActive({ kind: 'all' })
      }
    }
  }, [active, indexes, sections, serverCollections, serverCollectionsLoaded])

  // After creating an index, select it and open its add-documents panel.
  useEffect(() => {
    if (selectNewestIndex.current && indexes.length > 0) {
      selectNewestIndex.current = false
      setActive({ indexId: indexes[0].id, kind: 'index' })
      setPickerIndexId(indexes[0].id)
    }
  }, [indexes.length])

  const sectionTitle = (sectionId: string) => sections.find((section) => section.id === sectionId)?.title ?? ''
  const groupTitle = (groupId: string | null) => (groupId ? groups.find((group) => group.id === groupId)?.title ?? null : null)

  const moveTargets: MoveTarget[] = useMemo(
    () => customCollections.flatMap((collection) => [
      { groupId: null, key: `${collection.id}:root`, label: `${collection.title} · ${t.fileLibrary.ungrouped}`, sectionId: collection.id },
      ...groups
        .filter((group) => group.sectionId === collection.id)
        .map((group) => ({ groupId: group.id, key: `${collection.id}:${group.id}`, label: `${collection.title} · ${group.title}`, sectionId: collection.id })),
    ]),
    [customCollections, groups, t.fileLibrary.ungrouped],
  )

  const sortAssets = (list: FileAssetRecord[]): FileAssetRecord[] => {
    if (sort === 'recent') return list
    const sorted = [...list]
    if (sort === 'name') sorted.sort((a, b) => a.label.localeCompare(b.label, locale))
    else if (sort === 'size') sorted.sort((a, b) => b.sizeBytes - a.sizeBytes)
    else if (sort === 'pages') sorted.sort((a, b) => (b.pageCount ?? 0) - (a.pageCount ?? 0))
    return sorted
  }

  const q = query.trim().toLowerCase()
  const matchesQuery = (asset: FileAssetRecord) =>
    !q || `${asset.label} ${asset.fileName} ${sectionTitle(asset.sectionId)} ${groupTitle(asset.groupId) ?? ''}`.toLowerCase().includes(q)
  const breadcrumbFor = (asset: FileAssetRecord) => [sectionTitle(asset.sectionId), groupTitle(asset.groupId)].filter(Boolean).join(' / ')

  const blocks: Block[] = useMemo(() => {
    if (active.kind === 'index' || active.kind === 'server-collection') return []
    const pool = assets.filter(matchesQuery)
    if (q) return [{ band: null, breadcrumb: true, items: sortAssets(pool), key: 'search' }]
    if (active.kind === 'all') {
      return railCollections
        .map((collection) => {
          const items = sortAssets(pool.filter((asset) => asset.sectionId === collection.id))
          return {
            band: { count: items.length, groupId: null, isGroup: false, sectionId: collection.id, title: collection.title },
            breadcrumb: false,
            items,
            key: collection.id,
          }
        })
        .filter((block) => block.items.length > 0)
    }
    const sectionId = active.sectionId
    const ungrouped = sortAssets(pool.filter((asset) => asset.sectionId === sectionId && asset.groupId === null))
    const out: Block[] = ungrouped.length > 0
      ? [{
          band: { count: ungrouped.length, groupId: null, isGroup: false, sectionId, title: t.fileLibrary.ungrouped },
          breadcrumb: false,
          items: ungrouped,
          key: `${sectionId}:ungrouped`,
        }]
      : []
    groups
      .filter((group) => group.sectionId === sectionId)
      .forEach((group) => {
        const items = sortAssets(pool.filter((asset) => asset.groupId === group.id))
        out.push({
          band: {
            count: items.length,
            deletionError: group.deletionError,
            deletionOperationId: group.deletionOperationId,
            groupId: group.id,
            isGroup: true,
            lifecycleStatus: group.lifecycleStatus,
            sectionId,
            title: group.title,
          },
          breadcrumb: false,
          items,
          key: group.id,
        })
      })
    return out
  }, [active, assets, groups, railCollections, q, sort, locale, t.fileLibrary.ungrouped])

  const activeIndex = useMemo(
    () => (active.kind === 'index' ? vectorIndexById(state, active.indexId) : null),
    [active, state.vectorIndexes],
  )
  const activeServerCollection = useMemo(
    () => active.kind === 'server-collection'
      ? serverCollections.find((collection) => collection.id === active.collectionId) ?? null
      : null,
    [active, serverCollections],
  )
  const allIndexMembers = useMemo(
    () => (activeIndex ? vectorIndexMembersResolved(state, activeIndex.id) : []),
    [activeIndex, state.vectorIndexes, state.fileAssets],
  )
  const indexMembers = useMemo(
    () => sortMembersForSort(q ? allIndexMembers.filter((entry) => matchesQuery(entry.asset)) : allIndexMembers),
    [allIndexMembers, q, sort, locale, sections, groups],
  )
  const activeIndexJob = activeIndex ? state.indexingJobs[activeIndex.id] : null
  // The per-file "Index this file" action only makes sense when the click can
  // actually run the INCREMENTAL path (existing collection, same model). On a
  // first build or a model change a single file forces a full rebuild — so the
  // per-row action is hidden there and the top button (which honestly says it
  // touches the whole index) takes over.
  const activeIndexCanIncrementalIndex =
    !!activeIndex
    && !indexesReadOnly
    && Boolean(activeIndex.serverCollectionId)
    && activeIndex.serverCollectionModel === activeIndex.model
  // A member's server-confirmed live outcome during an active client-build run
  // (the live job carries the per-file sets; absent → not yet processed).
  const memberLiveProgress = (fileId: string): 'embedded' | 'skipped' | undefined =>
    activeIndexJob?.embeddedFileIds?.includes(fileId)
      ? 'embedded'
      : activeIndexJob?.skippedFileIds?.includes(fileId)
        ? 'skipped'
        : undefined
  const memberJobProgress = (fileId: string) =>
    activeIndexJob?.memberProgress?.[fileId]
  // Whether THIS file is part of the *actively running* job's working set —
  // only then does its row read "läuft" (see isMemberInRun: a still-queued job
  // pulses nothing, a file outside the run keeps its real state).
  const memberInRun = (fileId: string): boolean => isMemberInRun(activeIndexJob, fileId)
  function sortMembersForSort(members: ReturnType<typeof vectorIndexMembersResolved>) {
    if (sort === 'recent') return members
    const ordered = [...members]
    if (sort === 'name') ordered.sort((a, b) => a.asset.label.localeCompare(b.asset.label, locale))
    else if (sort === 'size') ordered.sort((a, b) => b.asset.sizeBytes - a.asset.sizeBytes)
    else if (sort === 'pages') ordered.sort((a, b) => (b.asset.pageCount ?? 0) - (a.asset.pageCount ?? 0))
    return ordered
  }
  // A built index keeps its rail entry; its backing knowledge collection is
  // internal storage and must not appear as a second, separate thing. The
  // UNFILTERED list stays in use for resolving the active target below.
  const backedCollectionIds = useMemo(() => indexBackedCollectionIds(indexes), [indexes])
  const railServerCollections = useMemo(
    () => railVisibleServerCollections(serverCollections, backedCollectionIds),
    [serverCollections, backedCollectionIds],
  )
  const activeIndexCollection = useMemo(
    () => (activeIndex?.serverCollectionId
      ? serverCollections.find((collection) => collection.id === activeIndex.serverCollectionId) ?? null
      : null),
    [activeIndex, serverCollections],
  )
  // Chunks the content filter left without context, per server document of
  // the active index, from the document metadata.
  const indexContextOmissions = useCollectionContextOmissions({
    collectionId: activeIndex?.serverCollectionId ?? null,
    idle: activeIndex?.status !== 'indexing',
    knowledgeSync,
    scopeKey: deletionScopeKey,
  })
  const memberIds = useMemo(() => new Set(activeIndex ? activeIndex.members.map((member) => member.fileId) : []), [activeIndex])
  const isLibraryEmpty = active.kind !== 'index' && blocks.every((block) => block.items.length === 0)

  // ---- multi-select ----
  const isLibraryView = active.kind === 'all' || active.kind === 'collection'
  const selectionActive = isLibraryView && (selectionArmed || selectedIds.size > 0)
  // The flattened visual order of the current view — shift-click ranges and
  // "Alle auswählen" operate on exactly what the user sees (filter-scoped).
  const visibleAssetIds = useMemo(() => blocks.flatMap((block) => block.items.map((item) => item.id)), [blocks])
  const clearSelection = useCallback(() => {
    setSelectedIds(new Set())
    setSelectionArmed(false)
    selectionAnchorRef.current = null
  }, [])
  const toggleSelect = useCallback(
    (fileId: string, options: { additive?: boolean; range?: boolean } = {}) => {
      setSelectedIds((previous) => {
        const next = new Set(previous)
        if (options.range) {
          for (const id of rangeBetween(visibleAssetIds, selectionAnchorRef.current, fileId)) next.add(id)
        } else if (next.has(fileId)) {
          next.delete(fileId)
        } else {
          next.add(fileId)
        }
        return next
      })
      if (!options.range) selectionAnchorRef.current = fileId
    },
    [visibleAssetIds],
  )
  const selectAllVisible = useCallback(() => {
    setSelectedIds(new Set(visibleAssetIds))
  }, [visibleAssetIds])
  // Leaving the library view (or switching its target) drops the selection —
  // a hidden selection acting on invisible files would be a footgun.
  useEffect(() => {
    clearSelection()
  }, [active, clearSelection])
  // INVARIANT: the selection is always a subset of the VISIBLE rows. Files
  // that leave the view (deleted, moved to another collection, filtered out
  // by the search box) fall out of the selection, so the count chip and the
  // bulk actions can never touch anything the user does not see.
  useEffect(() => {
    setSelectedIds((previous) => {
      if (previous.size === 0) return previous
      const visible = new Set(visibleAssetIds)
      let changed = false
      const next = new Set<string>()
      for (const id of previous) {
        if (visible.has(id)) next.add(id)
        else changed = true
      }
      return changed ? next : previous
    })
  }, [visibleAssetIds])
  // Esc exits; Ctrl/Cmd+A selects everything visible (never while typing).
  useEffect(() => {
    if (!selectionActive) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      // An Esc that a layer (dropdown, tooltip, dialog) already consumed at
      // capture phase dismissed THAT layer — it must not also wipe the
      // selection underneath.
      if (event.defaultPrevented) return
      const target = event.target as HTMLElement | null
      if (target?.closest('input, textarea, [contenteditable="true"]')) return
      if (event.key === 'Escape') {
        event.preventDefault()
        clearSelection()
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') {
        event.preventDefault()
        selectAllVisible()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectionActive, clearSelection, selectAllVisible])
  const bandSelectionState = useCallback(
    (items: readonly FileAssetRecord[]): 'all' | 'some' | 'none' => {
      let selected = 0
      for (const item of items) if (selectedIds.has(item.id)) selected += 1
      return selected === 0 ? 'none' : selected === items.length ? 'all' : 'some'
    },
    [selectedIds],
  )
  const toggleBandSelection = useCallback(
    (items: readonly FileAssetRecord[]) => {
      setSelectedIds((previous) => {
        const next = new Set(previous)
        const allSelected = items.length > 0 && items.every((item) => next.has(item.id))
        for (const item of items) {
          if (allSelected) next.delete(item.id)
          else next.add(item.id)
        }
        return next
      })
    },
    [],
  )

  // ---- mutations ----
  // The just-uploaded record for the background server-parse kickoff: the
  // live state already carries the settled row, only serverFileId may still
  // be missing from the ref snapshot in the same tick.
  const settledRecordFor = useCallback(
    (assetId: string, serverFileId: string): FileAssetRecord | null => {
      const record = stateRef.current.fileAssets[assetId]
      return record ? { ...record, serverFileId } : null
    },
    [],
  )

  function ingestInto(files: File[], target: UploadTarget) {
    if (files.length === 0) return
    const existingLabels = assets.map((asset) => asset.label)
    const { queue, records } = createFileAssetPlaceholders(
      files,
      { groupId: target.groupId, kind: 'library', sectionId: target.sectionId },
      existingLabels,
      Boolean(serverFileUpload),
    )
    // Feedback first, work second: every selected file appears as a pending
    // row (and pending index member) before any parse or upload starts.
    dispatch({ assets: records, type: 'ingestFileAssets' })
    if (target.indexId) {
      dispatch({ fileIds: records.map((record) => record.id), indexId: target.indexId, type: 'addDocsToVectorIndex' })
    }
    const bindings = new Map<string, UploadBinding>(records.map((record) => [
      record.id,
      uploadBindingForRecord(record),
    ]))
    // Retry source registered SYNCHRONOUSLY with the placeholder dispatch: a
    // delete during the pre-flight window removes the entry, and the upload
    // guard below then skips the file instead of uploading a deleted row.
    for (const item of queue) {
      const binding = bindings.get(item.assetId)
      if (!binding) continue
      activeUploadRegistry.register(item.assetId, { binding, file: item.file })
    }
    void (async () => {
      // Persist the target once per batch BEFORE the first byte moves. A
      // failed pre-flight is a visible upload failure; there is no unbound
      // server upload that could strand bytes outside the project lifecycle.
      let bindingReady = false
      if (serverFileUpload && ensureUploadTarget) {
        bindingReady = await ensureUploadTarget(target.sectionId, target.groupId ?? null).catch(() => false)
      }
      await runFileIngestPipeline(queue, {
        needsClientParse: (assetId) => {
          const asset = stateRef.current.fileAssets[assetId]
          return Boolean(asset) && asset.parserId !== 'markitdown'
        },
        onParsed: (assetId, parsed, clearParsePending) => dispatch({
          assetId,
          clearParsePending,
          extractedText: parsed.extractedText,
          pageCount: parsed.pageCount,
          parseStatus: parsed.parseStatus,
          parseWarning: parsed.parseWarning,
          textTruncated: parsed.textTruncated,
          type: 'applyFileAssetClientParse',
        }),
        onUploadFailed: (assetId, message) => dispatch({ assetId, message, type: 'failFileAssetUpload' }),
        onUploadAccepted: (assetId, result) => {
          dispatch({ assetId, ...result, type: 'adoptFileAssetUploadLifecycle' })
          if (result.status !== 'ready' || !result.serverFileId) return
          activeUploadRegistry.delete(assetId)
          uploadNeedsBytesRef.current.delete(assetId)
          const settled = settledRecordFor(assetId, result.serverFileId)
          if (settled) onAssetsIngested?.([settled])
        },
        parse: (file) => parser.parse(file),
        serverParseWillRun: (_assetId, uploaded) => uploaded && serverParserAvailable,
        upload: serverFileUpload
          ? (item) => {
              const entry = activeUploadRegistry.get(item.assetId)
              if (!entry || !stateRef.current.fileAssets[item.assetId]) {
                // Deleted while queued: never upload a removed row (the
                // rejection settles in onUploadFailed, which no-ops there).
                return Promise.reject(readerError('uploadFileRemoved'))
              }
              if (!bindingReady) {
                return Promise.reject(
                  readerError('uploadTargetUnavailable'),
                )
              }
              return serverFileUpload(entry.file, entry.binding)
            }
          : undefined,
      })
    })()
  }

  const retryUpload = useCallback((assetId: string) => {
    const pending = activeUploadRegistry.get(assetId)
    const asset = stateRef.current.fileAssets[assetId]
    if (
      !pending
      && asset
      && (
        (asset.uploadStatus === 'awaiting_upload' && !asset.uploadOperationId)
        || uploadNeedsBytesRef.current.has(assetId)
      )
    ) {
      resumeUploadAssetIdRef.current = assetId
      fileInputRef.current?.click()
      return
    }
    dispatch({ assetId, pending: true, type: 'setFileAssetUploadPending' })
    void (async () => {
      try {
        if (!pending || !serverFileUpload) {
          if (!retryServerUpload) {
            throw readerError('uploadOriginalRequired')
          }
          await retryServerUpload(assetId)
          return
        }
        if (ensureUploadTarget) {
          const ready = await ensureUploadTarget(
            pending.binding.sectionId,
            pending.binding.groupId,
          )
          if (!ready) {
            throw readerError('uploadTargetUnavailable')
          }
        }
        const result = await serverFileUpload(pending.file, pending.binding)
        dispatch({ assetId, ...result, type: 'adoptFileAssetUploadLifecycle' })
        if (result.status !== 'ready' || !result.serverFileId) return
        activeUploadRegistry.delete(assetId)
        uploadNeedsBytesRef.current.delete(assetId)
        const settled = settledRecordFor(assetId, result.serverFileId)
        if (settled) onAssetsIngested?.([settled])
      } catch (error) {
        if (error instanceof Error && error.name === 'upload_bytes_required') {
          uploadNeedsBytesRef.current.add(assetId)
        }
        dispatch({ assetId, message: serverUploadFailureMessage(error), type: 'failFileAssetUpload' })
      }
    })()
  }, [
    activeUploadRegistry,
    dispatch,
    ensureUploadTarget,
    onAssetsIngested,
    retryServerUpload,
    serverFileUpload,
    settledRecordFor,
  ])
  /** Retry only where the File survived (same session); elsewhere the row
   * keeps the persisted warning + remove affordance. */
  const canRetryUpload = useCallback(
    (assetId: string) => (
      (Boolean(serverFileUpload) && activeUploadRegistry.has(assetId))
      || Boolean(
        retryServerUpload
        && stateRef.current.fileAssets[assetId]?.uploadOperationId,
      )
      || stateRef.current.fileAssets[assetId]?.uploadStatus === 'awaiting_upload'
    ),
    [activeUploadRegistry, retryServerUpload, serverFileUpload],
  )

  const resumeUploadWithFile = useCallback((assetId: string, file: File) => {
    const asset = stateRef.current.fileAssets[assetId]
    if (!asset) return
    if (file.name !== asset.fileName || file.size !== asset.sizeBytes) {
      dispatch({
        assetId,
        message: serverUploadFailureMessage(
          readerError('uploadSameFileRequired'),
        ),
        type: 'failFileAssetUpload',
      })
      return
    }
    activeUploadRegistry.register(assetId, {
      binding: uploadBindingForRecord(asset),
      file,
    })
    uploadNeedsBytesRef.current.delete(assetId)
    retryUpload(assetId)
  }, [activeUploadRegistry, dispatch, retryUpload])

  const openUpload = (target: UploadTarget) => {
    targetRef.current = target
    fileInputRef.current?.click()
  }

  const moveFile = useCallback(
    (fileId: string, sectionId: string, groupId: string | null) => dispatch({ fileId, groupId, sectionId, type: 'moveFileAsset' }),
    [dispatch],
  )
  const renameFile = useCallback(
    (fileId: string, label: string) => dispatch({ fileId, label, type: 'renameFileAsset' }),
    [dispatch],
  )
  const deleteFile = useCallback((fileId: string) => {
    activeUploadRegistry.delete(fileId)
    uploadNeedsBytesRef.current.delete(fileId)
    void deletionApi.startAssets([fileId]).catch(() => undefined)
  }, [activeUploadRegistry, deletionApi])
  const deleteSelected = useCallback(() => {
    const fileIds = [...selectedIds].filter(
      (fileId) => (stateRef.current.fileAssets[fileId]?.lifecycleStatus ?? 'active') === 'active',
    )
    if (fileIds.length === 0) return
    for (const fileId of fileIds) activeUploadRegistry.delete(fileId)
    for (const fileId of fileIds) uploadNeedsBytesRef.current.delete(fileId)
    void deletionApi.startAssets(fileIds).catch(() => undefined)
    clearSelection()
  }, [activeUploadRegistry, clearSelection, deletionApi, selectedIds])
  const moveSelected = useCallback((sectionId: string, groupId: string | null) => {
    const fileIds = [...selectedIds]
    if (fileIds.length === 0) return
    dispatch({ fileIds, groupId, sectionId, type: 'moveFileAssets' })
    clearSelection()
  }, [clearSelection, dispatch, selectedIds])

  /** Start the server-owned index aggregate deletion. The operation feed owns
   * progress, retry, terminal removal and reload recovery. */
  const deleteIndex = (indexId: string) => {
    const index = vectorIndexById(state, indexId)
    indexRunControllers.get(indexId)?.abort()
    setIndexActionError(null)
    if (!index) return
    const route = resolveVectorIndexDeletionRoute({
      knowledgeAvailable: knowledgeSync !== null,
      projectPersistenceActive: assetDeletionApiOptions !== null,
      serverCollectionId: index.serverCollectionId,
    })
    if (route === 'local') {
      dispatch({ indexId, type: 'deleteVectorIndex' })
      return
    }
    if (deletingIndexIds.has(indexId)) return
    setDeletingIndexIds((current) => new Set(current).add(indexId))
    const started = route === 'knowledge_collection'
      ? deletionApi.startKnowledgeCollection(index.serverCollectionId!)
      : deletionApi.startVectorIndex(indexId, index.serverCollectionId)
    void started.catch((error: unknown) => {
      setDeletingIndexIds((current) => {
        const next = new Set(current)
        next.delete(indexId)
        return next
      })
      setIndexActionError({
        indexId,
        message: error instanceof Error ? error.message : String(error),
      })
    })
  }

  const localIndexIdForCollection = (collectionId: string): string | null =>
    indexes.find((index) => index.serverCollectionId === collectionId)?.id ?? null
  const clearServerJob = (collectionId: string, jobId?: string) => {
    setServerJobs((current) => {
      if (!current[collectionId] || (
        jobId !== undefined && current[collectionId].jobId !== jobId
      )) return current
      const next = { ...current }
      delete next[collectionId]
      return next
    })
  }

  // One paused run of several documents: a dependency pause when any document
  // waits on the provider, and its first reason with how many documents wait.
  const pausedRunStatus = (
    jobs: ReadonlyArray<{ status: 'paused_dependency' | 'paused_validation' }>,
  ): 'paused_dependency' | 'paused_validation' => (
    jobs.some((job) => job.status === 'paused_dependency')
      ? 'paused_dependency'
      : 'paused_validation'
  )
  const {
    cancelReindex,
    resumeRawReindex,
    resumeReindex,
    startReindex,
  } = useIndexingJobApi({
    apiKey: knowledgeSync?.apiKey,
    enabled: knowledgeSync !== null,
    onCancelled: (collectionId) => {
      clearServerJob(collectionId)
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({ indexId, type: 'markVectorIndexCancelled' })
    },
    onComplete: (collectionId, summary) => {
      clearServerJob(collectionId)
      void onRefreshServerCollections?.()
      const indexId = localIndexIdForCollection(collectionId)
      if (!indexId) return
      if (summary.operation_kind === 'document_revision') {
        const current = stateRef.current.vectorIndexes[indexId]
        const live = stateRef.current.indexingJobs[indexId]
        const completedFileId = current?.members.find(
          (member) => member.serverDocumentId === summary.document_id,
        )?.fileId
        const embeddedFileIds = [
          ...new Set([
            ...(current?.members
              .filter((member) => member.state === 'embedded')
              .map((member) => member.fileId) ?? []),
            ...(live?.embeddedFileIds ?? []),
            ...(completedFileId ? [completedFileId] : []),
          ]),
        ]
        dispatch({
          embeddedFileIds,
          indexId,
          skippedFileIds: current?.members
            .filter((member) => member.state === 'skipped')
            .map((member) => member.fileId) ?? [],
          type: 'completeVectorIndexReindex',
        })
        return
      }
      dispatch({ indexId, type: 'completeVectorIndexReindex' })
    },
    onDocumentCompleted: (collectionId, documentId) => {
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({ indexId, serverDocumentId: documentId, type: 'markVectorIndexDocumentEmbedded' })
    },
    onDocumentProgress: (collectionId, documentId, progress) => {
      const indexId = localIndexIdForCollection(collectionId)
      const member = indexId
        ? stateRef.current.vectorIndexes[indexId]?.members.find(
            (candidate) => candidate.serverDocumentId === documentId,
          )
        : null
      if (!indexId || !member) return
      dispatch({
        currentBatch: progress.currentBatch,
        fileId: member.fileId,
        indexId,
        phase: progress.phase,
        status: progress.status,
        totalBatches: progress.totalBatches,
        type: 'markVectorIndexMemberProgress',
      })
    },
    onDocumentStarted: (collectionId, documentId) => {
      const indexId = localIndexIdForCollection(collectionId)
      const member = indexId
        ? stateRef.current.vectorIndexes[indexId]?.members.find(
            (candidate) => candidate.serverDocumentId === documentId,
          )
        : null
      if (!indexId || !member) return
      dispatch({
        fileId: member.fileId,
        indexId,
        phase: 'starting',
        status: 'running',
        type: 'markVectorIndexMemberProgress',
      })
    },
    onError: (collectionId, message) => {
      setServerJobs((current) => ({
        ...current,
        [collectionId]: {
          ...(current[collectionId] ?? {
            completedDocuments: 0,
            jobId: '',
            totalDocuments: 0,
          }),
          error: message,
          status: 'error',
        },
      }))
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({ indexId, message, type: 'markVectorIndexError' })
    },
    onProgress: (collectionId, completedDocuments, totalDocuments, currentDocumentTitle) => {
      setServerJobs((current) => ({
        ...current,
        [collectionId]: {
          ...(current[collectionId] ?? { jobId: '', status: 'running' }),
          completedDocuments,
          currentDocumentTitle,
          status: current[collectionId]?.status === 'cancelling' ? 'cancelling' : 'running',
          totalDocuments,
        },
      }))
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({ completedDocuments, currentDocumentTitle, indexId, totalDocuments, type: 'markVectorIndexProgress' })
    },
    onQueued: (collectionId, queuePosition) => {
      setServerJobs((current) => {
        const existing = current[collectionId]
        if (!existing) return current
        return { ...current, [collectionId]: { ...existing, status: 'queued' } }
      })
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({ indexId, queuePosition, type: 'markVectorIndexQueued' })
    },
    onPaused: (collectionId, pause) => {
      setServerJobs((current) => ({
        ...current,
        [collectionId]: {
          completedDocuments: pause.completedDocuments,
          currentBatch: pause.currentBatch,
          autoResume: pause.autoResume,
          jobId: pause.jobId,
          pauseErrorType: pause.errorType,
          phase: pause.phase,
          status: pause.status,
          totalBatches: pause.totalBatches,
          totalDocuments: pause.totalDocuments,
        },
      }))
      const indexId = localIndexIdForCollection(collectionId)
      if (indexId) dispatch({
        completedDocuments: pause.completedDocuments,
        currentBatch: pause.currentBatch,
        indexId,
        autoResume: pause.autoResume,
        errorType: pause.errorType,
        phase: pause.phase,
        status: pause.status,
        totalBatches: pause.totalBatches,
        totalDocuments: pause.totalDocuments,
        type: 'markVectorIndexPaused',
      })
    },
    onPausedDocuments: (collectionId, jobs) => {
      const indexId = localIndexIdForCollection(collectionId)
      const index = indexId ? stateRef.current.vectorIndexes[indexId] : null
      if (!indexId || !index) return
      // A run in flight owns the index's live state; it may be the very
      // continuation that is resuming these jobs right now.
      const live = stateRef.current.indexingJobs[indexId]
      if (live && live.status !== 'paused_dependency' && live.status !== 'paused_validation') return
      const paused = jobs.flatMap((summary) => {
        const member = index.members.find(
          (candidate) => candidate.serverDocumentId === summary.document_id,
        )
        const disposition = indexingJobDisposition(summary.status)
        return member && disposition.kind === 'paused'
          ? [{ fileId: member.fileId, status: disposition.status, summary }]
          : []
      })
      if (paused.length === 0) return
      dispatch({
        indexId,
        jobId: `paused-${collectionId}`,
        queuedFileIds: [],
        runningFileIds: [],
        source: 'build',
        status: pausedRunStatus(paused),
        totalDocuments: paused.length,
        type: 'startVectorIndexReindex',
      })
      for (const job of paused) {
        dispatch({
          currentBatch: job.summary.current_batch,
          fileId: job.fileId,
          indexId,
          jobId: job.summary.job_id,
          autoResume: job.summary.checkpoint.auto_resume ?? null,
          errorType: job.summary.error?.type ?? null,
          phase: job.summary.phase,
          status: job.status,
          totalBatches: job.summary.total_batches,
          type: 'markVectorIndexMemberProgress',
        })
      }
      dispatch({
        indexId,
        status: pausedRunStatus(paused),
        type: 'markVectorIndexRunPaused',
      })
    },
    onReadyRaw: (collectionId, jobId) => {
      clearServerJob(collectionId, jobId)
      void onRefreshServerCollections?.()
      const indexId = localIndexIdForCollection(collectionId)
      const live = indexId ? stateRef.current.indexingJobs[indexId] : null
      if (indexId && live?.jobId === jobId) {
        dispatch({ indexId, type: 'completeVectorIndexReindex' })
      }
    },
    onResumed: (collectionId, jobId, totalDocuments) => {
      setServerJobs((current) => ({
        ...current,
        [collectionId]: {
          ...(current[collectionId] ?? {
            completedDocuments: 0,
            jobId,
          }),
          error: undefined,
          autoResume: undefined,
          jobId,
          pauseErrorType: undefined,
          phase: undefined,
          status: 'running',
          currentBatch: undefined,
          totalBatches: undefined,
          totalDocuments,
        },
      }))
      const indexId = localIndexIdForCollection(collectionId)
      const live = indexId ? stateRef.current.indexingJobs[indexId] : null
      if (indexId && live?.jobId === jobId) {
        dispatch({ indexId, totalDocuments, type: 'markVectorIndexResumed' })
      }
    },
    onStart: (collectionId, jobId, totalDocuments, status, summary) => {
      setServerJobs((current) => ({
        ...current,
        [collectionId]: current[collectionId]?.jobId === jobId
          ? {
              ...current[collectionId],
              status,
              totalDocuments,
            }
          : {
              completedDocuments: 0,
              jobId,
              status,
              totalDocuments,
            },
      }))
      const indexId = localIndexIdForCollection(collectionId)
      const index = indexId ? stateRef.current.vectorIndexes[indexId] : null
      const documentFileId = summary.operation_kind === 'document_revision'
        ? index?.members.find(
            (member) => member.serverDocumentId === summary.document_id,
          )?.fileId
        : undefined
      if (indexId) dispatch({
        indexId,
        jobId,
        queuedFileIds: documentFileId
          ? [documentFileId]
          : index?.members.map((member) => member.fileId),
        runningFileIds: documentFileId ? [documentFileId] : [],
        source: 'server',
        status,
        totalDocuments,
        type: 'startVectorIndexReindex',
      })
    },
    onSuperseded: (collectionId, jobId) => {
      clearServerJob(collectionId, jobId)
      const indexId = localIndexIdForCollection(collectionId)
      const live = indexId ? stateRef.current.indexingJobs[indexId] : null
      if (indexId && live?.jobId === jobId) {
        dispatch({ indexId, type: 'markVectorIndexSuperseded' })
      }
    },
    refreshToken: serverCollectionsRefreshToken,
    workspaceId: knowledgeSync?.workspaceId ?? '',
  })

  const requestReindexRecovery = async (
    jobId: string,
    mode: 'raw' | 'resume',
  ) => {
    if (reindexRecoveryInFlightRef.current.has(jobId)) return
    reindexRecoveryInFlightRef.current.add(jobId)
    setReindexRecoveryPending((current) => ({ ...current, [jobId]: mode }))
    try {
      if (mode === 'raw') await resumeRawReindex(jobId)
      else await resumeReindex(jobId)
    } finally {
      reindexRecoveryInFlightRef.current.delete(jobId)
      setReindexRecoveryPending((current) => {
        if (current[jobId] !== mode) return current
        const next = { ...current }
        delete next[jobId]
        return next
      })
    }
  }

  // `onlyFileId` scopes the run to a SINGLE pending member (the per-row "Index
  // this file" action) — it filters the pending set to that one document so the
  // incremental path ingests just it. Omitted = the whole index (top button).
  // `resumeJobs` continues a paused run ("Alle fortsetzen"): its paused
  // documents resume their jobs, the members it never reached start.
  const triggerReindex = (
    indexId: string,
    onlyFileId?: string,
    resumeJobs?: ReadonlyMap<string, PausedMemberJob>,
  ) => {
    const index = vectorIndexById(state, indexId)
    // One job per index at a time (the per-row action must not race the top
    // run); continuing the paused run is that same job.
    if (!index || (index.status === 'indexing' && !resumeJobs)) return
    const memberEntries = vectorIndexMembersResolved(state, indexId)
    // Half-ingested members (upload or parse still in flight) stay OUT of
    // the working set: embedding them now would read an empty body and
    // terminally mark them 'skipped'; they run on the next index pass.
    const pendingEntries = memberEntries.filter(
      (entry) =>
        entry.member.state === 'pending'
        && !entry.asset.uploadPending
        && !entry.asset.parsePending
        && (!onlyFileId || entry.asset.id === onlyFileId),
    )
    // Single-file action on a non-pending member, or a continuation with
    // nothing waiting: nothing to do (never fall through to a full re-embed of
    // the whole collection).
    if ((onlyFileId || resumeJobs) && pendingEntries.length === 0) return
    if (!knowledgeSync) {
      // Demo: the local simulator (effect below) drives progress to done. Scope
      // mirrors the real paths — index only the new (pending) members for an
      // incremental add / per-row action, the whole index for a refresh/rebuild,
      // so the demo never makes already-embedded rows read "läuft".
      const demoWorkingSet =
        pendingEntries.length > 0
          ? pendingEntries.map((entry) => entry.asset.id)
          : memberEntries.map((entry) => entry.asset.id)
      dispatch({
        indexId,
        jobId: `demo-${indexId}-${Date.now()}`,
        runningFileIds: demoWorkingSet,
        source: 'demo',
        totalDocuments: demoWorkingSet.length,
        type: 'startVectorIndexReindex',
      })
      return
    }
    const sync = knowledgeSync
    // serverCollectionModel is absent on indexes built before the field existed
    // -> reads as a mismatch, so the next reindex heals via a full rebuild.
    const sameModel = index.serverCollectionModel === index.model

    // Resolve member bodies (a server-synced project hydrates them empty,
    // load-on-use), run a client-driven ingest, back-fill re-parsed text, and
    // report the embedded set so the reducer marks ONLY what actually landed
    // (skipped/text-less members stay pending -> the index reads honestly
    // stale). Shared by the incremental-add and full-rebuild branches.
    const runClientIngest = (
      assets: FileAssetRecord[],
      ingest: (
        resolved: FileAssetRecord[],
        progress: IngestProgress,
      ) => Promise<KnowledgeReindexResult>,
      controller: AbortController,
    ) => {
      const titleByFileId = new Map(
        assets.map((asset) => [asset.id, asset.title || asset.label]),
      )
      // Several documents embed at once, so the label must name something
      // that is ACTUALLY still running: the oldest in-flight member. Without
      // it the line would flip to whichever document finished last while
      // others are still being embedded.
      const inFlight = new Set<string>()
      const runningTitle = (): string | undefined => {
        for (const fileId of inFlight) return titleByFileId.get(fileId)
        return undefined
      }
      // Each document revision runs as a durable server job (potentially
      // minutes for a large PDF), so the run announces which ones it STARTED
      // before subscribing to progress. It reports the CONFIRMED completion
      // count (never the queue position, which would overstate progress and
      // then jump backwards); a start is not an outcome and must not sort the
      // member anywhere.
      const onMemberStart: MemberStart = ({ fileId, done, total }) => {
        inFlight.add(fileId)
        dispatch({
          fileId,
          indexId,
          phase: 'starting',
          status: 'running',
          type: 'markVectorIndexMemberProgress',
        })
        dispatch({
          completedDocuments: done,
          currentDocumentTitle: runningTitle(),
          indexId,
          runningFileIds: [...inFlight],
          totalDocuments: total,
          type: 'markVectorIndexProgress',
        })
      }
      const onMemberJobProgress: MemberJobProgress = ({
        autoResume,
        currentBatch,
        errorType,
        fileId,
        jobId,
        phase,
        queuePosition,
        status,
        totalBatches,
      }) => {
        dispatch({
          autoResume,
          currentBatch,
          errorType,
          fileId,
          indexId,
          jobId,
          phase,
          queuePosition,
          status,
          totalBatches,
          type: 'markVectorIndexMemberProgress',
        })
      }
      // Server-confirmed per-member progress → advance the bar + flip each
      // file row live (no cosmetic guessing; fires only after the await).
      const onMemberFailed: MemberFailed = ({ fileId }) => {
        inFlight.delete(fileId)
      }
      const onMemberDone: MemberProgress = ({ fileId, done, total, embedded }) => {
        inFlight.delete(fileId)
        dispatch({
          completedDocuments: done,
          currentDocumentTitle: runningTitle(),
          embedded,
          fileId,
          indexId,
          runningFileIds: [...inFlight],
          totalDocuments: total,
          type: 'markVectorIndexProgress',
        })
      }
      void (async () => {
        try {
          const bodies = ensureAssetBodiesLoaded
            ? await ensureAssetBodiesLoaded(assets.map((asset) => asset.id))
            : null
          if (controller.signal.aborted) {
            dispatch({ indexId, type: 'markVectorIndexCancelled' })
            return
          }
          const resolved = bodies
            ? assets.map((asset) => ({
                ...asset,
                extractedText: bodies.get(asset.id) ?? asset.extractedText,
              }))
            : assets
          const result = await ingest(resolved, {
            onMemberDone,
            onMemberFailed,
            onMemberJobProgress,
            onMemberStart,
          })
          if (result.cancelled && !result.collectionId) {
            // Cancelled before anything was embedded — the empty collection is
            // already gone, so the index is exactly as it was.
            dispatch({ indexId, type: 'markVectorIndexCancelled' })
          } else if (result.collectionId) {
            // Terminal reconcile — ALSO for a cancelled run: whatever embedded
            // is real, so the index adopts the collection and the finished
            // members. The next run then resumes instead of starting over.
            dispatch({
              embeddedFileIds: result.embeddedFileIds,
              skippedFileIds: result.skippedFileIds,
              indexId,
              result: result.cancelled ? 'cancelled' : 'ok',
              serverCollectionId: result.collectionId,
              serverCollectionModel: result.serverCollectionModel,
              serverDocumentIds: result.serverDocumentIds,
              type: 'completeVectorIndexReindex',
            })
          }
          if (result.collectionId && !vectorIndexById(stateRef.current, indexId) && knowledgeSync) {
            // The index was deleted while this run was still going: its
            // collection now belongs to nobody, so remove it rather than
            // leaving an unowned copy of the embeddings behind.
            try {
              await deletionApi.startKnowledgeCollection(result.collectionId)
            } catch (error: unknown) {
              console.error('Inqtrix could not remove the collection of a deleted index.', error)
            }
          }
          await onRefreshServerCollections?.()
        } catch (error: unknown) {
          if (isAbortError(error) || controller.signal.aborted) {
            dispatch({ indexId, type: 'markVectorIndexCancelled' })
            return
          }
          if (error instanceof KnowledgeReindexPartialError) {
            const current = vectorIndexById(stateRef.current, indexId)
            const embeddedFileIds = [
              ...new Set([
                ...(current?.members
                  .filter((member) => member.state === 'embedded')
                  .map((member) => member.fileId) ?? []),
                ...error.result.embeddedFileIds,
              ]),
            ]
            dispatch({
              embeddedFileIds,
              indexId,
              serverCollectionId: error.result.collectionId!,
              serverCollectionModel: error.result.serverCollectionModel,
              serverDocumentIds: error.result.serverDocumentIds,
              skippedFileIds: error.result.skippedFileIds,
              type: 'adoptVectorIndexPartialResult',
            })
            if (error.pausedJobs.length > 0) {
              // The run stays open as ONE paused run. Each paused document
              // already reported its pause and job to its row while the run
              // went on, so one action continues all of them.
              dispatch({
                indexId,
                status: pausedRunStatus(error.pausedJobs),
                type: 'markVectorIndexRunPaused',
              })
            } else {
              dispatch({
                indexId,
                message: error.message,
                type: 'markVectorIndexError',
              })
            }
          } else {
            dispatch({
              indexId,
              message: error instanceof Error ? error.message : String(error),
              type: 'markVectorIndexError',
            })
          }
        } finally {
          if (indexRunControllers.get(indexId) === controller) {
            indexRunControllers.delete(indexId)
          }
        }
      })()
    }

    /** One abort handle per client-driven run, so Abbrechen can actually stop
     * the in-flight request instead of only resetting local state. */
    const startClientRun = (): { controller: AbortController; sync: KnowledgeSyncOptions } => {
      const controller = new AbortController()
      indexRunControllers.set(indexId, controller)
      return { controller, sync: { ...sync, signal: controller.signal } }
    }

    // Incremental add: a built collection, SAME embedding model, and only new
    // (pending) members — ingest just those into the existing collection. No
    // full rebuild, no re-embedding of documents already present. (This closes
    // the bug where docs added after the first build were never ingested.)
    if (index.serverCollectionId && sameModel && pendingEntries.length > 0) {
      const pendingAssets = pendingEntries.map((entry) => entry.asset)
      // The complete embedded set after this run = members already embedded
      // plus whatever of the pending set actually ingests.
      const alreadyEmbedded = memberEntries
        .filter((entry) => entry.member.state === 'embedded')
        .map((entry) => entry.asset.id)
      dispatch({
        indexId,
        jobId: `ingest-${indexId}-${Date.now()}`,
        // Only the new (pending) members run — already-embedded members stay out
        // of the working set so their rows keep reading "Indexiert".
        queuedFileIds: pendingAssets.map((asset) => asset.id),
        runningFileIds: [],
        source: 'build',
        totalDocuments: pendingAssets.length,
        type: 'startVectorIndexReindex',
      })
      const incremental = startClientRun()
      runClientIngest(pendingAssets, async (resolved, progress) => {
        const result = await ingestNewVectorIndexMembers(
          index,
          resolved,
          incremental.sync,
          progress,
          resumeJobs,
        )
        return {
          ...result,
          embeddedFileIds: [...alreadyEmbedded, ...result.embeddedFileIds],
        }
      }, incremental.controller)
      return
    }

    // Re-embed in place: a built collection, SAME model, nothing new pending —
    // the durable server job re-vectorizes the stored text in place (real
    // per-document progress, cancellable, the collection stays online and is
    // never deleted/recreated). It preserves per-chunk page numbers via the
    // document's stored `_chunk_pages` (reembed_document re-aligns by index), so
    // a refresh keeps the PDF page-jump. NEW documents get their pages captured
    // at ingest (the incremental-add + file paths), not here — so this stays the
    // cheap, churn-free refresh rather than a delete+recreate that risks
    // orphaning the prior collection.
    if (index.serverCollectionId && sameModel) {
      void startReindex(index.serverCollectionId).catch((error: unknown) => {
        dispatch({
          indexId,
          message: error instanceof Error ? error.message : String(error),
          type: 'markVectorIndexError',
        })
      })
      return
    }

    // A built collection keeps both its id and embedding model. Replacing it
    // would revoke shares and leave existing references pointing at nothing.
    if (index.serverCollectionId) {
      dispatch({
        indexId,
        message: t.vectorIndex.modelImmutable,
        type: 'markVectorIndexError',
      })
      return
    }

    // First build only: create the server collection once, then ingest the
    // complete local working set. Subsequent refreshes use the in-place job.
    const memberAssets = memberEntries.map((entry) => entry.asset)
    dispatch({
      indexId,
      jobId: `build-${indexId}-${Date.now()}`,
      // Starts empty: only members the pool has actually picked up read
      // "läuft" (reported per start/finish), the rest stay queued.
      queuedFileIds: memberAssets.map((asset) => asset.id),
      runningFileIds: [],
      source: 'build',
      totalDocuments: memberAssets.length,
      type: 'startVectorIndexReindex',
    })
    const firstBuild = startClientRun()
    runClientIngest(
      memberAssets,
      (resolved, progress) =>
        createVectorIndexCollectionOnServer(index, resolved, firstBuild.sync, progress),
      firstBuild.controller,
    )
  }

  // "X" on a member: delete the exact document from the searchable collection
  // through the shared durable deletion ledger. The local member remains
  // visible until that receipt reaches `deleted`. Members created before
  // serverDocumentId was persisted are reconciled by the stable asset source
  // inside this exact collection. Missing/ambiguous identity or an unavailable
  // Knowledge service remains visibly blocked — a server-backed index never
  // falls back to a local-only success.
  const removeMember = (fileId: string) => {
    if (!activeIndex) return
    const indexId = activeIndex.id
    const removalKey = indexMemberRemovalKey(indexId, fileId)
    if (indexMemberRemovalInFlightRef.current.has(removalKey)) return
    const serverDocumentId = activeIndex.members.find(
      (member) => member.fileId === fileId,
    )?.serverDocumentId

    // An unbuilt/demo index has no searchable server collection, so local
    // membership is the whole truth and can be removed synchronously.
    if (!activeIndex.serverCollectionId && !serverDocumentId) {
      dispatch({ fileId, indexId, type: 'removeDocFromVectorIndex' })
      return
    }

    if (!knowledgeSync) {
      setIndexMemberRemovalFeedback((current) => ({
        ...current,
        [removalKey]: {
          error: t.vectorIndex.removalUnavailable,
          status: 'blocked',
        },
      }))
      return
    }

    const requestedScope = deletionScopeKey
    indexMemberRemovalInFlightRef.current.add(removalKey)
    setIndexMemberRemovalFeedback((current) => ({
      ...current,
      [removalKey]: { status: 'reconciling' },
    }))

    const fail = (error: unknown, status: 'blocked' | 'delete_failed') => {
      if (deletionScopeRef.current !== requestedScope) return
      setIndexMemberRemovalFeedback((current) => ({
        ...current,
        [removalKey]: {
          error: status === 'blocked'
            ? t.vectorIndex.removalUnresolved
            : error instanceof Error ? error.message : String(error),
          status,
        },
      }))
    }

    const run = async () => {
      let resolvedDocumentId = serverDocumentId
      if (!resolvedDocumentId) {
        if (!activeIndex.serverCollectionId) {
          fail(new Error(t.vectorIndex.removalUnresolved), 'blocked')
          return
        }
        try {
          const document = await resolveKnowledgeDocumentBySource(
            activeIndex.serverCollectionId,
            assetSourceId(fileId),
            knowledgeSync,
          )
          if (deletionScopeRef.current !== requestedScope) return
          resolvedDocumentId = document.id
          dispatch({
            fileId,
            indexId,
            serverDocumentId: resolvedDocumentId,
            type: 'reconcileVectorIndexMemberDocument',
          })
        } catch (error) {
          fail(
            error,
            hasHttpStatus(error, 404) || hasHttpStatus(error, 409)
              ? 'blocked'
              : 'delete_failed',
          )
          return
        }
      }

      try {
        await deletionApi.startKnowledgeDocument(resolvedDocumentId)
        if (deletionScopeRef.current !== requestedScope) return
        setIndexMemberRemovalFeedback((current) => {
          if (!(removalKey in current)) return current
          const next = { ...current }
          delete next[removalKey]
          return next
        })
      } catch (error) {
        // A 404 is not guessed to mean deletion success: another revision or
        // retained operation may still own searchable state.
        fail(error, 'delete_failed')
      }
    }

    void run().finally(() => {
      indexMemberRemovalInFlightRef.current.delete(removalKey)
    })
  }

  const retryMemberRemoval = (
    fileId: string,
    serverDocumentId?: string,
  ) => {
    const operation = serverDocumentId
      ? knowledgeDocumentDeletionById.get(serverDocumentId)
      : undefined
    if (operation?.status === 'delete_failed') {
      void deletionApi.retry(operation.operation_id)
      return
    }
    removeMember(fileId)
  }

  // A paused client run continues as ONE run: every paused document resumes
  // its job (or, by explicit choice, rebuilds without context) and the members
  // the run never reached start. A paused durable server job resumes there.
  const pausedRunJobs = (
    job: IndexingJobLive,
    mode: PausedMemberJob['mode'],
  ): Map<string, PausedMemberJob> => new Map(
    Object.entries(job.memberProgress ?? {}).flatMap(([fileId, progress]) => (
      progress.jobId
      && (progress.status === 'paused_dependency' || progress.status === 'paused_validation')
        ? [[fileId, { jobId: progress.jobId, mode }] as const]
        : []
    )),
  )

  const handleCancelReindex = (indexId: string) => {
    const job = state.indexingJobs[indexId]
    if (!job) return
    // Only a durable server job can be cancelled server-side; demo and
    // first-build runs cancel locally. The decision reads the authoritative
    // `source` fact, never the job-id format (No-Silent-Fallbacks).
    if (knowledgeSync && job.source === 'server') {
      void cancelReindex(job.jobId).catch((error: unknown) => {
        // The server still owns this job. A local-only cancel would lie while
        // the worker continues, so keep it active and expose the refusal.
        setIndexActionError({
          indexId,
          message: error instanceof Error ? error.message : String(error),
        })
      })
      return
    }
    // Client-driven run: abort the in-flight request. The run's own unwind
    // writes the terminal state (single writer — no cancel-vs-completion
    // double history) and keeps whatever actually embedded.
    const controller = indexRunControllers.get(indexId)
    if (controller) {
      controller.abort()
      return
    }
    // A paused client run still holds its paused jobs on the server: cancel
    // each there, or a reload would bring them back as a paused run.
    const pausedJobIds = [...pausedRunJobs(job, 'resume').values()].map(({ jobId }) => jobId)
    if (knowledgeSync && pausedJobIds.length > 0) {
      const options = { apiKey: knowledgeSync.apiKey, workspaceId: knowledgeSync.workspaceId }
      void Promise.all(pausedJobIds.map((jobId) => cancelIndexingJob(jobId, options)))
        .then(() => dispatch({ indexId, type: 'markVectorIndexCancelled' }))
        .catch((error: unknown) => {
          setIndexActionError({
            indexId,
            message: error instanceof Error ? error.message : String(error),
          })
        })
      return
    }
    dispatch({ indexId, type: 'markVectorIndexCancelled' })
  }

  const handleResumeReindex = (indexId: string) => {
    const job = state.indexingJobs[indexId]
    if (
      !job
      || (job.status !== 'paused_dependency' && job.status !== 'paused_validation')
    ) return
    setIndexActionError(null)
    if (job.source === 'build') {
      triggerReindex(indexId, undefined, pausedRunJobs(job, 'resume'))
      return
    }
    if (job.source !== 'server') return
    void requestReindexRecovery(job.jobId, 'resume').catch((error: unknown) => {
      setIndexActionError({
        indexId,
        message: error instanceof Error ? error.message : String(error),
      })
    })
  }

  const handleResumeRawReindex = (indexId: string) => {
    const job = state.indexingJobs[indexId]
    if (
      !job
      || (job.status !== 'paused_dependency' && job.status !== 'paused_validation')
    ) return
    setIndexActionError(null)
    if (job.source === 'build') {
      triggerReindex(indexId, undefined, pausedRunJobs(job, 'raw'))
      return
    }
    if (job.source !== 'server') return
    void requestReindexRecovery(job.jobId, 'raw').catch((error: unknown) => {
      setIndexActionError({
        indexId,
        message: error instanceof Error ? error.message : String(error),
      })
    })
  }

  useEffect(() => {
    // Demo-only reindex simulator: step each demo job's progress to done
    // (mount picks up a seeded mid-reindex too — demo "resume"). Server
    // jobs are driven by the hook's SSE stream instead.
    if (knowledgeSync) return undefined
    const timers = reindexTimers.current
    for (const [indexId, job] of Object.entries(state.indexingJobs)) {
      if (job.source !== 'demo' || timers.has(indexId)) continue
      const total = Math.max(1, job.totalDocuments)
      // The working set in processing order — confirm one file per tick so the
      // demo shows the same per-file flip as the real server path.
      const running = job.runningFileIds ?? []
      let done = job.completedDocuments
      const interval = setInterval(() => {
        const fileId = running[done]
        done += 1
        dispatch({
          completedDocuments: done,
          embedded: true,
          fileId,
          indexId,
          totalDocuments: total,
          type: 'markVectorIndexProgress',
        })
        if (done >= total) {
          dispatch({ indexId, type: 'completeVectorIndexReindex' })
          const handle = timers.get(indexId)
          if (handle) clearInterval(handle)
          timers.delete(indexId)
        }
      }, 850)
      timers.set(indexId, interval)
    }
    for (const indexId of Array.from(timers.keys())) {
      if (!state.indexingJobs[indexId]) {
        const handle = timers.get(indexId)
        if (handle) clearInterval(handle)
        timers.delete(indexId)
      }
    }
    return undefined
  }, [dispatch, knowledgeSync, state.indexingJobs])

  const handleNewIndex = () => {
    selectNewestIndex.current = true
    const defaultModel = embedModels[0]
    dispatch({
      dims: defaultModel?.dims,
      fileIds: [],
      model: defaultModel?.id,
      title: t.vectorIndex.newIndexTitle,
      type: 'createVectorIndex',
    })
  }
  const handleNewCollection = () => dispatch({ sectionId: '', title: t.fileLibrary.newCollectionTitle, type: 'createFileLibrarySection' })

  // ---- drop helpers ----
  const dropProps = (key: string, sectionId: string, groupId: string | null) => ({
    dropOver: dropKey === key,
    onDragLeave: () => setDropKey((current) => (current === key ? null : current)),
    onDragOver: (event: DragEvent) => {
      if (!isInternalFileDrag(event)) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      setDropKey(key)
    },
    onDrop: (event: DragEvent) => {
      if (!isInternalFileDrag(event)) return
      event.preventDefault()
      event.stopPropagation()
      const fileId = event.dataTransfer.getData('application/x-inqtrix-file-id')
      if (fileId) moveFile(fileId, sectionId, groupId)
      setDropKey(null)
    },
  })

  // One-pass "used in" counts for all rows (the per-id scan is O(indexes +
  // threads) and would otherwise run per row per render). Memoised on the
  // slices it reads, like the base projections above.
  const referenceCounts = useMemo(
    () => fileAssetReferenceCounts(state),
    [state.fileAssets, state.vectorIndexes, state.chatThreads],
  )

  const rowCallbacks = useMemo(() => ({
    canRetryUpload,
    moveTargets,
    onDelete: deleteFile,
    onMove: moveFile,
    onPreview: setPreviewAssetId,
    onRename: renameFile,
    onRetryUpload: retryUpload,
    onRetryDeletion: (operationId: string) => {
      void deletionApi.retry(operationId).catch(() => undefined)
    },
    onToggleSelect: isLibraryView ? toggleSelect : undefined,
    selectionActive,
  }), [canRetryUpload, deleteFile, deletionApi, isLibraryView, moveFile, moveTargets, renameFile, retryUpload, selectionActive, toggleSelect])

  // File preview overlay: the local record always carries the markdown for a
  // local asset; the Original (PDF) tab needs a connected files server, so the
  // options are null in demo/offline and the tab disables itself. Gated on the
  // FILES/persistence tier (fileApiOptions) — NOT on knowledge, so an uploaded
  // original stays viewable even when vector/knowledge is off.
  const previewAsset = previewAssetId ? state.fileAssets[previewAssetId] ?? null : null
  const previewOptions = fileApiOptions

  // ---- header bits ----
  const activeCollection = active.kind === 'collection' ? sections.find((section) => section.id === active.sectionId) ?? null : null
  const headerTitle = q
    ? t.fileLibrary.searchPlaceholderDocs
    : active.kind === 'all'
      ? t.fileLibrary.allCollections
      : active.kind === 'collection'
        ? activeCollection?.title ?? ''
        : active.kind === 'server-collection'
          ? activeServerCollection?.name ?? ''
          : activeIndex?.title ?? ''
  const crumbRoot = active.kind === 'index'
    ? t.vectorIndex.title
    : active.kind === 'server-collection'
      ? t.fileLibrary.sectionServerCollections
      : t.fileLibrary.sectionCollections

  function selectActiveTarget(target: ActiveTarget) {
    setActive(target)
    setIsMobileDetailOpen(true)
  }

  return (
    <div className="grid h-[calc(100svh-var(--header-h))] grid-cols-1 bg-background lg:grid-cols-[17rem_minmax(0,1fr)]">
      <input
        className="hidden"
        multiple
        onChange={(event) => {
          const resumeAssetId = resumeUploadAssetIdRef.current
          resumeUploadAssetIdRef.current = null
          const selected = Array.from(event.target.files ?? [])
          if (resumeAssetId && selected[0]) {
            resumeUploadWithFile(resumeAssetId, selected[0])
          } else if (targetRef.current) {
            void ingestInto(selected, targetRef.current)
          }
          event.target.value = ''
        }}
        ref={fileInputRef}
        type="file"
      />

      <Rail
        active={active}
        className={cn('lg:flex', isMobileDetailOpen ? 'hidden' : 'flex')}
        collections={railCollections.map((collection) => ({ count: assetsInSection(collection.id).length, id: collection.id, title: collection.title }))}
        embeddingQuota={embeddingQuota}
        indexes={indexes.map((index) => ({ count: index.members.length, id: index.id, status: index.status, title: index.title }))}
        newIndexBlockedReason={
          knowledgeUnavailableReason === null
            ? null
            : knowledgeUnavailableLabel(knowledgeUnavailableReason, t)
        }
        onDropToCollection={(sectionId, fileId) => moveFile(fileId, sectionId, null)}
        onNewCollection={handleNewCollection}
        onNewIndex={handleNewIndex}
        onQueryChange={setQuery}
        onSelectAll={() => selectActiveTarget({ kind: 'all' })}
        onSelectCollection={(sectionId) => selectActiveTarget({ kind: 'collection', sectionId })}
        onSelectIndex={(indexId) => selectActiveTarget({ indexId, kind: 'index' })}
        onSelectServerCollection={(collectionId) => selectActiveTarget({ collectionId, kind: 'server-collection' })}
        onPrefetchServerCollection={knowledgeSync
          ? (collectionId) => {
              void prefetchServerCollectionDocuments(
                collectionId,
                knowledgeSync,
                deletionScopeKey,
              ).catch(() => undefined)
            }
          : undefined}
        query={query}
        serverCollections={railServerCollections.map((collection) => ({
          access: collection.access,
          count: collection.document_count,
          id: collection.id,
          title: collection.name,
        }))}
        storage={{ collectionCount: railServerCollections.length, docCount: assets.length, indexCount: indexes.length, usedBytes: storageTotalBytes }}
        totalDocCount={assets.length}
      />

      <div className={cn('min-h-0 min-w-0 flex-col lg:flex', isMobileDetailOpen ? 'flex' : 'hidden')}>
        {/* No bottom divider on the workspace header (deliberate, unlike the default
            panel header): the only line per section is the hairline under its section title. */}
        <header className="flex shrink-0 flex-wrap items-center gap-3 px-4 py-3 md:px-6">
          <Button
            aria-label={t.common.back}
            className="size-8 lg:hidden"
            onClick={() => setIsMobileDetailOpen(false)}
            size="icon"
            type="button"
            variant="ghost"
          >
            <ChevronLeft className="size-4" />
          </Button>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 t-meta text-muted-foreground">
              <span>{crumbRoot}</span>
              <ChevronRight className="size-3" />
              <span className="truncate text-foreground">{headerTitle}</span>
            </div>
            {active.kind === 'collection' && activeCollection ? (
              <InlineText
                ariaLabel={t.fileLibrary.renameCollection}
                className="mt-0.5 t-section text-foreground"
                onCommit={(title) => dispatch({ sectionId: activeCollection.id, title, type: 'renameFileLibrarySection' })}
                value={activeCollection.title}
              />
            ) : active.kind === 'index' && activeIndex ? (
              <InlineText
                ariaLabel={t.vectorIndex.rename}
                className="mt-0.5 t-section text-foreground"
                onCommit={(title) => dispatch({ indexId: activeIndex.id, title, type: 'renameVectorIndex' })}
                value={activeIndex.title}
              />
            ) : (
              <h1 className="mt-0.5 truncate t-section text-foreground">{headerTitle}</h1>
            )}
          </div>
          <div className="flex w-full min-w-0 flex-wrap items-center justify-end gap-2 lg:ml-auto lg:w-auto lg:shrink-0">
            {selectionActive ? (
              <>
                {/* Contextual selection bar (replaces the view controls while
                    the mode is active): clear + count,
                    select-all scoped to the visible filter, move, delete. */}
                <Button
                  aria-label={t.fileLibrary.clearSelection}
                  className="size-8"
                  onClick={clearSelection}
                  size="icon"
                  type="button"
                  variant="ghost"
                >
                  <X className="size-4" />
                </Button>
                <span className="t-list font-medium tabular-nums text-foreground">
                  {t.fileLibrary.selectedCount.replace('{count}', String(selectedIds.size))}
                </span>
                {selectedIds.size < visibleAssetIds.length ? (
                  <Button className="gap-1.5" onClick={selectAllVisible} size="sm" type="button" variant="outline">
                    {t.fileLibrary.selectAllVisible.replace('{count}', String(visibleAssetIds.length))}
                  </Button>
                ) : null}
                <BulkMoveMenu
                  disabled={selectedIds.size === 0}
                  label={t.fileLibrary.move}
                  onMoveAll={moveSelected}
                  targets={moveTargets}
                />
                {selectedIds.size > 0 ? (
                  <ConfirmDelete
                    ariaLabel={t.fileLibrary.removeSelected}
                    hint={countLabel(
                      selectedIds.size,
                      t.fileLibrary.removeSelectedHintOne,
                      t.fileLibrary.removeSelectedHint,
                    )}
                    label={`${t.fileLibrary.removeSelected} (${selectedIds.size})`}
                    onConfirm={deleteSelected}
                  />
                ) : null}
              </>
            ) : active.kind !== 'server-collection' ? (
              <>
                {isLibraryView && !isLibraryEmpty ? (
                  <Button
                    className="gap-1.5"
                    onClick={() => setSelectionArmed(true)}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    <ListChecks className="size-4" />
                    {t.fileLibrary.select}
                  </Button>
                ) : null}
                <ViewToggle onChange={setView} value={view} />
                <SortSelect onChange={setSort} value={sort} />
              </>
            ) : null}
            {selectionActive ? null : active.kind === 'index' && activeIndex ? (
              <Button className="gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90" disabled={indexesReadOnly} onClick={() => setPickerIndexId(activeIndex.id)} size="sm" type="button">
                <Plus className="size-4" />
                {t.vectorIndex.addDocuments}
              </Button>
            ) : active.kind === 'server-collection' ? null : (
              <>
                {active.kind === 'collection' && activeCollection ? (
                  <>
                    {(activeCollection.lifecycleStatus ?? 'active') === 'active' ? (
                      <Button
                        className="gap-1.5"
                        onClick={() => dispatch({ sectionId: activeCollection.id, title: t.fileLibrary.newGroupTitle, type: 'createFileGroup' })}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        <FolderOpen className="size-4" />
                        {t.fileLibrary.createGroup}
                      </Button>
                    ) : null}
                    {activeCollection.kind === 'custom' ? (
                      activeCollection.lifecycleStatus === 'deleting' ? (
                        <span
                          className="inline-flex items-center gap-2 rounded-md border border-brand/25 bg-brand-subtle px-3 py-1.5 t-meta text-brand"
                          role="status"
                        >
                          <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand motion-reduce:animate-none" />
                          {t.fileLibrary.collectionDeletionRunning}
                        </span>
                      ) : activeCollection.lifecycleStatus === 'delete_failed'
                        && activeCollection.deletionOperationId ? (
                          <div
                            className="inline-flex items-center gap-2 rounded-md border border-warning/35 bg-warning-subtle px-2 py-1 text-warning"
                            title={activeCollection.deletionError ?? undefined}
                          >
                            <span className="t-meta" role="status">{t.fileLibrary.collectionDeletionFailed}</span>
                            <Button
                              aria-label={t.fileLibrary.collectionDeletionRetry}
                              className="h-7 gap-1.5 text-warning hover:text-warning"
                              onClick={() => {
                                void deletionApi.retry(activeCollection.deletionOperationId as string).catch(() => undefined)
                              }}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              <RotateCcw className="size-3.5" />
                              {t.fileLibrary.collectionDeletionRetry}
                            </Button>
                          </div>
                        ) : (
                          <ConfirmDelete
                            ariaLabel={t.fileLibrary.removeCollection}
                            hint={t.fileLibrary.removeCollectionHint}
                            label={t.fileLibrary.removeCollection}
                            onConfirm={() => {
                              const fileIds = assetsInSection(activeCollection.id).map((asset) => asset.id)
                              fileIds.forEach((fileId) => activeUploadRegistry.delete(fileId))
                              fileIds.forEach((fileId) => uploadNeedsBytesRef.current.delete(fileId))
                              void deletionApi.startSection(activeCollection.id, fileIds).catch(() => undefined)
                            }}
                          />
                        )
                    ) : null}
                  </>
                ) : null}
                <Button
                  className="gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
                  disabled={active.kind === 'collection' && activeCollection?.lifecycleStatus !== undefined
                    && activeCollection.lifecycleStatus !== 'active'}
                  onClick={() => openUpload(activeUploadTarget)}
                  size="sm"
                  type="button"
                >
                  <Upload className="size-4" />
                  {t.fileLibrary.upload}
                </Button>
              </>
            )}
          </div>
        </header>

        {deletionApi.error || deletionApi.observationStopped ? (
          <div
            className="mx-4 mb-3 rounded-md border border-warning/35 bg-warning-subtle px-3 py-2 t-meta text-warning md:mx-6"
            role="status"
          >
            {t.fileLibrary.deletionStatusUnavailable}:{' '}
            {deletionApi.observationStopped
              ? t.common.observationStopped(deletionApi.error)
              : deletionApi.error}
          </div>
        ) : null}

        {/* Local targets are reducer snapshots and switch directly. The only
            genuinely asynchronous target, a server collection, owns a delayed
            body fallback inside ServerCollectionPanel. */}
        {active.kind === 'server-collection' && activeServerCollection && knowledgeSync ? (
          <ServerCollectionPanel
            assets={assets}
            cacheScopeKey={deletionScopeKey}
            collection={activeServerCollection}
            deletionOperations={deletionApi.operations}
            ensureAssetBodiesLoaded={ensureAssetBodiesLoaded}
            groups={groups}
            job={serverJobs[activeServerCollection.id] ?? null}
            knowledgeSync={knowledgeSync}
            onCancelReindex={async (jobId) => {
              setServerJobs((current) => ({
                ...current,
                [activeServerCollection.id]: {
                  ...current[activeServerCollection.id],
                  status: 'cancelling',
                },
              }))
              await cancelReindex(jobId)
            }}
            onCollectionDeleted={() => {
              const localIndexId = localIndexIdForCollection(activeServerCollection.id)
              if (localIndexId && assetDeletionApiOptions) {
                selectActiveTarget({ indexId: localIndexId, kind: 'index' })
                deleteIndex(localIndexId)
              } else {
                selectActiveTarget({ kind: 'all' })
              }
            }}
            onCollectionMutated={() => void onRefreshServerCollections?.()}
            onDeleteCollection={deletionApi.startKnowledgeCollection}
            onDeleteDocument={deletionApi.startKnowledgeDocument}
            onRetryDeletion={deletionApi.retry}
            onResumeReindex={async (jobId) => {
              await requestReindexRecovery(jobId, 'resume')
            }}
            onResumeRawReindex={async (jobId) => {
              await requestReindexRecovery(jobId, 'raw')
            }}
            onShare={onShareServerCollection}
            onStartReindex={async (collectionId) => {
              await startReindex(collectionId)
            }}
            query={query}
            recoveryPending={
              serverJobs[activeServerCollection.id]
                ? reindexRecoveryPending[serverJobs[activeServerCollection.id].jobId] ?? null
                : null
            }
            refreshToken={serverCollectionsRefreshToken}
            sections={railCollections}
          />
        ) : active.kind === 'index' && activeIndex ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* Sticky sub-header: the IndexBar stays put; only the member list
                below scrolls. Kept OUTSIDE the ScrollArea (shrink-0), matching the
                header/scroll idiom in KnowledgeSourcePanel and ChatWorkspace. The
                pt-0.5 aligns the card's top border with the sidebar "Im Index
                suchen" field: the workspace header is measured 2px shorter than
                the rail header, so this 2px (same fine-tune as the header's
                mt-0.5) makes the two top borders meet. */}
            <div className="shrink-0 px-4 pt-0.5 md:px-6">
              <IndexBar
                actionError={indexActionError?.indexId === activeIndex.id ? indexActionError.message : null}
                deleting={deletingIndexIds.has(activeIndex.id)}
                embedModels={embedModels}
                embeddingQuota={embeddingQuota}
                index={activeIndex}
                live={state.indexingJobs[activeIndex.id] ?? null}
                members={allIndexMembers}
                onCancel={handleCancelReindex}
                onDelete={deleteIndex}
                onModel={(indexId, model: EmbedModelId) => {
                  const descriptor = embedModels.find((entry) => entry.id === model)
                  dispatch({ dims: descriptor?.dims, indexId, model, type: 'setVectorIndexModel' })
                }}
                onOpenServerCollection={
                  activeIndexCollection
                    ? () => setActive({
                        collectionId: activeIndexCollection.id,
                        fromIndexId: activeIndex.id,
                        kind: 'server-collection',
                      })
                    : undefined
                }
                onReindex={triggerReindex}
                onRetryDelete={
                  indexActionError?.indexId === activeIndex.id
                    && indexActionError.operationId
                    ? () => {
                        const operationId = indexActionError.operationId!
                        setDeletingIndexIds((current) => new Set(current).add(activeIndex.id))
                        setIndexActionError(null)
                        void deletionApi.retry(operationId).catch((error: unknown) => {
                          setIndexActionError({
                            indexId: activeIndex.id,
                            message: error instanceof Error ? error.message : String(error),
                            operationId,
                          })
                        })
                      }
                    : undefined
                }
                onResume={handleResumeReindex}
                onResumeRaw={handleResumeRawReindex}
                recoveryPending={
                  state.indexingJobs[activeIndex.id]
                    ? reindexRecoveryPending[state.indexingJobs[activeIndex.id].jobId] ?? null
                    : null
                }
                onShare={
                  activeIndexCollection && activeIndexCollection.access.mode === 'owner' && onShareServerCollection
                    ? () => onShareServerCollection(activeIndexCollection)
                    : undefined
                }
                serverBacked={knowledgeSync !== null}
                unavailableReason={knowledgeUnavailableReason}
                contextualRetrievalEnabled={contextualRetrievalEnabled}
                serverFeatureLabels={serverFeatureLabels}
              />
            </div>
            <ScrollArea className="min-h-0 flex-1">
              <div className="flex flex-col gap-4 px-4 pb-4 pt-4 md:px-6 md:pb-6">
                {pickerIndexId === activeIndex.id ? (
                  <AddDocsPanel
                    docs={assets}
                    groups={groups}
                    memberIds={memberIds}
                    onAdd={(fileIds) => {
                      dispatch({ fileIds, indexId: activeIndex.id, type: 'addDocsToVectorIndex' })
                      setPickerIndexId(null)
                    }}
                    onClose={() => setPickerIndexId(null)}
                    onUpload={() => openUpload({ groupId: null, indexId: activeIndex.id, sectionId: temporarySectionId })}
                    sections={railCollections}
                  />
                ) : null}
                {allIndexMembers.length === 0 ? (
                  <IndexEmpty disabled={indexesReadOnly} onAdd={() => setPickerIndexId(activeIndex.id)} />
                ) : indexMembers.length === 0 ? (
                  <EmptyState
                    onUpload={() => openUpload({ groupId: null, indexId: activeIndex.id, sectionId: temporarySectionId })}
                    searching
                  />
                ) : view === 'list' ? (
                  <div className="min-w-0 overflow-x-auto">
                    <div className="min-w-[54rem]">
                      <div className="flex flex-col">
                        {indexMembers.map(({ asset, member }) => (
                          <FileRow
                            asset={asset}
                            contextOmittedChunks={member.serverDocumentId
                              ? indexContextOmissions.get(member.serverDocumentId)
                              : undefined}
                            inRun={memberInRun(asset.id)}
                            indexRemoval={indexRemovalFor(
                              activeIndex.id,
                              asset.id,
                              member.serverDocumentId,
                            )}
                            indexing={activeIndex.status === 'indexing'}
                            jobProgress={memberJobProgress(asset.id)}
                            key={asset.id}
                            liveProgress={memberLiveProgress(asset.id)}
                            memberState={member.state}
                            mode="index"
                            onIndexMember={activeIndexCanIncrementalIndex ? (fileId) => triggerReindex(activeIndex.id, fileId) : undefined}
                            onRemoveFromIndex={removeMember}
                            onRetryIndexRemoval={() => retryMemberRemoval(
                              asset.id,
                              member.serverDocumentId,
                            )}
                            source={sectionTitle(asset.sectionId)}
                          />
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="grid grid-cols-[repeat(auto-fill,minmax(210px,1fr))] gap-2.5">
                    {indexMembers.map(({ asset, member }) => (
                      <FileCard
                        asset={asset}
                        contextOmittedChunks={member.serverDocumentId
                          ? indexContextOmissions.get(member.serverDocumentId)
                          : undefined}
                        inRun={memberInRun(asset.id)}
                        indexRemoval={indexRemovalFor(
                          activeIndex.id,
                          asset.id,
                          member.serverDocumentId,
                        )}
                        indexing={activeIndex.status === 'indexing'}
                        jobProgress={memberJobProgress(asset.id)}
                        key={asset.id}
                        liveProgress={memberLiveProgress(asset.id)}
                        memberState={member.state}
                        mode="index"
                        onIndexMember={activeIndexCanIncrementalIndex ? (fileId) => triggerReindex(activeIndex.id, fileId) : undefined}
                        onRemoveFromIndex={removeMember}
                        onRetryIndexRemoval={() => retryMemberRemoval(
                          asset.id,
                          member.serverDocumentId,
                        )}
                        source={sectionTitle(asset.sectionId)}
                      />
                    ))}
                  </div>
                )}
              </div>
            </ScrollArea>
          </div>
        ) : (
          <ScrollArea className="min-h-0 flex-1">
            <div className="flex flex-col gap-4 p-4 md:p-6">
              <Dropzone
                label={t.fileLibrary.dropFiles}
                onFiles={(files) => void ingestInto(files, activeUploadTarget)}
              >
                {isLibraryEmpty ? (
                  <EmptyState
                    onUpload={() => openUpload(activeUploadTarget)}
                    searching={Boolean(q)}
                  />
                ) : (
                  <div className="flex flex-col">
                    {view === 'list' ? (
                      <div className="min-w-0 overflow-x-auto">
                        <div className="min-w-[54rem]">
                          <div className="flex flex-col gap-1.5">
                            {blocks.map((block) => {
                              const key = block.band ? block.key : `${block.key}:list`
                              const drop = block.band
                                ? dropProps(block.key, block.band.sectionId, block.band.groupId)
                                : active.kind === 'collection' && !q
                                  ? dropProps(block.key, active.sectionId, null)
                                  : null
                              return (
                                <section key={key}>
                                  {block.band ? (
                                    <BandHeader
                                      band={block.band}
                                      dropOver={drop?.dropOver ?? false}
                                      onDeleteGroup={(groupId) => {
                                        void deletionApi.startGroup(groupId).catch(() => undefined)
                                      }}
                                      onDragLeave={drop?.onDragLeave ?? (() => undefined)}
                                      onDragOver={drop?.onDragOver ?? (() => undefined)}
                                      onDrop={drop?.onDrop ?? (() => undefined)}
                                      onNavigate={active.kind === 'all' ? () => selectActiveTarget({ kind: 'collection', sectionId: block.band!.sectionId }) : undefined}
                                      onRenameGroup={(groupId, title) => dispatch({ groupId, title, type: 'renameFileGroup' })}
                                      onRetryGroup={(operationId) => {
                                        void deletionApi.retry(operationId).catch(() => undefined)
                                      }}
                                      onToggleSelectAll={selectionActive ? () => toggleBandSelection(block.items) : undefined}
                                      onUpload={() => openUpload({ groupId: block.band!.groupId, sectionId: block.band!.sectionId })}
                                      selectionState={selectionActive ? bandSelectionState(block.items) : undefined}
                                    />
                                  ) : null}
                                  {block.items.length === 0 ? (
                                    <p className="px-2 py-3 t-meta-sm text-muted-foreground">{t.fileLibrary.emptyGroup}</p>
                                  ) : (
                                    <div
                                      className={cn('flex flex-col transition-colors', !block.band && drop?.dropOver && 'rounded-md bg-brand-subtle/40 ring-1 ring-brand/25')}
                                      onDragLeave={!block.band ? drop?.onDragLeave : undefined}
                                      onDragOver={!block.band ? drop?.onDragOver : undefined}
                                      onDrop={!block.band ? drop?.onDrop : undefined}
                                    >
                                      {block.items.map((asset) => (
                                        <FileRow
                                          asset={asset}
                                          breadcrumb={block.breadcrumb ? breadcrumbFor(asset) : null}
                                          key={asset.id}
                                          mode="library"
                                          referenceCount={referenceCounts.get(asset.id) ?? 0}
                                          selected={selectedIds.has(asset.id)}
                                          source={breadcrumbFor(asset)}
                                          {...rowCallbacks}
                                        />
                                      ))}
                                    </div>
                                  )}
                                </section>
                              )
                            })}
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div className="flex flex-col gap-5">
                        {blocks.map((block) => {
                          const key = block.band ? block.key : `${block.key}:list`
                          const drop = block.band
                            ? dropProps(block.key, block.band.sectionId, block.band.groupId)
                            : active.kind === 'collection' && !q
                              ? dropProps(block.key, active.sectionId, null)
                              : null
                          return (
                            <section key={key}>
                              {block.band ? (
                                <BandHeader
                                  band={block.band}
                                  dropOver={drop?.dropOver ?? false}
                                  onDeleteGroup={(groupId) => {
                                    void deletionApi.startGroup(groupId).catch(() => undefined)
                                  }}
                                  onDragLeave={drop?.onDragLeave ?? (() => undefined)}
                                  onDragOver={drop?.onDragOver ?? (() => undefined)}
                                  onDrop={drop?.onDrop ?? (() => undefined)}
                                  onNavigate={active.kind === 'all' ? () => selectActiveTarget({ kind: 'collection', sectionId: block.band!.sectionId }) : undefined}
                                  onRenameGroup={(groupId, title) => dispatch({ groupId, title, type: 'renameFileGroup' })}
                                  onRetryGroup={(operationId) => {
                                    void deletionApi.retry(operationId).catch(() => undefined)
                                  }}
                                  onToggleSelectAll={selectionActive ? () => toggleBandSelection(block.items) : undefined}
                                  onUpload={() => openUpload({ groupId: block.band!.groupId, sectionId: block.band!.sectionId })}
                                  selectionState={selectionActive ? bandSelectionState(block.items) : undefined}
                                />
                              ) : null}
                              {block.items.length === 0 ? (
                                <p className="px-2 py-3 t-meta-sm text-muted-foreground">{t.fileLibrary.emptyGroup}</p>
                              ) : (
                                <div
                                  className="grid grid-cols-[repeat(auto-fill,minmax(210px,1fr))] gap-2.5"
                                  onDragLeave={!block.band ? drop?.onDragLeave : undefined}
                                  onDragOver={!block.band ? drop?.onDragOver : undefined}
                                  onDrop={!block.band ? drop?.onDrop : undefined}
                                >
                                  {block.items.map((asset) => (
                                    <FileCard
                                      asset={asset}
                                      breadcrumb={block.breadcrumb ? breadcrumbFor(asset) : null}
                                      key={asset.id}
                                      mode="library"
                                      referenceCount={referenceCounts.get(asset.id) ?? 0}
                                      selected={selectedIds.has(asset.id)}
                                      {...rowCallbacks}
                                    />
                                  ))}
                                </div>
                              )}
                            </section>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}
              </Dropzone>
            </div>
          </ScrollArea>
        )}
      </div>
      {previewAsset ? (
        <FilePreviewPanel
          asset={previewAsset}
          onClose={() => setPreviewAssetId(null)}
          options={previewOptions}
        />
      ) : null}
    </div>
  )
}
