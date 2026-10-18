import { BadgeCheck, FileSearch, FileText } from '@/components/icons'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { citationKey, type CitationDocumentGroup, type CitationView } from './citations'

/**
 * One citation row: the supporting passage (primary) over a quiet metadata line
 * (filename + section + verified badge). Shared by the answer's flat source list
 * and the panel's grouped Belege list — one citation looks identical wherever it
 * appears. `showTitle` is off inside a document group (the title is the header).
 */
export function CitationRow({
  active = false,
  onOpen,
  showTitle = true,
  view,
}: {
  active?: boolean
  onOpen: (view: CitationView) => void
  showTitle?: boolean
  view: CitationView
}) {
  const { t } = useLocale()
  const pageLabel = view.pageNumber != null
    ? t.knowledge.citationPage.replace('{n}', String(view.pageNumber))
    : null
  const meta = [showTitle ? view.title : null, view.sectionLabel, pageLabel].filter(Boolean).join(' · ')
  return (
    <button
      className={cn(
        'flex w-full min-w-0 items-start gap-2 rounded-md px-1.5 py-1 text-left transition-colors',
        view.canOpen ? 'group hover:bg-accent/60' : 'cursor-default',
        active && 'bg-brand-subtle/60',
      )}
      disabled={!view.canOpen}
      onClick={() => view.canOpen && onOpen(view)}
      title={view.canOpen ? t.knowledge.openReference : undefined}
      type="button"
    >
      <span className="t-mono mt-0.5 shrink-0 rounded bg-surface px-1 py-0.5 text-muted-foreground transition-colors group-hover:bg-brand-subtle group-hover:text-brand">
        {view.label}
      </span>
      <span className="min-w-0 flex-1">
        {view.snippet ? (
          <span className="line-clamp-2 t-meta text-foreground/90">{view.snippet}</span>
        ) : (
          <span className="block truncate t-list text-foreground">{view.title}</span>
        )}
        {meta ? (
          <span className="mt-0.5 flex min-w-0 items-center gap-1 t-meta-sm text-muted-foreground">
            {showTitle ? <FileSearch className="icon-xs shrink-0 text-muted-foreground/70" /> : null}
            <span className="min-w-0 truncate">{meta}</span>
            {view.verified ? <VerifiedBadge /> : null}
          </span>
        ) : view.verified ? (
          <span className="mt-0.5 flex"><VerifiedBadge /></span>
        ) : null}
      </span>
    </button>
  )
}

function VerifiedBadge() {
  const { t } = useLocale()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 items-center gap-0.5 text-success">
          <BadgeCheck className="icon-xs" />
          <span className="t-hint font-medium">{t.knowledge.viewerVerified}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top">{t.knowledge.viewerVerifiedTooltip}</TooltipContent>
    </Tooltip>
  )
}

/**
 * Citations grouped by document (panel Belege list): each document's title is
 * shown once as a header, its cited passages nested beneath — so several chunks
 * of the same PDF read as one source with N passages, not N identical filenames.
 *
 * When `onOpenDocument` is supplied, the document header is clickable (opens the
 * document at its first cited passage) — the document-centric reader entry point
 * shared by the answer's source list and the panel. `activeDocumentId` marks the
 * header of the document currently open in the reader.
 */
export function CitationGroupList({
  activeDocumentId = null,
  activeKey,
  groups,
  onOpen,
  onOpenDocument,
}: {
  activeDocumentId?: string | null
  activeKey: string | null
  groups: CitationDocumentGroup[]
  onOpen: (view: CitationView) => void
  onOpenDocument?: (group: CitationDocumentGroup) => void
}) {
  return (
    <ul className="space-y-2">
      {groups.map((group) => (
        <li key={group.documentId ?? `title:${group.title}`}>
          <DocumentGroupHeader
            active={activeDocumentId != null && group.documentId === activeDocumentId}
            group={group}
            onOpenDocument={onOpenDocument}
          />
          <ul className="space-y-px border-l border-border/60 pl-1.5">
            {group.citations.map((view) => (
              <li key={`${view.label}-${view.reference.url}`}>
                <CitationRow
                  active={activeKey === citationKey(view.documentId, view.reference.chunkIndex)}
                  onOpen={onOpen}
                  showTitle={false}
                  view={view}
                />
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  )
}

/** Document title row of a citation group. A plain label by default; a button
 * (styled like a `CitationRow`) when it can open the document. */
function DocumentGroupHeader({
  active,
  group,
  onOpenDocument,
}: {
  active: boolean
  group: CitationDocumentGroup
  onOpenDocument?: (group: CitationDocumentGroup) => void
}) {
  const { t } = useLocale()
  const interactive = Boolean(onOpenDocument) && group.documentId != null
  const content = (
    <>
      <FileText className="icon-sm shrink-0 text-muted-foreground/70" />
      <span className="min-w-0 truncate t-list font-medium text-foreground">{group.title}</span>
      {group.citations.length > 1 ? (
        <span className="shrink-0 t-hint tabular-nums text-muted-foreground">
          {group.citations.length}
        </span>
      ) : null}
    </>
  )
  if (!interactive) {
    return <div className="flex min-w-0 items-center gap-1.5 px-1.5 py-0.5">{content}</div>
  }
  return (
    <button
      className={cn(
        'flex min-h-6 w-full min-w-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-left transition-colors hover:bg-accent/60',
        active && 'bg-brand-subtle/60',
      )}
      onClick={() => onOpenDocument?.(group)}
      title={t.knowledge.openReference}
      type="button"
    >
      {content}
    </button>
  )
}
