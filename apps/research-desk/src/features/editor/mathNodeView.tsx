/** Browser controls for the existing math nodes. Source changes use ordinary
 * node-attribute transactions, so persistence and history keep one path. */
import type { Editor, NodeViewRenderer } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import type { NodeView } from '@tiptap/pm/view'
import { ReactRenderer } from '@tiptap/react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import katex from 'katex'

import { Check } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import { nodeAttributeEditingState } from './nodeAttributeEditing'
import { runEditorHistoryCommand } from './core/editorHistoryCommands'

export type MathNodeViewLabels = {
  edit: string
  source: string
  done: string
  unavailable: string
}

function writeMode(editor: Editor): string | undefined {
  return ((editor.storage as unknown as Record<string, unknown>)
    .collaborationReview as { writeMode?: string } | undefined)?.writeMode
}

export function updateMathNodeSource(
  editor: Editor,
  getPos: () => number | undefined,
  nodeType: string,
  latex: string,
): boolean {
  if (!nodeAttributeEditingState(writeMode(editor), editor.isEditable).enabled) return false
  const pos = getPos()
  if (typeof pos !== 'number') return false
  const node = editor.state.doc.nodeAt(pos)
  if (!node || node.type.name !== nodeType
    || (nodeType !== 'inlineMath' && nodeType !== 'blockMath')) return false
  if (node.attrs.latex === latex) return true
  editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, {
    ...node.attrs,
    latex,
  }, node.marks))
  return true
}

function MathContent({
  block, editable, labels, latex, onChange, onHistory,
}: {
  block: boolean
  editable: boolean
  labels: MathNodeViewLabels
  latex: string
  onChange: (value: string) => void
  onHistory: (command: 'undo' | 'redo') => void
}) {
  const [editing, setEditing] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const preview = useRef<HTMLButtonElement>(null)
  const html = useMemo(() => katex.renderToString(latex, {
    displayMode: block, throwOnError: false, trust: false,
  }), [block, latex])
  useEffect(() => { if (!editable) setEditing(false) }, [editable])
  useLayoutEffect(() => {
    if (editing) {
      input.current?.focus()
      input.current?.select()
    }
  }, [editing])
  useLayoutEffect(() => {
    resizeTextareaToRows(input.current, 6)
  }, [editing, latex])
  const finish = () => {
    setEditing(false)
    preview.current?.focus()
  }
  return (
    <span
      className={block ? 'editor-math-content editor-math-content-block' : 'editor-math-content'}
      contentEditable={false}
    >
      <button
        aria-label={editable ? labels.edit : labels.unavailable}
        aria-expanded={editing}
        aria-haspopup="dialog"
        className="editor-math-preview"
        disabled={!editable}
        onClick={() => setEditing(true)}
        ref={preview}
        title={editable ? labels.edit : labels.unavailable}
        type="button"
      >
        {latex.trim()
          ? <span dangerouslySetInnerHTML={{ __html: html }} />
          : <span className="t-hint">{labels.source}</span>}
      </button>
      <Dialog
        closeLabel={labels.done}
        onClose={finish}
        open={editing && editable}
        title={labels.edit}
      >
        <div className="mb-3 overflow-x-auto text-sm" dangerouslySetInnerHTML={{ __html: html }} />
        <span className="editor-math-source">
          <Textarea
            aria-label={labels.source}
            className="min-h-8 resize-none font-mono text-xs"
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              event.stopPropagation()
              if (event.nativeEvent.isComposing) return
              if (event.key === 'Escape' || (event.key === 'Enter' && !event.shiftKey)) {
                event.preventDefault()
                finish()
              } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
                event.preventDefault()
                onHistory(event.shiftKey ? 'redo' : 'undo')
              } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'y') {
                event.preventDefault()
                onHistory('redo')
              }
            }}
            ref={input}
            rows={1}
            value={latex}
          />
          <Button aria-label={labels.done} onClick={finish} size="icon" type="button" variant="ghost">
            <Check className="size-4" />
          </Button>
        </span>
      </Dialog>
    </span>
  )
}

class MathNodeView implements NodeView {
  dom: HTMLElement
  private node: ProseMirrorNode
  private readonly editor: Editor
  private readonly renderer: ReactRenderer
  private readonly refresh: () => void

  constructor(
    node: ProseMirrorNode,
    editor: Editor,
    getPos: () => number | undefined,
    labels: MathNodeViewLabels,
  ) {
    this.node = node
    this.editor = editor
    this.dom = document.createElement(node.type.name === 'blockMath' ? 'div' : 'span')
    this.dom.className = 'tiptap-mathematics-render editor-math-node'
    this.dom.setAttribute('data-type', node.type.name === 'blockMath' ? 'block-math' : 'inline-math')
    this.dom.contentEditable = 'false'
    const props = () => ({
      block: this.node.type.name === 'blockMath',
      editable: nodeAttributeEditingState(writeMode(editor), editor.isEditable).enabled,
      labels,
      latex: String(this.node.attrs.latex ?? ''),
      onChange: (latex: string) => updateMathNodeSource(editor, getPos, this.node.type.name, latex),
      onHistory: (command: 'undo' | 'redo') => runEditorHistoryCommand(editor, command, { focus: false }),
    })
    let rendered = props()
    this.renderer = new ReactRenderer(MathContent, { editor, as: 'span', props: rendered })
    this.dom.appendChild(this.renderer.element)
    this.dom.setAttribute('data-latex', rendered.latex)
    this.refresh = () => {
      const next = props()
      if (next.latex === rendered.latex && next.editable === rendered.editable) return
      rendered = next
      this.dom.setAttribute('data-latex', next.latex)
      this.renderer.updateProps(next)
    }
    editor.on('transaction', this.refresh)
    editor.on('update', this.refresh)
  }

  update(node: ProseMirrorNode): boolean {
    if (node.type !== this.node.type) return false
    this.node = node
    this.refresh()
    return true
  }

  selectNode(): void { this.dom.classList.add('ProseMirror-selectednode') }
  deselectNode(): void { this.dom.classList.remove('ProseMirror-selectednode') }
  stopEvent(event: Event): boolean { return this.dom.contains(event.target as Node) }
  ignoreMutation(): boolean { return true }
  destroy(): void {
    this.editor.off('transaction', this.refresh)
    this.editor.off('update', this.refresh)
    this.renderer.destroy()
  }
}

export function createMathNodeView(labels: MathNodeViewLabels): NodeViewRenderer {
  return ({ node, editor, getPos }) => new MathNodeView(node, editor, getPos, labels)
}
