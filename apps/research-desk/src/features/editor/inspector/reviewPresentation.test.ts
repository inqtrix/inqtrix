import { describe, expect, it } from 'vitest'

import {
  caretOnlySelectionRender,
  collaborationCaretLabelLayout,
  collaborationCaretLabelColor,
  collaborationReviewMarkPresentation,
  createEditorExtensions,
  groupTeamCommentMarkers,
  shouldRenderCollaborationAwarenessState,
} from '../tiptap'

describe('collaboration presence presentation', () => {
  it('keeps remote selection ranges transparent while retaining a selection decoration', () => {
    const attributes = caretOnlySelectionRender({ color: '#ff0000', name: 'Ada' })

    expect(attributes).toMatchObject({
      class: 'inqtrix-collaboration-selection',
      nodeName: 'span',
      style: 'background-color: transparent; box-shadow: none;',
      'data-collaboration-selection': 'transparent',
    })
    expect(attributes.style).not.toContain('#ff0000')
  })

  it('chooses legible caret-label text for verified light and dark colors', () => {
    expect(collaborationCaretLabelColor('#f8fafc')).toBe('#111827')
    expect(collaborationCaretLabelColor('#1d4ed8')).toBe('#ffffff')
  })

  it('places bounded labels toward the edge with the least overflow', () => {
    expect(collaborationCaretLabelLayout({
      boundaryLeft: 12,
      boundaryRight: 378,
      caretLeft: 16,
      caretRight: 18,
      labelWidth: 192,
    })).toEqual({ maxWidth: 192, shiftX: 0, side: 'left' })
    expect(collaborationCaretLabelLayout({
      boundaryLeft: 12,
      boundaryRight: 378,
      caretLeft: 372,
      caretRight: 374,
      labelWidth: 192,
    })).toEqual({ maxWidth: 192, shiftX: 0, side: 'right' })
    expect(collaborationCaretLabelLayout({
      boundaryLeft: 12,
      boundaryRight: 183,
      caretLeft: 16,
      caretRight: 18,
      labelWidth: 192,
    })).toEqual({ maxWidth: 119, shiftX: 0, side: 'left' })
  })

  it('never renders the local transport state or another session of the same user', () => {
    expect(shouldRenderCollaborationAwarenessState(
      'user-1',
      101,
      101,
      { user: { id: 'user-1' } },
    )).toBe(false)
    expect(shouldRenderCollaborationAwarenessState(
      'user-1',
      101,
      202,
      { user: { id: 'user-1' } },
    )).toBe(false)
    expect(shouldRenderCollaborationAwarenessState(
      'user-1',
      101,
      303,
      { user: { id: 'user-2' } },
    )).toBe(true)
  })
})

describe('collaboration review overlay', () => {
  it('registers exactly one review extension on the editor', () => {
    const reviewExtensions = createEditorExtensions().filter(
      (extension) => extension.name === 'collaborationReview',
    )

    expect(reviewExtensions).toHaveLength(1)
  })

  it('projects final and original displays without unrelated change overlays', () => {
    expect(collaborationReviewMarkPresentation('deletion', 'final', false).style)
      .toBe('display: none;')
    expect(collaborationReviewMarkPresentation('insertion', 'original', false).style)
      .toBe('display: none;')
    expect(collaborationReviewMarkPresentation('deletion', 'original', false).style)
      .toContain('text-decoration: none')
    expect(collaborationReviewMarkPresentation('insertion', 'all', false).style)
      .toContain('var(--success-subtle)')

    const filtered = collaborationReviewMarkPresentation('deletion', 'all', false, false)
    expect(filtered.display).toBe('final')
    expect(filtered.style).toBe('display: none;')
  })

  it('adds a stable active class without changing the requested display', () => {
    const presentation = collaborationReviewMarkPresentation('modification', 'simple', true)

    expect(presentation.className).toContain('inqtrix-review-overlay-active')
    expect(presentation.display).toBe('simple')
    expect(presentation.style).toContain('var(--brand)')
  })

  it('reveals only the active deletion in Simple Markup', () => {
    const active = collaborationReviewMarkPresentation('deletion', 'simple', true)
    const inactive = collaborationReviewMarkPresentation('deletion', 'simple', false)

    expect(active.display).toBe('simple')
    expect(active.style).toContain('text-decoration: line-through')
    expect(active.style).not.toBe('display: none;')
    expect(inactive.style).toBe('display: none;')
  })
})

describe('team comment marker presentation', () => {
  it('groups colocated threads and keeps the selected thread as representative', () => {
    const groups = groupTeamCommentMarkers([
      { from: 1, id: 'thread-1', kind: 'team', selected: false, status: 'open', to: 11 },
      { from: 1, id: 'thread-2', kind: 'team', selected: true, status: 'open', to: 11 },
      { from: 4, id: 'thread-3', kind: 'team', selected: false, status: 'open', to: 17 },
      { from: 1, id: 'private-1', kind: 'collect', selected: false, status: 'open', to: 11 },
    ])

    expect(groups).toEqual([
      { count: 2, representativeId: 'thread-2', selected: true, to: 11 },
      { count: 1, representativeId: 'thread-3', selected: false, to: 17 },
    ])
  })

  it('separates the lone dot from the count pill, which globals.css needs', () => {
    // `data-editor-comment-count` is written from `count`, and the
    // stylesheet keys the two appearances off it: exactly "1" is the
    // quiet dot that leaves the text flow until hovered or focused,
    // anything else is the pill that stays visible because "3 comments
    // here" is information a hover must not hide. If grouping ever
    // reported 1 as something else, the dot would be permanently
    // visible again — or the pill would vanish.
    const [pill, dot] = groupTeamCommentMarkers([
      { from: 1, id: 'a', kind: 'team', selected: false, status: 'open', to: 9 },
      { from: 2, id: 'b', kind: 'team', selected: false, status: 'open', to: 9 },
      { from: 3, id: 'c', kind: 'team', selected: false, status: 'open', to: 20 },
    ])

    expect(String(dot.count)).toBe('1')
    expect(String(pill.count)).not.toBe('1')
  })
})
