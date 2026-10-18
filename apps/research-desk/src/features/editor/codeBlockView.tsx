/**
 * Code-block header chrome: language picker + copy button, mounted
 * as a ProseMirror node view from an APP-SIDE plugin — the schema-layer
 * codeBlock extension (StarterKit + suggestion marks) stays untouched,
 * so the collaboration fingerprint never moves.
 *
 * Attribute writes go through `getPos() + setNodeMarkup` on purpose:
 * the selection-driven `editor.commands.updateAttributes` can hit the
 * WRONG block after the header stole focus.
 * The picker is visibly disabled outside plain edit mode — in suggest
 * mode the schema guard would hard-reject an attribute-only
 * transaction, and a disabled control with a hint beats an error
 * banner. Foreign language values are shown as their own option and
 * never silently overwritten.
 */

import { Extension, type Editor } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import type { EditorView, NodeView } from '@tiptap/pm/view'
import { ReactRenderer } from '@tiptap/react'
import { useState } from 'react'

import { Check, Copy } from '@/components/icons'
import { MARKDOWN_COMMON_LANGUAGES } from '@/components/markdown/markdownLanguage'
import { copyTextToClipboard } from '@/lib/clipboard'
import { nodeAttributeEditingState } from './nodeAttributeEditing'

export type CodeBlockViewLabels = {
  copy: string
  copied: string
  languageAria: string
  plainOption: string
  unavailable: string
}

/** Fence tags offered by the picker ('' = no language). `plaintext` is
 * the highlighter-internal name, not a fence tag people write — the
 * plain option maps to an EMPTY attribute instead. */
const PICKER_LANGUAGES = MARKDOWN_COMMON_LANGUAGES.filter(
  (language) => language !== 'plaintext',
)

function CodeBlockHeader({
  editable,
  labels,
  language,
  onCopy,
  onLanguageChange,
  writeMode,
}: {
  editable: boolean
  labels: CodeBlockViewLabels
  language: string | null
  onCopy: () => Promise<boolean>
  onLanguageChange: (language: string | null) => void
  writeMode: string | undefined
}) {
  const [copied, setCopied] = useState(false)
  const picker = nodeAttributeEditingState(writeMode, editable)
  const value = language ?? ''
  const foreign = value !== '' && !PICKER_LANGUAGES.includes(
    value as (typeof PICKER_LANGUAGES)[number],
  )
  // Compact hover overlay: no permanent bar above the code — a small
  // language pill and an icon-only copy button float in the top-right
  // corner and appear on hover/focus, so a block sits flush in the
  // flowing text.
  return (
    <div className="editor-code-block-controls" contentEditable={false}>
      <select
        aria-label={labels.languageAria}
        className="editor-code-block-language"
        disabled={!picker.enabled}
        onChange={(event) => {
          onLanguageChange(event.target.value || null)
        }}
        title={picker.enabled ? labels.languageAria : labels.unavailable}
        value={value}
      >
        <option value="">{labels.plainOption}</option>
        {foreign && <option value={value}>{value}</option>}
        {PICKER_LANGUAGES.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      <button
        aria-label={copied ? labels.copied : labels.copy}
        className="editor-code-block-copy"
        onClick={() => {
          void onCopy().then((ok) => {
            if (!ok) return
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1200)
          })
        }}
        title={copied ? labels.copied : labels.copy}
        type="button"
      >
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      </button>
    </div>
  )
}

class CodeBlockNodeView implements NodeView {
  dom: HTMLElement
  contentDOM: HTMLElement

  private node: ProseMirrorNode
  private readonly view: EditorView
  private readonly getPos: () => number | undefined
  private readonly editor: Editor
  private readonly headerHost: HTMLElement
  private readonly renderer: ReactRenderer
  private readonly refresh: () => void

  constructor(
    node: ProseMirrorNode,
    view: EditorView,
    getPos: () => number | undefined,
    editor: Editor,
    labels: CodeBlockViewLabels,
  ) {
    this.node = node
    this.view = view
    this.getPos = getPos
    this.editor = editor
    this.dom = document.createElement('div')
    this.dom.className = 'editor-code-block'
    this.headerHost = document.createElement('div')
    const pre = document.createElement('pre')
    this.contentDOM = document.createElement('code')
    pre.appendChild(this.contentDOM)
    this.dom.append(this.headerHost, pre)

    this.renderer = new ReactRenderer(CodeBlockHeader, {
      editor,
      props: this.headerProps(labels),
    })
    this.headerHost.appendChild(this.renderer.element)
    // Mode/editable changes arrive as editor events, not node updates —
    // the header must follow them live (the editor instance itself is
    // never rebuilt in legacy mode).
    this.refresh = () => this.renderer.updateProps(this.headerProps(labels))
    editor.on('transaction', this.refresh)
    editor.on('update', this.refresh)
  }

  private headerProps(labels: CodeBlockViewLabels) {
    const storage = (
      this.editor.storage as unknown as Record<string, unknown>
    ).collaborationReview as { writeMode?: string } | undefined
    return {
      editable: this.editor.isEditable,
      labels,
      language:
        typeof this.node.attrs.language === 'string'
          ? this.node.attrs.language
          : null,
      onCopy: async () =>
        copyTextToClipboard(this.node.textContent),
      onLanguageChange: (language: string | null) => {
        const pos = this.getPos()
        if (typeof pos !== 'number') return
        const current = this.view.state.doc.nodeAt(pos)
        if (!current || current.type.name !== 'codeBlock') return
        this.view.dispatch(
          this.view.state.tr.setNodeMarkup(pos, undefined, {
            ...current.attrs,
            language,
          }),
        )
      },
      writeMode: storage?.writeMode,
    }
  }

  update(node: ProseMirrorNode): boolean {
    if (node.type.name !== 'codeBlock') return false
    this.node = node
    this.refresh()
    return true
  }

  ignoreMutation(mutation: MutationRecord | { type: 'selection' }): boolean {
    if (mutation.type === 'selection') return false
    return !this.contentDOM.contains(mutation.target as Node)
  }

  stopEvent(event: Event): boolean {
    return this.headerHost.contains(event.target as Node)
  }

  destroy(): void {
    this.editor.off('transaction', this.refresh)
    this.editor.off('update', this.refresh)
    this.renderer.destroy()
  }
}

const codeBlockViewKey = new PluginKey('codeBlockView')

export const CodeBlockViewExtension = Extension.create<{
  labels: CodeBlockViewLabels
}>({
  name: 'codeBlockView',

  addOptions() {
    return {
      labels: {
        copied: '',
        copy: '',
        languageAria: '',
        plainOption: '',
        unavailable: '',
      },
    }
  },

  addProseMirrorPlugins() {
    const { editor } = this
    const { labels } = this.options
    return [
      new Plugin({
        key: codeBlockViewKey,
        props: {
          nodeViews: {
            codeBlock: (node, view, getPos) =>
              new CodeBlockNodeView(node, view, getPos, editor, labels),
          },
        },
      }),
    ]
  },
})
