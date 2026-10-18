import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FocusEvent,
  type MouseEvent,
} from 'react'
import { createPortal } from 'react-dom'

/** Space between the tapped line of a mark and its note, in CSS pixels. */
const CLAIM_NOTE_GAP = 6
/** The room a note always keeps to each side of the window. */
const CLAIM_NOTE_EDGE = 8
/** A note is never wider than the window leaves it between both edges. */
const CLAIM_NOTE_MAX_WIDTH = `min(24rem, calc(100vw - ${2 * CLAIM_NOTE_EDGE}px))`

type Box = { top: number; left: number; bottom: number; right: number }

export type ClaimNotePlace = { top: number; left: number; side: 'below' | 'above' }

/**
 * Where the note of a claim mark stands, in window coordinates: right under
 * the tapped line, above it when the window has no room below but room
 * above, and never past a side of the window — where a document editor
 * shows a comment, at its anchor.
 */
export function claimNotePosition(
  line: Box,
  card: { width: number; height: number },
  view: { width: number; height: number },
): ClaimNotePlace {
  const below = line.bottom + CLAIM_NOTE_GAP
  const above = line.top - CLAIM_NOTE_GAP - card.height
  const side = below + card.height > view.height - CLAIM_NOTE_EDGE && above >= CLAIM_NOTE_EDGE
    ? 'above'
    : 'below'
  const left = Math.min(
    Math.max(line.left, CLAIM_NOTE_EDGE),
    Math.max(CLAIM_NOTE_EDGE, view.width - CLAIM_NOTE_EDGE - card.width),
  )
  return { left, side, top: side === 'below' ? below : above }
}

type Shown = { anchor: Element; line: number; note: string; root: Element }

/**
 * The note of a claim mark (`components/markdown/claimMarks.ts`), read
 * where the marked text is: a tap on a mark or the focus on it shows why
 * it is marked, at the tapped line (`ClaimNote`). Escape, a tap anywhere
 * else — in the text or outside it — and the focus leaving every mark clear
 * it. The handlers go on the element around the text.
 */
export function useClaimNote() {
  const [shown, setShown] = useState<Shown | null>(null)
  const close = useCallback(() => setShown(null), [])
  useEffect(() => {
    if (!shown) return undefined
    // A tap inside the text is the click handler's: it shows the tapped
    // mark's note or clears it. A tap anywhere else clears it here.
    const onPointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !shown.root.contains(event.target)) close()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [close, shown])
  return {
    anchor: shown?.anchor ?? null,
    close,
    line: shown?.line ?? 0,
    note: shown?.note ?? null,
    onClickCapture: (event: MouseEvent<HTMLElement>) => {
      setShown(shownAt(event.target, event.currentTarget, event.clientY))
    },
    onFocusCapture: (event: FocusEvent<HTMLElement>) => {
      const value = shownAt(event.target, event.currentTarget, null)
      if (value) setShown(value)
    },
    onBlurCapture: (event: FocusEvent<HTMLElement>) => {
      if (!markOf(event.relatedTarget)) close()
    },
  }
}

function markOf(target: EventTarget | null): Element | null {
  return target instanceof Element ? target.closest('[data-claim-note]') : null
}

/** The mark under *target* with its note, and which of its lines the tap
 * at *y* hit (the first one for the focus). */
function shownAt(target: EventTarget | null, root: Element, y: number | null): Shown | null {
  const anchor = markOf(target)
  const note = anchor?.getAttribute('data-claim-note')
  if (!anchor || !note) return null
  const lines = [...anchor.getClientRects()]
  const hit = y === null ? 0 : lines.findIndex((rect) => y >= rect.top && y <= rect.bottom)
  return { anchor, line: hit >= 0 ? hit : Math.max(0, lines.length - 1), note, root }
}

/**
 * The note of the tapped mark: a live region a screen reader hears each
 * note from (there before any mark is tapped, so it hears the first one
 * too), and the note as a card at the tapped line — hidden from the screen
 * reader, so each note is heard once.
 */
export function ClaimNote({
  anchor,
  line = 0,
  note,
  onClose,
}: {
  anchor: Element | null
  line?: number
  note: string | null
  onClose: () => void
}) {
  return (
    <>
      <p aria-live="polite" className="sr-only" data-testid="agent-claim-note">
        {note ?? ''}
      </p>
      {anchor && note && <ClaimNoteCard anchor={anchor} line={line} note={note} onClose={onClose} />}
    </>
  )
}

function ClaimNoteCard({
  anchor,
  line,
  note,
  onClose,
}: {
  anchor: Element
  line: number
  note: string
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [place, setPlace] = useState<ClaimNotePlace | null>(null)
  useLayoutEffect(() => {
    const update = () => {
      const card = ref.current
      if (!card) return
      // A new rendering of the text replaced the mark: its note goes too.
      if (!anchor.isConnected) {
        onClose()
        return
      }
      const lines = anchor.getClientRects()
      setPlace(claimNotePosition(
        lines[Math.min(line, lines.length - 1)] ?? anchor.getBoundingClientRect(),
        { height: card.offsetHeight, width: card.offsetWidth },
        { height: window.innerHeight, width: window.innerWidth },
      ))
    }
    update()
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  }, [anchor, line, note, onClose])
  return createPortal(
    <div
      aria-hidden="true"
      className="fixed z-50 rounded-lg border border-border bg-popover px-3 py-2 t-meta-sm text-popover-foreground shadow-lg"
      data-side={place?.side}
      data-testid="agent-claim-note-card"
      ref={ref}
      style={place
        ? { left: place.left, maxWidth: CLAIM_NOTE_MAX_WIDTH, top: place.top }
        : { left: 0, maxWidth: CLAIM_NOTE_MAX_WIDTH, top: 0, visibility: 'hidden' }}
    >
      {note}
    </div>,
    document.body,
  )
}
