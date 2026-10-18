import { Node, mergeAttributes } from '@tiptap/core'
import { ReactNodeViewRenderer } from '@tiptap/react'
import { MentionPillView } from './MentionPillView'

export const MENTION_PILL_NAME = 'mentionPill'

/** Kinds that are referenced positionally (get a `[N]` pill) in the chat
 * composer. Rules/templates carry no reading-order number and are never a
 * mention pill: they are chips in the chat and the editor, and command pills
 * (`CommandPill`, kind `rule`) in the agent composer. The agent composer
 * names its documents by title instead of a number: a research report there
 * is a labelled command pill (kind `report`), not a mention pill. */
export type MentionPillKind = 'file-asset' | 'file-group' | 'research-report'

/**
 * Inline atomic node for a positional reference (file / file group / research
 * report) inside the composer. Rendered as a compact `[N]` pill whose number is
 * its reading-order index among all pills (computed live in the NodeView), so
 * the numbering renumbers automatically as text is edited. The node is atomic
 * and not text-editable; deleting it removes the reference, and the chip legend
 * mirrors the pills.
 */
export const MentionPill = Node.create({
  name: MENTION_PILL_NAME,
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      refId: { default: '' },
      refKind: { default: 'file-asset' },
      refLabel: { default: '' },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-mention-pill]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes({ 'data-mention-pill': '' }, HTMLAttributes)]
  },

  addNodeView() {
    return ReactNodeViewRenderer(MentionPillView)
  },
})
