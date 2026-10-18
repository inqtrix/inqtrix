import type { Editor } from '@tiptap/react'

export type EditorHistoryCommand = 'redo' | 'undo'

/** Collaboration deliberately removes local history until the Yjs binding is
 * ready. Treat those temporarily absent commands as disabled UI, not as an
 * exceptional editor state. A read-only editor runs no history command: a
 * live collaboration editor keeps its Yjs undo history while it reconnects,
 * and undo would write to the document behind the read-only surface. */
export function canRunEditorHistoryCommand(
  editor: Editor | null,
  command: EditorHistoryCommand,
): boolean {
  if (!editor || editor.isDestroyed || !editor.isEditable) return false
  const commands = editor.can()
  const candidate = commands[command]
  return typeof candidate === 'function' && candidate.call(commands)
}

export function runEditorHistoryCommand(
  editor: Editor | null,
  command: EditorHistoryCommand,
  { focus = true }: { focus?: boolean } = {},
): boolean {
  if (!editor || editor.isDestroyed || !editor.isEditable) return false
  const chain = focus ? editor.chain().focus() : editor.chain()
  const candidate = chain[command]
  if (typeof candidate !== 'function') return false
  return candidate.call(chain).run()
}
