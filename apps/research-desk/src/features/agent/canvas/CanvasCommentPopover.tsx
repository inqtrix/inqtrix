/**
 * Compact anchored comment composer: a small popover at the
 * selection/highlight instead of a block above the document. Grows
 * with the text up to six rows, then scrolls; Enter queues,
 * Shift+Enter breaks the line, Escape cancels. An outside click
 * closes the popover — EXCEPT while it holds unsaved input (new
 * non-empty text, or an edit whose text was changed): typed text
 * never dies to a stray click.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { createPortal } from 'react-dom'

import { CornerDownLeft, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'

export type CanvasCommentPopoverProps = {
  position: CSSProperties
  /** Rendered-text preview of the anchored selection. */
  quotePreview: string
  initialText?: string
  placeholder: string
  submitLabel: string
  cancelLabel: string
  onSubmit: (text: string) => void
  onCancel: () => void
}

export function CanvasCommentPopover({
  cancelLabel,
  initialText = '',
  onCancel,
  onSubmit,
  placeholder,
  position,
  quotePreview,
  submitLabel,
}: CanvasCommentPopoverProps) {
  const [text, setText] = useState(initialText)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const textRef = useRef(text)
  textRef.current = text

  useLayoutEffect(() => {
    resizeTextareaToRows(textareaRef.current, 6)
  }, [text])

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (containerRef.current?.contains(event.target as Node)) return
      // Unsaved input survives a stray click; everything else closes
      // (an unchanged edit is already stored in the stack).
      const dirty =
        textRef.current.trim().length > 0
        && textRef.current !== initialText
      if (dirty) return
      onCancel()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [initialText, onCancel])

  const submit = () => {
    if (!text.trim()) return
    onSubmit(text.trim())
  }

  return createPortal(
    <div
      className="fixed z-50 w-[26rem] max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-popover p-1.5 text-popover-foreground shadow-lg"
      ref={containerRef}
      style={position}
    >
      <div className="mb-1 flex items-center gap-1.5 pl-1">
        <p className="min-w-0 flex-1 truncate t-hint text-muted-foreground">
          „{quotePreview}“
        </p>
        <Button
          aria-label={cancelLabel}
          className="size-5 shrink-0 text-muted-foreground hover:text-foreground"
          onClick={onCancel}
          size="icon"
          type="button"
          variant="ghost"
        >
          <X className="size-3" />
        </Button>
      </div>
      <div className="flex items-end gap-1">
        <Textarea
          autoFocus
          className="min-h-8 flex-1 resize-none border-0 bg-transparent px-1 py-1 text-sm shadow-none focus-visible:ring-0 [scrollbar-width:thin]"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
            if (event.key === 'Escape') onCancel()
          }}
          placeholder={placeholder}
          ref={textareaRef}
          rows={1}
          value={text}
        />
        <Button
          aria-label={submitLabel}
          className="size-7 shrink-0 text-muted-foreground hover:text-foreground disabled:opacity-40"
          disabled={!text.trim()}
          onClick={submit}
          size="icon"
          type="button"
          variant="ghost"
        >
          <CornerDownLeft className="size-4" />
        </Button>
      </div>
    </div>,
    document.body,
  )
}
