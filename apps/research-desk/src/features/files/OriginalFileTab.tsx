import { lazy, Suspense, useEffect, useState } from 'react'
import { Download, LoaderCircle } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { useLocale } from '@/i18n/LocaleProvider'

// Lazy so react-pdf + pdfjs-dist load only when an original PDF is actually
// opened, instead of weighing down the initial bundle (they are large, and most
// sessions never open the Original tab).
const PdfViewer = lazy(() =>
  import('./PdfViewer').then((module) => ({ default: module.PdfViewer })),
)

type LoadResult = { blob: Blob; contentType: string }

type State =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; contentType: string; objectUrl: string }

/**
 * Shared "Original" tab for the file-library preview and the knowledge document
 * viewer. Fetches the original bytes via `load` and builds a blob object URL (a
 * canvas/iframe cannot carry the Bearer/cookie auth a direct GET needs), then
 * shows the PDF in `PdfViewer` or a download button for non-PDF types. The
 * object URL is revoked on unmount / reload.
 *
 * `load` must be stable (memoized by the caller): its identity drives the
 * (re)fetch, so an unstable closure would refetch on every render.
 */
export function OriginalFileTab({
  fileName,
  highlightPage = null,
  highlightTargets,
  load,
}: {
  fileName?: string
  /** 1-based page to scroll to + softly highlight (a cited source page). */
  highlightPage?: number | null
  /** Quote-first highlight targets for the cited passage on that page. */
  highlightTargets?: readonly string[]
  load: () => Promise<LoadResult>
}) {
  const { t } = useLocale()
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    let createdUrl: string | null = null
    setState({ kind: 'loading' })
    load()
      .then(({ blob, contentType }) => {
        if (cancelled) return
        createdUrl = URL.createObjectURL(blob)
        setState({ contentType, kind: 'ready', objectUrl: createdUrl })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setState({
          kind: 'error',
          message: error instanceof Error ? error.message : t.pdfViewer.error,
        })
      })
    return () => {
      cancelled = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [load, t])

  if (state.kind === 'loading') {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center gap-2 t-hint text-muted-foreground">
        <LoaderCircle className="size-4 animate-spin" />
        {t.pdfViewer.originalLoading}
      </div>
    )
  }
  if (state.kind === 'error') {
    return <p className="px-6 py-6 t-meta text-destructive">{state.message}</p>
  }
  if (state.contentType.toLowerCase().includes('pdf')) {
    return (
      <Suspense
        fallback={
          <div className="flex min-h-0 flex-1 items-center justify-center gap-2 t-hint text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" />
            {t.pdfViewer.originalLoading}
          </div>
        }
      >
        <PdfViewer
          highlightPage={highlightPage}
          highlightTargets={highlightTargets}
          objectUrl={state.objectUrl}
        />
      </Suspense>
    )
  }
  return (
    <div className="flex flex-col items-start gap-3 px-6 py-6">
      <p className="t-meta text-muted-foreground">{t.pdfViewer.noPreview}</p>
      <Button asChild size="sm" type="button" variant="outline">
        <a download={fileName || undefined} href={state.objectUrl}>
          <Download className="size-4" />
          {t.pdfViewer.download}
        </a>
      </Button>
    </div>
  )
}
