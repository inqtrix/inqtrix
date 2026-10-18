import type { Editor, Range } from '@tiptap/core'
import {
  INQTRIX_STRUCTURE_COMMAND_META,
  type StructureSuggestionCommand,
} from '@inqtrix/editor-schema'

/**
 * Block transformations shared by the `/` slash menu and (later) the block "…"
 * menu, so both stay consistent ("aus einem Guss"). Every action is a
 * StarterKit / Table built-in that already round-trips through markdown.
 */
export type BlockActionId =
  | 'paragraph'
  | 'heading1'
  | 'heading2'
  | 'heading3'
  | 'bulletList'
  | 'orderedList'
  | 'taskList'
  | 'blockquote'
  | 'codeBlock'
  | 'table'
  | 'divider'

/** Apply a block transformation. When `range` is given (the slash menu's typed
 * `/query`), it is deleted first so only the block conversion remains. Returns
 * whether a command was applied. */
export function runBlockAction(editor: Editor, id: BlockActionId, range?: Range): boolean {
  const command: StructureSuggestionCommand = {
    action: id,
    ...(range ? { commandRange: { from: range.from, to: range.to } } : {}),
  }
  const chain = editor
    .chain()
    .focus()
    .command(({ tr }) => {
      tr.setMeta(INQTRIX_STRUCTURE_COMMAND_META, command)
      return true
    })
  if (range) chain.deleteRange(range)
  switch (id) {
    case 'paragraph':
      return chain.setParagraph().run()
    case 'heading1':
      return chain.toggleHeading({ level: 1 }).run()
    case 'heading2':
      return chain.toggleHeading({ level: 2 }).run()
    case 'heading3':
      return chain.toggleHeading({ level: 3 }).run()
    case 'bulletList':
      return chain.toggleBulletList().run()
    case 'orderedList':
      return chain.toggleOrderedList().run()
    case 'taskList':
      return chain.toggleTaskList().run()
    case 'blockquote':
      return chain.toggleBlockquote().run()
    case 'codeBlock':
      return chain.toggleCodeBlock().run()
    case 'table':
      return chain.insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()
    case 'divider':
      return chain.setHorizontalRule().run()
    default:
      return false
  }
}
