import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

type ModalFocusTrapOptions = {
  dismissable?: boolean
  /** Preferred first focus target. Falls back to the first visible control
   * and finally the panel itself when absent or currently hidden. */
  initialFocusRef?: RefObject<HTMLElement | null>
  onClose: () => void
  open: boolean
  panelRef: RefObject<HTMLElement | null>
  /** Explicit launcher for portals opened from a transient menu item. */
  returnFocusTarget?: HTMLElement | null
}

export function useModalFocusTrap({
  dismissable = true,
  initialFocusRef,
  onClose,
  open,
  panelRef,
  returnFocusTarget: explicitReturnFocusTarget,
}: ModalFocusTrapOptions) {
  const onCloseRef = useRef(onClose)
  const dismissableRef = useRef(dismissable)
  onCloseRef.current = onClose
  dismissableRef.current = dismissable

  useEffect(() => {
    if (!open) return undefined
    let returnFocusTarget = explicitReturnFocusTarget
      ?? document.activeElement as HTMLElement | null
    const visibleFocusables = () => {
      const panel = panelRef.current
      if (!panel) return [] as HTMLElement[]
      return Array.from(
        panel.querySelectorAll<HTMLElement>(FOCUSABLE),
      ).filter((element) => element.offsetParent !== null)
    }

    const focusInitialControl = () => {
      const panel = panelRef.current
      if (!panel || panel.contains(document.activeElement)) return
      const active = document.activeElement as HTMLElement | null
      if (
        explicitReturnFocusTarget === undefined
        &&
        active
        && active !== document.body
        && active !== document.documentElement
        && active.isConnected
      ) {
        returnFocusTarget = active
      }
      const preferred = initialFocusRef?.current
      ;(
        preferred && preferred.offsetParent !== null
          ? preferred
          : visibleFocusables()[0] ?? panel
      )?.focus()
    }
    focusInitialControl()
    // A menu/popover that launched the modal may restore focus to its trigger
    // while it unmounts, after this effect's first focus transfer. Revalidate
    // once after that teardown without overriding focus already inside the
    // dialog.
    const initialFocusFrame = window.requestAnimationFrame(focusInitialControl)

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && dismissableRef.current) {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = visibleFocusables()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      window.cancelAnimationFrame(initialFocusFrame)
      document.removeEventListener('keydown', onKeyDown)
      const restorePreviousFocus = () => {
        if (!returnFocusTarget?.isConnected) return
        const active = document.activeElement as HTMLElement | null
        const activeIsMeaningful = Boolean(
          active
          && active !== document.body
          && active !== document.documentElement
          && active.isConnected,
        )
        if (!activeIsMeaningful) returnFocusTarget.focus()
      }
      returnFocusTarget?.focus?.()
      // WebKit can move focus back to the document body after a focused
      // dialog subtree is removed. Repair that teardown race once, but never
      // override another control that has already received focus.
      window.requestAnimationFrame(restorePreviousFocus)
    }
  }, [explicitReturnFocusTarget, initialFocusRef, open, panelRef])
}
