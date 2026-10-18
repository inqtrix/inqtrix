import { useCallback, useRef } from 'react'
import { DragHandle } from '@tiptap/extension-drag-handle-react'
import type { Editor } from '@tiptap/react'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import {
  ChevronDown,
  ChevronUp,
  Code2,
  Copy,
  GripVertical,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  ListTodo,
  Quote,
  Trash2,
  Type,
  type LucideIcon,
} from '@/components/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { runBlockAction, type BlockActionId } from './blockActions'

export type BlockHandleLabels = {
  ariaLabel: string
  turnInto: string
  duplicate: string
  deleteBlock: string
  moveUp: string
  moveDown: string
  text: string
  heading1: string
  heading2: string
  heading3: string
  bulletList: string
  orderedList: string
  taskList: string
  blockquote: string
  codeBlock: string
}

const TURN_INTO: { id: BlockActionId; icon: LucideIcon; key: keyof BlockHandleLabels }[] = [
  { id: 'paragraph', icon: Type, key: 'text' },
  { id: 'heading1', icon: Heading1, key: 'heading1' },
  { id: 'heading2', icon: Heading2, key: 'heading2' },
  { id: 'heading3', icon: Heading3, key: 'heading3' },
  { id: 'bulletList', icon: List, key: 'bulletList' },
  { id: 'orderedList', icon: ListOrdered, key: 'orderedList' },
  { id: 'taskList', icon: ListTodo, key: 'taskList' },
  { id: 'blockquote', icon: Quote, key: 'blockquote' },
  { id: 'codeBlock', icon: Code2, key: 'codeBlock' },
]

/**
 * Block handle: a grip that follows the hovered block (drag to
 * reorder) and opens a "…" menu (Turn into / Duplicate / Move / Delete). The
 * block actions share `blockActions.ts` with the slash menu. Live mode only.
 * `@tiptap/extension-drag-handle-react` is MIT.
 */
export function BlockHandle({ editor, labels }: { editor: Editor; labels: BlockHandleLabels }) {
  // Last hovered block. Only updated for real blocks, so it stays valid while
  // the menu is open (the mouse leaves the editor → onNodeChange fires null).
  const nodeRef = useRef<ProseMirrorNode | null>(null)
  const posRef = useRef(-1)

  // Stable identity is required. `@tiptap/extension-drag-handle-react` re-registers
  // its ProseMirror plugin whenever `onNodeChange` changes identity, and a plugin
  // unregister tears down ALL plugin views via EditorView.updateState — including an
  // open slash/mention suggestion popup. An inline arrow would re-register on every
  // parent re-render (e.g. a live run's progress tick re-rendering the editor subtree)
  // and close the popup mid-interaction. The handler only writes refs, so deps are [].
  const handleNodeChange = useCallback(({ node, pos }: { node: ProseMirrorNode | null; pos: number }) => {
    if (node && pos >= 0) {
      nodeRef.current = node
      posRef.current = pos
    }
  }, [])

  const turnInto = (id: BlockActionId) => {
    if (posRef.current < 0) return
    editor.chain().focus().setTextSelection(posRef.current + 1).run()
    runBlockAction(editor, id)
  }

  const duplicate = () => {
    const node = nodeRef.current
    const pos = posRef.current
    if (!node || pos < 0) return
    editor.chain().focus().insertContentAt(pos + node.nodeSize, node.toJSON()).run()
  }

  const remove = () => {
    const node = nodeRef.current
    const pos = posRef.current
    if (!node || pos < 0) return
    editor.chain().focus().deleteRange({ from: pos, to: pos + node.nodeSize }).run()
  }

  const move = (direction: 'up' | 'down') => {
    const node = nodeRef.current
    const pos = posRef.current
    if (!node || pos < 0) return
    const from = pos
    const to = pos + node.nodeSize
    if (direction === 'up') {
      const before = editor.state.doc.resolve(from).nodeBefore
      if (!before) return
      editor.chain().focus().deleteRange({ from, to }).insertContentAt(from - before.nodeSize, node.toJSON()).run()
    } else {
      const after = editor.state.doc.resolve(to).nodeAfter
      if (!after) return
      editor.chain().focus().deleteRange({ from, to }).insertContentAt(from + after.nodeSize, node.toJSON()).run()
    }
  }

  return (
    <DragHandle
      editor={editor}
      onNodeChange={handleNodeChange}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            aria-label={labels.ariaLabel}
            className="grid size-6 cursor-grab place-items-center rounded text-muted-foreground/55 transition-colors hover:bg-accent hover:text-foreground active:cursor-grabbing"
            type="button"
          >
            <GripVertical className="size-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56" side="bottom">
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <Type className="size-4 text-muted-foreground" />
              {labels.turnInto}
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent className="w-48">
              {TURN_INTO.map((item) => {
                const Icon = item.icon
                return (
                  <DropdownMenuItem key={item.id} onSelect={() => turnInto(item.id)}>
                    <Icon className="size-4 text-muted-foreground" />
                    {labels[item.key]}
                  </DropdownMenuItem>
                )
              })}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={duplicate}>
            <Copy className="size-4 text-muted-foreground" />
            {labels.duplicate}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => move('up')}>
            <ChevronUp className="size-4 text-muted-foreground" />
            {labels.moveUp}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => move('down')}>
            <ChevronDown className="size-4 text-muted-foreground" />
            {labels.moveDown}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="text-destructive focus:bg-destructive/10 focus:text-destructive"
            onSelect={remove}
          >
            <Trash2 className="size-4" />
            {labels.deleteBlock}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </DragHandle>
  )
}
