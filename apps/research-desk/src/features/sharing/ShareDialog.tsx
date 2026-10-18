import { Search, X } from '@/components/icons'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { getEditorAccessSummary, searchUsers } from '@/api/inqtrixClient'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { useModalFocusTrap } from '@/components/ui/use-modal-focus-trap'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { searchDemoUsers } from './demoShares'
import {
  personLabel,
  selectableSearchResults,
  sharePermissionLabel,
  sharePermissionsForResource,
  toggleSelectedUser,
} from './shareModel'
import type {
  EditorAccessSummary,
  SharePermissionValue,
  UserSearchResult,
} from './types'
import { useShares } from './useShares'
import { EditorGuestLinksSection } from './EditorGuestLinksSection'

const SEARCH_DEBOUNCE_MS = 250
const MIN_QUERY_LENGTH = 2

type ShareDialogProps = {
  collaborationGeneration?: number
  documentDetails?: {
    createdAt: string
    openCommentCount: number | null
    openSuggestionCount: number | null
    participantCount: number | null
    updatedAt: string
    wordCount: number
  }
  /** Demo mode resolves users + shares from seeded data (no backend). */
  demo?: boolean
  onChanged?: () => void
  onClose: () => void
  ownerEmail: string | null
  ownerName: string | null
  guestLinksEnabled?: boolean
  /** Which tab the dialog opens on. `'access'` (the default) is the sharing
   * intent and focuses the recipient search; details intents focus their
   * active tab while the shared modal contract traps and restores focus. */
  initialTab?: 'access' | 'activity' | 'overview'
  onLeave?: () => Promise<void> | void
  recipientAccess?: {
    ownerId: string
    ownerName: string
    permission: SharePermissionValue
  }
  refreshToken: number
  resourceId: string
  resourceTitle: string
  resourceType: string
  returnFocusTarget?: HTMLElement | null
}

/**
 * Share dialog: typeahead over the user mirror,
 * batch selection as chips with ONE permission picker, and the live
 * access list below (owner pinned, revoke hover-revealed). Server is
 * the only source of truth — every mutation re-reads the listing.
 */
export function ShareDialog({
  collaborationGeneration,
  documentDetails,
  demo = false,
  onChanged,
  onClose,
  ownerEmail,
  ownerName,
  guestLinksEnabled = false,
  initialTab = 'access',
  onLeave,
  recipientAccess,
  refreshToken,
  resourceId,
  resourceTitle,
  resourceType,
  returnFocusTarget,
}: ShareDialogProps) {
  const { locale, t } = useLocale()
  const { grant, reload, revoke, state, updatePermission } = useShares(
    resourceType,
    recipientAccess ? null : resourceId,
    demo,
    refreshToken,
  )
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<readonly UserSearchResult[]>([])
  const [searchError, setSearchError] = useState(false)
  const [searchPending, setSearchPending] = useState(false)
  const [selected, setSelected] = useState<readonly UserSearchResult[]>([])
  const [permission, setPermission] = useState<SharePermissionValue>('view')
  const [submitState, setSubmitState] = useState<'error-grant' | 'error-revoke' | 'error-update' | 'idle' | 'submitting'>('idle')
  const [updatingShareId, setUpdatingShareId] = useState<string | null>(null)
  const [detailsTab, setDetailsTab] = useState<'access' | 'activity' | 'overview'>(initialTab)
  const [accessWindow, setAccessWindow] = useState<'7d' | '30d'>('7d')
  const [accessSummary, setAccessSummary] =
    useState<EditorAccessSummary | null>(null)
  const [accessSummaryStatus, setAccessSummaryStatus] =
    useState<'error' | 'idle' | 'loading' | 'ready'>('idle')
  const [leaveStatus, setLeaveStatus] =
    useState<'error' | 'idle' | 'leaving'>('idle')
  const isEditorDocument = resourceType === 'editor_document'
  const isRecipient = isEditorDocument && recipientAccess !== undefined
  const panelRef = useRef<HTMLElement | null>(null)
  const searchInputRef = useRef<HTMLInputElement | null>(null)
  const overviewTabRef = useRef<HTMLButtonElement | null>(null)
  const accessTabRef = useRef<HTMLButtonElement | null>(null)
  const activityTabRef = useRef<HTMLButtonElement | null>(null)
  const titleId = useId()
  const tabsId = useId()
  const tabRefs = {
    access: accessTabRef,
    activity: activityTabRef,
    overview: overviewTabRef,
  }
  const initialFocusRef = !isEditorDocument
    ? searchInputRef
    : initialTab === 'access' && !isRecipient
      ? searchInputRef
      : tabRefs[initialTab]
  /** A guest secret is shown exactly once and is irreversible when lost, so
   * every dismissal asks first: the close button, the backdrop, AND Escape,
   * which the shared focus trap fires. `submitLeave` is exempt — guest links
   * never render for a recipient, the only person who can leave. */
  const [uncopiedSecret, setUncopiedSecret] = useState(false)
  const closeGuarded = () => {
    if (uncopiedSecret && !window.confirm(locale === 'de'
      ? 'Der Gastlink wurde noch nicht kopiert. Er wird nur einmal angezeigt '
        + 'und ist danach nicht mehr abrufbar. Trotzdem schließen?'
      : 'The guest link has not been copied yet. It is shown only once and '
        + 'cannot be retrieved afterwards. Close anyway?')) {
      return
    }
    onClose()
  }
  useModalFocusTrap({
    initialFocusRef,
    onClose: closeGuarded,
    open: true,
    panelRef,
    returnFocusTarget,
  })
  const permissionOptions = sharePermissionsForResource(resourceType)
  const permissionLabels = {
    edit: t.sharing.permissionEdit,
    view: t.sharing.permissionView,
  }

  const selectDetailsTab = (
    value: 'access' | 'activity' | 'overview',
    focusContent: boolean,
  ) => {
    setDetailsTab(value)
    if (focusContent && value === 'access' && !isRecipient) {
      window.requestAnimationFrame(() => searchInputRef.current?.focus())
    }
  }

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    value: 'access' | 'activity' | 'overview',
  ) => {
    const order = ['overview', 'access', 'activity'] as const
    const current = order.indexOf(value)
    let next: number
    if (event.key === 'ArrowRight') next = (current + 1) % order.length
    else if (event.key === 'ArrowLeft') next = (current - 1 + order.length) % order.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = order.length - 1
    else return
    event.preventDefault()
    const nextTab = order[next]
    setDetailsTab(nextTab)
    tabRefs[nextTab].current?.focus()
  }

  useEffect(() => {
    if (!permissionOptions.includes(permission)) setPermission('view')
  }, [permission, permissionOptions])

  useEffect(() => {
    if (
      !isEditorDocument
      || isRecipient
      || !guestLinksEnabled
      || detailsTab !== 'activity'
      || demo
    ) {
      return
    }
    let active = true
    setAccessSummaryStatus('loading')
    void getEditorAccessSummary(resourceId, accessWindow)
      .then((summary) => {
        if (!active) return
        setAccessSummary(summary)
        setAccessSummaryStatus('ready')
      })
      .catch(() => {
        if (!active) return
        setAccessSummary(null)
        setAccessSummaryStatus('error')
      })
    return () => {
      active = false
    }
  }, [
    accessWindow,
    demo,
    detailsTab,
    guestLinksEnabled,
    isEditorDocument,
    isRecipient,
    refreshToken,
    resourceId,
  ])

  const trimmedQuery = query.trim()
  // Generation guard (same pattern as useShares): a slow response for
  // an EARLIER query must never overwrite the current query's results.
  const searchGenerationRef = useRef(0)
  useEffect(() => {
    const generation = ++searchGenerationRef.current
    if (trimmedQuery.length < MIN_QUERY_LENGTH) {
      setResults([])
      setSearchError(false)
      setSearchPending(false)
      return undefined
    }
    setSearchError(false)
    setSearchPending(true)
    const lookup = demo
      ? (value: string) => Promise.resolve(searchDemoUsers(value))
      : searchUsers
    const timeout = window.setTimeout(() => {
      lookup(trimmedQuery)
        .then((found) => {
          if (searchGenerationRef.current !== generation) return
          setResults(found)
          setSearchError(false)
          setSearchPending(false)
        })
        .catch(() => {
          if (searchGenerationRef.current !== generation) return
          setResults([])
          setSearchError(true)
          setSearchPending(false)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timeout)
  }, [demo, trimmedQuery])

  const existingUserIds = useMemo(() => {
    const userIds = new Set(state.records.map((record) => record.recipient_user_id))
    for (const user of selected) userIds.add(user.id)
    return userIds
  }, [selected, state.records])
  const visibleResults = selectableSearchResults(results, existingUserIds)

  const submitGrant = async () => {
    if (selected.length === 0 || submitState === 'submitting') return
    setSubmitState('submitting')
    try {
      await grant(selected.map((user) => ({ permission, userId: user.id })))
      setSelected([])
      setQuery('')
      setResults([])
      setSubmitState('idle')
      onChanged?.()
    } catch {
      setSubmitState('error-grant')
    }
  }

  const submitRevoke = async (shareId: string) => {
    try {
      await revoke(shareId)
      setSubmitState('idle')
      onChanged?.()
    } catch {
      setSubmitState('error-revoke')
    }
  }

  const submitPermissionUpdate = async (
    shareId: string,
    nextPermission: SharePermissionValue,
    expectedRevision: number,
  ) => {
    setUpdatingShareId(shareId)
    try {
      await updatePermission(shareId, nextPermission, expectedRevision)
      setSubmitState('idle')
      onChanged?.()
    } catch {
      await reload()
      setSubmitState('error-update')
    } finally {
      setUpdatingShareId(null)
    }
  }

  const submitLeave = async () => {
    if (!onLeave || leaveStatus === 'leaving') return
    setLeaveStatus('leaving')
    try {
      await onLeave()
      setLeaveStatus('idle')
      onClose()
    } catch {
      setLeaveStatus('error')
    }
  }

  return (
    <div
      className={cn(
        'fixed inset-0 z-50 flex overflow-y-auto bg-background/75 backdrop-blur',
        isEditorDocument
          ? 'items-stretch justify-end p-0'
          : 'items-start justify-center px-4 py-8',
      )}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeGuarded()
      }}
    >
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className={cn(
          'w-full overflow-hidden border border-border bg-background shadow-xl',
          isEditorDocument
            ? 'h-full max-w-[27rem] border-y-0 border-r-0 sm:w-[27rem]'
            : 'max-w-lg rounded-lg',
        )}
        ref={panelRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="t-section truncate text-foreground" id={titleId}>
              {isEditorDocument
                ? (locale === 'de' ? 'Dokumentdetails' : 'Document details')
                : t.sharing.dialogTitle}
            </h2>
            <p className="truncate t-meta text-muted-foreground">
              {isEditorDocument ? resourceTitle : t.sharing.dialogHint}
            </p>
          </div>
          <button
            aria-label={t.sharing.close}
            className="grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            onClick={closeGuarded}
            type="button"
          >
            <X className="size-4" />
          </button>
        </div>

        {isEditorDocument ? (
          <div
            aria-label={locale === 'de' ? 'Dokumentdetails' : 'Document details'}
            className="grid grid-cols-3 border-b border-border px-3"
            role="tablist"
          >
            {([
              ['overview', locale === 'de' ? 'Übersicht' : 'Overview'],
              ['access', locale === 'de' ? 'Zugriff' : 'Access'],
              ['activity', locale === 'de' ? 'Aktivität' : 'Activity'],
            ] as const).map(([value, label]) => (
              <button
                aria-controls={`${tabsId}-${value}-panel`}
                aria-selected={detailsTab === value}
                className={cn(
                  'relative h-9 t-meta font-medium text-muted-foreground hover:text-foreground',
                  detailsTab === value && 'text-foreground after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-brand',
                )}
                id={`${tabsId}-${value}-tab`}
                key={value}
                onClick={() => selectDetailsTab(value, true)}
                onKeyDown={(event) => handleTabKeyDown(event, value)}
                ref={tabRefs[value]}
                role="tab"
                tabIndex={detailsTab === value ? 0 : -1}
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
        ) : null}

        {isEditorDocument && detailsTab === 'overview' ? (
          <div
            aria-labelledby={`${tabsId}-overview-tab`}
            className="space-y-3 overflow-y-auto px-4 py-4"
            id={`${tabsId}-overview-panel`}
            role="tabpanel"
            tabIndex={0}
          >
            <DetailsFact
              label={locale === 'de' ? 'Eigentümer' : 'Owner'}
              value={recipientAccess?.ownerName
                ?? personLabel(ownerName, ownerEmail, t.sharing.you)}
            />
            <DetailsFact
              label={locale === 'de' ? 'Ablageort' : 'Location'}
              value={isRecipient
                ? (locale === 'de' ? 'Mit mir geteilt' : 'Shared with me')
                : (locale === 'de' ? 'Meine Dokumente' : 'My documents')}
            />
            {documentDetails ? (
              <>
                <DetailsFact
                  label={locale === 'de' ? 'Erstellt' : 'Created'}
                  value={formatDetailsDate(documentDetails.createdAt, locale)}
                />
                <DetailsFact
                  label={locale === 'de' ? 'Geändert' : 'Modified'}
                  value={formatDetailsDate(documentDetails.updatedAt, locale)}
                />
                <DetailsFact
                  label={locale === 'de' ? 'Wörter' : 'Words'}
                  value={new Intl.NumberFormat(locale).format(documentDetails.wordCount)}
                />
                <DetailsFact
                  label={locale === 'de' ? 'Offene Kommentare' : 'Open comments'}
                  value={formatOptionalCount(documentDetails.openCommentCount)}
                />
                <DetailsFact
                  label={locale === 'de' ? 'Offene Vorschläge' : 'Open suggestions'}
                  value={formatOptionalCount(documentDetails.openSuggestionCount)}
                />
                <DetailsFact
                  label={locale === 'de' ? 'Aktive Teilnehmende' : 'Active participants'}
                  value={formatOptionalCount(documentDetails.participantCount)}
                />
              </>
            ) : null}
            {!isRecipient ? (
              <DetailsFact
                label={locale === 'de' ? 'Direkte Freigaben' : 'Direct shares'}
                value={String(state.records.length)}
              />
            ) : null}
            <p className="border-t border-border pt-3 t-meta text-muted-foreground">
              {locale === 'de'
                ? 'Kommentare und Vorschläge bleiben bewusst im Dokument-Inspector; hier verwalten Sie Eigenschaften und Zugriff.'
                : 'Comments and suggestions stay in the document inspector; properties and access are managed here.'}
            </p>
          </div>
        ) : isEditorDocument && detailsTab === 'activity' ? (
          <div
            aria-labelledby={`${tabsId}-activity-tab`}
            className="space-y-4 overflow-y-auto px-4 py-4"
            id={`${tabsId}-activity-panel`}
            role="tabpanel"
            tabIndex={0}
          >
            {isRecipient ? (
              <p className="t-meta text-muted-foreground">
                {locale === 'de'
                  ? 'Detaillierte Zugriffsstatistiken sind nur für den Eigentümer sichtbar.'
                  : 'Detailed access metrics are visible only to the owner.'}
              </p>
            ) : (
              <>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="t-caption text-muted-foreground">
                    {locale === 'de' ? 'Zugriff' : 'Access'}
                  </h3>
                  {guestLinksEnabled ? (
                    <div className="flex rounded-md bg-surface p-0.5">
                      {(['7d', '30d'] as const).map((value) => (
                        <button
                          aria-pressed={accessWindow === value}
                          className={cn(
                            'h-6 rounded px-2 t-meta-sm',
                            accessWindow === value
                              ? 'bg-background text-foreground shadow-sm'
                              : 'text-muted-foreground',
                          )}
                          key={value}
                          onClick={() => setAccessWindow(value)}
                          type="button"
                        >
                          {value === '7d'
                            ? (locale === 'de' ? '7 Tage' : '7 days')
                            : (locale === 'de' ? '30 Tage' : '30 days')}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-border bg-border">
                  <MetricFact
                    label={locale === 'de' ? 'Direkte Freigaben' : 'Direct shares'}
                    value={accessSummary?.direct_share_count ?? state.records.length}
                  />
                  <MetricFact
                    label={locale === 'de' ? 'Aktive Gastlinks' : 'Active guest links'}
                    value={accessSummary?.guest_link_count ?? 0}
                  />
                  <MetricFact
                    label={locale === 'de' ? 'Gastöffnungen' : 'Guest opens'}
                    value={accessSummary?.guest_open_count ?? 0}
                  />
                  <MetricFact
                    label={locale === 'de' ? 'Gastsitzungen' : 'Guest sessions'}
                    value={accessSummary?.guest_session_count ?? 0}
                  />
                </div>
                {accessSummaryStatus === 'loading' ? (
                  <p className="t-meta text-muted-foreground">
                    {locale === 'de' ? 'Zugriff wird geladen …' : 'Loading access …'}
                  </p>
                ) : accessSummaryStatus === 'error' ? (
                  <p className="t-meta text-destructive">
                    {locale === 'de'
                      ? 'Die Gastzugriffsstatistik konnte nicht geladen werden.'
                      : 'Guest access metrics could not be loaded.'}
                  </p>
                ) : accessSummary?.last_guest_accessed_at ? (
                  <p className="t-meta text-muted-foreground">
                    {locale === 'de' ? 'Letzter Gastzugriff' : 'Last guest access'}
                    {' · '}
                    {formatEpochDate(accessSummary.last_guest_accessed_at, locale)}
                  </p>
                ) : null}
                <div>
                  <h3 className="t-caption text-muted-foreground">
                    {locale === 'de' ? 'Letzte Freigaben' : 'Recent shares'}
                  </h3>
                  {state.records.length === 0 ? (
                    <p className="mt-3 t-meta text-muted-foreground">
                      {locale === 'de' ? 'Noch keine Freigabeaktivität.' : 'No sharing activity yet.'}
                    </p>
                  ) : (
                    <ul className="mt-2 divide-y divide-border">
                      {state.records.map((record) => (
                        <li className="py-2.5" key={record.id}>
                          <p className="t-list text-foreground">
                            {personLabel(record.display_name, record.email, record.recipient_user_id)}
                          </p>
                          <p className="t-meta-sm text-muted-foreground">
                            {locale === 'de' ? 'Freigabe erstellt' : 'Share created'}
                            {' · '}
                            {formatEpochDate(record.created_at, locale)}
                          </p>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            )}
          </div>
        ) : (
        <div
          {...(isEditorDocument
            ? {
                'aria-labelledby': `${tabsId}-access-tab`,
                id: `${tabsId}-access-panel`,
                role: 'tabpanel',
                tabIndex: 0,
              }
            : {})}
          className="space-y-3 overflow-y-auto px-4 py-3"
        >
          {isRecipient && recipientAccess ? (
            <div className="space-y-4">
              <div>
                <h3 className="t-caption text-muted-foreground">
                  {t.sharing.peopleAndInvites}
                </h3>
                <ul className="mt-1.5 space-y-0.5">
                  <li className="flex items-center gap-2 rounded-md px-1.5 py-2">
                    <InitialsAvatar
                      displayName={recipientAccess.ownerName}
                      email={null}
                      size="sm"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate t-list text-foreground">
                        {recipientAccess.ownerName}
                      </span>
                      <span className="block truncate t-meta-sm text-muted-foreground">
                        {locale === 'de' ? 'Dokumenteigentümer' : 'Document owner'}
                      </span>
                    </span>
                    <span className="shrink-0 t-meta text-muted-foreground">
                      {t.sharing.owner}
                    </span>
                  </li>
                  <li className="flex items-center gap-2 rounded-md bg-surface/55 px-1.5 py-2">
                    <InitialsAvatar
                      displayName={ownerName}
                      email={ownerEmail}
                      size="sm"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate t-list text-foreground">
                        {locale === 'de' ? 'Sie' : 'You'}
                      </span>
                      <span className="block truncate t-meta-sm text-muted-foreground">
                        {locale === 'de' ? 'Aktive Freigabe' : 'Active share'}
                      </span>
                    </span>
                    <span className="rounded border border-border px-1.5 py-0.5 t-meta-sm text-muted-foreground">
                      {sharePermissionLabel(
                        recipientAccess.permission,
                        locale,
                        permissionLabels,
                      )}
                    </span>
                  </li>
                </ul>
              </div>
              <div className="rounded-md border border-border p-3">
                <p className="t-meta text-muted-foreground">
                  {locale === 'de'
                    ? 'Wenn Sie die Freigabe verlassen, verschwindet das Dokument aus „Mit mir geteilt“. Andere Freigaben bleiben unverändert.'
                    : 'Leaving removes this document from “Shared with me”. Your other shares remain unchanged.'}
                </p>
                <Button
                  className="mt-3"
                  disabled={!onLeave || leaveStatus === 'leaving'}
                  onClick={() => void submitLeave()}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  {leaveStatus === 'leaving'
                    ? (locale === 'de' ? 'Wird verlassen …' : 'Leaving …')
                    : (locale === 'de' ? 'Freigabe verlassen' : 'Leave share')}
                </Button>
                {leaveStatus === 'error' ? (
                  <p className="mt-2 t-meta text-destructive">
                    {locale === 'de'
                      ? 'Die Freigabe konnte nicht verlassen werden.'
                      : 'The share could not be left.'}
                  </p>
                ) : null}
              </div>
            </div>
          ) : (
          <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              className="h-8 w-full rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:border-brand"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t.sharing.searchPlaceholder}
              ref={searchInputRef}
              type="text"
              value={query}
            />
          </div>

          {trimmedQuery.length > 0 && trimmedQuery.length < MIN_QUERY_LENGTH && (
            <p className="t-meta text-muted-foreground">
              {t.sharing.searchMinChars}
            </p>
          )}
          {trimmedQuery.length >= MIN_QUERY_LENGTH && (
            <div className="overflow-hidden rounded-md border border-border">
              {visibleResults.length === 0 ? (
                <p className={cn(
                  'px-3 py-2 t-meta',
                  searchError ? 'text-destructive' : 'text-muted-foreground',
                )}>
                  {searchPending
                    ? '…'
                    : searchError
                      ? t.sharing.searchFailed
                      : t.sharing.searchEmpty}
                </p>
              ) : (
                <ul className="max-h-44 overflow-y-auto">
                  {visibleResults.map((user) => (
                    <li key={user.id}>
                      <button
                        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-accent"
                        onMouseDown={(event) => {
                          event.preventDefault()
                          setSelected((current) => toggleSelectedUser(current, user))
                          setQuery('')
                          setResults([])
                          setSearchError(false)
                        }}
                        type="button"
                      >
                        <InitialsAvatar
                          displayName={user.display_name}
                          email={user.email}
                          size="sm"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate t-list text-foreground">
                          {personLabel(user.display_name, user.email, user.id)}
                          </span>
                          {user.email && (
                            <span className="block truncate t-meta-sm text-muted-foreground">
                              {user.email}
                            </span>
                          )}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {selected.length > 0 && (
            <div className="space-y-2.5">
              <div className="flex flex-wrap items-center gap-1.5">
                {selected.map((user) => (
                  <span
                    className="inline-flex h-6 items-center gap-1.5 rounded-full bg-brand-subtle pl-1 pr-1.5 text-brand"
                    key={user.id}
                  >
                    <InitialsAvatar
                      displayName={user.display_name}
                      email={user.email}
                      size="sm"
                    />
                    <span className="max-w-40 truncate t-meta-sm font-medium">
                      {personLabel(user.display_name, user.email, user.id)}
                    </span>
                    <button
                      aria-label={t.sharing.removeSelected}
                      className="grid size-3.5 place-items-center rounded-full hover:bg-brand/15"
                      onClick={() => setSelected((current) => toggleSelectedUser(current, user))}
                      type="button"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
              <div className="flex items-center justify-between gap-3">
                <div
                  aria-label={t.sharing.permissionLabel}
                  className="flex h-7 items-center gap-0.5 rounded-md bg-surface p-0.5"
                  role="group"
                >
                  {permissionOptions.map((value) => (
                    <button
                      aria-pressed={permission === value}
                      className={cn(
                        'h-6 rounded px-2 text-xs font-medium transition-colors',
                        permission === value
                          ? 'bg-background text-foreground shadow-sm'
                          : 'text-muted-foreground hover:text-foreground',
                      )}
                      key={value}
                      onClick={() => setPermission(value)}
                      type="button"
                    >
                      {sharePermissionLabel(value, locale, permissionLabels)}
                    </button>
                  ))}
                </div>
                <Button
                  className="h-8 bg-brand px-3 text-xs text-brand-foreground hover:bg-brand/90"
                  disabled={submitState === 'submitting'}
                  onClick={() => void submitGrant()}
                  size="sm"
                  type="button"
                >
                  {submitState === 'submitting' ? t.sharing.granting : t.sharing.grantCta}
                </Button>
              </div>
            </div>
          )}

          {submitState === 'error-grant' && (
            <p className="t-meta text-destructive">{t.sharing.shareFailed}</p>
          )}
          {submitState === 'error-revoke' && (
            <p className="t-meta text-destructive">{t.sharing.revokeFailed}</p>
          )}
          {submitState === 'error-update' && (
            <p className="t-meta text-destructive">{t.sharing.updateFailed}</p>
          )}

          <div>
            <h3 className="t-caption text-muted-foreground">
              {t.sharing.peopleAndInvites}
            </h3>
            <ul className="mt-1.5 space-y-0.5">
              <li className="flex items-center gap-2 rounded-md px-1.5 py-1.5">
                <InitialsAvatar displayName={ownerName} email={ownerEmail} size="sm" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate t-list text-foreground">
                    {personLabel(ownerName, ownerEmail, t.sharing.you)}
                  </span>
                  {ownerEmail && (
                    <span className="block truncate t-meta-sm text-muted-foreground">
                      {ownerEmail}
                    </span>
                  )}
                </span>
                <span className="shrink-0 t-meta text-muted-foreground">
                  {t.sharing.owner}
                </span>
              </li>
              {state.status === 'error' ? (
                <li className="px-1.5 py-1.5 t-meta text-destructive">
                  {t.sharing.loadFailed}
                </li>
              ) : state.records.length === 0 && state.status === 'ready' ? (
                <li className="px-1.5 py-1.5 t-meta text-muted-foreground">
                  {t.sharing.noShares}
                </li>
              ) : (
                state.records.map((record) => (
                  <li
                    className="group flex items-center gap-2 rounded-md px-1.5 py-1.5 hover:bg-accent/50"
                    key={record.id}
                  >
                    <InitialsAvatar
                      displayName={record.display_name}
                      email={record.email}
                      size="sm"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate t-list text-foreground">
                        {personLabel(record.display_name, record.email, record.recipient_user_id)}
                      </span>
                      {record.email && (
                        <span className="block truncate t-meta-sm text-muted-foreground">
                          {record.email}
                        </span>
                      )}
                    </span>
                    {record.accepted_at === null ? (
                      <span className="shrink-0 rounded-md border border-warning/25 bg-warning-subtle px-1.5 py-0.5 t-meta-sm font-medium text-warning">
                        {t.sharing.pending}
                      </span>
                    ) : null}
                    <div
                      aria-label={t.sharing.permissionLabel}
                      className="flex h-7 shrink-0 items-center gap-0.5 rounded-md bg-surface p-0.5"
                      role="group"
                    >
                      {permissionOptions.map((value) => (
                        <button
                          aria-pressed={record.permission === value}
                          className={cn(
                            'h-6 rounded px-2 text-xs font-medium transition-colors',
                            record.permission === value
                              ? 'bg-background text-foreground shadow-sm'
                              : 'text-muted-foreground hover:text-foreground',
                          )}
                          disabled={updatingShareId === record.id}
                          key={value}
                          onClick={() => {
                            if (record.permission === value) return
                            void submitPermissionUpdate(record.id, value, record.revision)
                          }}
                          type="button"
                        >
                          {sharePermissionLabel(value, locale, permissionLabels)}
                        </button>
                      ))}
                    </div>
                    <button
                      aria-label={t.sharing.revoke}
                      className="grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                      onClick={() => void submitRevoke(record.id)}
                      title={t.sharing.revoke}
                      type="button"
                    >
                      <X className="size-3.5" />
                    </button>
                  </li>
                ))
              )}
            </ul>
          </div>
          {isEditorDocument
            && !isRecipient
            && guestLinksEnabled
            && collaborationGeneration !== undefined ? (
              <EditorGuestLinksSection
                documentId={resourceId}
                generation={collaborationGeneration}
                locale={locale}
                onChanged={onChanged}
                onUncopiedSecretChange={setUncopiedSecret}
              />
            ) : null}
          </>
          )}
        </div>
        )}
      </section>
    </div>
  )
}

function DetailsFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] items-start gap-3 border-b border-border pb-3 last:border-b-0">
      <span className="t-meta text-muted-foreground">{label}</span>
      <span className="truncate text-right t-list text-foreground">{value}</span>
    </div>
  )
}

function MetricFact({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-background px-3 py-2.5">
      <p className="t-title tabular-nums text-foreground">
        {new Intl.NumberFormat().format(value)}
      </p>
      <p className="t-meta-sm text-muted-foreground">{label}</p>
    </div>
  )
}

function formatOptionalCount(value: number | null): string {
  return value === null ? '—' : new Intl.NumberFormat().format(value)
}

function formatDetailsDate(value: string, locale: 'de' | 'en'): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '—'
    : new Intl.DateTimeFormat(locale, {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(date)
}

function formatEpochDate(value: number, locale: 'de' | 'en'): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value * 1_000))
}
