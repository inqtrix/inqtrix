import { Extension } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin, PluginKey, type EditorState } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

import type { CitationLabelPredicate } from '@/components/markdown/citationGrammar'
import { citationLabelSpans } from '@/components/markdown/citationLinks'

/**
 * Citation labels shown as chips while a document is edited, where reading
 * shows them as links (`citationLabelSpans`). The text stays `[K1]`: a chip
 * is a decoration, no schema node, so what is stored does not change.
 */
export type CitationChips = {
  isLabel: CitationLabelPredicate
  /** The labels the document's sources know. */
  labels: ReadonlySet<string>
  /** Opens the evidence of *label*. */
  onOpen: (label: string) => void
}

export type CitationChipRange = { from: number; label: string; to: number }

const citationChipPluginKey = new PluginKey<DecorationSet>('inqtrixCitationChips')

// Stands in for code and for an inline node in a textblock's prose: a line
// feed ends every bracket and word, and one character per position keeps text
// offsets equal to document positions.
const BOUNDARY = String.fromCharCode(10)

/** Every chip of *doc*: the citation labels of its prose, code excluded. */
export function citationChipRanges(doc: ProseMirrorNode, chips: CitationChips): CitationChipRange[] {
  const ranges: CitationChipRange[] = []
  doc.descendants((node, position) => {
    if (!node.isTextblock) return true
    if (node.type.spec.code) return false
    let prose = ''
    node.forEach((child) => {
      prose += child.isText && !child.marks.some((mark) => mark.type.spec.code)
        ? child.text ?? ''
        : BOUNDARY.repeat(child.nodeSize)
    })
    for (const span of citationLabelSpans(prose, chips.isLabel, chips.labels)) {
      ranges.push({ from: position + 1 + span.start, label: span.label, to: position + 1 + span.end })
    }
    return false
  })
  return ranges
}

function chipDecorations(doc: ProseMirrorNode, chips: CitationChips | null): DecorationSet {
  if (!chips || chips.labels.size === 0) return DecorationSet.empty
  return DecorationSet.create(doc, citationChipRanges(doc, chips).map((range) => Decoration.inline(
    range.from,
    range.to,
    { class: 'citation-chip', 'data-citation-label': range.label },
    { label: range.label },
  )))
}

/** The chip the caret stands strictly inside, so Enter there opens it. */
function chipAtCaret(state: EditorState): string | null {
  const { selection } = state
  if (!selection.empty) return null
  const chip = citationChipPluginKey.getState(state)
    ?.find(selection.from, selection.from)
    .find((decoration) => decoration.from < selection.from && selection.from < decoration.to)
  return typeof chip?.spec.label === 'string' ? chip.spec.label : null
}

/**
 * The chips of an editor whose host passes `chips`: a click on a chip, or
 * Enter with the caret inside one, opens its evidence. The host's labels
 * are read again with every change of the document.
 */
export const CitationChipExtension = Extension.create<{ chips: () => CitationChips | null }>({
  name: 'citationChips',

  addOptions() {
    return { chips: () => null }
  },

  addProseMirrorPlugins() {
    const chips = this.options.chips
    return [
      new Plugin<DecorationSet>({
        key: citationChipPluginKey,
        state: {
          init: (_config, state) => chipDecorations(state.doc, chips()),
          apply: (transaction, decorations, _before, state) => (
            transaction.docChanged ? chipDecorations(state.doc, chips()) : decorations
          ),
        },
        props: {
          decorations: (state) => citationChipPluginKey.getState(state),
          handleClick: (_view, _position, event) => {
            const target = event.target instanceof Element ? event.target : null
            const label = target?.closest('[data-citation-label]')?.getAttribute('data-citation-label')
            if (!label) return false
            chips()?.onOpen(label)
            return true
          },
          handleKeyDown: (view, event) => {
            if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.metaKey || event.ctrlKey) {
              return false
            }
            const label = chipAtCaret(view.state)
            if (!label) return false
            chips()?.onOpen(label)
            return true
          },
        },
      }),
    ]
  },
})
