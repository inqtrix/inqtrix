import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ClaimNote, claimNotePosition } from './claimNote'

const VIEW = { width: 1280, height: 900 }
const CARD = { width: 320, height: 60 }

/** The box of one tapped line of a mark. */
function line(top: number, left: number, height = 20, width = 200) {
  return { top, left, bottom: top + height, right: left + width }
}

// The numbers are the design's: 6 px from the line, 8 px from the window.
describe('where the note of a tapped mark stands', () => {
  it('right under the tapped line', () => {
    expect(claimNotePosition(line(400, 300), CARD, VIEW)).toEqual({ top: 426, left: 300, side: 'below' })
  })

  it('above the line when the window has no room below it', () => {
    expect(claimNotePosition(line(850, 300), CARD, VIEW)).toEqual({ top: 784, left: 300, side: 'above' })
  })

  it('below the line when there is no room above it either', () => {
    expect(claimNotePosition(line(20, 300), CARD, { width: 1280, height: 90 }).side).toBe('below')
  })

  it('never past a side of the window', () => {
    expect([
      claimNotePosition(line(400, 1200), CARD, VIEW).left,
      claimNotePosition(line(400, -40), CARD, VIEW).left,
    ]).toEqual([952, 8])
  })
})

describe('the note of a claim mark', () => {
  it('keeps a live region for screen readers before any mark is tapped, and no card', () => {
    const html = renderToStaticMarkup(<ClaimNote anchor={null} note={null} onClose={() => undefined} />)
    expect([
      /<p[^>]*aria-live="polite"[^>]*>/.test(html),
      /<p[^>]*class="[^"]*\bsr-only\b[^"]*"[^>]*data-testid="agent-claim-note"/.test(html),
      html.includes('agent-claim-note-card'),
    ]).toEqual([true, true, false])
  })
})
