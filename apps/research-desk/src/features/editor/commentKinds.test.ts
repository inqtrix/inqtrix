import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { AUTHORED_COMMENT_KIND, isAuthoredComment } from './commentKinds'

describe('one authored kind, with the old rows still readable', () => {
  // Picking a type before writing a note is a decision nobody needs: there is
  // exactly one authored kind. Rows stored under the earlier kinds are NOT
  // migrated, so the read mapping is the only thing keeping them alive.
  it('writes every new note as the one kind', () => {
    expect(AUTHORED_COMMENT_KIND).toBe('collect')
  })

  it('reads every legacy authored kind as a note', () => {
    // The trap this guards: filtering on `kind === 'collect'` anywhere would
    // silently drop notes stored under a legacy kind — they would stop being
    // attachable and vanish from the global run without a word.
    expect(isAuthoredComment({ kind: 'collect' })).toBe(true)
    expect(isAuthoredComment({ kind: 'inline_edit' })).toBe(true)
    expect(isAuthoredComment({ kind: 'evidence_review' })).toBe(true)
  })

  it('never reads the machine carrier as a note', () => {
    // `assistant_edit` is the line an assistant run hangs its private draft
    // on, and only such a draft authorises publishing that one suggestion.
    // Collapsing it into the authored kind would put machine rows in the
    // user's note list and hand them a task they never wrote.
    expect(isAuthoredComment({ kind: 'assistant_edit' })).toBe(false)
  })
})

describe('what a comment kind does not decide', () => {
  const suggestions = readFileSync(
    'src/features/editor/useEditorSuggestions.ts',
    'utf8',
  )
  const workspace = readFileSync(
    'src/features/editor/EditorWorkspace.tsx',
    'utf8',
  )
  const surface = readFileSync(
    'src/features/editor/core/MarkdownEditorSurface.tsx',
    'utf8',
  )

  it('asks for one thing, because a note says what it wants', () => {
    // A note already carries its instruction, so an evidence preset would be
    // a second channel for the same thing — and that channel would never
    // reach a model: it would route every `evidence_review` run to the local
    // stand-in producer, which has no sources to ground anything in, even
    // when a live model is configured.
    expect(suggestions).toContain("const origin: EditorSuggestionOrigin = { commentId: comment.id, kind: 'inline_edit' }")
    expect(suggestions).not.toContain("comment.kind === 'evidence_review'")
    expect(suggestions).not.toContain('comment.evidencePreset')
  })

  it('attaches notes by what they ARE, not by a literal kind', () => {
    expect(suggestions).not.toContain("comment.kind === 'collect'")
    expect(workspace).not.toContain("comment.kind === 'collect'")
    expect(workspace).toContain('isAuthoredComment')
  })

  it('offers no type picker before the first word', () => {
    expect(surface).not.toContain('COMMENT_KIND_ORDER')
    expect(surface).not.toContain('setCommentKind')
    expect(surface).toContain('onCreateComment(value, AUTHORED_COMMENT_KIND)')
  })

  it('shows no filter row and no type menu', () => {
    expect(workspace).not.toContain('COMMENT_KIND_ORDER')
    expect(workspace).not.toContain('CommentKindMenu')
    expect(workspace).not.toContain('kindFilter')
  })

  it('puts both actions on the finished note', () => {
    expect(workspace).toContain('onToggleCollect(comment.id)')
    expect(workspace).toContain('onRunComment(comment)')
    // Runnability is not a property of the kind: every finished note is
    // collectable and runnable.
    expect(workspace).not.toContain('const isRunnable')
  })
})

describe('no evidence presets anywhere', () => {
  const workspace = readFileSync(
    'src/features/editor/EditorWorkspace.tsx',
    'utf8',
  )
  const producer = readFileSync(
    'src/features/editor/suggestionProducer.ts',
    'utf8',
  )

  // Absent rather than hidden: a preset picker would feed the local stand-in
  // producer, which has no sources, into a panel that renders its answer as
  // if it were grounded. A hidden control would leave that path reachable.
  it('leaves no preset control in the panel', () => {
    expect(workspace).not.toContain('EvidencePresetPicker')
    expect(workspace).not.toContain('EVIDENCE_PRESET_ORDER')
    expect(workspace).not.toContain('setEditorCommentEvidencePreset')
  })

  it('leaves no mock that invents a source', () => {
    expect(producer).not.toContain('mockSourcesForPreset')
    expect(producer).not.toContain('example.org')
    expect(producer).not.toContain("origin.kind === 'evidence_review'")
  })

  it('renders no sources block that only a mock could fill', () => {
    expect(workspace).not.toContain('suggestion.evidence')
  })
})

describe('a comment box is one line, and its actions are findable', () => {
  const team = readFileSync(
    'src/features/editor/inspector/TeamCommentsPanel.tsx',
    'utf8',
  )
  const surface = readFileSync(
    'src/features/editor/core/MarkdownEditorSurface.tsx',
    'utf8',
  )

  // Editing and deleting a team comment are wired to real endpoints, but a
  // trigger kept at `opacity-0` until the mouse crosses the message, inside a
  // thread that has to be opened first, leaves only resolve reachable in
  // practice. Wiring can be complete while the action is invisible — a
  // failure every assertion on the handlers still passes through.
  it('shows the message actions instead of hiding them until hover', () => {
    const actions = team.slice(
      team.indexOf('{!truncate && (message.can_edit || message.can_delete)'),
      team.indexOf('</DropdownMenu>', team.indexOf('message.can_delete ?')),
    )
    expect(actions.length).toBeGreaterThan(200)
    expect(actions).not.toContain('opacity-0')
    // The permission belongs to the server, not to the panel.
    expect(actions).toContain('message.can_edit ?')
    expect(actions).toContain('message.can_delete ?')
  })

  it('opens the inline comment box at one row, not three', () => {
    expect(surface).not.toContain('t-body min-h-16 resize-none border-border/70')
    expect(surface).toContain('rows={1}')
    // Submit is the return key; the glyph states it instead of a footer bar
    // with a labelled button under a one-sentence field.
    expect(surface).toContain('<CornerDownLeft className="size-3.5" />')
    expect(surface).not.toContain('copy.inlineCommentSubmit}\n              </Button>')
  })

  it('replies in one growing row too', () => {
    expect(team).not.toContain('min-h-14')
    expect(team).toContain('<CornerDownLeft className="size-3.5" />')
  })
})

describe('the comment form lets go when the selection does', () => {
  const surface = readFileSync(
    'src/features/editor/core/MarkdownEditorSurface.tsx',
    'utf8',
  )

  // Opening the private note form on a selection, typing nothing and clicking
  // elsewhere must close it; otherwise the form stays put and returns on the
  // next selection, because two latches outlive the bubble.
  //
  // `toolbarInteracting` is set by the toolbar's own dropdown and cleared by
  // that dropdown's close event; choosing "private note" UNMOUNTS the toolbar,
  // so the close never fires and `shouldShow` stops consulting the selection
  // at all — the bubble can then never hide, whatever the user clicks.
  it('clears the toolbar latch when the toolbar is replaced by the form', () => {
    const start = surface.slice(
      surface.indexOf('onStartComment={(visibility) => {'),
      surface.indexOf('setIsCommenting(true)'),
    )
    expect(start).toContain('toolbarInteractingRef.current = false')
    expect(surface).toContain('    toolbarInteractingRef.current = false\n    setIsCommenting(false)')
  })

  it('closes an untouched form on a click outside it, and keeps a written one', () => {
    // Measured, not assumed: after clicking away the editor holds no focus
    // and the ProseMirror selection is UNCHANGED — a click outside the
    // content never moves it. The bubble reads only that selection, so
    // nothing could hide the form, and a selection watcher would never fire
    // either. The close has to come from the click.
    const effect = surface.slice(
      surface.indexOf('    const onPointerDown = (event: PointerEvent) => {'),
      surface.indexOf("document.addEventListener('pointerdown'"),
    )
    expect(effect.length).toBeGreaterThan(80)
    expect(effect).toContain('if (commentDraftRef.current.trim()) return')
    expect(effect).toContain('commentFormRef.current?.contains(target)')
    expect(effect).toContain('closeCommentComposer()')
    // Capture phase, so a click on a control that stops propagation still
    // closes the form.
    expect(surface).toContain("document.addEventListener('pointerdown', onPointerDown, true)")
  })
})

describe('a written comment form always has a way out', () => {
  const surface = readFileSync(
    'src/features/editor/core/MarkdownEditorSurface.tsx',
    'utf8',
  )

  // Once a draft is written the form deliberately does NOT close on an
  // outside click (the text is worth more), and Escape cannot reach it
  // either — the handler sits on the form and the focus has left. Without a
  // visible close control that state has no exit at all, so the ✕ is rendered
  // for exactly that state.
  it('shows a close control once something is written', () => {
    expect(surface).toContain('{commentDraft.trim() ? (')
    const guard = surface.slice(
      surface.indexOf('{commentDraft.trim() ? ('),
      surface.indexOf('</div>', surface.indexOf('{commentDraft.trim() ? (')),
    )
    expect(guard).toContain('onClick={cancelComment}')
    expect(guard).toContain('<X className="size-3.5" />')
  })
})
