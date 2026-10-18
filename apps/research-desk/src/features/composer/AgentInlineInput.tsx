import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import type { JSONContent } from '@tiptap/core'

import { cn } from '@/lib/utils'

import {
  COMMAND_PILL_NAME,
  CommandPill,
  DOCUMENT_PILL_KINDS,
  type CommandPillKind,
} from './CommandPill'
import { commandPillKey } from './commandPillState'

export type CommandPillSpec = {
  id: string
  kind: CommandPillKind
  label: string
}

export type AgentInlineInputHandle = {
  /** Empty the draft. Pills of the `keep` kinds stay, as settings. */
  clear: (keep?: readonly CommandPillKind[]) => void
  /** Remove the last `length` CHARACTERS before the caret (a typed token). */
  deleteBeforeCaret: (length: number) => void
  focus: () => void
  /** Replace the `tokenLength` characters before the caret with a pill. */
  replaceTokenWithPill: (tokenLength: number, pill: CommandPillSpec) => void
  setText: (text: string) => void
  /** Plain text before the caret — what the `/` and `@` detectors read. */
  textBeforeCaret: () => string
  /** The text to SEND: a pill spells its label so the sentence stays whole,
   * except pills of the `unspelled` kinds, which leave a word boundary; a
   * document is spelled in `quote`. */
  textWithPillLabels: (
    unspelled?: readonly CommandPillKind[],
    quote?: (label: string) => string,
  ) => string
}

type Props = {
  ariaLabel: string
  className?: string
  disabled?: boolean
  /** The document the input starts from — the draft as the desk held it,
   * capsules at their places. Read once, when the editor is created. */
  initialDoc?: JSONContent | null
  onBlur?: () => void
  onCaretChange?: () => void
  /** Fires on every edit with the whole document, capsules included. */
  onDocChange?: (doc: JSONContent) => void
  /** Fires on every edit with the plain text (pills contribute nothing). */
  onTextChange: (text: string) => void
  /** The pills the document must carry, derived from the session switches. */
  pills: readonly CommandPillSpec[]
  /** The document's pills after a USER edit — the composer sets state from it. */
  onPillsEdited: (pills: CommandPillSpec[]) => void
  placeholder: string
  /** Return true when the key was handled and must not reach the editor. */
  onKeyDown: (event: KeyboardEvent) => boolean
}

function pillKey(pill: { kind: string; id: string }): string {
  return commandPillKey(pill.kind, pill.id)
}

function sameSet(a: readonly CommandPillSpec[], b: readonly CommandPillSpec[]): boolean {
  if (a.length !== b.length) return false
  const left = a.map(pillKey).sort()
  const right = b.map(pillKey).sort()
  return left.every((value, index) => value === right[index])
}

/** The pills a document carries, in document order. */
export function pillsFromDoc(doc: JSONContent): CommandPillSpec[] {
  const found: CommandPillSpec[] = []
  for (const block of doc.content ?? []) {
    for (const node of block.content ?? []) {
      if (node.type !== COMMAND_PILL_NAME) continue
      found.push({
        id: String(node.attrs?.pillId ?? ''),
        kind: (node.attrs?.pillKind ?? 'knowledge') as CommandPillKind,
        label: String(node.attrs?.pillLabel ?? ''),
      })
    }
  }
  return found
}

/** How far back the `/` and `@` detectors look from the caret.
 * They only ever read the token being typed, and both are caret-anchored,
 * so this bounds the read instead of walking the whole document on every
 * keystroke. It is not a content limit: nothing the user typed is dropped,
 * only the window the menus search for a token start. */
const CARET_LOOKBEHIND = 200

/** THE pill node, for both ways a pill enters the document.
 *
 * `typed` says whether the user typed it as a word of their sentence (a
 * `/`-command replaced in place) or a switch placed it (the run-setup menu, a
 * restored session). One constructor, so the two insertion paths cannot drift
 * apart in anything but that one fact. */
export function commandPillNode(pill: CommandPillSpec, typed: boolean): JSONContent {
  return {
    attrs: {
      pillId: pill.id,
      pillKind: pill.kind,
      pillLabel: pill.label,
      pillTyped: typed,
    },
    type: COMMAND_PILL_NAME,
  }
}

/** Where a setting pill stood in the SUBMITTED text: a word boundary. A
 * private-use code point: nothing a user types, and no control character. */
const SETTING_GAP = '\uE000'

/** Close the gaps setting pills leave: nothing at a line's edge, one space
 * between words — so a setting can neither leave a hole nor glue the words
 * on either side of it together. Whitespace away from a gap is untouched. */
function closeSettingGaps(text: string): string {
  return text
    .split('\n')
    .map((line) => line
      .replace(/^[ \t]*(?:\uE000[ \t]*)+/, '')
      .replace(/(?:[ \t]*\uE000)+[ \t]*$/, '')
      .replace(/[ \t]*(?:\uE000[ \t]*)+/g, ' '))
    .join('\n')
}

/** The document as text. `pillLabels` decides what a pill contributes.
 *
 * The LIVE value (draft, menus) leaves every pill out: a pill is a switch, and
 * a restored draft is written back as plain text, where a label would grow a
 * second pill beside the one the switches already produce.
 *
 * The SUBMITTED question spells a pill's label only where the user TYPED it as
 * a word of their sentence: it occupies a document position but no character,
 * and without the label their own sentence would reach the transcript with a
 * hole in it ("gib die Antwort in  und nutze"). A pill a switch placed was
 * never a word — its label in the question is noise the model reads as part
 * of the task, glued to the first typed word ("CanvasErstelle …") — so it
 * contributes a word boundary and nothing else. Neither does a pill of an
 * `unspelled` kind: what it stands for does not apply to this send (a rule
 * in an answer to a clarification). Either way the EFFECT travels in the
 * request fields (`response_form`, `skill_ids`), never in this text.
 *
 * A DOCUMENT is the material of the task, not a setting of it: it is
 * spelled however it entered the draft ("@" in the sentence or "+"), in
 * `quote`, so the question — and the transcript after it — names every
 * document the task was given. */
export function composerTextFromDoc(
  doc: JSONContent,
  pillLabels = false,
  unspelled: readonly CommandPillKind[] = [],
  quote: (label: string) => string = (label) => label,
): string {
  const text = (doc.content ?? [])
    .map((block) => (block.content ?? [])
      .map((node) => {
        if (node.type === 'hardBreak') return '\n'
        if (node.type === COMMAND_PILL_NAME) {
          if (!pillLabels) return ''
          const kind = node.attrs?.pillKind as CommandPillKind
          if (unspelled.includes(kind)) return SETTING_GAP
          const label = String(node.attrs?.pillLabel ?? '')
          const typed = node.attrs?.pillTyped === true
          if (DOCUMENT_PILL_KINDS.includes(kind)) {
            // Placed by a switch, it sits where no space was typed: a word
            // boundary on both sides keeps it a word of its own.
            return typed ? quote(label) : `${SETTING_GAP}${quote(label)}${SETTING_GAP}`
          }
          return typed ? label : SETTING_GAP
        }
        return node.text ?? ''
      })
      .join(''))
    .join('\n')
  return pillLabels ? closeSettingGaps(text) : text
}

/**
 * The draft a send leaves: empty, except the pills of the `keep` kinds,
 * which stay attached as settings at its start (no longer a word of a
 * sentence that was sent).
 */
export function docKeepingPills(
  doc: JSONContent,
  keep: readonly CommandPillKind[],
): JSONContent {
  const kept = pillsFromDoc(doc).filter((pill) => keep.includes(pill.kind))
  return {
    content: [
      kept.length > 0
        ? { content: kept.map((pill) => commandPillNode(pill, false)), type: 'paragraph' }
        : { type: 'paragraph' },
    ],
    type: 'doc',
  }
}

function docFromText(text: string): JSONContent {
  return {
    content: text.split('\n').map((line) => (
      line.length > 0
        ? { content: [{ text: line, type: 'text' }], type: 'paragraph' }
        : { type: 'paragraph' }
    )),
    type: 'doc',
  }
}

/**
 * The agent composer's text surface: prose with INLINE command pills.
 *
 * Why an editor and not a textarea: a `<textarea>` holds characters, never
 * elements, so a pill could only ever sit beside the text. The chat composer
 * already solves this with TipTap (`MentionComposer` / `MentionPill`); this is
 * the same mechanism for the agent's `/`-commands.
 *
 * Ownership, so the two directions cannot fight each other: a USER edit makes
 * the document authoritative (`onPillsEdited` reports what is left, the
 * composer sets the switches from it), while a switch changed ANYWHERE ELSE
 * arrives as a new `pills` prop and is written into the document. The ref of
 * the last reported set tells the two apart — a prop that merely echoes our
 * own report is not an external change and is ignored.
 */
export const AgentInlineInput = forwardRef<AgentInlineInputHandle, Props>(
  function AgentInlineInput({
    ariaLabel,
    className,
    disabled = false,
    initialDoc = null,
    onBlur,
    onCaretChange,
    onDocChange,
    onKeyDown,
    onPillsEdited,
    onTextChange,
    pills,
    placeholder,
  }, ref) {
    const live = useRef({ onBlur, onCaretChange, onDocChange, onKeyDown, onPillsEdited, onTextChange })
    live.current = { onBlur, onCaretChange, onDocChange, onKeyDown, onPillsEdited, onTextChange }
    /** The last pill set this component reported upward. */
    const reportedRef = useRef<CommandPillSpec[]>([])
    /** True while WE are rewriting the document to match the switches. The
     * reconcile runs transactions, every transaction fires `onUpdate`, and a
     * half-finished reconcile must not be reported as if the user had edited
     * it: deleting the old pill before inserting the new one looked exactly
     * like "the user removed the pill", which switched the form back to
     * auto and swallowed the choice that started the reconcile. */
    const syncingRef = useRef(false)

    const editor = useEditor({
      content: initialDoc ?? undefined,
      editable: !disabled,
      extensions: [
        StarterKit.configure({
          blockquote: false,
          bulletList: false,
          code: false,
          codeBlock: false,
          heading: false,
          horizontalRule: false,
          listItem: false,
          orderedList: false,
        }),
        CommandPill,
      ],
      editorProps: {
        attributes: {
          'aria-label': ariaLabel,
          'aria-multiline': 'true',
          class: 'agent-inline-input focus:outline-none',
          role: 'textbox',
        },
        handleKeyDown: (_view, event) => live.current.onKeyDown(event),
      },
      onBlur: () => live.current.onBlur?.(),
      onSelectionUpdate: () => live.current.onCaretChange?.(),
      onUpdate: ({ editor: instance }) => {
        const doc = instance.getJSON()
        live.current.onDocChange?.(doc)
        live.current.onTextChange(composerTextFromDoc(doc))
        const current = pillsFromDoc(doc)
        if (!syncingRef.current && !sameSet(current, reportedRef.current)) {
          reportedRef.current = current
          live.current.onPillsEdited(current)
        }
        live.current.onCaretChange?.()
      },
    })

    useEffect(() => {
      if (!editor || editor.isDestroyed) return
      editor.setEditable(!disabled)
    }, [disabled, editor])

    // A switch changed somewhere else (run-setup menu, a restored session):
    // write the document to match. Our own reports are skipped, so a user
    // edit never bounces back.
    useEffect(() => {
      if (!editor || editor.isDestroyed) return
      if (sameSet(pills, reportedRef.current)) return
      const current = pillsFromDoc(editor.getJSON())
      if (sameSet(pills, current)) {
        reportedRef.current = [...pills]
        return
      }
      const wanted = new Map(pills.map((pill) => [pillKey(pill), pill]))
      /** Pills the switches no longer carry. A document the user TYPED as a
       * word of their sentence stays that word, as plain text: its
       * attachment ended, the sentence did not. Everything else goes. */
      const stale: { pos: number; word: string | null }[] = []
      editor.state.doc.descendants((node, pos) => {
        if (node.type.name !== COMMAND_PILL_NAME) return
        const key = pillKey({ id: String(node.attrs.pillId), kind: String(node.attrs.pillKind) })
        if (wanted.has(key)) {
          wanted.delete(key)
          return
        }
        const keepsWord = node.attrs.pillTyped === true
          && DOCUMENT_PILL_KINDS.includes(node.attrs.pillKind as CommandPillKind)
        stale.push({ pos, word: keepsWord ? String(node.attrs.pillLabel) : null })
      })
      // Two separate transactions, deliberately. Deleting and inserting in
      // ONE chain crashes with "Position N out of range" when the response
      // form switches from canvas to chat: the insertion point is then read
      // from the document BEFORE the deletions shorten it.
      // Deleting back-to-front keeps the earlier positions valid within the
      // first transaction; the insertion point is then read fresh.
      syncingRef.current = true
      try {
        if (stale.length > 0) {
          const chain = editor.chain()
          for (const { pos, word } of stale.reverse()) {
            if (word) chain.insertContentAt({ from: pos, to: pos + 1 }, word)
            else chain.deleteRange({ from: pos, to: pos + 1 })
          }
          chain.run()
        }
        for (const pill of wanted.values()) {
          editor.chain()
            .insertContentAt(Math.max(1, editor.state.doc.content.size - 1), [
              // Placed by a switch, never typed: a setting, not a word.
              commandPillNode(pill, false),
            ])
            .run()
        }
      } finally {
        syncingRef.current = false
      }
      reportedRef.current = [...pills]
    }, [editor, pills])

    useImperativeHandle(ref, () => ({
      clear: (keep = []) => {
        if (!editor) return
        // Reported like a user edit (the update fires before the ref is
        // reset), so every switch follows the emptied document — except
        // the kept pills, which are still in it.
        editor.commands.setContent(docKeepingPills(editor.getJSON(), keep))
        reportedRef.current = pillsFromDoc(editor.getJSON())
      },
      deleteBeforeCaret: (length) => {
        if (!editor || length <= 0) return
        const { from } = editor.state.selection
        editor.chain().focus()
          .deleteRange({ from: Math.max(0, from - length), to: from })
          .run()
      },
      focus: () => editor?.commands.focus(),
      replaceTokenWithPill: (tokenLength, pill) => {
        if (!editor) return
        const { from } = editor.state.selection
        const start = Math.max(0, from - tokenLength)
        editor.chain().focus()
          .deleteRange({ from: start, to: from })
          .insertContentAt(start, [
            // The user typed this as a word of their sentence.
            commandPillNode(pill, true),
            { text: ' ', type: 'text' },
          ])
          .run()
      },
      setText: (text) => {
        editor?.commands.setContent(docFromText(text))
        reportedRef.current = []
      },
      textBeforeCaret: () => {
        if (!editor) return ''
        const { from } = editor.state.selection
        return editor.state.doc.textBetween(
          Math.max(0, from - CARET_LOOKBEHIND), from, '\n', '',
        )
      },
      textWithPillLabels: (unspelled = [], quote) => (
        editor ? composerTextFromDoc(editor.getJSON(), true, unspelled, quote) : ''
      ),
    }), [editor])

    return (
      <div
        className={cn(
          'agent-inline-input-shell relative w-full text-sm leading-6',
          disabled && 'opacity-60',
          className,
        )}
        data-testid="agent-composer-input"
      >
        {/* Same placeholder mechanism as the chat composer: an overlay while
            the document is empty, not an extension — one pattern, no new
            dependency. */}
        {editor?.isEmpty && (
          <div className="pointer-events-none absolute left-2 top-2 text-muted-foreground/70">
            {placeholder}
          </div>
        )}
        <EditorContent editor={editor} />
      </div>
    )
  },
)
