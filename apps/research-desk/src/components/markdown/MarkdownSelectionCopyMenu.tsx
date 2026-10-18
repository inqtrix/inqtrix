import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import { Check, Code2, Copy, MessageSquarePlus } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { useLocale } from '@/i18n/LocaleProvider'
import { AI_PRODUCER } from '@/lib/aiDisclosure'
import { cn } from '@/lib/utils'
import { markdownForVisibleSelection } from './selectionCopy'
import { copyTextToClipboard } from '@/lib/clipboard'
import { readerError } from '@/api/requestErrorText'

type SelectionMenuState = {
  markdownText: string | null
  plainText: string
  position: CSSProperties
}

type CopiedMode = 'markdown' | 'plain' | null

type MarkdownSelectionCopyMenuProps = ComponentPropsWithoutRef<'div'> & {
  markdown: string
  /** Mark the wrapped body as model-generated. Emits `data-ai-generated` /
   * `data-ai-producer` so a script, an extension, or an archiving tool can
   * identify generated text without parsing prose. Passed explicitly rather
   * than hard-coded here, because this component also wraps bodies that are
   * not model output. */
  aiGenerated?: boolean
  /** Optional extra selection action (e.g. the canvas comment queue).
   * Receives the selection in both forms — the mapped markdown source
   * is null when the visible selection has no clean source mapping —
   * plus the menu's viewport position so the caller can anchor a
   * follow-up popover at the same spot. */
  action?: {
    label: string
    onSelect: (
      selection: {
        markdownText: string | null
        plainText: string
      },
      context: { position: CSSProperties },
    ) => void
  }
}

export function MarkdownSelectionCopyMenu({
  action,
  aiGenerated,
  children,
  className,
  markdown,
  ...props
}: MarkdownSelectionCopyMenuProps) {
  const { t } = useLocale()
  const rootRef = useRef<HTMLDivElement | null>(null)
  const copiedTimeoutRef = useRef<number | null>(null)
  const [menu, setMenu] = useState<SelectionMenuState | null>(null)
  const [copiedMode, setCopiedMode] = useState<CopiedMode>(null)

  const refreshSelection = useCallback(() => {
    const root = rootRef.current
    const selection = document.getSelection()
    if (!root || !selection || selection.rangeCount === 0 || selection.isCollapsed) {
      setMenu(null)
      return
    }
    if (!nodeInside(root, selection.anchorNode) || !nodeInside(root, selection.focusNode)) {
      setMenu(null)
      return
    }

    const plainText = selection.toString()
    if (!plainText.trim()) {
      setMenu(null)
      return
    }

    const range = selection.getRangeAt(0)
    const rect = selectionRect(range)
    if (!rect) {
      setMenu(null)
      return
    }

    const placeAbove = rect.top > 44
    const left = Math.max(112, Math.min(rect.left + rect.width / 2, window.innerWidth - 112))
    setCopiedMode(null)
    setMenu({
      markdownText: markdownForVisibleSelection(markdown, plainText),
      plainText,
      position: {
        left,
        top: placeAbove ? rect.top - 8 : rect.bottom + 8,
        transform: placeAbove ? 'translate(-50%, -100%)' : 'translate(-50%, 0)',
      },
    })
  }, [markdown])

  useEffect(() => {
    document.addEventListener('selectionchange', refreshSelection)
    document.addEventListener('mouseup', refreshSelection)
    document.addEventListener('keyup', refreshSelection)
    document.addEventListener('touchend', refreshSelection)
    window.addEventListener('resize', refreshSelection)
    window.addEventListener('scroll', refreshSelection, true)
    return () => {
      document.removeEventListener('selectionchange', refreshSelection)
      document.removeEventListener('mouseup', refreshSelection)
      document.removeEventListener('keyup', refreshSelection)
      document.removeEventListener('touchend', refreshSelection)
      window.removeEventListener('resize', refreshSelection)
      window.removeEventListener('scroll', refreshSelection, true)
      if (copiedTimeoutRef.current !== null) {
        window.clearTimeout(copiedTimeoutRef.current)
      }
    }
  }, [refreshSelection])

  async function copySelectedText(mode: Exclude<CopiedMode, null>) {
    const value = mode === 'markdown' ? menu?.markdownText : menu?.plainText
    if (!value) return
    try {
      if (!(await copyTextToClipboard(value))) {
        throw readerError('clipboardUnavailable')
      }
      setCopiedMode(mode)
      if (copiedTimeoutRef.current !== null) {
        window.clearTimeout(copiedTimeoutRef.current)
      }
      copiedTimeoutRef.current = window.setTimeout(() => setCopiedMode(null), 1200)
    } catch (error) {
      console.warn('Inqtrix markdown selection copy failed.', error)
    }
  }

  return (
    <div
      {...props}
      className={className}
      data-ai-generated={aiGenerated ? 'true' : undefined}
      data-ai-producer={aiGenerated ? AI_PRODUCER : undefined}
      ref={rootRef}
    >
      {children}
      {menu && createPortal(
        <div
          className="fixed z-50 flex items-center gap-1 rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg"
          onMouseDown={(event) => event.preventDefault()}
          style={menu.position}
        >
          <Button
            aria-label={t.chat.copySelectionText}
            className="h-7 gap-1 px-2 text-xs"
            onClick={() => void copySelectedText('plain')}
            size="sm"
            type="button"
            variant="ghost"
          >
            {copiedMode === 'plain' ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            {copiedMode === 'plain' ? t.chat.copiedSelection : t.chat.copySelectionText}
          </Button>
          <Button
            aria-label={menu.markdownText ? t.chat.copySelectionMarkdown : t.chat.selectionMarkdownUnavailable}
            className={cn(
              'h-7 gap-1 px-2 text-xs',
              copiedMode === 'markdown' && 'text-success hover:text-success',
            )}
            disabled={!menu.markdownText}
            onClick={() => void copySelectedText('markdown')}
            size="sm"
            title={menu.markdownText ? t.chat.copySelectionMarkdown : t.chat.selectionMarkdownUnavailable}
            type="button"
            variant="ghost"
          >
            {copiedMode === 'markdown' ? <Check className="size-3.5" /> : <Code2 className="size-3.5" />}
            {copiedMode === 'markdown' ? t.chat.copiedSelection : t.chat.copySelectionMarkdown}
          </Button>
          {action && (
            <Button
              aria-label={action.label}
              className="h-7 gap-1 px-2 text-xs"
              onClick={() => {
                action.onSelect(
                  {
                    markdownText: menu.markdownText,
                    plainText: menu.plainText,
                  },
                  { position: menu.position },
                )
                setMenu(null)
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              <MessageSquarePlus className="size-3.5" />
              {action.label}
            </Button>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}

function nodeInside(root: HTMLElement, node: Node | null): boolean {
  if (!node) return false
  return node === root || root.contains(node)
}

function selectionRect(range: Range): DOMRect | null {
  const rect = range.getBoundingClientRect()
  if (rect.width > 0 || rect.height > 0) return rect
  const firstRect = range.getClientRects()[0]
  return firstRect ?? null
}
