import { useCallback, useEffect, useState } from 'react'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { fetchServerFileContent, getAsset, type ClientOptions } from '@/api/inqtrixClient'
import type { FileAssetRecord } from '@/features/project/types'
import { OriginalFileTab } from '@/features/files/OriginalFileTab'

export type FilePreviewTab = 'markdown' | 'original'

type BodyState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; text: string }

/**
 * Tab state for a file preview: the Original tab lazily mounts on first
 * visit and then stays mounted (`hidden`-toggled) so the PDF is not
 * re-fetched on every switch.
 */
export function useFilePreviewTabs() {
  const [tab, setTab] = useState<FilePreviewTab>('markdown')
  const [originalOpened, setOriginalOpened] = useState(false)
  const switchTab = useCallback((next: FilePreviewTab) => {
    setTab(next)
    if (next === 'original') setOriginalOpened(true)
  }, [])
  return { originalOpened, switchTab, tab }
}

/** The Markdown/Original segmented control of a file preview header. */
export function FilePreviewTabSwitch({
  canShowOriginal,
  onSwitch,
  tab,
}: {
  canShowOriginal: boolean
  onSwitch: (tab: FilePreviewTab) => void
  tab: FilePreviewTab
}) {
  const { t } = useLocale()
  return (
    <div className="grid h-7 shrink-0 grid-cols-2 rounded-md bg-surface p-0.5">
      {(['markdown', 'original'] as const).map((tabKey) => {
        const isActive = tab === tabKey
        const isDisabled = tabKey === 'original' && !canShowOriginal
        return (
          <button
            aria-pressed={isActive}
            className={cn(
              'inline-flex items-center justify-center rounded px-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40',
              isActive
                ? 'bg-background text-foreground shadow-[0_1px_2px_var(--shadow-hairline)]'
                : 'text-muted-foreground hover:text-foreground',
            )}
            disabled={isDisabled}
            key={tabKey}
            onClick={() => onSwitch(tabKey)}
            title={isDisabled ? t.filePreview.originalUnavailable : undefined}
            type="button"
          >
            {tabKey === 'markdown' ? t.filePreview.tabMarkdown : t.filePreview.tabOriginal}
          </button>
        )
      })}
    </div>
  )
}

/**
 * The preview body of one library file: the transformed Markdown (report
 * renderer) plus — when the original is server-managed — the original PDF
 * via the shared `OriginalFileTab`. Chrome-free so both the file-library
 * overlay panel and the agent canvas file view can host it. The Markdown
 * body comes from the local record when present and is otherwise fetched
 * on open (`getAsset`, the load-on-use pattern).
 */
export function FilePreviewBody({
  asset,
  options,
  originalOpened,
  tab,
}: {
  asset: FileAssetRecord
  /** Server client options, or null when no server is connected. */
  options: ClientOptions | null
  originalOpened: boolean
  tab: FilePreviewTab
}) {
  const { t } = useLocale()
  const localText = asset.extractedText
  const [body, setBody] = useState<BodyState>(
    localText.trim() ? { kind: 'ready', text: localText } : { kind: 'loading' },
  )

  useEffect(() => {
    if (localText.trim()) {
      setBody({ kind: 'ready', text: localText })
      return undefined
    }
    if (!options) {
      // Local-only asset with no server to fetch from: nothing more to show.
      setBody({ kind: 'ready', text: '' })
      return undefined
    }
    let cancelled = false
    setBody({ kind: 'loading' })
    getAsset(asset.id, options)
      .then((detail) => {
        if (!cancelled) setBody({ kind: 'ready', text: detail.extracted_text ?? '' })
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setBody({
            kind: 'error',
            message: error instanceof Error ? error.message : t.filePreview.markdownError,
          })
        }
      })
    return () => {
      cancelled = true
    }
  }, [asset.id, localText, options, t])

  const canShowOriginal = Boolean(asset.serverFileId && options)
  const serverFileId = asset.serverFileId
  const loadOriginal = useCallback((): Promise<{ blob: Blob; contentType: string }> => {
    if (!serverFileId || !options) {
      return Promise.reject(new Error(t.filePreview.originalUnavailable))
    }
    return fetchServerFileContent(serverFileId, options)
  }, [options, serverFileId, t])

  return (
    <>
      <ScrollArea className={cn('min-h-0 flex-1', tab !== 'markdown' && 'hidden')}>
        <div className="w-full px-6 py-6">
          {body.kind === 'loading' && (
            <p className="t-hint text-muted-foreground">{t.filePreview.markdownLoading}</p>
          )}
          {body.kind === 'error' && (
            <p className="t-meta text-destructive">{body.message}</p>
          )}
          {body.kind === 'ready' && (
            body.text.trim() ? (
              <div className="w-full min-w-0 max-w-full [overflow-wrap:anywhere]">
                <MarkdownRenderer markdown={body.text} variant="report" />
              </div>
            ) : (
              <div className="space-y-1">
                <p className="t-hint text-muted-foreground">{t.filePreview.markdownEmpty}</p>
                {asset.parseWarning ? (
                  <p className="t-meta text-warning">{asset.parseWarning}</p>
                ) : null}
              </div>
            )
          )}
        </div>
      </ScrollArea>

      {originalOpened && canShowOriginal && (
        <div className={cn('flex min-h-0 flex-1 flex-col', tab !== 'original' && 'hidden')}>
          <OriginalFileTab fileName={asset.fileName} load={loadOriginal} />
        </div>
      )}
    </>
  )
}
