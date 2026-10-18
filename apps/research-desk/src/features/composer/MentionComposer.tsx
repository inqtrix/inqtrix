import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { MENTION_PILL_NAME, MentionPill, type MentionPillKind } from './MentionPill'
import {
  instructionTextFromDoc,
  mentionDocFromText,
  mentionTextFromDoc,
  pillRefsFromDoc,
  plainPastedSlice,
  type LabelResolver,
} from './mentionDoc'
import {
  buildMentionOptions,
  detectMentionTrigger,
  MentionAutocomplete,
  type MentionCategoryLabels,
  type MentionKind,
  type MentionMatch,
  type MentionOption,
  type MentionSources,
} from './mention'
import type { ChatContextReferenceRecord } from '@/features/project/types'
import { cn } from '@/lib/utils'
import { moveItem } from './reorder'

export type MentionComposerHandle = {
  clear: () => void
  focus: () => void
  getInstructionText: () => string
  getMentionText: () => string
  isEmpty: () => boolean
  removeRef: (ref: ChatContextReferenceRecord) => void
  /**
   * Reorder the inline `[N]` pills by their reading-order index. The pill nodes
   * stay at their text positions; only their attributes (which reference each
   * source) are permuted, so the prose is untouched while the `[N]` markers
   * reassign to the new source order.
   */
  reorderPill: (fromIndex: number, toIndex: number) => void
  setMentionText: (text: string) => void
}

type MentionComposerProps = {
  ariaLabel: string
  categoryLabels: MentionCategoryLabels
  className?: string
  contentClassName?: string
  enabledKinds: MentionKind[]
  maxRows?: number
  mentionSources: MentionSources
  onAttachRule: (ruleId: string) => void
  onChange?: () => void
  onRefsChange: (refs: ChatContextReferenceRecord[]) => void
  onSubmit: () => void
  placeholder: string
  resolveLabel: LabelResolver
}

type AutocompleteState = {
  match: MentionMatch | null
  options: MentionOption[]
  range: { from: number; to: number } | null
}

const EMPTY_AUTOCOMPLETE: AutocompleteState = { match: null, options: [], range: null }

function refTargetId(ref: ChatContextReferenceRecord): string | null {
  switch (ref.kind) {
    case 'file-asset':
      return ref.fileId
    case 'file-group':
      return ref.groupId
    case 'research-report':
      return ref.runId
    case 'chat-rule':
      return null
  }
}

function optionPill(option: MentionOption): { id: string; kind: MentionPillKind; label: string } | null {
  const ref = option.ref
  if (!ref) return null
  if (ref.kind === 'file-asset') return { id: ref.fileId, kind: 'file-asset', label: option.label }
  if (ref.kind === 'file-group') return { id: ref.groupId, kind: 'file-group', label: option.label }
  if (ref.kind === 'research-report') return { id: ref.runId, kind: 'research-report', label: option.label }
  return null
}

export const MentionComposer = forwardRef<MentionComposerHandle, MentionComposerProps>(function MentionComposer({
  ariaLabel,
  categoryLabels,
  className,
  contentClassName,
  enabledKinds,
  maxRows = 8,
  mentionSources,
  onAttachRule,
  onChange,
  onRefsChange,
  onSubmit,
  placeholder,
  resolveLabel,
}, ref) {
  const [autocomplete, setAutocomplete] = useState<AutocompleteState>(EMPTY_AUTOCOMPLETE)
  const [activeIndex, setActiveIndex] = useState(0)

  // Live mirror of props/state so the editor's static handlers always read the
  // latest values without recreating the editor.
  const live = useRef({ autocomplete, activeIndex, categoryLabels, enabledKinds, mentionSources, onAttachRule, onChange, onRefsChange, onSubmit, resolveLabel })
  live.current = { autocomplete, activeIndex, categoryLabels, enabledKinds, mentionSources, onAttachRule, onChange, onRefsChange, onSubmit, resolveLabel }

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        blockquote: false,
        bulletList: false,
        codeBlock: false,
        heading: false,
        horizontalRule: false,
        listItem: false,
        orderedList: false,
      }),
      MentionPill,
    ],
    editorProps: {
      attributes: {
        'aria-label': ariaLabel,
        'aria-multiline': 'true',
        class: 'mention-composer-prose focus:outline-none',
        role: 'textbox',
      },
      handleKeyDown: (_view, event) => handleEditorKeyDown(event),
      // Pasted and dropped text stays text: its tokens never become references.
      transformPasted: (slice, view) => plainPastedSlice(slice, view),
    },
    onUpdate: ({ editor: instance }) => {
      live.current.onRefsChange(pillRefsFromDoc(instance.getJSON()))
      live.current.onChange?.()
      refreshAutocomplete()
    },
    onSelectionUpdate: () => refreshAutocomplete(),
  })

  useEffect(() => {
    if (!editor || editor.isDestroyed) return
    editor.view.dom.setAttribute('aria-label', ariaLabel)
  }, [ariaLabel, editor])

  function refreshAutocomplete() {
    if (!editor) return
    const { empty, from } = editor.state.selection
    if (!empty) {
      setAutocomplete(EMPTY_AUTOCOMPLETE)
      return
    }
    const textBefore = editor.state.doc.textBetween(Math.max(0, from - 120), from, '\n', '')
    const match = detectMentionTrigger(textBefore, textBefore.length)
    if (!match) {
      setAutocomplete(EMPTY_AUTOCOMPLETE)
      return
    }
    const tokenLength = textBefore.length - match.start
    const range = { from: from - tokenLength, to: from }
    const options = buildMentionOptions(match, live.current.mentionSources, live.current.categoryLabels, live.current.enabledKinds)
    setAutocomplete({ match, options, range })
    setActiveIndex(0)
  }

  function applyOption(option: MentionOption) {
    const { autocomplete: state } = live.current
    if (!editor || !state.range) return
    if (option.prefix) {
      editor.chain().focus().insertContentAt(state.range, option.prefix).run()
      return
    }
    if (!option.ref) return
    if (option.ref.kind === 'chat-rule') {
      editor.chain().focus().deleteRange(state.range).run()
      live.current.onAttachRule(option.ref.ruleId)
    } else {
      const pill = optionPill(option)
      if (!pill) return
      editor.chain().focus()
        .deleteRange(state.range)
        .insertContentAt(state.range.from, [
          { attrs: { refId: pill.id, refKind: pill.kind, refLabel: pill.label }, type: MENTION_PILL_NAME },
          { text: ' ', type: 'text' },
        ])
        .run()
    }
    setAutocomplete(EMPTY_AUTOCOMPLETE)
  }

  function goToRoot() {
    const { autocomplete: state } = live.current
    if (!editor || !state.range) return
    // Replace the typed `@kind:` token with a bare `@` so the root trigger
    // re-fires. The trigger regex is anchored to the cursor, so otherwise the
    // only way out of a category is to delete the whole token by hand.
    editor.chain().focus().insertContentAt(state.range, '@').run()
  }

  function handleEditorKeyDown(event: KeyboardEvent): boolean {
    const { activeIndex: index, autocomplete: state } = live.current
    if (event.key === 'Backspace' && state.match && state.match.kind !== 'root' && state.match.query === '') {
      goToRoot()
      return true
    }
    if (state.match && state.options.length > 0) {
      if (event.key === 'ArrowDown') {
        setActiveIndex((value) => (value + 1) % state.options.length)
        return true
      }
      if (event.key === 'ArrowUp') {
        setActiveIndex((value) => (value - 1 + state.options.length) % state.options.length)
        return true
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        applyOption(state.options[index] ?? state.options[0])
        return true
      }
      if (event.key === 'Escape') {
        setAutocomplete(EMPTY_AUTOCOMPLETE)
        return true
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      live.current.onSubmit()
      return true
    }
    return false
  }

  useImperativeHandle(ref, () => ({
    clear: () => {
      editor?.commands.clearContent()
      live.current.onRefsChange([])
    },
    focus: () => editor?.commands.focus(),
    getInstructionText: () => (editor ? instructionTextFromDoc(editor.getJSON()) : ''),
    getMentionText: () => (editor ? mentionTextFromDoc(editor.getJSON()) : ''),
    isEmpty: () => editor?.isEmpty ?? true,
    removeRef: (target) => {
      if (!editor) return
      const targetId = refTargetId(target)
      if (!targetId) return
      const positions: number[] = []
      editor.state.doc.descendants((node, pos) => {
        if (node.type.name === MENTION_PILL_NAME && String(node.attrs.refId) === targetId) positions.push(pos)
      })
      if (positions.length === 0) return
      const tr = editor.state.tr
      for (const pos of positions.reverse()) tr.delete(pos, pos + 1)
      editor.view.dispatch(tr)
      live.current.onRefsChange(pillRefsFromDoc(editor.getJSON()))
    },
    reorderPill: (fromIndex, toIndex) => {
      if (!editor || fromIndex === toIndex) return
      const pills: { attrs: Record<string, unknown>; pos: number }[] = []
      editor.state.doc.descendants((node, pos) => {
        if (node.type.name === MENTION_PILL_NAME) pills.push({ attrs: { ...node.attrs }, pos })
      })
      if (fromIndex < 0 || toIndex < 0 || fromIndex >= pills.length || toIndex >= pills.length) return
      const reordered = moveItem(pills.map((pill) => pill.attrs), fromIndex, toIndex)
      const tr = editor.state.tr
      // setNodeMarkup keeps each atomic pill's size, so positions stay valid
      // across the loop; only the attributes are swapped.
      pills.forEach((pill, index) => {
        tr.setNodeMarkup(pill.pos, undefined, reordered[index])
      })
      editor.view.dispatch(tr)
      live.current.onRefsChange(pillRefsFromDoc(editor.getJSON()))
    },
    setMentionText: (text) => {
      if (!editor) return
      editor.commands.setContent(mentionDocFromText(text, live.current.resolveLabel))
      live.current.onRefsChange(pillRefsFromDoc(editor.getJSON()))
    },
  }), [editor])

  return (
    <div className={cn('relative', className)}>
      {editor?.isEmpty && (
        <div className="pointer-events-none absolute left-2 top-2 text-sm leading-6 text-muted-foreground/70">
          {placeholder}
        </div>
      )}
      <div
        className={cn('overflow-y-auto [scrollbar-width:thin]', contentClassName)}
        style={{ maxHeight: `${maxRows * 1.5}rem` }}
      >
        <EditorContent editor={editor} />
      </div>
      {autocomplete.match && autocomplete.options.length > 0 && (
        <MentionAutocomplete
          activeIndex={activeIndex}
          match={autocomplete.match}
          onBack={goToRoot}
          onHover={setActiveIndex}
          onSelect={applyOption}
          options={autocomplete.options}
        />
      )}
    </div>
  )
})
