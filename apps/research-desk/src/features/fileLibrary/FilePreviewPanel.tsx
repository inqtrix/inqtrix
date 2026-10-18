import { useEffect } from 'react'
import { FileText, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import type { ClientOptions } from '@/api/inqtrixClient'
import type { FileAssetRecord } from '@/features/project/types'
import {
  FilePreviewBody,
  FilePreviewTabSwitch,
  useFilePreviewTabs,
} from '@/features/files/FilePreviewBody'

/**
 * Right-hand overlay that previews one library file: the transformed Markdown
 * (rendered with the app's report renderer, the same styling as chat/reports)
 * and — when the original is server-managed — the original PDF via react-pdf.
 *
 * Same overlay idiom as the knowledge `DocumentViewer`. The body/tab
 * mechanics live in the shared `FilePreviewBody` (also hosted by the agent
 * canvas file view); this panel owns only the overlay chrome.
 */
export function FilePreviewPanel({
  asset,
  onClose,
  options,
}: {
  asset: FileAssetRecord
  onClose: () => void
  /** Server client options, or null when no server is connected (local-only). */
  options: ClientOptions | null
}) {
  const { t } = useLocale()
  const { originalOpened, switchTab, tab } = useFilePreviewTabs()

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const canShowOriginal = Boolean(asset.serverFileId && options)
  const title = asset.label || asset.fileName || t.filePreview.title

  return (
    <div aria-modal="true" className="fixed inset-0 z-50" role="dialog">
      <ResizablePanelGroup className="h-full w-full" orientation="horizontal">
        <ResizablePanel defaultSize="58%" minSize="20%">
          <button
            aria-label={t.filePreview.close}
            className="h-full w-full cursor-default bg-background/60 backdrop-blur-sm"
            onClick={onClose}
            type="button"
          />
        </ResizablePanel>
        <ResizableHandle aria-label={t.filePreview.resize} />
        <ResizablePanel
          className="min-h-0 min-w-0 overflow-hidden"
          defaultSize="42%"
          maxSize="80%"
          minSize="30%"
        >
          <aside className="flex h-full w-full flex-col border-l border-border bg-background shadow-[0_8px_28px_-12px_var(--shadow-soft)]">
            <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
          <FileText className="icon-md shrink-0 text-foreground/80" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate t-section text-foreground">{title}</h2>
            <p className="truncate t-meta text-muted-foreground">{asset.fileName}</p>
          </div>

          <FilePreviewTabSwitch
            canShowOriginal={canShowOriginal}
            onSwitch={switchTab}
            tab={tab}
          />

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.filePreview.close}
                className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
                onClick={onClose}
                size="icon"
                type="button"
                variant="ghost"
              >
                <X className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t.filePreview.close}</TooltipContent>
          </Tooltip>
        </header>

            <FilePreviewBody
              asset={asset}
              options={options}
              originalOpened={originalOpened}
              tab={tab}
            />
          </aside>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}
