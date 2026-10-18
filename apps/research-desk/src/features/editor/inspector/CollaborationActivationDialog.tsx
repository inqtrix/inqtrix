import { LoaderCircle, Share2 } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'

const copy = {
  de: {
    activate: 'Aktivieren und teilen',
    body: 'Das Dokument wird dauerhaft auf Zusammenarbeit umgestellt. Diese Änderung kann nicht rückgängig gemacht werden.',
    cancel: 'Abbrechen',
    conflict: 'Das Dokument wurde gleichzeitig geändert — bitte erneut versuchen.',
    dirty: 'Speichern Sie das Dokument vor dem Teilen.',
    error: 'Die Zusammenarbeit konnte nicht aktiviert werden.',
    share: 'Teilen',
    title: 'Zusammenarbeit aktivieren',
    unavailable: 'Teilen ist in dieser Ansicht noch nicht verfügbar.',
    unavailableDocument: 'Das Dokument ist noch nicht auf dem Server verfügbar.',
  },
  en: {
    activate: 'Enable and share',
    body: 'The document will permanently switch to collaboration. This change cannot be undone.',
    cancel: 'Cancel',
    conflict: 'The document was changed concurrently — please try again.',
    dirty: 'Save the document before sharing.',
    error: 'Collaboration could not be enabled.',
    share: 'Share',
    title: 'Enable collaboration',
    unavailable: 'Sharing is not available in this view yet.',
    unavailableDocument: 'The document is not available on the server yet.',
  },
} as const

export function EditorDocumentShareButton({
  dirty,
  onClick,
  owner,
  serverReady,
  sharingAvailable,
}: {
  dirty: boolean
  onClick: () => void
  owner: boolean
  serverReady: boolean
  sharingAvailable: boolean
}) {
  const { locale } = useLocale()
  const labels = copy[locale]
  if (!owner) return null
  const disabled = dirty || !serverReady || !sharingAvailable
  const tooltip = dirty
    ? labels.dirty
    : !serverReady
      ? labels.unavailableDocument
      : !sharingAvailable
        ? labels.unavailable
        : labels.share
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex" tabIndex={disabled ? 0 : undefined}>
          <Button
            aria-label={labels.share}
            className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
            disabled={disabled}
            onClick={onClick}
            size="icon"
            type="button"
            variant="ghost"
          >
            <Share2 className="icon-sm" />
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  )
}

export function CollaborationActivationDialog({
  error,
  onCancel,
  onConfirm,
  open,
  pending,
}: {
  error: string | null
  onCancel: () => void
  onConfirm: () => void
  open: boolean
  pending: boolean
}) {
  const { locale } = useLocale()
  const labels = copy[locale]
  return (
    <Dialog
      closeLabel={labels.cancel}
      description={labels.body}
      dismissable={!pending}
      footer={(
        <>
          <Button disabled={pending} onClick={onCancel} size="sm" type="button" variant="outline">
            {labels.cancel}
          </Button>
          <Button disabled={pending} onClick={onConfirm} size="sm" type="button">
            {pending ? <LoaderCircle className="animate-spin" /> : <Share2 />}
            {labels.activate}
          </Button>
        </>
      )}
      onClose={onCancel}
      open={open}
      title={labels.title}
    >
      {error ? <p className="t-meta text-destructive" role="alert">{error || labels.error}</p> : null}
    </Dialog>
  )
}

export function collaborationActivationFallbackError(locale: 'de' | 'en'): string {
  return copy[locale].error
}

/** Message for a flush that lost against a concurrent writer (retryable). */
export function collaborationActivationConflictError(locale: 'de' | 'en'): string {
  return copy[locale].conflict
}
