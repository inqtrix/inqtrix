import type { ReactNode } from 'react'
import * as React from 'react'
import { createPortal } from 'react-dom'

import { X } from '@/components/icons'
import { useModalFocusTrap } from '@/components/ui/use-modal-focus-trap'
import { cn } from '@/lib/utils'

type DialogProps = {
  contentClassName?: string
  contentProps?: Omit<React.ComponentPropsWithoutRef<'div'>, 'children' | 'className'>
  children: ReactNode
  className?: string
  /** Localised label for the close affordance (defaults to "Close"). */
  closeLabel?: string
  description?: ReactNode
  /** When false, overlay-click and Esc do NOT close (e.g. mid-submit). */
  dismissable?: boolean
  footer?: ReactNode
  onClose: () => void
  open: boolean
  title: ReactNode
}

/**
 * Hand-built modal dialog (overlay surface tier: `rounded-xl` + `shadow-lg`),
 * modelled on the share dialog but extracted as the shared primitive.
 * Accessibility: focus moves into the panel on open and is trapped
 * with Tab/Shift+Tab, Esc closes (when dismissable), and focus returns to
 * the previously-focused element on unmount. Wrap the children in a
 * `<form>` to get Enter-to-submit. Renders nothing when `open` is false.
 */
export function Dialog({
  children,
  className,
  closeLabel = 'Close',
  contentClassName,
  contentProps,
  description,
  dismissable = true,
  footer,
  onClose,
  open,
  title,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  useModalFocusTrap({ dismissable, onClose, open, panelRef })

  if (!open) return null
  return createPortal((
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-background/75 px-4 py-8 backdrop-blur"
      onMouseDown={(event) => {
        if (dismissable && event.target === event.currentTarget) onClose()
      }}
    >
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className={cn(
          'w-full max-w-lg overflow-hidden rounded-xl border border-border bg-background shadow-lg',
          className,
        )}
        ref={panelRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="t-section truncate text-foreground" id={titleId}>
              {title}
            </h2>
            {description ? (
              <p className="t-meta text-muted-foreground">{description}</p>
            ) : null}
          </div>
          <button
            aria-label={closeLabel}
            className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface hover:text-foreground"
            onClick={onClose}
            type="button"
          >
            <X className="icon-sm" />
          </button>
        </div>
        <div {...contentProps} className={cn('px-4 py-4', contentClassName)}>{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
            {footer}
          </div>
        ) : null}
      </section>
    </div>
  ), document.body)
}
