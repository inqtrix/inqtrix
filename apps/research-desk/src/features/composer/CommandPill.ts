import { Node, mergeAttributes } from '@tiptap/core'
import { ReactNodeViewRenderer } from '@tiptap/react'

import { CommandPillView } from './CommandPillView'

export const COMMAND_PILL_NAME = 'commandPill'

/**
 * What a command pill stands for in the agent composer: a state the next
 * turn RUNS UNDER — a session switch (`knowledge`, `responseForm`), a skill,
 * a library rule attached as the result requirement — or a DOCUMENT the task
 * works with: a research report it reads, a canvas document it works on, the
 * editor document it may change (`editTarget`). Documents are labelled
 * capsules named by their title, not `[N]` mention pills: the agent composer
 * carries no reading-order number, and the question names each document in
 * words.
 *
 * Rules are command pills in the agent composer, inline where the user
 * picked them (like the skill they sit next to), and chips everywhere else
 * (chat and editor). A rule pill holds for ONE task, exactly like a skill:
 * an accepted submission clears it. So do a report and a canvas document;
 * the editing target stays for the next task.
 */
export type CommandPillKind =
  | 'knowledge'
  | 'responseForm'
  | 'skill'
  | 'rule'
  | 'report'
  | 'canvas'
  | 'editTarget'

/** The kinds that name a document: always spelled into the question, in
 * the quotes of the reader's language, however they entered the draft. */
export const DOCUMENT_PILL_KINDS: readonly CommandPillKind[] = ['report', 'canvas', 'editTarget']

/**
 * Inline atomic node for a `/`-command or an `@`-picked rule or document
 * inside the agent composer. The typed token (`/wissen`, `/canvas`,
 * `/<skill>`, `@rules:…`, `@research:…`) is replaced IN PLACE by this node,
 * so the pill sits in the text where the user put it — deleting it with
 * Backspace clears the switch or detaches the rule or document, exactly like
 * deleting any other word.
 *
 * The node holds only what it needs to render itself; the switch it mirrors
 * lives in the composer's state and stays the single source of truth for what
 * is submitted.
 */
export const CommandPill = Node.create({
  name: COMMAND_PILL_NAME,
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      /** `CommandPillKind`. */
      pillKind: { default: 'knowledge' },
      /** Stable id within the kind: '' for switches, the skill id for
       * skills, the server template id for rules, the run id for a report,
       * the artifact id for a canvas document, the editor document id for
       * the editing target. */
      pillId: { default: '' },
      /** Visible text — already localized by the caller. */
      pillLabel: { default: '' },
      /**
       * Whether the user TYPED this pill as a word of their sentence (a
       * `/`-command replaced in place). A pill a switch placed — the
       * run-setup menu, a restored session — is a setting, and a setting is
       * never question text. Spelled out explicitly in HTML: the attribute
       * default would render `false` as the string "false", which is true.
       */
      pillTyped: {
        default: false,
        parseHTML: (element: HTMLElement) => element.getAttribute('data-pill-typed') === 'true',
        renderHTML: (attributes: Record<string, unknown>) => (
          attributes.pillTyped ? { 'data-pill-typed': 'true' } : {}
        ),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-command-pill]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes({ 'data-command-pill': '' }, HTMLAttributes)]
  },

  addNodeView() {
    return ReactNodeViewRenderer(CommandPillView)
  },
})
